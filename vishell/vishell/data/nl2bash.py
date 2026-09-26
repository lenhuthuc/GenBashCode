"""NL2Bash (TellinaTool) -> Vietnamese -> SFT examples for the `sft_nl2bash` stage.

Either loads an already-translated `data/nl2bash_vi/` (flexible column detection, per
AGENT.md section 2 — the file's real columns decide the names, nothing is guessed) or
downloads+translates from scratch via an OpenAI-compatible server (vLLM). Bashlex
filtering reuses classify.py; noise reuses noise.py; the SFT label format reuses
schema.ModelOutput so this file never re-invents the JSON shape.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from ..classify import classify
from ..noise import noisy_for, selected_for_noise
from ..schema import ModelOutput

NL2BASH_NL_URL = "https://raw.githubusercontent.com/TellinaTool/nl2bash/master/data/bash/all.nl"
NL2BASH_CM_URL = "https://raw.githubusercontent.com/TellinaTool/nl2bash/master/data/bash/all.cm"

NL_COL_CANDIDATES = ["nl_vi", "vi", "description_vi", "nl_vi_clean", "cau_vi", "request_vi", "nl"]
CMD_COL_CANDIDATES = ["cmd", "bash", "command", "bash_cmd", "reference_command"]

_CJK_RE = re.compile(r"[一-鿿぀-ヿ가-힣]")


def normalize_cmd(cmd: str) -> str:
    """Collapse whitespace for dedup/grouping — same command written with different
    spacing must land in the same train/test group."""
    return " ".join((cmd or "").split())


def download_nl2bash(dest_dir: str | Path) -> tuple[Path, Path]:
    """Download all.nl/all.cm once; skip if already present (resume-friendly)."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    nl_path, cm_path = dest / "all.nl", dest / "all.cm"
    for url, path in ((NL2BASH_NL_URL, nl_path), (NL2BASH_CM_URL, cm_path)):
        if path.exists():
            continue
        with urllib.request.urlopen(url, timeout=60) as resp:
            path.write_bytes(resp.read())
    return nl_path, cm_path


def load_raw_pairs(nl_path: str | Path, cm_path: str | Path) -> list[dict]:
    nls = Path(nl_path).read_text(encoding="utf-8").splitlines()
    cms = Path(cm_path).read_text(encoding="utf-8").splitlines()
    if len(nls) != len(cms):
        raise ValueError(f"all.nl has {len(nls)} lines, all.cm has {len(cms)} — misaligned")
    return [{"id": f"nl2b-{i}", "nl": nl.strip(), "bash": cm.strip()} for i, (nl, cm) in enumerate(zip(nls, cms))]


