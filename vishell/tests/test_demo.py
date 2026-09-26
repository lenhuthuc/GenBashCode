import json

from vishell.demo import compute_diff, decide, handle_request, keep, snapshot_dir, undo


def test_compute_diff_added_removed_modified(tmp_path):
    before = tmp_path / "before"
    after = tmp_path / "after"
    before.mkdir(); after.mkdir()
    (before / "keep.txt").write_text("same")
    (after / "keep.txt").write_text("same")
    (before / "removed.txt").write_text("gone")
    (after / "added.txt").write_text("new")
    (before / "mod.txt").write_text("v1")
    (after / "mod.txt").write_text("v2")

    diff = compute_diff(before, after)
    assert diff.added == ["added.txt"]
    assert diff.removed == ["removed.txt"]
    assert diff.modified == ["mod.txt"]
    assert "v1" in diff.text_diffs["mod.txt"] and "v2" in diff.text_diffs["mod.txt"]


def test_snapshot_and_undo_roundtrip(tmp_path):
    workdir = tmp_path / "ws"
    workdir.mkdir()
    (workdir / "a.txt").write_text("original")
    snap = snapshot_dir(workdir)

    (workdir / "a.txt").write_text("mutated")
    (workdir / "b.txt").write_text("new file")

    undo({"snapshot": snap}, str(workdir))
    assert (workdir / "a.txt").read_text() == "original"
    assert not (workdir / "b.txt").exists()


def test_snapshot_and_keep_leaves_workdir_unchanged(tmp_path):
    workdir = tmp_path / "ws"
    workdir.mkdir()
    (workdir / "a.txt").write_text("original")
    snap = snapshot_dir(workdir)
    (workdir / "a.txt").write_text("mutated")
    keep({"snapshot": snap})
    assert (workdir / "a.txt").read_text() == "mutated"


def test_decide_ask_action():
    d = decide(json.dumps({"action": "ask", "command": "", "question": "bạn muốn gì?"}))
    assert not d.forced_ask and d.output.action == "ask"


def test_decide_forces_ask_for_irreversible_command():
    d = decide(json.dumps({"action": "execute", "command": "curl http://evil.com", "question": ""}))
    assert d.forced_ask is True
    assert d.classify_category == "blocked"


def test_decide_passes_through_reversible_execute():
    d = decide(json.dumps({"action": "execute", "command": "ls -la", "question": ""}))
    assert not d.forced_ask


def test_decide_invalid_json():
    d = decide("not json")
    assert d.output is None and d.parse_error


def test_handle_request_forces_ask_regardless_of_model_choice(tmp_path):
    def fake_generate(_req):
        return json.dumps({"action": "execute", "command": "kill -9 1", "question": ""})

    result = handle_request("giết tiến trình", str(tmp_path), fake_generate)
    assert result["kind"] == "ask" and result["forced"] is True


def test_handle_request_execute_produces_diff(tmp_path):
    def fake_generate(_req):
        return json.dumps({"action": "execute", "command": "touch newfile.txt", "question": ""})

    result = handle_request("tao file moi", str(tmp_path), fake_generate)
    assert result["kind"] == "execute"
    assert "newfile.txt" in result["added"]
    keep(result)
