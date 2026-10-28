"""The Glimmer (self-hosted, OpenAI-compatible) path against a local stub server."""

import json
import threading
from datetime import UTC, date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from cairnlab.llm.client import Client, extract_json
from cairnlab.synth.entry import write_entry
from tests.test_gather import ROOT, day  # noqa: F401  (synthetic day fixture)


class Stub:
    """Behaves like llama.cpp / Ollama serving muse-glimmer."""

    def __init__(self):
        self.requests: list[dict] = []
        self.reply = '{"x": "y"}'
        self.reject_images = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def _handler(self):
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.requests.append(body)
                has_image = any(
                    isinstance(m.get("content"), list)
                    and any(c.get("type") == "image_url" for c in m["content"])
                    for m in body["messages"]
                )
                if stub.reject_images and has_image:
                    # What llama-server (b10353+, no --mmproj) actually returns: a 500.
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(
                        b'{"error": {"code": 500, "message": "image input is not supported -'
                        b' hint: if this is unexpected, you may need to provide the mmproj",'
                        b' "type": "server_error"}}'
                    )
                    return
                reply = stub.reply(body) if callable(stub.reply) else stub.reply
                out = {
                    "id": "x",
                    "object": "chat.completion",
                    "created": 0,
                    "model": body["model"],
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": reply},
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
                }
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return H


@pytest.fixture
def stub(monkeypatch):
    s = Stub()
    monkeypatch.setenv("CAIRN_GLIMMER_BASE_URL", s.url)
    yield s
    s.server.shutdown()


SCHEMA = {
    "type": "object",
    "properties": {"x": {"type": "string"}},
    "required": ["x"],
    "additionalProperties": False,
}


@pytest.mark.parametrize(
    "text",
    [
        '{"x": "y"}',
        '<think>The user wants JSON. Let me think.</think>\n{"x": "y"}',
        'Here it is:\n```json\n{"x": "y"}\n```',
        'Sure. {"x": "y"} Hope that helps!',
    ],
)
def test_extract_json(text):
    assert extract_json(text) == {"x": "y"}


def test_structured_against_glimmer_server(stub, tmp_path):
    stub.reply = '<think>reasoning</think>{"x": "hello"}'
    c = Client(home=tmp_path, backend="glimmer")
    out = c.structured(
        purpose="t",
        messages=[{"role": "user", "content": "q"}],
        schema_name="T",
        schema=SCHEMA,
        temperature=0,
    )
    assert out == {"x": "hello"}
    req = stub.requests[0]
    assert req["model"] == "muse-glimmer" and req["temperature"] == 1.0
    assert req["top_p"] == 0.95 and req["top_k"] == 64
    assert req["response_format"]["type"] == "json_schema"
    assert "reasoning_effort" not in req
    c.structured(purpose="t", messages=[], schema_name="T", schema=SCHEMA, reasoning_effort="low")
    assert stub.requests[1]["chat_template_kwargs"] == {"reasoning_strength": "low"}
    rec = json.loads((tmp_path / "usage.jsonl").read_text().splitlines()[0])
    assert rec["backend"] == "glimmer" and rec["cost_usd"] == 0
    assert rec["tokens_per_s"] > 0


def test_entry_with_glimmer_falls_back_to_text_when_images_are_rejected(stub, day, tmp_path):  # noqa: F811
    import re

    stub.reject_images = True

    def reply(body):
        text = body["messages"][1]["content"]
        text = text if isinstance(text, str) else text[0]["text"]
        rid = re.findall(r"\[(R:[0-9A-Z]{26})\]", text)[0]
        return json.dumps(
            {
                "title": "Fits",
                "goal": "Not recorded.",
                "phases": [
                    {
                        "heading": "Fitting",
                        "narrative": f"fit.py ran [{rid}].",
                        "claims": [{"text": "The fit ran.", "evidence": [rid]}],
                    }
                ],
                "results": [],
                "decisions": [],
                "drift": [],
                "open_questions": [],
                "next_steps": [],
            }
        )

    stub.reply = reply
    home = tmp_path / "store"
    home.mkdir()
    (home / "objects").symlink_to(day.objects)
    client = Client(home=home, backend="glimmer")
    res = write_entry(
        day.conn,
        home,
        date(2026, 10, 28),
        client=client,
        tz=UTC,
        root=Path(ROOT),
        backend="glimmer",
    )
    assert res.markdown.startswith("# Fits")
    assert len(stub.requests) == 2  # with the figure, rejected; then text only
    retry = stub.requests[1]["messages"][1]["content"]
    assert isinstance(retry, str) and "figures could not be shown" in retry
    assert res.model == "muse-glimmer"


def test_cli_queue_with_glimmer(stub, tmp_path, monkeypatch):
    """cairn entry --queue --backend glimmer writes every day with runs but no entry."""
    from typer.testing import CliRunner

    from cairnlab.cli import app

    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    runner = CliRunner()
    runner.invoke(app, ["init"])
    cfg = proj / ".cairn" / "config.toml"
    cfg.write_text(cfg.read_text().replace('# timezone = "Asia/Kolkata"', 'timezone = "UTC"'))
    from datetime import datetime, timedelta

    lines = []
    for i, d in enumerate([datetime.now(UTC) - timedelta(days=2), datetime.now(UTC)]):
        ts = d.strftime("%Y-%m-%dT10:00:00.000Z")
        lines.append(
            json.dumps(
                {
                    "v": 1,
                    "type": "cmd",
                    "id": f"S-{i}",
                    "session": "S",
                    "host": "h",
                    "shell": "bash",
                    "user": "t",
                    "cwd": "/p",
                    "cmd": f"python step{i}.py",
                    "ts_start": ts,
                    "ts_end": ts,
                    "exit": 0,
                }
            )
        )
    (proj / ".cairn" / "events" / "h.jsonl").write_text("\n".join(lines) + "\n")
    runner.invoke(app, ["ingest"])
    stub.reply = json.dumps(
        {
            "title": "T",
            "goal": "Not recorded.",
            "phases": [],
            "results": [],
            "decisions": [],
            "drift": [],
            "open_questions": [],
            "next_steps": [],
        }
    )
    res = runner.invoke(app, ["entry", "--queue", "--backend", "glimmer", "--no-check"])
    assert res.exit_code == 0, res.output
    assert res.output.count("Entry written") == 2
    again = runner.invoke(app, ["entry", "--queue", "--backend", "glimmer"])
    assert "already has an entry" in again.output
    status = runner.invoke(app, ["status"]).output
    assert "muse-glimmer" in status and "tokens/s" in status


def test_glimmer_url_from_user_env_file_and_empty_means_default(tmp_path, monkeypatch):
    from cairnlab.llm.client import GLIMMER_DEFAULT_URL

    monkeypatch.delenv("CAIRN_GLIMMER_BASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    env = Path.home() / ".config" / "cairn" / "env"
    env.parent.mkdir(parents=True)
    env.write_text("CAIRN_GLIMMER_BASE_URL=\n")
    assert str(Client(backend="glimmer")._oai.base_url).rstrip("/") == GLIMMER_DEFAULT_URL
    monkeypatch.delenv("CAIRN_GLIMMER_BASE_URL", raising=False)
    env.write_text("CAIRN_GLIMMER_BASE_URL=http://127.0.0.1:9999/v1\n")
    assert str(Client(backend="glimmer")._oai.base_url).rstrip("/") == "http://127.0.0.1:9999/v1"
    monkeypatch.delenv("CAIRN_GLIMMER_BASE_URL", raising=False)