def clean_and_dedupe(rows: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """Drop empty/multiline/too-long/unparseable rows, then exact-duplicate commands."""
    stats: dict[str, int] = {}
    kept, seen = [], set()
    for r in rows:
        nl, cmd = (r.get("nl") or "").strip(), (r.get("bash") or "").strip()
        if not nl or not cmd:
            stats["empty"] = stats.get("empty", 0) + 1
            continue
        if "\n" in cmd:
            stats["multiline"] = stats.get("multiline", 0) + 1
            continue
        if len(cmd) > 300 or len(nl) > 400:
            stats["too_long"] = stats.get("too_long", 0) + 1
            continue
        if not classify(cmd).parseable:
            stats["unparseable"] = stats.get("unparseable", 0) + 1
            continue
        key = normalize_cmd(cmd)
        if key in seen:
            stats["duplicate"] = stats.get("duplicate", 0) + 1
            continue
        seen.add(key)
        kept.append(r)
    return kept, stats


def _pick_column(columns: list[str], candidates: list[str]) -> str:
    for c in candidates:
        if c in columns:
            return c
    raise ValueError(f"none of {candidates} found in columns {columns} — read the real file, don't guess")


def load_existing_vi(path: str | Path) -> list[dict]:
    """Load an already-translated dataset, auto-detecting the NL/command columns from
    whatever the real file actually has (jsonl, csv, or parquet)."""
    path = Path(path)
    if path.is_dir():
        files = sorted(path.glob("*.jsonl")) + sorted(path.glob("*.csv")) + sorted(path.glob("*.parquet"))
        if not files:
            raise FileNotFoundError(f"no .jsonl/.csv/.parquet under {path}")
        rows: list[dict] = []
        for f in files:
            rows.extend(load_existing_vi(f))
        return rows

    if path.suffix == ".jsonl":
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        import pandas as pd
        df = pd.read_csv(path) if path.suffix == ".csv" else pd.read_parquet(path)
        rows = df.to_dict(orient="records")

    if not rows:
        return []
    columns = list(rows[0].keys())
    nl_col = _pick_column(columns, NL_COL_CANDIDATES)
    cmd_col = _pick_column(columns, CMD_COL_CANDIDATES)
    return [{"id": r.get("id", str(i)), "nl_vi": r[nl_col], "bash": r[cmd_col]} for i, r in enumerate(rows)]


# --------------------------------------------------------------------------- translation
ChatFn = Callable[[list[dict], float], str]  # (messages, temperature) -> assistant text


def _openai_chat(server_url: str, api_key: str, model: str) -> ChatFn:
    """Real implementation: one OpenAI-compatible /chat/completions call via stdlib
    urllib (no extra dependency needed just to hit a JSON HTTP endpoint)."""

    def call(messages: list[dict], temperature: float) -> str:
        body = json.dumps({"model": model, "messages": messages, "temperature": temperature,
                            "max_tokens": 256}).encode("utf-8")
        req = urllib.request.Request(
            server_url.rstrip("/") + "/chat/completions", data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"].strip()

    return call


_FEWSHOT = [
    ("find all .txt files in /home/user larger than 10MB",
     "tìm tất cả file .txt trong /home/user có kích thước lớn hơn 10MB"),
    ("print the last 5 lines of config_backup.sh",
     "in ra 5 dòng cuối của file config_backup.sh"),
]
_TRANSLATE_SYS = (
    "Bạn là biên dịch viên kỹ thuật. Dịch yêu cầu tiếng Anh sang tiếng Việt tự nhiên, "
    "giống cách một lập trình viên Việt Nam gõ yêu cầu cho trợ lý dòng lệnh. Giữ nguyên "
    "tên file, đường dẫn, tên tiện ích, tuỳ chọn, số, URL và mọi nội dung trong dấu nháy. "
    "Không giải thích, không viết lệnh bash, chỉ trả về một dòng là bản dịch."
)


def check_translation(nl_vi: str, reference_command: str) -> tuple[bool, str]:
    """Cheap, deterministic acceptance check (no LLM judge): non-empty, no CJK leakage,
    reasonable length, and every quoted/path-like token from the command survives."""
    if not nl_vi.strip():
        return False, "empty"
    if _CJK_RE.search(nl_vi):
        return False, "cjk"
    if len(nl_vi) > 4 * max(len(reference_command), 20):
        return False, "length_ratio"
    protected = re.findall(r"'[^']+'|\"[^\"]+\"|/[\w./-]+|[\w.-]+\.\w{1,4}\b", reference_command)
    for tok in protected:
        if tok.strip("'\"") not in nl_vi:
            return False, "missing_token"
    return True, "ok"


def translate_row(nl: str, reference_command: str, chat_fn: ChatFn, max_tries: int = 3) -> tuple[str, str, int]:
    """Returns (nl_vi, reason, tries). Retries at increasing temperature on rejection."""
    messages = [{"role": "system", "content": _TRANSLATE_SYS}]
    for en, vi in _FEWSHOT:
        messages += [{"role": "user", "content": en}, {"role": "assistant", "content": vi}]
    messages.append({"role": "user", "content": nl})

    last_reason = "empty"
    for attempt in range(max_tries):
        temp = [0.2, 0.7, 0.9][min(attempt, 2)]
        out = chat_fn(messages, temp)
        ok, reason = check_translation(out, reference_command)
        if ok:
            return out, reason, attempt + 1
        last_reason = reason
    return "", last_reason, max_tries


# --------------------------------------------------------------------------- SFT target
def assign_action(bash_cmd: str, drop_r2: bool = True) -> Optional[str]:
    level = classify(bash_cmd).level
    if level == "R0":
        return "probe"
    if level == "R1":
        return "execute"
    return None if drop_r2 else "ask"


def to_sft_example(nl_vi: str, bash_cmd: str, drop_r2: bool = True) -> Optional[dict]:
    action = assign_action(bash_cmd, drop_r2)
    if action is None:
        return None
    output = ModelOutput(action=action, command=bash_cmd if action != "ask" else "", question="")
    return {"nl_vi": nl_vi, "target_json": output.model_dump_json()}


def build_sft_nl2bash_dataset(
    rows: list[dict], seed: int = 0, noise_ratio: float = 0.3, drop_r2: bool = True
) -> list[dict]:
    """rows: [{"id", "nl_vi", "bash"}]. Adds noisy variants to ~noise_ratio of rows,
    matching AGENT.md section 5's train noise ratio."""
    out = []
    for r in rows:
        ex = to_sft_example(r["nl_vi"], r["bash"], drop_r2)
        if ex is None:
            continue
        out.append({"id": r["id"], "variant": "clean", **ex})
        if selected_for_noise(r["id"], noise_ratio, seed):
            noisy_text, ops = noisy_for(r["id"], r["nl_vi"], r["bash"], seed)
            noisy_ex = to_sft_example(noisy_text, r["bash"], drop_r2)
            if noisy_ex is not None:
                out.append({"id": r["id"] + "-n", "variant": "noisy", "noise_ops": ops, **noisy_ex})
    return out
