"""GRPO stage: TRL GRPOTrainer, new LoRA on top of the merged sft_scenarios model,
rewarded entirely by rewards.make_grpo_reward_fns (sandbox execution, no LLM judge).
Tries vLLM generation first (much faster), falls back to plain `generate` if it errors
out — vLLM+GRPO version pinning is notoriously fragile, per AGENT.md section 6.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..prompts import SYSTEM_PROMPT, build_messages
from ..rewards import make_grpo_reward_fns
from ..schema import Instance
from .sft import filter_kwargs, latest_checkpoint, pick_dtype, read_jsonl


def _to_grpo_row(inst: Instance) -> dict:
    return {"prompt": build_messages(inst.request_vi, SYSTEM_PROMPT), "instance": inst.model_dump()}


def train_grpo(
    train_path: str, base_model: str, out_dir: str, backend, lora_r: int, lora_alpha: int,
    lora_dropout: float, lr: float, max_steps: int, save_steps: int, num_generations: int,
    max_prompt_length: int, max_completion_length: int, decision_coef: float, seed: int,
    temperature: float = 1.0, undo_coef: float = 0.0,
    dtype: str = "auto", use_vllm: bool = True, force: bool = False,
) -> dict:
    out = Path(out_dir)
    final = out / "final"
    if (final / "adapter_config.json").exists() and not force:
        return {"skipped": True, "final": str(final)}

    import torch
    from datasets import Dataset
    from peft import LoraConfig
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from trl import GRPOConfig, GRPOTrainer
    from trl.import_utils import is_vllm_available

    torch_dtype = pick_dtype(dtype)
    rows = [_to_grpo_row(Instance.model_validate(r)) for r in read_jsonl(train_path)]
    train_ds = Dataset.from_list(rows)

    tok = AutoTokenizer.from_pretrained(base_model)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    model = AutoModelForCausalLM.from_pretrained(base_model, attn_implementation="sdpa", dtype=torch_dtype)
    # Qwen ships temperature=0.7/top_p=0.8/repetition_penalty=1.1, and generate() silently swaps
    # those in for GRPO's sampling args -> near-identical completions, zero reward std, no gradient.
    model.generation_config.update(temperature=temperature, top_p=1.0, top_k=0, repetition_penalty=1.0)

    peft_cfg = LoraConfig(r=lora_r, lora_alpha=lora_alpha, lora_dropout=lora_dropout,
                          target_modules="all-linear", task_type="CAUSAL_LM")
    reward_fns = make_grpo_reward_fns(backend, coef=decision_coef)

    resume_from = latest_checkpoint(out)
    base_kwargs = dict(
        output_dir=str(out), learning_rate=lr, max_steps=max_steps,
        num_generations=num_generations, temperature=temperature, top_p=1.0, max_prompt_length=max_prompt_length,
        max_completion_length=max_completion_length, save_strategy="steps",
        save_steps=save_steps, save_total_limit=3, logging_steps=1,
        bf16=(torch_dtype == torch.bfloat16), fp16=(torch_dtype == torch.float16),
        report_to="none", seed=seed, use_vllm=use_vllm,
        reward_weights=[1.0, 1.0, undo_coef],  # r_format, r_decision, r_undo (0 = logged only)
    )
    # Decide before building the trainer: GRPOTrainer wraps `model` with LoRA in place,
    # so constructing it twice (try vLLM, fall back) stacks two adapters.
    if use_vllm and not is_vllm_available():
        print("[grpo] vLLM not installed; generating with use_vllm=False")
        base_kwargs["use_vllm"] = False
    grpo_args = GRPOConfig(**filter_kwargs(GRPOConfig, base_kwargs))
    trainer = GRPOTrainer(**filter_kwargs(GRPOTrainer, dict(
        model=model, args=grpo_args, train_dataset=train_ds, processing_class=tok,
        reward_funcs=reward_fns, peft_config=peft_cfg,
    )))

    trainer.train(resume_from_checkpoint=str(resume_from) if resume_from else None)
    trainer.save_model(str(final))
    tok.save_pretrained(str(final))
    (out / "log_history.json").write_text(json.dumps(trainer.state.log_history), encoding="utf-8")
    return {"skipped": False, "final": str(final), "log_history": trainer.state.log_history}
