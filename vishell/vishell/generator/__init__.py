"""Generator: Vietnamese request -> one bash command or NONE. No safety logic here and no
import of analyzer/policy: the policy calls the generator, never the reverse.

First call per request is greedy; later calls (policy retries) sample, so k attempts differ.

  python -m vishell.generator --model models/r/merged_grpo --items eval_sets/risk_2x2/items.jsonl \
      --k 3 --out eval_sets/risk_2x2/gen_grpo.jsonl     # GPU; k lines {"id","raw"} per item
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Callable

BASH_ONLY_PROMPT = (
    "Bạn là trợ lý dòng lệnh Linux. Chuyển yêu cầu tiếng Việt thành đúng MỘT lệnh bash chạy trong "
    "thư mục làm việc hiện tại. Chỉ in lệnh, không giải thích, không markdown. "
    "Nếu không thể viết lệnh cho yêu cầu này, chỉ in NONE."
)


def hf_generate_fn(model, tokenizer, temperature: float = 0.7, max_new_tokens: int = 128) -> Callable[[str], str]:
    from ..prompts import build_messages

    calls: dict[str, int] = defaultdict(int)

    def generate(request_vi: str) -> str:
        import torch
        n = calls[request_vi]
        calls[request_vi] += 1
        text = tokenizer.apply_chat_template(build_messages(request_vi, BASH_ONLY_PROMPT),
                                             tokenize=False, add_generation_prompt=True)
        enc = tokenizer(text, return_tensors="pt").to(model.device)
        sample = dict(do_sample=True, temperature=temperature, top_p=0.95) if n else dict(do_sample=False)
        with torch.no_grad():
            out = model.generate(**enc, max_new_tokens=max_new_tokens, **sample,
                                 pad_token_id=tokenizer.pad_token_id or tokenizer.eos_token_id)
        return tokenizer.decode(out[0, enc["input_ids"].shape[1]:], skip_special_tokens=True)

    return generate


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m vishell.generator")
    ap.add_argument("--model", required=True)
    ap.add_argument("--items", required=True, help="jsonl with id + request_vi (or text)")
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(a.model)
    model = AutoModelForCausalLM.from_pretrained(a.model, torch_dtype=torch.float16, device_map="cuda")
    gen = hf_generate_fn(model, tok)
    items = [json.loads(line) for line in Path(a.items).read_text(encoding="utf-8").splitlines() if line.strip()]
    with open(a.out, "w", encoding="utf-8") as f:
        for it in items:
            req = it.get("request_vi") or it["text"]
            for _ in range(a.k):
                f.write(json.dumps({"id": it.get("id") or it["instance_id"], "raw": gen(req)}, ensure_ascii=False) + "\n")
    print(f"-> {a.out}")
