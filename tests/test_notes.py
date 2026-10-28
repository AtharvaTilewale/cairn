"""Tests for typed notes: writing, ingesting and attaching to the run they explain."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cairnlab.capture.ingest import ingest
from cairnlab.cli import app
from cairnlab.store import db

runner = CliRunner()


def cmd(i: int, start: str, session: str = "S", host: str = "hp") -> dict:
    return {
        "v": 1,
        "type": "cmd",
        "id": f"{session}-{i}",
        "session": session,
        "host": host,
        "shell": "bash",
        "user": "t",
        "cwd": "/p",
        "cmd": f"python step{i}.py",
        "ts_start": start,
        "ts_end": start,
        "exit": 0,
    }


def note(i: int, ts: str, text: str, session: str | None = "S", **kw) -> dict:
    return {
        "v": 1,
        "type": "note",
        "id": f"01ARZ3NDEKTSV4RRFFQ69G5F{i:02d}",
        "session": session,
        "host": "hp",
        "user": "t",
        "ts": ts,
        "text": text,
        "source": "typed",
        **kw,
    }


def load(tmp_path, files: dict[str, list[dict]]):
    events = tmp_path / "events"
    events.mkdir(exist_ok=True)
    for name, lines in files.items():
        (events / name).write_text("".join(json.dumps(x) + "\n" for x in lines))
    conn = db.connect(tmp_path / "cairn.db")
    db.migrate(conn)
    ingest(conn, events)
    return conn


def attached(conn) -> dict[str, str | None]:
    rows = conn.execute(
        "SELECT n.text, r.cmd FROM notes n LEFT JOIN runs r ON r.id = n.run_id ORDER BY n.ts"
    )
    return {text: cmd for text, cmd in rows}


def test_note_attaches_to_last_run_of_its_session(tmp_path):
    conn = load(
        tmp_path,
        {
            "hp-2026-10-28.jsonl": [
                cmd(1, "2026-10-28T10:00:00.000Z"),
                cmd(2, "2026-10-28T10:05:00.000Z"),
                cmd(3, "2026-10-28T10:06:00.000Z", session="OTHER"),  # another terminal
                cmd(4, "2026-10-28T10:30:00.000Z"),
            ],
            # Sorts before the run file, so runs are not ingested yet when notes are read.
            "hp-2026-10-28-note.jsonl": [
                note(1, "2026-10-28T10:07:00.000Z", "cutoff too small"),
                note(2, "2026-10-28T09:00:00.000Z", "before anything"),
            ],
        },
    )
    got = attached(conn)
    assert got["cutoff too small"] == "python step2.py"
    assert got["before anything"] is None
    row = conn.execute("SELECT source, ts FROM notes WHERE text = 'cutoff too small'").fetchone()
    assert tuple(row) == ("typed", "2026-10-28T10:07:00.000Z")


def test_note_without_session_uses_last_run_on_host(tmp_path):
    conn = load(
        tmp_path,
        {
            "e.jsonl": [
                cmd(1, "2026-10-28T10:00:00.000Z", session="A"),
                cmd(2, "2026-10-28T10:05:00.000Z", session="B"),
                note(1, "2026-10-28T10:06:00.000Z", "general thought", session=None),
            ]
        },
    )
    assert attached(conn)["general thought"] == "python step2.py"


def test_explicit_run_wins(tmp_path):
    from ulid import ULID

    conn = load(
        tmp_path,
        {
            "e.jsonl": [
                cmd(1, "2026-10-28T10:00:00.000Z"),
                cmd(2, "2026-10-28T10:05:00.000Z"),
            ]
        },
    )
    first = conn.execute("SELECT id FROM runs WHERE cmd = 'python step1.py'").fetchone()[0]
    (tmp_path / "events" / "n.jsonl").write_text(
        json.dumps(note(1, "2026-10-28T10:06:00.000Z", "about the first", run=f"R:{first}")) + "\n"
    )
    ingest(conn, tmp_path / "events")
    assert attached(conn)["about the first"] == "python step1.py"
    assert str(ULID.from_str(first))


def test_stale_notes_do_not_attach(tmp_path):
    conn = load(
        tmp_path,
        {
            "e.jsonl": [
                cmd(1, "2026-10-28T01:00:00.000Z"),
                note(1, "2026-10-28T12:00:00.000Z", "much later"),
            ]
        },
    )
    assert attached(conn)["much later"] is None


def test_cli_note_writes_an_event(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    runner.invoke(app, ["init"])
    monkeypatch.setenv("CAIRN_USER", "tester")
    monkeypatch.setenv("CAIRN_HOST", "synth")
    monkeypatch.setenv("CAIRN_SESSION", "01ARZ3NDEKTSV4RRFFQ69G5FAV")
    result = runner.invoke(app, ["note", "switching", "to", "the", "poly", "model"])
    assert result.exit_code == 0, result.output
    (f,) = (proj / ".cairn" / "events").glob("synth-*-note.jsonl")
    ev = json.loads(f.read_text())
    assert ev["type"] == "note" and ev["text"] == "switching to the poly model"
    assert ev["session"] == "01ARZ3NDEKTSV4RRFFQ69G5FAV" and ev["source"] == "typed"
    assert runner.invoke(app, ["ingest"]).exit_code == 0
    assert "switching" in runner.invoke(app, ["status"]).output


def test_note_requires_user(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    runner.invoke(app, ["init"])
    assert runner.invoke(app, ["note", "x"]).exit_code == 1


@pytest.mark.shell
def test_note_in_an_activated_shell_explains_the_previous_command(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    env = {
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "HISTFILE": str(tmp_path / "h"),
        "CAIRN_HOST": "synth",
        "TERM": "dumb",
        "CAIRN_READSET": "0",
    }
    script = "\n".join(
        [
            "cairn init > /dev/null",
            "export CAIRN_USER=tester",
            'eval "$(cairn activate)"',
            "python3 -c 'print(1)'",
            'cairn note "tau looks too long; try a larger cutoff"',
            "echo later",
            "cairn ingest > /dev/null",
            "exit",
        ]
    )
    subprocess.run(
        ["bash", "--noprofile", "--norc", "--noediting", "-i"],
        input=script,
        cwd=proj,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    conn = db.connect(proj / ".cairn" / "cairn.db")
    assert attached(conn) == {"tau looks too long; try a larger cutoff": "python3 -c 'print(1)'"}


def test_note_skips_a_background_job_that_is_still_running(tmp_path):
    """Held-out run 1: the --bg monitor recorded its start 19 ms after the next command."""
    from ulid import ULID

    rid = str(ULID())
    report = cmd(1, "2026-10-28T15:14:44.327Z")
    report["ts_end"] = "2026-10-28T15:14:44.338Z"
    start = {
        "v": 1,
        "type": "run_start",
        "id": f"{rid}-start",
        "run_id": rid,
        "pid": 1,
        "monitor_pid": 1,
        "session": "S",
        "host": "hp",
        "user": "t",
        "cwd": "/p",
        "cmd": "python align_sim.py",
        "ts_start": "2026-10-28T15:14:44.346Z",
    }
    end = {
        "v": 1,
        "type": "run_end",
        "id": f"{rid}-end",
        "run_id": rid,
        "host": "hp",
        "user": "t",
        "exit": 0,
        "ts_end": "2026-10-28T15:14:46.167Z",
        "wrapper": {},
    }
    conn = load(
        tmp_path,
        {"e.jsonl": [report, start, end, note(1, "2026-10-28T15:14:44.759Z", "keeping k=7")]},
    )
    assert attached(conn)["keeping k=7"] == "python step1.py"


def test_note_falls_back_to_a_running_job_when_nothing_finished(tmp_path):
    from ulid import ULID

    rid = str(ULID())
    start = {
        "v": 1,
        "type": "run_start",
        "id": f"{rid}-start",
        "run_id": rid,
        "pid": 1,
        "monitor_pid": 1,
        "session": "S",
        "host": "hp",
        "user": "t",
        "cwd": "/p",
        "cmd": "gmx mdrun",
        "ts_start": "2026-10-28T10:00:00.000Z",
    }
    conn = load(tmp_path, {"e.jsonl": [start, note(1, "2026-10-28T10:05:00.000Z", "md running")]})
    assert attached(conn)["md running"] == "gmx mdrun"


def test_note_skips_a_background_job_that_finished_during_the_last_command(tmp_path):
    """synthlab flake: a --bg job ends while the next command runs, just before the note."""
    from ulid import ULID

    rid = str(ULID())
    start = {
        "v": 1,
        "type": "run_start",
        "id": f"{rid}-start",
        "run_id": rid,
        "pid": 1,
        "monitor_pid": 1,
        "session": "S",
        "host": "hp",
        "user": "t",
        "cwd": "/p",
        "cmd": "python simulate.py",
        "ts_start": "2026-10-28T10:00:00.000Z",
    }
    end = {
        "v": 1,
        "type": "run_end",
        "id": f"{rid}-end",
        "run_id": rid,
        "host": "hp",
        "user": "t",
        "exit": 0,
        "ts_end": "2026-10-28T10:00:03.500Z",
        "wrapper": {},
    }
    last = cmd(1, "2026-10-28T10:00:02.000Z")
    last["ts_end"] = "2026-10-28T10:00:03.200Z"
    conn = load(
        tmp_path,
        {"e.jsonl": [start, last, end, note(1, "2026-10-28T10:00:04.000Z", "poly fits better")]},
    )
    assert attached(conn)["poly fits better"] == "python step1.py"
