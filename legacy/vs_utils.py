"""Tiện ích dùng chung cho notebook ViShell và các script con."""
import os, re, sys, json, gc, time, glob, signal, threading, subprocess, unicodedata
from pathlib import Path
from contextlib import contextmanager

SYSTEM_PROMPT = ("Bạn là trợ lý dòng lệnh Linux. Hãy chuyển yêu cầu của người dùng thành đúng MỘT lệnh bash "
                 "trên một dòng. Chỉ trả về lệnh, không giải thích, không dùng markdown.")

# ------------------------------------------------------------------ I/O
def read_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows

def _atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def write_jsonl(path, rows):
    _atomic_write_text(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))

def write_json(path, obj):
    _atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))

def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)

class ChunkStore:
    """Lưu kết quả theo chunk cố định [i*cs, (i+1)*cs). Chunk có file = đã xong."""
    def __init__(self, dirpath, n_items, chunk_size):
        self.dir = Path(dirpath)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.n, self.cs = int(n_items), int(chunk_size)
        meta = {"n_items": self.n, "chunk_size": self.cs}
        meta_p = self.dir / "meta.json"
        old = read_json(meta_p)
        if old is None:
            write_json(meta_p, meta)
        elif old != meta:
            raise ValueError(f"{self.dir} đã có dữ liệu với cấu hình khác ({old} != {meta}). "
                             "Xoá thư mục này hoặc giữ nguyên cấu hình để resume.")

    @property
    def n_chunks(self):
        return (self.n + self.cs - 1) // self.cs

    def bounds(self, i):
        return i * self.cs, min((i + 1) * self.cs, self.n)

    def path(self, i):
        return self.dir / f"chunk_{i:05d}.jsonl"

    def pending(self):
        return [i for i in range(self.n_chunks) if not self.path(i).exists()]

    def write(self, i, rows):
        lo, hi = self.bounds(i)
        assert len(rows) == hi - lo, (len(rows), hi - lo)
        write_jsonl(self.path(i), rows)

    def read_all(self):
        miss = self.pending()
        if miss:
            raise RuntimeError(f"Còn {len(miss)} chunk chưa xong, ví dụ {miss[:5]}")
        out = []
        for i in range(self.n_chunks):
            out.extend(read_jsonl(self.path(i)))
        return out

    def progress(self):
        return f"{self.n_chunks - len(self.pending())}/{self.n_chunks} chunk"

def latest_checkpoint(out_dir):
    best, best_step = None, -1
    for p in glob.glob(os.path.join(str(out_dir), "checkpoint-*")):
        m = re.search(r"checkpoint-(\d+)$", p)
        if m and os.path.exists(os.path.join(p, "trainer_state.json")):
            s = int(m.group(1))
            if s > best_step:
                best, best_step = p, s
    return best

# ------------------------------------------------------------------ GPU / tiến trình
def gpu_used_gb():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True)
        return float(out.stdout.strip().splitlines()[0]) / 1024
    except Exception:
        return float("nan")

def free_gpu(verbose=True):
    """Gọi sau khi `del model` để trả VRAM của kernel hiện tại."""
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except ImportError:
        pass
    if verbose:
        print(f"GPU đang dùng: {gpu_used_gb():.1f} GB")

