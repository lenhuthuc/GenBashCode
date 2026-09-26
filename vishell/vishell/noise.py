"""Vietnamese noise variants for train/test robustness, per AGENT.md section 5.

Three kinds, 1-3 picked per instance: no_accent (strip diacritics, whole or partial),
mix_en (swap common Vietnamese IT terms for their English equivalent), typo (light
keyboard typos: swap/drop/duplicate/neighbor-key). Everything is seeded by instance id
so re-running produces byte-identical output (required for train/test resume + the
paper's "test giữ cả bản sạch lẫn bản nhiễu" invariant).
"""
from __future__ import annotations

import random
import re
import unicodedata

TERM_MIX = {
    "thư mục": ["folder", "directory", "dir"], "tệp tin": ["file"], "tập tin": ["file"],
    "tệp": ["file"], "xoá": ["delete", "remove"], "xóa": ["delete", "remove"],
    "tìm kiếm": ["search"], "tìm": ["find", "search"], "liệt kê": ["list"],
    "sao chép": ["copy"], "di chuyển": ["move"], "đổi tên": ["rename"],
    "quyền": ["permission"], "tiến trình": ["process"], "giải nén": ["unzip", "extract"],
    "nén": ["zip", "compress"], "dòng": ["line"], "đường dẫn": ["path"],
    "người dùng": ["user"], "mạng": ["network"], "ổ đĩa": ["disk"],
    "dung lượng": ["size"], "kích thước": ["size"], "hiển thị": ["show", "display"],
    "in ra": ["print"], "tải xuống": ["download"], "tải về": ["download"],
    "biến môi trường": ["env", "environment variable"], "chuỗi": ["string"],
    "cổng": ["port"], "đệ quy": ["recursive"], "rỗng": ["empty"], "thay thế": ["replace"],
    "sắp xếp": ["sort"], "đếm": ["count"], "lệnh": ["command"], "máy chủ": ["server"],
    "nhật ký": ["log"], "cấu hình": ["config"], "gói": ["package"], "tạo": ["create"],
    "chạy": ["run"], "dừng": ["stop", "kill"],
}
_TERM_PATS = [
    (re.compile(r"(?<!\w)" + re.escape(k) + r"(?!\w)", re.I), v)
    for k, v in sorted(TERM_MIX.items(), key=lambda kv: -len(kv[0]))
]

_ROWS = ["qwertyuiop", "asdfghjkl", "zxcvbnm"]
NEIGHBORS: dict[str, list[str]] = {}
for _ri, _row in enumerate(_ROWS):
    for _ci, _ch in enumerate(_row):
        _nb = [_row[j] for j in (_ci - 1, _ci + 1) if 0 <= j < len(_row)]
        for _rj in (_ri - 1, _ri + 1):
            if 0 <= _rj < len(_ROWS) and _ci < len(_ROWS[_rj]):
                _nb.append(_ROWS[_rj][_ci])
        NEIGHBORS[_ch] = _nb

NOISE_OPS = ("no_accent", "mix_en", "typo")


def strip_accents(s: str) -> str:
    s = s.replace("đ", "d").replace("Đ", "D")
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return unicodedata.normalize("NFC", s)


def partial_no_accent(s: str, rng: random.Random, p: float = 0.5) -> str:
    return " ".join(strip_accents(w) if rng.random() < p else w for w in s.split(" "))


def mix_terms(s: str, rng: random.Random, p: float = 0.5) -> str:
    for pat, opts in _TERM_PATS:
        s = pat.sub(lambda m: rng.choice(opts) if rng.random() < p else m.group(0), s)
    return s


def add_typos(s: str, rng: random.Random, protected_tokens: set[str], n: int = 1) -> str:
    """Apply n light typos to whole alphabetic words that don't appear in the reference
    command (so we never corrupt a filename/flag/utility the check depends on)."""
    spans = [
        m.span() for m in re.finditer(r"\w+", s)
        if m.group(0).isalpha() and len(m.group(0)) >= 3
        and m.group(0) not in protected_tokens
        and not any(m.group(0) in t for t in protected_tokens)
    ]
    if not spans:
        return s
    chosen = sorted(rng.sample(spans, k=min(n, len(spans))), reverse=True)
    for a, b in chosen:
        word = s[a:b]
        kind = rng.choice(["swap", "drop", "dup", "neighbor"])
        i = rng.randrange(len(word))
        if kind == "swap" and len(word) >= 2:
            j = min(i + 1, len(word) - 1)
            chars = list(word)
            chars[i], chars[j] = chars[j], chars[i]
            word = "".join(chars)
        elif kind == "drop" and len(word) > 3:
            word = word[:i] + word[i + 1:]
        elif kind == "dup":
            word = word[:i] + word[i] + word[i:]
        elif kind == "neighbor":
            nb = NEIGHBORS.get(word[i].lower())
            if nb:
                repl = rng.choice(nb)
                repl = repl.upper() if word[i].isupper() else repl
                word = word[:i] + repl + word[i + 1:]
        s = s[:a] + word + s[b:]
    return s


def selected_for_noise(instance_id: str, ratio: float, seed: int = 0) -> bool:
    """Deterministic per-id coin flip: same id always gets the same yes/no."""
    return random.Random(f"{seed}:select:{instance_id}").random() < ratio


def noisy_for(
    instance_id: str, text: str, reference_command: str, seed: int = 0
) -> tuple[str, list[str]]:
    """Return (noisy_text, ops_applied), deterministic in (seed, instance_id)."""
    rng = random.Random(f"{seed}:noise:{instance_id}")
    ops = rng.sample(NOISE_OPS, k=rng.randint(1, len(NOISE_OPS)))
    protected = set(re.findall(r"\S+", reference_command))
    out = text
    for op in ops:
        if op == "no_accent":
            out = strip_accents(out) if rng.random() < 0.7 else partial_no_accent(out, rng)
        elif op == "mix_en":
            out = mix_terms(out, rng)
        elif op == "typo":
            out = add_typos(out, rng, protected, n=rng.randint(1, 2))
    return out, ops
