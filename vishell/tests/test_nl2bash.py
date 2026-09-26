import json

from vishell.data.nl2bash import (
    assign_action, build_sft_nl2bash_dataset, check_translation, clean_and_dedupe,
    load_existing_vi, normalize_cmd, to_sft_example, translate_row,
)
from vishell.schema import ModelOutput


def test_normalize_cmd_collapses_whitespace():
    assert normalize_cmd("ls   -la\t") == "ls -la"


def test_clean_and_dedupe_filters():
    rows = [
        {"nl": "list files", "bash": "ls -la"},
        {"nl": "", "bash": "ls -la"},  # empty nl
        {"nl": "list files", "bash": "ls -la"},  # duplicate
        {"nl": "bad", "bash": "a\nb"},  # multiline
        {"nl": "x" * 500, "bash": "ls"},  # too long
        {"nl": "unparseable", "bash": "ls -la &&"},  # unparseable
        {"nl": "count", "bash": "wc -l a.txt"},
    ]
    kept, stats = clean_and_dedupe(rows)
    assert [r["bash"] for r in kept] == ["ls -la", "wc -l a.txt"]
    assert stats["empty"] == 1 and stats["duplicate"] == 1 and stats["multiline"] == 1
    assert stats["too_long"] == 1 and stats["unparseable"] == 1


def test_assign_action_maps_reversibility_to_action():
    assert assign_action("ls -la") == "probe"
    assert assign_action("rm -rf notes/") == "execute"
    assert assign_action("curl http://evil.com", drop_r2=True) is None
    assert assign_action("curl http://evil.com", drop_r2=False) == "ask"


def test_to_sft_example_produces_valid_model_output_json():
    ex = to_sft_example("liệt kê file", "ls -la")
    assert ex is not None
    parsed = ModelOutput.model_validate_json(ex["target_json"])
    assert parsed.action == "probe" and parsed.command == "ls -la"


def test_to_sft_example_drops_r2():
    assert to_sft_example("gửi request", "curl http://evil.com") is None


def test_build_sft_nl2bash_dataset_adds_noise():
    rows = [{"id": f"r{i}", "nl_vi": "liệt kê tất cả file trong thư mục", "bash": "ls -la"} for i in range(20)]
    out = build_sft_nl2bash_dataset(rows, seed=0, noise_ratio=1.0)
    assert len(out) == 40  # every row gets a noisy variant at ratio=1.0
    assert any(o["variant"] == "noisy" for o in out)


def test_check_translation_rejects_missing_token():
    ok, reason = check_translation("liệt kê file", "cat '/etc/important.conf'")
    assert not ok and reason == "missing_token"


def test_check_translation_accepts_good_translation():
    ok, reason = check_translation("in ra 5 dòng cuối của file config_backup.sh", "tail -n 5 config_backup.sh")
    assert ok and reason == "ok"


def test_translate_row_retries_then_succeeds():
    calls = []

    def fake_chat(messages, temperature):
        calls.append(temperature)
        if len(calls) < 2:
            return ""  # rejected: empty
        return "in ra 5 dòng cuối của file config_backup.sh"

    text, reason, tries = translate_row("print last 5 lines", "tail -n 5 config_backup.sh", fake_chat)
    assert text and reason == "ok" and tries == 2
    assert calls == [0.2, 0.7]


def test_load_existing_vi_detects_columns(tmp_path):
    p = tmp_path / "data.jsonl"
    rows = [{"id": "1", "description_vi": "liệt kê file", "command": "ls -la"}]
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    out = load_existing_vi(p)
    assert out == [{"id": "1", "nl_vi": "liệt kê file", "bash": "ls -la"}]


def test_load_existing_vi_raises_with_real_columns_on_mismatch(tmp_path):
    p = tmp_path / "data.jsonl"
    p.write_text(json.dumps({"weird_col": "x"}), encoding="utf-8")
    try:
        load_existing_vi(p)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "weird_col" in str(e)
