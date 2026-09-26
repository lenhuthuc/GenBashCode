import sys, json, os
from vs_utils import read_jsonl, write_jsonl, extract_command

def main():
    cfg = json.load(open(sys.argv[1]))
    rows = read_jsonl(cfg["test"])
    out_path = cfg["out"]
    preds = read_jsonl(out_path) if os.path.exists(out_path) else []
    done = {(p["system"], p["variant"], p["id"]) for p in preds}
    blocks = [(s, v, f) for s in cfg["systems"] for v, f in cfg["variants"].items()]
    todo_blocks = [(s, v, f) for s, v, f in blocks if any((s, v, r["id"]) not in done for r in rows)]
    print(f"[eval] {len(rows)} câu test, còn {len(todo_blocks)}/{len(blocks)} khối cần sinh", flush=True)
    if not todo_blocks:
        return
    from vllm import LLM, SamplingParams
    from vllm.lora.request import LoRARequest
    use_lora = "sft" in cfg["systems"]
    llm = LLM(model=cfg["base_model"], dtype="float16", max_model_len=1024,
              gpu_memory_utilization=cfg["gpu_mem"], seed=cfg["seed"],
              enable_lora=use_lora, max_lora_rank=cfg["lora_rank"] if use_lora else 16)
    sp = SamplingParams(temperature=0.0, max_tokens=cfg["max_tokens"])
    lora = LoRARequest("sft", 1, cfg["lora_path"]) if use_lora else None
    for system, variant, field in todo_blocks:
        todo = [r for r in rows if (system, variant, r["id"]) not in done]
        convs = [[{"role": "system", "content": cfg["system_prompt"]}, {"role": "user", "content": r[field]}]
                 for r in todo]
        outs = llm.chat(convs, sp, use_tqdm=True, lora_request=lora if system == "sft" else None)
        for r, o in zip(todo, outs):
            raw = o.outputs[0].text
            preds.append({"id": r["id"], "system": system, "variant": variant, "input": r[field],
                          "raw": raw, "pred": extract_command(raw)})
        write_jsonl(out_path, preds)       # lưu sau mỗi khối → resume được
        print(f"[eval] xong {system}/{variant}", flush=True)

if __name__ == "__main__":
    main()
