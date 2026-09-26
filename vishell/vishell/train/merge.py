"""Merge a LoRA adapter into its base model at 16-bit (never into 4-bit weights, per
AGENT.md section 6 step 2/4/6). GPU-only; imports are local to the function."""
from __future__ import annotations

from pathlib import Path

from .sft import pick_dtype


def merge_adapter(base_model: str, adapter_dir: str, out_dir: str, dtype: str = "auto", force: bool = False) -> dict:
    out = Path(out_dir)
    if (out / "config.json").exists() and not force:
        return {"skipped": True, "out_dir": str(out)}

    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    torch_dtype = pick_dtype(dtype)
    base = AutoModelForCausalLM.from_pretrained(base_model, dtype=torch_dtype)
    merged = PeftModel.from_pretrained(base, adapter_dir).merge_and_unload()
    merged.save_pretrained(str(out))
    AutoTokenizer.from_pretrained(base_model).save_pretrained(str(out))
    return {"skipped": False, "out_dir": str(out)}
