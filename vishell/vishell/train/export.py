"""Export the final merged model to GGUF q4_k_m for the Gradio demo (AGENT.md step 6).
GPU-only; imports local to the function."""
from __future__ import annotations

from pathlib import Path


def export_gguf(model_dir: str, out_dir: str, quant: str = "q4_k_m", force: bool = False) -> dict:
    out = Path(out_dir)
    marker = out / f"model-{quant}.gguf"
    if marker.exists() and not force:
        return {"skipped": True, "path": str(marker)}
    out.mkdir(parents=True, exist_ok=True)

    from unsloth import FastLanguageModel

    model, tokenizer = FastLanguageModel.from_pretrained(model_dir)
    model.save_pretrained_gguf(str(out), tokenizer, quantization_method=quant)
    return {"skipped": False, "path": str(marker)}
