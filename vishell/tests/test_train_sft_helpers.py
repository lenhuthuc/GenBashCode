"""Only the torch-free helpers in train/sft.py — the rest needs [train] extras/GPU."""
from pathlib import Path

from vishell.train.sft import filter_kwargs, latest_checkpoint, read_jsonl


class Sig:
    def __init__(self, a, b=1):
        pass


def test_filter_kwargs_drops_unknown_and_warns(recwarn):
    out = filter_kwargs(Sig, {"a": 1, "b": 2, "unknown_arg": 3})
    assert out == {"a": 1, "b": 2}
    assert any("unknown_arg" in str(w.message) for w in recwarn.list)


def test_filter_kwargs_keeps_everything_for_var_kwargs():
    def fn(**kwargs):
        pass

    out = filter_kwargs(fn, {"anything": 1, "goes": 2})
    assert out == {"anything": 1, "goes": 2}


def test_latest_checkpoint_picks_highest_step(tmp_path):
    for step in (100, 300, 200):
        d = tmp_path / f"checkpoint-{step}"
        d.mkdir()
        (d / "trainer_state.json").write_text("{}")
    (tmp_path / "checkpoint-999").mkdir()  # no trainer_state.json -> incomplete, ignored
    assert latest_checkpoint(tmp_path).name == "checkpoint-300"


def test_latest_checkpoint_none_when_absent(tmp_path):
    assert latest_checkpoint(tmp_path) is None


def test_read_jsonl(tmp_path):
    p = tmp_path / "d.jsonl"
    p.write_text('{"a": 1}\n\n{"a": 2}\n', encoding="utf-8")
    assert read_jsonl(p) == [{"a": 1}, {"a": 2}]