def _log_timing(log_dir, name, seconds, extra=None):
    rec = {"step": name, "seconds": round(seconds, 1), "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    if extra:
        rec.update(extra)
    with open(Path(log_dir) / "timings.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

@contextmanager
def timed(name, log_dir):
    t0 = time.time()
    yield
    dt = time.time() - t0
    _log_timing(log_dir, name, dt)
    print(f"⏱ {name}: {dt / 60:.1f} phút")

def run_py(script, cfg, log_dir, name):
    """Chạy script Python trong tiến trình con; stream stdout ra notebook và logs/<name>.log."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = log_dir / f"{name}_cfg.json"
    write_json(cfg_path, cfg)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(script).parent) + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("TQDM_MININTERVAL", "15")
    env.setdefault("TOKENIZERS_PARALLELISM", "false")
    t0 = time.time()
    with open(log_dir / f"{name}.log", "a", encoding="utf-8") as lf:
        lf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        proc = subprocess.Popen([sys.executable, "-u", str(script), str(cfg_path)],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, env=env)
        for line in proc.stdout:
            print(line, end="")
            lf.write(line)
        rc = proc.wait()
    dt = time.time() - t0
    _log_timing(log_dir, name, dt, {"exit_code": rc})
    print(f"⏱ {name}: {dt / 60:.1f} phút | GPU sau bước: {gpu_used_gb():.1f} GB")
    if rc != 0:
        raise RuntimeError(f"Bước '{name}' lỗi (exit {rc}). Log: {log_dir / (name + '.log')}")
    return dt

def hf_dtype_kw(torch_dtype):
    """transformers >= 4.56 dùng `dtype`, bản cũ dùng `torch_dtype`."""
    import transformers
    from packaging import version
    key = "dtype" if version.parse(transformers.__version__) >= version.parse("4.56") else "torch_dtype"
    return {key: torch_dtype}

# ------------------------------------------------------------------ Bash
class _ParseTimeout(Exception):
    pass

def _on_alarm(signum, frame):
    raise _ParseTimeout()

def bash_parse(cmd, timeout=2.0):
    import bashlex
    use_alarm = hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread()
    old = None
    if use_alarm:
        old = signal.signal(signal.SIGALRM, _on_alarm)
        signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        return bashlex.parse(cmd)
    finally:
        if use_alarm:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)

def parse_ok(cmd):
    if not cmd or not cmd.strip():
        return False
    try:
        bash_parse(cmd)
        return True
    except Exception:
        return False

def normalize_cmd(c):
    c = (c or "").strip()
    c = re.sub(r"\s+", " ", c)
    c = re.sub(r"\s*;\s*$", "", c)
    return c

WRAPPERS = {"sudo", "doas", "nohup", "time", "nice", "ionice", "env", "command", "builtin",
            "exec", "stdbuf", "timeout", "watch"}

def _first_command_words(node):
    if getattr(node, "kind", None) == "command":
        return [p.word for p in node.parts if getattr(p, "kind", None) == "word"]
    for attr in ("parts", "list"):
        for ch in (getattr(node, attr, None) or []):
            if hasattr(ch, "kind"):
                r = _first_command_words(ch)
                if r:
                    return r
    return None

def _pick_utility(words):
    i = 0
    while i < len(words):
        base = os.path.basename(words[i])
        if base in WRAPPERS:
            i += 1
            while i < len(words) and (words[i].startswith("-") or re.match(r"^[A-Za-z_]\w*=", words[i])
                                      or (base == "timeout" and re.match(r"^\d", words[i]))):
                i += 1
            continue
        if re.match(r"^[A-Za-z_]\w*=", words[i]):
            i += 1
            continue
        return base or None
    return None

def main_utility(cmd):
    """Tiện ích chính = từ lệnh đầu tiên của simple command đầu tiên (bỏ sudo/env/time/..., bỏ gán biến)."""
    if not cmd or not cmd.strip():
        return None
    try:
        for tree in bash_parse(cmd):
            words = _first_command_words(tree)
            if words:
                u = _pick_utility(words)
                if u:
                    return u
    except Exception:
        pass
    first = re.split(r"\|\||&&|[|;&]", cmd, maxsplit=1)[0]
    return _pick_utility(first.split())

FENCE_RE = re.compile(r"```[a-zA-Z]*\s*\n?(.*?)```", re.S)

def extract_command(text):
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    m = FENCE_RE.search(t)
    if m:
        t = m.group(1)
    for line in t.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        s = re.sub(r"^\$\s+", "", s)
        if len(s) > 1 and s.startswith("`") and s.endswith("`"):
            s = s.strip("`").strip()
        return s
    return ""

# ------------------------------------------------------------------ Kiểm tra bản dịch
VI_CHARS = set("àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ")
CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
QUOTE_RE = re.compile(r"""(?:^|(?<=[\s(\[]))(['"`])(.+?)\1(?=$|[\s.,;:!?)])""")
ORD_RE = re.compile(r"^(\d+)(st|nd|rd|th)$", re.I)
ABBR_RE = re.compile(r"^(?:[a-z]\.)+[a-z]\.?$|^etc\.?$", re.I)
COMMON_EN = set("""a an the and or of to in on at by for with from into all any each every this that these those
file files directory directories folder folders line lines word words name names current path size time date
find list print show display remove delete copy move make create change set get count sort search replace
read write open close kill stop start run test type which who watch touch head tail cut join split paste
less more free top find yes no true false echo cat sleep wait read mail more help info man user users group
number numbers first last new old all only not recursively recursive""".split())

def protected_tokens(nl, bash=""):
    """hard: bắt buộc giữ nguyên (đường dẫn, tên file, số, cờ, biến, chuỗi trong nháy).
       soft: từ ASCII xuất hiện trong lệnh (thường là tên tiện ích) — chỉ ghi nhận, không loại."""
    hard, soft = [], []
    for _, inner in QUOTE_RE.findall(nl):
        if inner.strip():
            hard.append(inner.strip())
    bash_words = set(re.findall(r"[A-Za-z][A-Za-z0-9_+-]*", bash or ""))
    for tok in nl.split():
        t = tok.strip("\"'`()[]{},;:!?")
        if t not in (".", ".."):
            t = t.rstrip(".")
        if not t or re.fullmatch(r"[\W_]+", t):
            continue
        m = ORD_RE.match(t)
        if m:
            hard.append(m.group(1))
            continue
        if ABBR_RE.match(t):
            continue
        if re.search(r"[/._$~*=\d]", t) or re.match(r"^--?[A-Za-z0-9]", t):
            hard.append(t)
        elif t.isascii() and t.isalpha() and len(t) >= 2 and t in bash_words and t.lower() not in COMMON_EN:
            soft.append(t)
    dedup = lambda xs: list(dict.fromkeys(xs))
    return dedup(hard), dedup(soft)

PREFIX_RE = re.compile(r"^(?:bản dịch|dịch|tiếng việt|vietnamese|translation|câu tiếng việt)\s*[:：\-]\s*", re.I)

def clean_translation(text):
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).replace("<think>", "").replace("</think>", "")
    lines = [l.strip() for l in t.strip().splitlines() if l.strip()]
    if not lines:
        return ""
    t = PREFIX_RE.sub("", lines[0]).strip()
    if len(t) >= 2 and t[0] in "\"“" and t[-1] in "\"”" and '"' not in t[1:-1]:
        t = t[1:-1].strip()
    return unicodedata.normalize("NFC", t)

def check_translation(en, vi, bash):
    """Trả về (ok, reason, soft_missing)."""
    if not vi:
        return False, "empty", []
    if CJK_RE.search(vi):
        return False, "cjk", []
    if not any(c in VI_CHARS for c in vi.lower()):
        en_w = set(re.findall(r"[a-z]+", en.lower()))
        vi_w = set(re.findall(r"[a-z]+", vi.lower()))
        if vi.strip().lower() == en.strip().lower() or (vi_w and len(vi_w & en_w) / len(vi_w) > 0.6):
            return False, "not_vietnamese", []
    ratio = len(vi) / max(1, len(en))
    if ratio < 0.4 or ratio > 4:
        return False, "length_ratio", []
    nb = normalize_cmd(bash)
    if len(nb) >= 8 and nb in vi and nb not in en:
        return False, "leaked_command", []
    hard, soft = protected_tokens(en, bash)
    miss = [t for t in hard if t not in vi]
    if miss:
        return False, "missing_token", []
    soft_miss = [t for t in soft if not re.search(r"(?<!\w)" + re.escape(t) + r"(?!\w)", vi)]
    return True, "ok", soft_miss
