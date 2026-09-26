"""Translation wired into the pipeline, tested against a fake OpenAI-compatible server
(no GPU, no vLLM, no network): chunked+resumable translation, server lifecycle checks,
split-by-command, and the data-nl2bash CLI step end to end."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from vishell import cli
from vishell.data import nl2bash as nb


class _Handler(BaseHTTPRequestHandler):
    calls: list = []

    def log_message(self, *args):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # /v1/models readiness probe
        self._send({"data": []})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Handler.calls.append(req)
        ref = req["messages"][-1]["content"].split("KHÔNG đưa vào bản dịch): ")[1]
        # a "translation" that keeps every token of the command, so check_translation accepts it
        self._send({"choices": [{"message": {"content": f"thực hiện lệnh {ref}"}}]})


@pytest.fixture()
def server_url():
    _Handler.calls = []
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


ROWS = [{"id": f"r{i}", "nl": f"do thing {i}", "bash": f"wc -l file{i}.txt"} for i in range(5)]


def test_translate_rows_chunks_and_resumes(server_url, tmp_path):
    chat = nb._openai_chat(server_url, "k", "m")
    first = nb.translate_rows(ROWS, chat, tmp_path, chunk_size=2, max_workers=4, progress=lambda _: None)
    assert [r["id"] for r in first] == [r["id"] for r in ROWS]
    assert all(r["reason"] == "ok" and r["nl_vi"] for r in first)
    assert len(list(tmp_path.glob("chunk_*.jsonl"))) == 3  # 5 rows / chunk_size 2
    calls_after_first = len(_Handler.calls)

    again = nb.translate_rows(ROWS, chat, tmp_path, chunk_size=2, max_workers=4, progress=lambda _: None)
    assert again == first
    assert len(_Handler.calls) == calls_after_first  # nothing re-requested: resumed from chunk files


def test_request_disables_qwen_thinking(server_url, tmp_path):
    nb.translate_rows(ROWS[:1], nb._openai_chat(server_url, "k", "m"), tmp_path, progress=lambda _: None)
    assert _Handler.calls[0]["chat_template_kwargs"] == {"enable_thinking": False}


def test_translator_server_reuses_running_server(server_url):
    with nb.translator_server(server_url, "k", "m", autostart=False, script_path="unused.sh"):
        pass  # returns without launching anything


def test_translator_server_down_without_autostart_raises():
    with pytest.raises(RuntimeError, match="no translation server"):
        with nb.translator_server("http://127.0.0.1:1/v1", "k", "m", autostart=False, script_path="unused.sh"):
            pass


def test_split_by_command_group_has_no_leak():
    rows = [{"id": f"{i}-{v}", "nl_vi": v, "bash": f"cmd{i} arg"} for i in range(40) for v in ("a", "b")]
    train, val, test = nb.split_by_command_group(rows, seed=1)
    cmds = lambda rs: {nb.normalize_cmd(r["bash"]) for r in rs}
    assert not (cmds(train) & cmds(val)) and not (cmds(train) & cmds(test)) and not (cmds(val) & cmds(test))
    assert len(train) + len(val) + len(test) == len(rows)
    assert val and test


def test_data_nl2bash_step_translates_end_to_end(server_url, tmp_path, monkeypatch):
    cmds = ["ls -la", "wc -l a.txt", "cat a.txt", "sort b.txt", "head -n 3 c.txt", "tail -n 2 c.txt",
            "rm -f old.tmp", "mkdir out", "touch new.txt", "cp a.txt b.txt", "mv a.txt z.txt", "grep foo a.txt"]

    def fake_download(dest):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "all.nl").write_text("\n".join(f"english request {i}" for i in range(len(cmds))), encoding="utf-8")
        (dest / "all.cm").write_text("\n".join(cmds), encoding="utf-8")
        return dest / "all.nl", dest / "all.cm"

    monkeypatch.setattr(nb, "download_nl2bash", fake_download)
    cfg = cli.load_config(None, [
        f"paths.root={tmp_path.as_posix()}", "run_name=t",
        "data.nl2bash_dir=does/not/exist", "models.sft_v1_dir=does/not/exist",
        f"data.translator.server_url={server_url}", "data.translator.autostart=false",
        "data.translator.chunk_size=5", "data.translator.max_workers=4",
    ])
    paths = cli.make_paths(cfg)
    cli.step_data_nl2bash(cfg, paths)

    stats = json.loads((paths.results / "data_stats.json").read_text(encoding="utf-8"))
    assert stats["n_raw"] == len(cmds) and stats["n_translated_ok"] == len(cmds)
    assert stats["n_test_rows"] >= 1 and stats["n_train_rows"] > 0
    assert (paths.data / "sft_nl2bash.jsonl").read_text(encoding="utf-8").strip()
    assert (paths.data / "sft_nl2bash_val.jsonl").exists() and (paths.data / "nl2bash_test.jsonl").exists()
