import sys, json
from vs_utils import read_jsonl, ChunkStore, clean_translation, check_translation

SYS = """Bạn là biên dịch viên kỹ thuật. Hãy dịch yêu cầu tiếng Anh sang tiếng Việt tự nhiên, giống cách một lập trình viên Việt Nam gõ yêu cầu cho trợ lý dòng lệnh.\nQuy tắc:\n1. GIỮ NGUYÊN, không dịch, không thêm dấu: tên file, đường dẫn, tên thư mục, tên chương trình/tiện ích (grep, find, tar...), tùy chọn (-l, --all), biến ($HOME), đuôi file (.txt), số, URL, tên người dùng/máy/gói, mẫu tìm kiếm.\n2. Giữ nguyên dấu nháy và toàn bộ nội dung bên trong dấu nháy.\n3. Không thêm, không bớt ý. Không giải thích. Không viết lệnh bash.\n4. Dùng thuật ngữ quen thuộc của dân IT Việt: file, thư mục, quyền, tiến trình, đường dẫn...\n5. Chỉ trả về MỘT dòng là bản dịch."""

FEWSHOT = [
    ("find all .txt files in /home/user larger than 10MB", 'find /home/user -name "*.txt" -size +10M',
     "tìm tất cả file .txt trong /home/user có kích thước lớn hơn 10MB"),
    ("print the last 5 lines of config_backup.sh", "tail -n 5 config_backup.sh",
     "in ra 5 dòng cuối của file config_backup.sh"),
    ("change the owner of /srv/app/data.db to user 'www-data'", "chown www-data /srv/app/data.db",
     "đổi chủ sở hữu của /srv/app/data.db thành người dùng 'www-data'"),
    ("Counts lines in all *.py files in the current directory tree", "find . -name '*.py' | xargs wc -l",
     "đếm số dòng của tất cả file *.py trong cây thư mục hiện tại"),
    ("Recursively removes all empty directories under current directory", "find . -type d -empty -delete",
     "xoá đệ quy tất cả thư mục rỗng trong thư mục hiện tại"),
]

def user_msg(nl, bash):
    return f"Câu tiếng Anh: {nl}\nLệnh tham chiếu (chỉ để hiểu ngữ cảnh, KHÔNG đưa vào bản dịch): {bash}"

def build_messages(r):
    msgs = [{"role": "system", "content": SYS}]
    for en, cmd, vi in FEWSHOT:
        msgs += [{"role": "user", "content": user_msg(en, cmd)}, {"role": "assistant", "content": vi}]
    msgs.append({"role": "user", "content": user_msg(r["nl"], r["bash"])})
    return msgs

def make_llm(cfg):
    from vllm import LLM
    kw = dict(model=cfg["model"], dtype="float16", max_model_len=cfg["max_model_len"],
              gpu_memory_utilization=cfg["gpu_mem"], seed=cfg["seed"],
              max_num_seqs=cfg.get("max_num_seqs", 128))   # giới hạn thấp cho VRAM nhỏ (T4): tránh lỗi thiếu Mamba cache block
    if cfg.get("multimodal"):
        try:
            return LLM(**kw, language_model_only=True)       # bỏ vision encoder, dành VRAM cho KV cache
        except TypeError:
            return LLM(**kw, limit_mm_per_prompt={"image": 0, "video": 0})
    return LLM(**kw)

def main():
    cfg = json.load(open(sys.argv[1]))
    rows = read_jsonl(cfg["input"])
    store = ChunkStore(cfg["out_dir"], len(rows), cfg["chunk_size"])
    pending = store.pending()
    print(f"[translate] {len(rows)} câu, {store.progress()} đã xong, còn {len(pending)} chunk", flush=True)
    if not pending:
        return
    from vllm import SamplingParams
    llm = make_llm(cfg)
    temps = cfg["temperatures"]
    for ci in pending:
        lo, hi = store.bounds(ci)
        batch = rows[lo:hi]
        res, todo = {}, batch
        for attempt, temp in enumerate(temps):
            if not todo:
                break
            sp = SamplingParams(temperature=temp, top_p=0.8, top_k=20, max_tokens=cfg["max_tokens"],
                                seed=cfg["seed"] + attempt)
            outs = llm.chat([build_messages(r) for r in todo], sp, use_tqdm=True,
                            chat_template_kwargs={"enable_thinking": False})
            nxt = []
            for r, o in zip(todo, outs):
                vi = clean_translation(o.outputs[0].text)
                ok, reason, soft_miss = check_translation(r["nl"], vi, r["bash"])
                res[r["id"]] = {"id": r["id"], "nl_vi": vi, "ok": ok, "reason": reason,
                                "soft_missing": soft_miss, "tries": attempt + 1, "model": cfg["model"]}
                if not ok:
                    nxt.append(r)
            todo = nxt
        store.write(ci, [res[r["id"]] for r in batch])
        n_ok = sum(res[r["id"]]["ok"] for r in batch)
        print(f"[translate] chunk {ci}: {n_ok}/{len(batch)} đạt | {store.progress()}", flush=True)

if __name__ == "__main__":
    main()
