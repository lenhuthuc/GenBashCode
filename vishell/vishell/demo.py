"""Gradio demo on a real, user-chosen working directory (not the training sandbox).
ask -> show the question; probe -> run read-only, show output; execute -> snapshot,
run, show a diff, then Keep/Undo. A command classify.py measures as irreversible is
always forced to "Cần xác nhận", no matter what the model chose (AGENT.md section 8).

The diff/snapshot logic is plain functions (tested without Gradio installed); only
`build_app`/`main` import gradio, and only when the demo is actually launched.
"""
from __future__ import annotations

import difflib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .classify import classify
from .prompts import SYSTEM_PROMPT, build_messages
from .schema import ModelOutput, parse_output


@dataclass
class DiffResult:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    text_diffs: dict[str, str] = field(default_factory=dict)


def _list_files(root: Path) -> dict[str, Path]:
    return {str(p.relative_to(root)): p for p in root.rglob("*") if p.is_file()}


def compute_diff(before_root: str | Path, after_root: str | Path, max_text_bytes: int = 200_000) -> DiffResult:
    before, after = _list_files(Path(before_root)), _list_files(Path(after_root))
    result = DiffResult()
    for rel in sorted(set(before) | set(after)):
        in_before, in_after = rel in before, rel in after
        if in_before and not in_after:
            result.removed.append(rel)
        elif in_after and not in_before:
            result.added.append(rel)
        else:
            b, a = before[rel].read_bytes(), after[rel].read_bytes()
            if b != a:
                result.modified.append(rel)
                if len(b) <= max_text_bytes and len(a) <= max_text_bytes:
                    try:
                        diff = difflib.unified_diff(
                            b.decode("utf-8").splitlines(keepends=True),
                            a.decode("utf-8").splitlines(keepends=True),
                            fromfile=f"before/{rel}", tofile=f"after/{rel}",
                        )
                        result.text_diffs[rel] = "".join(diff)
                    except UnicodeDecodeError:
                        result.text_diffs[rel] = "(binary file changed)"
    return result


def snapshot_dir(workdir: str | Path) -> str:
    snap = tempfile.mkdtemp(prefix="vishell_demo_snap_")
    shutil.copytree(workdir, snap, dirs_exist_ok=True)
    return snap


def restore_from_snapshot(workdir: str | Path, snapshot: str | Path) -> None:
    workdir = Path(workdir)
    for item in list(workdir.iterdir()):
        shutil.rmtree(item) if item.is_dir() else item.unlink()
    shutil.copytree(snapshot, workdir, dirs_exist_ok=True)


def run_in_dir(command: str, workdir: str | Path, timeout: float = 30) -> dict:
    try:
        proc = subprocess.run(["bash", "-c", command], cwd=str(workdir), capture_output=True,
                              text=True, timeout=timeout)
        return {"rc": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr, "timed_out": False}
    except subprocess.TimeoutExpired as e:
        return {"rc": None, "stdout": e.stdout or "", "stderr": e.stderr or "", "timed_out": True}


@dataclass
class Decision:
    output: Optional[ModelOutput]
    parse_error: Optional[str]
    forced_ask: bool  # classify forced this to "Cần xác nhận" regardless of model's action
    classify_level: Optional[str] = None
    classify_category: Optional[str] = None


def decide(raw_text: str) -> Decision:
    output, err = parse_output(raw_text)
    if output is None:
        return Decision(None, err, forced_ask=False)
    if output.action in ("execute", "probe") and output.command.strip():
        cls = classify(output.command)
        if cls.category in ("blocked", "irreversible"):
            return Decision(output, None, forced_ask=True, classify_level=cls.level, classify_category=cls.category)
        return Decision(output, None, forced_ask=False, classify_level=cls.level, classify_category=cls.category)
    return Decision(output, None, forced_ask=False)


