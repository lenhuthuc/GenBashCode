"""Assemble REPORT.md from whatever results/logs exist under a Paths' results/logs/
checkpoints directories. Every section degrades gracefully — a stage that hasn't run
yet gets one placeholder line instead of crashing the report (AGENT.md section 11).
"""
from __future__ import annotations

import json
from pathlib import Path

from .paths import Paths

_PIPELINE_MERMAID = """```mermaid
flowchart TD
    A[data-nl2bash: NL2Bash EN -> VI] --> B[sft_nl2bash: SFT LoRA]
    T[templates/*.json] --> V[verify: sandbox certification]
    V --> D[build-dataset: instances, train/test split]
    B --> M1[merge]
    M1 --> C[sft_scenarios: SFT LoRA on templates]
    D --> C
    C --> M2[merge]
    M2 --> G[grpo: TRL GRPOTrainer + sandbox reward]
    D --> G
    G --> M3[merge] --> X[export: GGUF q4_k_m]
    B & C & G --> E[evaluate: all systems x clean/noisy]
    E --> R[report: REPORT.md]
    X --> DE[demo: Gradio]
```"""


def _read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _md_table(rows: list[dict]) -> str:
    if not rows:
        return "_(chưa có dữ liệu)_"
    cols = list(rows[0].keys())
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for r in rows:
        lines.append("| " + " | ".join(_fmt(r.get(c)) for c in cols) + " |")
    return "\n".join(lines)


def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def _data_stats_section(paths: Paths) -> str:
    stats = _read_json(paths.results / "data_stats.json")
    if stats is None:
        return "### Thống kê dữ liệu\n_(chưa chạy data-nl2bash / build-dataset)_"
    return "### Thống kê dữ liệu\n" + _md_table([stats]) if isinstance(stats, list) else \
        "### Thống kê dữ liệu\n```json\n" + json.dumps(stats, ensure_ascii=False, indent=2) + "\n```"


def _verify_section(paths: Paths) -> str:
    v = _read_json(paths.results / "verify.json")
    if v is None:
        return "### Kiểm chứng template\n_(chưa chạy `python -m vishell verify`)_"
    history = v.get("history", [v])
    lines = ["### Kiểm chứng template", _md_table([
        {"round": i + 1, "n_templates": h["n_templates"], "n_passed": h["n_passed"],
         "pass_rate": h["pass_rate"], "disagreement_rate": h["disagreement_rate"]}
        for i, h in enumerate(history)
    ])]
    if v.get("failed"):
        lines.append(f"\n{len(v['failed'])} template bị loại sau kiểm chứng cuối cùng (xem `results/verify.json`).")
    return "\n".join(lines)


def _plot_curve(log_history: list[dict], keys: list[str], out_path: Path, title: str) -> bool:
    xs = [h["step"] for h in log_history if "step" in h]
    if not xs:
        return False
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.figure(figsize=(6, 3.5))
    for key in keys:
        pts = [(h["step"], h[key]) for h in log_history if key in h]
        if pts:
            plt.plot([p[0] for p in pts], [p[1] for p in pts], label=key)
    plt.xlabel("step")
    plt.title(title)
    plt.legend()
    plt.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_path)
    plt.close()
    return True


def _train_section(paths: Paths) -> str:
    stages = ["sft_nl2bash", "sft_scenarios", "grpo"]
    lines = ["### Cấu hình và tiến trình train"]
    plots_dir = paths.results / "plots"
    any_stage = False
    for stage in stages:
        log_path = paths.checkpoints / stage / "log_history.json"
        history = _read_json(log_path)
        if history is None:
            lines.append(f"- **{stage}**: chưa chạy")
            continue
        any_stage = True
        keys = ["loss", "eval_loss"] if stage != "grpo" else ["reward", "loss"]
        img = plots_dir / f"{stage}_curve.png"
        ok = _plot_curve(history, keys, img, f"{stage}: {'/'.join(keys)}")
        rel = f"results/plots/{img.name}" if ok else None
        lines.append(f"- **{stage}**: {len(history)} log entries" + (f" — ![curve]({rel})" if rel else ""))
    if not any_stage:
        lines.append("_(chưa có giai đoạn train nào chạy)_")
    return "\n".join(lines)


def _metrics_section(paths: Paths) -> str:
    parts = ["### Bảng so sánh các hệ"]
    tmpl_summary = _read_json(paths.results / "eval_templates_summary.json")
    parts.append("**Trên template test (execute/probe/ask):**\n" + _md_table(tmpl_summary or []))
    nl2b_summary = _read_json(paths.results / "eval_nl2bash_summary.json")
    parts.append("**Trên NL2Bash test (parse/EM/utility):**\n" + _md_table(nl2b_summary or []))
    if not tmpl_summary and not nl2b_summary:
        parts.append("_(chưa chạy evaluate)_")
    return "\n\n".join(parts)


def _format_example(p: dict | None) -> str:
    if p is None:
        return "_(không có ví dụ)_"
    return f"\"{p['request_vi']}\" → model chọn **{p['model_action']}** `{p['model_command']}`"


def _qualitative_section(paths: Paths) -> str:
    preds = [p for p in _read_jsonl(paths.results / "predictions_templates.jsonl") if not p.get("json_error")]
    if not preds:
        return "### Ví dụ định tính\n_(chưa có dự đoán để trích ví dụ)_"
    lines = ["### Ví dụ định tính"]
    for action in ("execute", "probe", "ask"):
        matching = [p for p in preds if p.get("expected_action") == action]
        good = next((p for p in matching if p.get("success")), None)
        bad = next((p for p in matching if not p.get("success")), None)
        lines.append(f"**{action}**\n- đúng: {_format_example(good)}\n- sai: {_format_example(bad)}")
    return "\n".join(lines)


def _limitations() -> str:
    return (
        "### Hạn chế, rủi ro, việc tiếp theo\n"
        "- classify.py là bộ lọc tĩnh dựa trên bashlex: không hiểu ngữ nghĩa lệnh (vd HTTP GET vs POST qua curl), "
        "mặc định về R2 khi không chắc — an toàn nhưng có thể quá thận trọng.\n"
        "- Reward hoàn toàn từ sandbox/hash, không có giám khảo LLM: các hành vi tinh vi hơn (side-effect logic bên trong "
        "script) không được đo.\n"
        "- Template do một người viết trong thời gian hạn chế: đa dạng kịch bản còn giới hạn so với dữ liệu thực tế.\n"
        "- GPU chỉ khả dụng trên Colab/Kaggle: các bước train chạy tách rời máy sinh dữ liệu, dễ lệch phiên bản thư viện.\n"
        "- Việc tiếp theo: mở rộng template (đặc biệt nhóm ask/irreversible), thêm baseline model lớn qua API, "
        "review thủ công bản dịch NL2Bash-vi."
    )


def render_report(paths: Paths, project_root: str | Path) -> str:
    header = "# ViShell — Báo cáo pipeline\n\n" + _PIPELINE_MERMAID
    sections = [
        header,
        _data_stats_section(paths),
        _verify_section(paths),
        _train_section(paths),
        _metrics_section(paths),
        _qualitative_section(paths),
        _limitations(),
    ]
    return "\n\n".join(sections) + "\n"


def write_report(paths: Paths, out_path: str | Path) -> Path:
    out_path = Path(out_path)
    out_path.write_text(render_report(paths, out_path.parent), encoding="utf-8")
    return out_path
