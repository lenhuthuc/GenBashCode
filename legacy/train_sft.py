import sys, json, math
from pathlib import Path
from vs_utils import read_jsonl, write_json, latest_checkpoint, hf_dtype_kw

def main():
    cfg = json.load(open(sys.argv[1]))
    out = Path(cfg["out_dir"])
    final = out / "final"
    if (final / "adapter_config.json").exists():
        print(f"[sft] Đã có adapter cuối ở {final} — bỏ qua.")
        return
    import torch
    from datasets import Dataset
    from transformers import AutoTokenizer, AutoModelForCausalLM
    from peft import LoraConfig
    from trl import SFTTrainer, SFTConfig

    sp = cfg["system_prompt"]
    def to_ex(r):
        return {"prompt": [{"role": "system", "content": sp}, {"role": "user", "content": r["nl_vi"]}],
                "completion": [{"role": "assistant", "content": r["bash"]}] }
    train = Dataset.from_list([to_ex(r) for r in read_jsonl(cfg["train"])])
    val = Dataset.from_list([to_ex(r) for r in read_jsonl(cfg["val"])])

    tok = AutoTokenizer.from_pretrained(cfg["base_model"])
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    model = AutoModelForCausalLM.from_pretrained(cfg["base_model"], attn_implementation="sdpa",
                                                 **hf_dtype_kw(torch.float16))

    steps_per_epoch = math.ceil(len(train) / (cfg["per_device_bs"] * cfg["grad_accum"]))
    total = cfg["max_steps"] if cfg["max_steps"] > 0 else steps_per_epoch * cfg["epochs"]
    args = SFTConfig(
        output_dir=str(out), num_train_epochs=cfg["epochs"], max_steps=cfg["max_steps"],
        per_device_train_batch_size=cfg["per_device_bs"], per_device_eval_batch_size=cfg["per_device_bs"],
        gradient_accumulation_steps=cfg["grad_accum"], learning_rate=cfg["lr"], lr_scheduler_type="cosine",
        warmup_steps=max(1, int(0.03 * total)), logging_steps=10,
        save_strategy="steps", save_steps=cfg["save_steps"], save_total_limit=3,
        eval_strategy="steps", eval_steps=cfg["eval_steps"],
        fp16=True, max_length=cfg["max_len"], completion_only_loss=True,
        report_to="none", seed=cfg["seed"], dataloader_num_workers=2)
    peft_cfg = LoraConfig(r=cfg["lora_r"], lora_alpha=cfg["lora_alpha"], lora_dropout=cfg["lora_dropout"],
                          target_modules="all-linear", task_type="CAUSAL_LM")
    trainer = SFTTrainer(model=model, args=args, train_dataset=train, eval_dataset=val,
                         processing_class=tok, peft_config=peft_cfg)
    trainer.model.print_trainable_parameters()

    ckpt = latest_checkpoint(out)
    print(f"[sft] {len(train)} mẫu train, {len(val)} val, ~{total} bước. Resume từ: {ckpt}", flush=True)
    trainer.train(resume_from_checkpoint=ckpt)
    trainer.save_model(str(final))
    tok.save_pretrained(str(final))
    write_json(out / "log_history.json", trainer.state.log_history)
    print(f"[sft] Xong. Adapter: {final}", flush=True)

if __name__ == "__main__":
    main()
