"""SFT LoRA training, used for both `sft_nl2bash` and `sft_scenarios` stages (only the
dataset and `completion_only_loss` differ — see cli.py). GPU-only; every heavy import is
local to the functions here so the rest of the pipeline works without [train] installed.

Ported from the notebook's proven `train_sft.py` (validated on real Kaggle T4), adapted
to resume via Paths and to tolerate different TRL/Unsloth versions across environments.
"""
from __future__ import annotations

import inspect
import json
import math
import warnings
from pathlib import Path
from typing import Any


def filter_kwargs(cls_or_fn, kwargs: dict) -> dict:
    """Keep only the kwargs `cls_or_fn` actually accepts, warning about the rest —
    TRL/Unsloth rename constructor args across versions (max_seq_length/max_length,
    tokenizer/processing_class, ...) and we'd rather log a warning than crash."""
    sig = inspect.signature(cls_or_fn)
    accepted = set(sig.parameters)
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return kwargs
    dropped = {k: v for k, v in kwargs.items() if k not in accepted}
    if dropped:
        warnings.warn(f"{getattr(cls_or_fn, '__name__', cls_or_fn)}: dropping unsupported kwargs {list(dropped)}")
    return {k: v for k, v in kwargs.items() if k in accepted}


def pick_dtype(requested: str = "auto"):
    import torch
    if requested == "bf16":
        return torch.bfloat16
    if requested == "fp16":
        return torch.float16
    return torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16


def latest_checkpoint(out_dir: Path) -> Path | None:
    candidates = [
        p for p in out_dir.glob("checkpoint-*")
        if p.is_dir() and (p / "trainer_state.json").exists()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: int(p.name.split("-")[-1]))


def read_jsonl(path: str | Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


class VramTimeCallback:
    """Prints VRAM/step-time/ETA. Mixed with TrainerCallback at registration (Trainer calls
    every on_* hook); kept import-free so this module loads without transformers."""

    def __init__(self):
        self._t0 = None
        self._step0 = None

    def on_train_begin(self, args, state, control, **kw):
        import time
        self._t0, self._step0 = time.time(), state.global_step

    def on_log(self, args, state, control, **kw):
        import time
        import torch
        if self._t0 is None or state.global_step == self._step0:
            return
        elapsed = time.time() - self._t0
        done = state.global_step - self._step0
        rate = elapsed / max(done, 1)
        remaining = (state.max_steps - state.global_step) * rate if state.max_steps else 0
        vram = f"{torch.cuda.memory_allocated() / 1e9:.1f}GB" if torch.cuda.is_available() else "n/a"
        print(f"[sft] step={state.global_step} {rate:.2f}s/step ETA={remaining/60:.1f}min VRAM={vram}", flush=True)


def train_sft(
    train_path: str, val_path: str, base_model: str, out_dir: str, system_prompt: str,
    lora_r: int, lora_alpha: int, lora_dropout: float, epochs: int, lr: float, max_len: int,
    per_device_bs: int, grad_accum: int, max_steps: int, save_steps: int, eval_steps: int,
    seed: int, dtype: str = "auto", force: bool = False, from_adapter: str | None = None,
) -> dict:
    """Trains prompt(system+user) -> completion(assistant) with loss only on the
    completion. `from_adapter` continues from an existing LoRA (used by sft_scenarios,
    which starts from the merged sft_nl2bash checkpoint per AGENT.md section 6)."""
    out = Path(out_dir)
    final = out / "final"
    if (final / "adapter_config.json").exists() and not force:
        return {"skipped": True, "final": str(final)}

    import torch
    from datasets import Dataset
    from peft import LoraConfig, PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer, TrainerCallback
    from trl import SFTConfig, SFTTrainer

    torch_dtype = pick_dtype(dtype)

    def to_example(r: dict) -> dict:
        return {
            "prompt": [{"role": "system", "content": system_prompt}, {"role": "user", "content": r["request_vi"]}],
            "completion": [{"role": "assistant", "content": r["target"]}],
        }

    train_rows, val_rows = read_jsonl(train_path), read_jsonl(val_path)
    train_ds = Dataset.from_list([to_example(r) for r in train_rows])
    val_ds = Dataset.from_list([to_example(r) for r in val_rows]) if val_rows else None

    tok = AutoTokenizer.from_pretrained(base_model)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    model = AutoModelForCausalLM.from_pretrained(base_model, attn_implementation="sdpa", dtype=torch_dtype)
    if from_adapter:
        model = PeftModel.from_pretrained(model, from_adapter, is_trainable=True)

    steps_per_epoch = math.ceil(len(train_ds) / (per_device_bs * grad_accum))
    total_steps = max_steps if max_steps and max_steps > 0 else steps_per_epoch * epochs

    resume_from = latest_checkpoint(out)
    args_kwargs = dict(
        output_dir=str(out), num_train_epochs=epochs, max_steps=max_steps,
        per_device_train_batch_size=per_device_bs, per_device_eval_batch_size=per_device_bs,
        gradient_accumulation_steps=grad_accum, learning_rate=lr, lr_scheduler_type="cosine",
        warmup_steps=max(1, int(0.03 * total_steps)), logging_steps=10,
        save_strategy="steps", save_steps=save_steps, save_total_limit=3,
        eval_strategy="steps" if val_ds is not None else "no", eval_steps=eval_steps,
        bf16=(torch_dtype == torch.bfloat16), fp16=(torch_dtype == torch.float16),
        max_length=max_len, max_seq_length=max_len, completion_only_loss=True,
        report_to="none", seed=seed, dataloader_num_workers=2,
    )
    sft_args = SFTConfig(**filter_kwargs(SFTConfig, args_kwargs))

    peft_cfg = LoraConfig(r=lora_r, lora_alpha=lora_alpha, lora_dropout=lora_dropout,
                          target_modules="all-linear", task_type="CAUSAL_LM")
    trainer_kwargs = dict(model=model, args=sft_args, train_dataset=train_ds, eval_dataset=val_ds,
                          processing_class=tok, tokenizer=tok, peft_config=None if from_adapter else peft_cfg)
    trainer = SFTTrainer(**filter_kwargs(SFTTrainer, trainer_kwargs))
    trainer.add_callback(type("VramTime", (VramTimeCallback, TrainerCallback), {})())

    trainer.train(resume_from_checkpoint=str(resume_from) if resume_from else None)
    trainer.save_model(str(final))
    tok.save_pretrained(str(final))
    (out / "log_history.json").write_text(json.dumps(trainer.state.log_history), encoding="utf-8")
    return {"skipped": False, "final": str(final), "log_history": trainer.state.log_history}
