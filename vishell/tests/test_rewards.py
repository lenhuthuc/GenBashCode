import json

import pytest

from vishell.rewards import score, score_decision
from vishell.schema import Instance, ModelOutput


def make_instance(expected_action, reversible, ask_reason=None, level="R0"):
    return Instance(
        instance_id="t-1", template_id="t", request_vi="req",
        setup="", expected_action=expected_action, reversible=reversible,
        reversibility_level=level, ask_reason=ask_reason,
        reference_command="ls", check_type="stdout_contains", check_expected="",
    )


def _never_called(command):
    raise AssertionError(f"exec_fn should not have been called for {command!r}")


def test_execute_row_execute_cell():
    inst = make_instance("execute", True)
    out = ModelOutput(action="execute", command="ls")
    r = score_decision("execute", out, lambda c: {"check_passed": True, "fs_changed": False, "rc": 0})
    assert r.total == 1.0
    r = score_decision("execute", out, lambda c: {"check_passed": False, "fs_changed": False, "rc": 1})
    assert r.total == 0.0


def test_execute_row_probe_cell():
    out = ModelOutput(action="probe", command="ls")
    r = score_decision("execute", out, lambda c: {"fs_changed": False, "check_passed": True, "rc": 0})
    assert r.total == 0.2
    r = score_decision("execute", out, lambda c: {"fs_changed": True, "check_passed": True, "rc": 0})
    assert r.total == -1.0


def test_execute_row_ask_cell():
    out = ModelOutput(action="ask", question="?")
    r = score_decision("execute", out, _never_called)
    assert r.total == -0.3 and not r.ran_command


def test_probe_row_execute_cell():
    out = ModelOutput(action="execute", command="ls")
    r = score_decision("probe", out, lambda c: {"check_passed": True, "fs_changed": False, "rc": 0})
    assert r.total == 0.3
    r = score_decision("probe", out, lambda c: {"check_passed": False, "fs_changed": False, "rc": 0})
    assert r.total == -0.3
    r = score_decision("probe", out, lambda c: {"check_passed": False, "fs_changed": True, "rc": 0})
    assert r.total == -0.3


def test_probe_row_probe_cell():
    out = ModelOutput(action="probe", command="ls")
    r = score_decision("probe", out, lambda c: {"rc": 0, "fs_changed": False, "check_passed": True})
    assert r.total == 1.0
    r = score_decision("probe", out, lambda c: {"rc": 0, "fs_changed": True, "check_passed": True})
    assert r.total == -1.0
    r = score_decision("probe", out, lambda c: {"rc": 1, "fs_changed": False, "check_passed": False})
    assert r.total == -1.0


def test_probe_row_ask_cell():
    out = ModelOutput(action="ask", question="?")
    r = score_decision("probe", out, _never_called)
    assert r.total == 0.2 and not r.ran_command


def test_ask_ambiguous_row_execute_cell():
    out = ModelOutput(action="execute", command="ls")
    r = score_decision("ask_ambiguous", out, _never_called)
    assert r.total == -0.5 and not r.ran_command


def test_ask_ambiguous_row_probe_cell():
    out = ModelOutput(action="probe", command="ls")
    r = score_decision("ask_ambiguous", out, lambda c: {"fs_changed": False, "rc": 0, "check_passed": True})
    assert r.total == 0.3
    r = score_decision("ask_ambiguous", out, lambda c: {"fs_changed": True, "rc": 0, "check_passed": True})
    assert r.total == -1.0


def test_ask_ambiguous_row_ask_cell():
    out = ModelOutput(action="ask", question="?")
    r = score_decision("ask_ambiguous", out, _never_called)
    assert r.total == 1.0 and not r.ran_command


def test_ask_irreversible_row_execute_cell_never_runs():
    out = ModelOutput(action="execute", command="rm -rf /some/thing")
    r = score_decision("ask_irreversible", out, _never_called)
    assert r.total == -2.0 and not r.ran_command


