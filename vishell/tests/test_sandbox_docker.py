import shutil

import pytest

from vishell.sandbox.backends import DockerBackend

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="needs Docker")


@pytest.fixture(scope="module")
def backend():
    return DockerBackend(timeout=10)


def test_fs_changed_true_on_mutation(backend):
    r = backend.run({
        "setup": "echo hi > a.txt", "command": "echo world >> a.txt",
        "check_type": "stdout_contains", "check_expected": "", "timeout": 5,
    })
    assert r["fs_changed"] is True and r["rc"] == 0


def test_fs_changed_false_on_read_only(backend):
    r = backend.run({
        "setup": "echo hi > a.txt", "command": "cat a.txt",
        "check_type": "stdout_equals", "check_expected": "hi", "timeout": 5,
    })
    assert r["fs_changed"] is False and r["check_passed"] is True


def test_timeout(backend):
    r = backend.run({
        "setup": "", "command": "sleep 5",
        "check_type": "stdout_contains", "check_expected": "", "timeout": 2,
    })
    assert r["timed_out"] is True


def test_check_types(backend):
    r = backend.run({
        "setup": "", "command": "echo hello world",
        "check_type": "stdout_contains", "check_expected": "lo wor", "timeout": 5,
    })
    assert r["check_passed"] is True
    r = backend.run({
        "setup": 'printf "b\\na\\nc\\n" > f.txt', "command": "sort f.txt",
        "check_type": "stdout_lines_set", "check_expected": "a\nb\nc", "timeout": 5,
    })
    assert r["check_passed"] is True
    r = backend.run({
        "setup": "echo hi > a.txt", "command": "cp a.txt b.txt",
        "check_type": "script", "check_expected": "test -f b.txt", "timeout": 5,
    })
    assert r["check_passed"] is True


def test_setup_failure_short_circuits(backend):
    r = backend.run({
        "setup": "exit 3", "command": "echo unreachable",
        "check_type": "stdout_contains", "check_expected": "x", "timeout": 5,
    })
    assert r["setup_rc"] == 3 and r.get("error") == "setup failed"


def test_network_and_readonly_root_are_boundary_violations(backend):
    r = backend.run({
        "setup": "", "command": 'python3 -c "import socket; s=socket.socket(); s.settimeout(2); s.connect((\'8.8.8.8\',80))"',
        "check_type": "stdout_contains", "check_expected": "x", "timeout": 8,
    })
    assert r["boundary_violation"] is True
    r = backend.run({
        "setup": "", "command": 'python3 -c "open(\'/etc/hosts\',\'a\').write(\'x\')"',
        "check_type": "stdout_contains", "check_expected": "x", "timeout": 5,
    })
    assert r["boundary_violation"] is True


def test_restore_check(backend):
    r = backend.run({"_mode": "restore_check", "setup": "echo a > f.txt", "command": "rm f.txt", "timeout": 5})
    assert r["restored_ok"] is True and r["fs_changed"] is True
