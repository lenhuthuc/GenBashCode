import json

from vishell.paths import Paths
from vishell.report import render_report, write_report


def test_render_report_degrades_gracefully_with_no_data(tmp_path):
    paths = Paths(root=tmp_path, run_name="empty")
    report = render_report(paths, tmp_path)
    assert "chưa chạy" in report or "chưa có" in report
    assert "mermaid" in report


def test_render_report_includes_verify_and_metrics_when_present(tmp_path):
    paths = Paths(root=tmp_path, run_name="r")
    round1 = {"n_templates": 10, "n_passed": 9, "n_failed": 1, "pass_rate": 0.9,
              "disagreement_rate": 0.05, "failed": [{"template_id": "x", "reasons": ["bad"]}]}
    (paths.results / "verify.json").write_text(json.dumps({**round1, "history": [round1]}), encoding="utf-8")
    (paths.results / "eval_templates_summary.json").write_text(json.dumps([
        {"system": "oracle", "variant": "clean", "n": 5, "json_error_rate": 0.0,
         "action_accuracy": 1.0, "execution_accuracy": 1.0, "danger_rate": 0.0,
         "over_ask_rate": 0.0, "probe_safety_violation_rate": 0.0},
    ]), encoding="utf-8")
    report = render_report(paths, tmp_path)
    assert "0.900" in report or "9" in report
    assert "oracle" in report


def test_write_report_creates_file(tmp_path):
    paths = Paths(root=tmp_path, run_name="r")
    out = write_report(paths, tmp_path / "REPORT.md")
    assert out.exists()
    assert "ViShell" in out.read_text(encoding="utf-8")
