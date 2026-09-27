import shutil

import pytest

from vishell.evaluate import (
    evaluate_instance, evaluate_nl2bash_row, evaluate_system_on_templates, main_utility,
    mock_generate, oracle_generate, summarize_nl2bash_eval, summarize_template_eval,
)
from vishell.schema import Instance


def make_instance(expected_action, reversible, ask_reason=None, ref="wc -l a.txt",
                   check_type="stdout_contains", check_expected="", setup="echo hi > a.txt"):
    return Instance(
        instance_id="i-1", template_id="t", request_vi="req", setup=setup,
        expected_action=expected_action, reversible=reversible, reversibility_level="R0",
        ask_reason=ask_reason, reference_command=ref, check_type=check_type,
        check_expected=check_expected,
    )


def test_main_utility_skips_wrappers_and_assignments():
    assert main_utility("sudo find . -name '*.log' | xargs rm") == "find"
    assert main_utility("FOO=1 env LANG=C sort a.txt") == "sort"
    assert main_utility("not valid &&") is None


def test_evaluate_nl2bash_row():
    r = evaluate_nl2bash_row(["tail -n 5 a.txt", "tail -5 a.txt"],
                              '{"action":"execute","command":"tail -n 5 a.txt","question":""}')
    assert r["parse"] and r["exact_match"] and r["utility_match"]
    r2 = evaluate_nl2bash_row(["tail -n 5 a.txt"], '{"action":"execute","command":"head -n 5 a.txt","question":""}')
    assert not r2["exact_match"] and not r2["utility_match"]


def test_summarize_nl2bash_eval():
    rows = [
        {"system": "base", "variant": "clean", "parse": True, "exact_match": True, "utility_match": True},
        {"system": "base", "variant": "clean", "parse": True, "exact_match": False, "utility_match": True},
    ]
    summary = summarize_nl2bash_eval(rows)
    assert summary[0]["parse_rate"] == 1.0
    assert summary[0]["exact_match"] == 0.5
    assert summary[0]["utility_acc"] == 1.0


class _FakeBackend:
    def run(self, payload):
        cmd = payload["command"]
        if cmd == "wc -l a.txt":
            return {"rc": 0, "check_passed": True, "fs_changed": False, "stdout": "1 a.txt\n"}
        if cmd == "":
            return {"rc": 0, "check_passed": False, "fs_changed": False, "stdout": ""}
        return {"rc": 0, "check_passed": False, "fs_changed": True, "stdout": ""}


def test_evaluate_instance_with_oracle_is_always_success():
    inst = make_instance("probe", True)
    raw = oracle_generate(inst)(inst.request_vi)
    metrics = evaluate_instance(inst, raw, _FakeBackend())
    assert metrics["success"] is True and metrics["json_error"] is False


def test_evaluate_instance_with_mock_flags_json_valid_but_wrong_action():
    inst = make_instance("execute", True, ref="wc -l a.txt")
    raw = mock_generate(inst.request_vi)
    metrics = evaluate_instance(inst, raw, _FakeBackend())
    assert metrics["json_error"] is False
    assert metrics["action_match"] is False
    assert metrics["over_ask"] is True  # expected execute, model said ask


def test_evaluate_instance_json_error():
    inst = make_instance("execute", True)
    metrics = evaluate_instance(inst, "not json", _FakeBackend())
    assert metrics == {"json_error": True}


def test_evaluate_system_on_templates_and_summary():
    instances = [make_instance("probe", True), make_instance("execute", True)]
    rows = evaluate_system_on_templates("oracle", lambda req: oracle_generate(instances[0])(req), instances, _FakeBackend())
    summary = summarize_template_eval(rows)
    assert len(summary) == 1
    assert summary[0]["n"] == 2


def test_compare_to_baseline_paired_template_bootstrap():
    from vishell.evaluate import compare_to_baseline

    def rows(system, correct_per_template):
        return [{"system": system, "template_id": f"t{t}", "action_match": i < k, "success": False,
                 "is_irreversible_expected": False, "is_execute_expected": False}
                for t, k in enumerate(correct_per_template) for i in range(4)]

    base = rows("sft", [1, 1, 1, 1, 1, 1, 1, 1])     # 25% action accuracy
    better = rows("grpo", [3, 3, 4, 3, 3, 4, 3, 3])  # ~81%
    same = rows("noop", [1, 1, 1, 1, 1, 1, 1, 1])
    res = {(c["system"], c["metric"]): c for c in compare_to_baseline(base + better + same, "sft", n_boot=500)}
    up = res[("grpo", "action_accuracy")]
    assert up["diff"] > 0.5 and up["ci_low"] > 0 and up["n_templates"] == 8
    flat = res[("noop", "action_accuracy")]
    assert flat["diff"] == 0 and flat["ci_low"] <= 0 <= flat["ci_high"]
    assert res[("grpo", "undo_score_mean")]["diff"] is None  # no undo rows -> no number, no crash
