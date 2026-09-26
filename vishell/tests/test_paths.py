from vishell.paths import Paths, detect_env


def test_detect_env_local_on_this_machine():
    assert detect_env() == "local"


def test_paths_creates_run_scoped_directories(tmp_path):
    p = Paths(root=tmp_path, run_name="myrun")
    assert p.data == tmp_path / "data" / "myrun"
    assert p.data.exists()
    assert p.checkpoints == tmp_path / "checkpoints" / "myrun"
    assert p.results.exists() and p.logs.exists() and p.models.exists()


def test_paths_done_marker_roundtrip(tmp_path):
    p = Paths(root=tmp_path, run_name="r")
    assert not p.is_done("verify")
    p.mark_done("verify")
    assert p.is_done("verify")
    assert not p.is_done("other-step")


def test_paths_different_run_names_are_isolated(tmp_path):
    a = Paths(root=tmp_path, run_name="smoke")
    b = Paths(root=tmp_path, run_name="full")
    a.mark_done("verify")
    assert a.is_done("verify")
    assert not b.is_done("verify")
