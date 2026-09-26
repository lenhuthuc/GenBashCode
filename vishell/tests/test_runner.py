import os
import shutil

import pytest

from vishell.sandbox.runner import hash_tree, restore_check, run_episode

# runner.py assumes a POSIX environment (it's what actually runs inside the Linux
# Docker sandbox / Colab / Kaggle runtime, per AGENT.md section 4) — bash path
# semantics and resource limits differ enough on native Windows that these direct
# subprocess tests aren't meaningful there. DockerBackend is exercised separately
# and is the accurate cross-platform check (see sandbox/backends.py docstring).
pytestmark = pytest.mark.skipif(
    os.name != "posix" or shutil.which("bash") is None,
    reason="runner.py subprocess semantics require a real POSIX host (Linux sandbox/Colab/Kaggle)",
)


def test_fs_changed_true_on_mutation():
    r = run_episode({
        "setup": "echo hi > a.txt", "command": "echo world >> a.txt",
        "check_type": "stdout_contains", "check_expected": "", "timeout": 5,
    })
    assert r["fs_changed"] is True
    assert r["rc"] == 0


def test_fs_changed_false_on_read_only():
    r = run_episode({
        "setup": "echo hi > a.txt", "command": "cat a.txt",
        "check_type": "stdout_equals", "check_expected": "hi", "timeout": 5,
    })
    assert r["fs_changed"] is False
    assert r["check_passed"] is True


def test_timeout():
    r = run_episode({
        "setup": "", "command": "sleep 5",
        "check_type": "stdout_contains", "check_expected": "", "timeout": 1,
    })
    assert r["timed_out"] is True


def test_check_stdout_equals():
    r = run_episode({
        "setup": "", "command": "echo -n hello",
        "check_type": "stdout_equals", "check_expected": "hello", "timeout": 5,
    })
    assert r["check_passed"] is True


def test_check_stdout_contains():
    r = run_episode({
        "setup": "", "command": "echo hello world",
        "check_type": "stdout_contains", "check_expected": "lo wor", "timeout": 5,
    })
    assert r["check_passed"] is True


def test_check_stdout_lines_set():
    r = run_episode({
        "setup": 'printf "b\\na\\nc\\n" > f.txt', "command": "sort f.txt",
        "check_type": "stdout_lines_set", "check_expected": "a\nb\nc", "timeout": 5,
    })
    assert r["check_passed"] is True


def test_check_script():
    r = run_episode({
        "setup": "echo hi > a.txt", "command": "cp a.txt b.txt",
        "check_type": "script", "check_expected": "test -f b.txt", "timeout": 5,
    })
    assert r["check_passed"] is True


def test_check_fails_on_noop():
    r = run_episode({
        "setup": "echo hi > a.txt", "command": "true",
        "check_type": "script", "check_expected": "test -f b.txt", "timeout": 5,
    })
    assert r["check_passed"] is False


def test_setup_failure_short_circuits():
    r = run_episode({
        "setup": "exit 3", "command": "echo unreachable",
        "check_type": "stdout_contains", "check_expected": "x", "timeout": 5,
    })
    assert r["setup_rc"] == 3
    assert r.get("error") == "setup failed"


def test_hash_tree_stable_and_sensitive(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    h1 = hash_tree(str(tmp_path))
    h2 = hash_tree(str(tmp_path))
    assert h1 == h2
    (tmp_path / "a.txt").write_text("world")
    assert hash_tree(str(tmp_path)) != h1


def test_restore_check_ok_for_reversible_command():
    r = restore_check({"setup": "echo a > f.txt", "command": "rm f.txt", "timeout": 5})
    assert r["restored_ok"] is True
    assert r["fs_changed"] is True