def handle_request(request_vi: str, workdir: str, generate_fn) -> dict:
    """Pure orchestration used by the Gradio callback: returns everything the UI needs
    to render (no gradio import here)."""
    raw = generate_fn(request_vi)
    decision = decide(raw)
    if decision.parse_error:
        return {"kind": "error", "message": f"JSON không hợp lệ: {decision.parse_error}\nRaw: {raw}"}

    output = decision.output
    if decision.forced_ask:
        return {"kind": "ask", "question": output.question or "Lệnh này có tác động khó hoàn tác — bạn có chắc chắn muốn tiếp tục?",
                "forced": True, "model_command": output.command}

    if output.action == "ask":
        return {"kind": "ask", "question": output.question, "forced": False}

    if output.action == "probe":
        res = run_in_dir(output.command, workdir)
        return {"kind": "probe", "command": output.command, **res}

    # execute
    snapshot = snapshot_dir(workdir)
    res = run_in_dir(output.command, workdir)
    diff = compute_diff(snapshot, workdir)
    return {"kind": "execute", "command": output.command, "snapshot": snapshot, **res,
            "added": diff.added, "removed": diff.removed, "modified": diff.modified,
            "text_diffs": diff.text_diffs}


def keep(result: dict) -> None:
    shutil.rmtree(result["snapshot"], ignore_errors=True)


def undo(result: dict, workdir: str) -> None:
    restore_from_snapshot(workdir, result["snapshot"])
    shutil.rmtree(result["snapshot"], ignore_errors=True)


def build_app(generate_fn, share: bool = False):
    import gradio as gr

    state_workdir = gr.State("")
    state_last_result = gr.State(None)

    with gr.Blocks(title="ViShell demo") as app:
        gr.Markdown("# ViShell — trợ lý dòng lệnh tiếng Việt (demo)")
        workdir_box = gr.Textbox(label="Thư mục làm việc")
        request_box = gr.Textbox(label="Yêu cầu")
        output_box = gr.Markdown()
        keep_btn = gr.Button("Giữ", visible=False)
        undo_btn = gr.Button("Hoàn tác", visible=False)

        def on_submit(request_vi, workdir):
            result = handle_request(request_vi, workdir, generate_fn)
            text = _render_result(result)
            show_buttons = result["kind"] == "execute"
            return text, result, gr.update(visible=show_buttons), gr.update(visible=show_buttons)

        request_box.submit(on_submit, [request_box, workdir_box],
                            [output_box, state_last_result, keep_btn, undo_btn])
        keep_btn.click(lambda r: (keep(r), gr.update(visible=False), gr.update(visible=False))[1:],
                       [state_last_result], [keep_btn, undo_btn])
        undo_btn.click(lambda r, w: (undo(r, w), gr.update(visible=False), gr.update(visible=False))[1:],
                       [state_last_result, workdir_box], [keep_btn, undo_btn])

    app.launch(share=share)
    return app


def _render_result(result: dict) -> str:
    if result["kind"] == "error":
        return f"❌ {result['message']}"
    if result["kind"] == "ask":
        prefix = "⚠️ Cần xác nhận (bị chặn vì khó hoàn tác)" if result.get("forced") else "❓"
        return f"{prefix}: {result['question']}"
    if result["kind"] == "probe":
        return f"🔍 `{result['command']}`\n```\n{result['stdout']}\n```"
    return (f"⚙️ `{result['command']}`\nThêm: {result['added']}\nXoá: {result['removed']}\n"
            f"Sửa: {result['modified']}")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--share", action="store_true")
    args = parser.parse_args()

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(args.model_dir)
    model = AutoModelForCausalLM.from_pretrained(args.model_dir, device_map="auto")

    def generate(request_vi: str) -> str:
        messages = build_messages(request_vi, SYSTEM_PROMPT)
        text = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        enc = tok(text, return_tensors="pt").to(model.device)
        with torch.no_grad():
            out = model.generate(**enc, max_new_tokens=128, do_sample=False,
                                 pad_token_id=tok.pad_token_id or tok.eos_token_id)
        return tok.decode(out[0, enc["input_ids"].shape[1]:], skip_special_tokens=True)

    build_app(generate, share=args.share)


if __name__ == "__main__":
    main()
