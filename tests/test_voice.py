"""Tests for voice notes: transcription client (mocked HTTP), WAV, toggle, CLI. No mic."""

import io
import json
import os
import subprocess
import sys
import threading
import wave

import httpx2 as httpx
import pytest
from typer.testing import CliRunner

from cairnlab.llm.client import LLMError
from cairnlab.voice.record import Toggle, to_wav
from cairnlab.voice.transcribe import transcribe_wav


def test_wav_container():
    with wave.open(io.BytesIO(to_wav(b"\x00\x01" * 16000))) as w:
        got = (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes())
    assert got == (
        1,
        2,
        16000,
        16000,
    )


def mock(handler):
    return httpx.MockTransport(handler)


def test_transcribe_request_shape_and_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "test-key-not-real")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        body = request.read().decode("latin-1")
        seen["body"] = body
        return httpx.Response(
            200,
            json={
                "sessionId": "s",
                "transcript": " cutoff too small ",
                "audioDurationMs": 3600,
                "turns": [],
            },
        )

    text = transcribe_wav(b"RIFFfake", home=tmp_path, transport=mock(handler))
    assert text == "cutoff too small"
    assert seen["url"] == "https://api.meta.ai/v1/asr/transcribe"
    assert seen["auth"] == "Bearer test-key-not-real"
    assert 'name="request"' in seen["body"] and '"audioEncoding": "WAV"' in seen["body"]
    assert 'name="audio"' in seen["body"] and "RIFFfake" in seen["body"]
    rec = json.loads((tmp_path / "usage.jsonl").read_text())
    assert rec["purpose"] == "voice_note" and rec["audio_seconds"] == 3.6
    assert rec["cost_usd"] == pytest.approx(3.6 / 3600 * 0.18)
    assert "test-key" not in (tmp_path / "usage.jsonl").read_text()


def test_transcribe_retries_5xx_but_not_4xx(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "k")
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={})
        return httpx.Response(200, json={"transcript": "ok", "audioDurationMs": 1})

    assert transcribe_wav(b"x", transport=mock(flaky), sleep=lambda s: None) == "ok"
    assert calls["n"] == 3

    def bad(request):
        return httpx.Response(400, json={"error": {"message": "Malformed request"}})

    with pytest.raises(LLMError, match="Malformed request"):
        transcribe_wav(b"x", transport=mock(bad), sleep=lambda s: None)


def test_toggle_stops_another_recorder(tmp_path):
    pid_file = tmp_path / "voice.pid"
    # A stand-in recorder process whose command line contains "cairn".
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, sys, time\n"
            "signal.signal(signal.SIGUSR1, lambda *a: sys.exit(7))\n"
            "print('ready', flush=True)\n"
            "time.sleep(30)\n",
            "cairn-recorder",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "ready"  # handler installed
        pid_file.write_text(str(proc.pid))
        t = Toggle(pid_file)
        assert t.running_pid() == proc.pid
        assert t.stop_other() is True
        assert proc.wait(timeout=10) == 7
    finally:
        proc.kill()
    assert Toggle(pid_file).running_pid() is None  # dead PID: stale file cleaned up
    assert not pid_file.exists()


def test_claim_and_release(tmp_path):
    stop = threading.Event()
    t = Toggle(tmp_path / "state" / "voice.pid")
    t.claim(stop)
    assert (tmp_path / "state" / "voice.pid").read_text() == str(os.getpid())
    os.kill(os.getpid(), __import__("signal").SIGUSR1)
    assert stop.wait(2)
    t.release()
    assert not (tmp_path / "state" / "voice.pid").exists()
    import signal

    for sig in (signal.SIGUSR1, signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, signal.SIG_DFL if sig != signal.SIGINT else signal.default_int_handler)


def test_cli_voice_note(tmp_path, monkeypatch):
    from cairnlab import cli

    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    runner = CliRunner()
    runner.invoke(cli.app, ["init"])
    monkeypatch.setenv("CAIRN_USER", "tester")
    monkeypatch.setenv("CAIRN_HOST", "synth")
    monkeypatch.setattr(cli, "record_audio", lambda stop, max_seconds: b"\x00\x00" * 16000)
    monkeypatch.setattr(cli, "transcribe_wav", lambda wav, home: "tau looks too long")
    res = runner.invoke(cli.app, ["note"])
    assert res.exit_code == 0, res.output
    (f,) = (proj / ".cairn" / "events").glob("synth-*-note.jsonl")
    ev = json.loads(f.read_text())
    assert ev["source"] == "voice" and ev["text"] == "tau looks too long"
    assert (proj / ".cairn" / "notes" / f"{ev['id']}.wav").is_file()
    assert ev["audio"] == f"notes/{ev['id']}.wav"


def test_cli_voice_note_keeps_audio_when_transcription_fails(tmp_path, monkeypatch):
    from cairnlab import cli

    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    runner = CliRunner()
    runner.invoke(cli.app, ["init"])
    monkeypatch.setenv("CAIRN_USER", "tester")
    monkeypatch.setattr(cli, "record_audio", lambda stop, max_seconds: b"\x00\x00" * 16000)

    def fail(wav, home):
        raise LLMError("network down")

    monkeypatch.setattr(cli, "transcribe_wav", fail)
    res = runner.invoke(cli.app, ["note", "--voice"])
    assert res.exit_code == 1 and "network down" in res.output
    (ev_file,) = (proj / ".cairn" / "events").glob("*-note.jsonl")
    ev = json.loads(ev_file.read_text())
    assert ev["text"] == "" and ev.get("untranscribed") is True
    assert (proj / ".cairn" / ev["audio"]).is_file()