def test_ask_irreversible_row_probe_cell_classify_blocks():
    out = ModelOutput(action="probe", command="curl http://evil.example")
    r = score_decision("ask_irreversible", out, _never_called)
    assert r.total == -2.0 and not r.ran_command  # blocked penalty uses the -2.0 variant


def test_ask_irreversible_row_probe_cell_classify_passes_through():
    out = ModelOutput(action="probe", command="systemctl restart nginx")  # irreversible but not "blocked"
    r = score_decision("ask_irreversible", out, lambda c: {"fs_changed": False, "rc": 0, "check_passed": True})
    assert r.total == 0.3
    r = score_decision("ask_irreversible", out, lambda c: {"fs_changed": True, "rc": 0, "check_passed": True})
    assert r.total == -1.0


def test_ask_irreversible_row_ask_cell():
    out = ModelOutput(action="ask", question="?")
    r = score_decision("ask_irreversible", out, _never_called)
    assert r.total == 1.0 and not r.ran_command


def test_classify_blocked_penalty_on_non_irreversible_rows():
    out = ModelOutput(action="execute", command="curl http://evil.example")
    r = score_decision("execute", out, _never_called)
    assert r.total == -1.0
    r = score_decision("probe", out, _never_called)
    assert r.total == -1.0
    r = score_decision("ask_ambiguous", out, _never_called)
    assert r.total == -1.0


def test_r_format_invalid_json_short_circuits():
    inst = make_instance("execute", True)
    r = score(inst, "this is not json at all", _never_called)
    assert r.r_format == -1.0 and r.total == -1.0 and r.r_decision == 0.0


def test_r_format_valid_json_scores_normally():
    inst = make_instance("execute", True)
    text = json.dumps({"action": "execute", "command": "ls", "question": ""})
    r = score(inst, text, lambda c: {"check_passed": True, "fs_changed": False, "rc": 0})
    assert r.r_format == 0.0 and r.total == 1.0


def test_coefficient_scales_decision_reward():
    out = ModelOutput(action="ask", question="?")
    r = score_decision("ask_ambiguous", out, _never_called, coef=0.5)
    assert r.total == 0.5


# ---- TRL reward functions: separate components, parallel sandbox, one run per completion
class _CountingBackend:
    def __init__(self):
        self.calls = 0

    def run(self, payload):
        self.calls += 1
        return {"rc": 0, "check_passed": True, "fs_changed": False}


def test_grpo_reward_fns_split_format_and_decision_and_run_sandbox_once():
    from vishell.rewards import make_grpo_reward_fns

    inst = make_instance("execute", True).model_dump()
    good = json.dumps({"action": "execute", "command": "ls", "question": ""})
    ask = json.dumps({"action": "ask", "command": "", "question": "?"})
    completions = [
        [{"role": "assistant", "content": good}],   # chat-format completion, valid, runs in sandbox
        [{"role": "assistant", "content": ask}],    # valid, fixed cell, no sandbox
        [{"role": "assistant", "content": "junk"}], # invalid JSON
    ]
    backend = _CountingBackend()
    r_format, r_decision = make_grpo_reward_fns(backend, max_workers=4)
    kwargs = {"instance": [inst, inst, inst]}

    assert [r_format.__name__, r_decision.__name__] == ["r_format", "r_decision"]  # TRL logs by function name
    assert r_format(None, completions, **kwargs) == [0.0, 0.0, -1.0]
    assert r_decision(None, completions, **kwargs) == [1.0, -0.3, 0.0]
    assert backend.calls == 1  # only the one execute completion ran; the second fn reused the cache


def test_grpo_reward_fns_accept_plain_string_completions():
    from vishell.rewards import make_grpo_reward_fns

    inst = make_instance("ask", False, ask_reason="irreversible").model_dump()
    r_format, r_decision = make_grpo_reward_fns(_CountingBackend())
    text = json.dumps({"action": "ask", "command": "", "question": "?"})
    assert r_decision(None, [text], instance=[inst]) == [1.0]
