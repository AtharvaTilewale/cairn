"""Tests for JSONL -> events/runs ingest."""

import json
import shutil
from datetime import datetime
from pathlib import Path

import pytest
from ulid import ULID

from cairnlab.capture.ingest import ingest, looks_secret
from cairnlab.store import db

FIXTURE = Path(__file__).parent / "fixtures" / "events" / "synth-2026-10-28.jsonl"
SESSION = "01JSYNTHSESSION00000000000"


def cmd_event(seq: int, cmd: str = "true", **overrides) -> dict:
    ev = {
        "v": 1,
        "type": "cmd",
        "id": f"{SESSION}-{seq}",
        "session": SESSION,
        "host": "synth",
        "shell": "bash",
        "user": "tester",
        "cwd": "/data/proj",
        "cmd": cmd,
        "ts_start": f"2026-10-28T10:00:{seq:02d}.000Z",
        "ts_end": f"2026-10-28T10:00:{seq:02d}.500Z",
        "exit": 0,
    }
    ev.update(overrides)
    return ev


def append(path: Path, *lines: str | dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for line in lines:
            fh.write((json.dumps(line) if isinstance(line, dict) else line) + "\n")


@pytest.fixture
def store(tmp_path):
    events = tmp_path / "events"
    events.mkdir()
    conn = db.connect(tmp_path / "cairn.db")
    db.migrate(conn)
    yield conn, events
    conn.close()


def runs(conn):
    return conn.execute("SELECT * FROM runs ORDER BY ts_start").fetchall()


def test_fixture_ingest_skips_malformed(store, caplog):
    conn, events = store
    shutil.copy(FIXTURE, events)
    stats = ingest(conn, events)
    assert stats.runs == 3
    assert stats.malformed == 4  # not json, missing user, array, bad timestamp
    assert stats.unsupported == 1  # the note event
    assert stats.duplicates == 1  # the repeated -1 line
    assert [r["cmd"] for r in runs(conn)] == [
        "python gen.py --n 100",
        "python fit.py --cutoff 1.2",
        'echo "héllo\tdone" 🎉',
    ]
    assert [r["exit"] for r in runs(conn)] == [0, 1, 0]
    assert "line 2" in caplog.text  # malformed lines are reported with their position


def test_reingest_adds_nothing(store):
    conn, events = store
    shutil.copy(FIXTURE, events)
    ingest(conn, events)
    again = ingest(conn, events)
    assert (again.runs, again.lines) == (0, 0)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 3


def test_only_new_lines_are_read(store):
    conn, events = store
    f = events / "synth-2026-10-28.jsonl"
    append(f, cmd_event(1), cmd_event(2))
    assert ingest(conn, events).runs == 2
    append(f, cmd_event(3))
    stats = ingest(conn, events)
    assert (stats.lines, stats.runs) == (1, 1)


def test_partial_last_line_waits_for_newline(store):
    conn, events = store
    f = events / "synth-2026-10-28.jsonl"
    append(f, cmd_event(1))
    half = json.dumps(cmd_event(2))
    with f.open("a") as fh:
        fh.write(half[:40])  # a writer is mid-line
    assert ingest(conn, events).runs == 1
    with f.open("a") as fh:
        fh.write(half[40:] + "\n")
    assert ingest(conn, events).runs == 1
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2


def test_duplicate_id_across_files_is_ignored(store):
    conn, events = store
    append(events / "a.jsonl", cmd_event(1))
    append(events / "b.jsonl", cmd_event(1))
    stats = ingest(conn, events)
    assert (stats.runs, stats.duplicates) == (1, 1)


def test_truncated_file_is_reread_without_duplicates(store):
    conn, events = store
    f = events / "x.jsonl"
    append(f, cmd_event(1), cmd_event(2))
    ingest(conn, events)
    f.write_text("")
    append(f, cmd_event(3))
    stats = ingest(conn, events)
    assert stats.runs == 1
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 3


def test_run_and_event_rows(store):
    conn, events = store
    append(events / "x.jsonl", cmd_event(7, "python fit.py", exit=3, extra_field="kept"))
    ingest(conn, events)
    run = runs(conn)[0]
    event = conn.execute("SELECT * FROM events").fetchone()
    assert run["id"] == event["id"]
    # The run id is a ULID whose time part is the command's start.
    ulid = ULID.from_str(run["id"])
    assert ulid.datetime == datetime.fromisoformat("2026-10-28T10:00:07.000Z")
    assert (run["session"], run["host"], run["shell"], run["cwd"]) == (
        SESSION,
        "synth",
        "bash",
        "/data/proj",
    )
    assert (run["ts_start"], run["ts_end"], run["exit"]) == (
        "2026-10-28T10:00:07.000Z",
        "2026-10-28T10:00:07.500Z",
        3,
    )
    assert (event["type"], event["ts"], event["host"]) == ("cmd", run["ts_start"], "synth")
    payload = json.loads(event["payload"])
    assert payload["id"] == f"{SESSION}-7"  # the hook's own id is kept
    assert payload["user"] == "tester"
    assert payload["extra_field"] == "kept"


def test_ingest_redacts_secrets_the_hook_missed(store):
    conn, events = store
    append(events / "x.jsonl", cmd_event(1, "export OPENAI_API_KEY=sk-live-123"))
    ingest(conn, events)
    assert runs(conn)[0]["cmd"] == "[redacted]"
    assert "sk-live-123" not in conn.execute("SELECT payload FROM events").fetchone()[0]


def test_invalid_utf8_is_replaced_not_fatal(store):
    conn, events = store
    line = json.dumps(cmd_event(1, "echo X")).encode().replace(b"X", b"\xff\xfe")
    (events / "x.jsonl").write_bytes(line + b"\n")
    assert ingest(conn, events).runs == 1


def test_missing_events_dir(tmp_path):
    conn = db.connect(tmp_path / "cairn.db")
    db.migrate(conn)
    assert ingest(conn, tmp_path / "nope").runs == 0


@pytest.mark.parametrize(
    "cmd, secret",
    [
        ("export MODEL_API_KEY=x", True),
        ("curl -H 'token: x'", True),
        ("echo $PASSWORD", True),
        ("python fit.py", False),
        ("ssh-keygen -t ed25519", False),
    ],
)
def test_looks_secret(cmd, secret):
    assert looks_secret(cmd) is secret


# --- file events --------------------------------------------------------------------------

SHA_A = "a" * 64
SHA_B = "b" * 64


def file_event(n: int, path: str = "/data/proj/runs/rmsd.xvg", op: str = "modified", **kw):
    ev = {
        "v": 1,
        "type": "file",
        "id": f"01JFILEEVENT{n:014d}",
        "host": "synth",
        "user": "tester",
        "path": path,
        "op": op,
        "sha256": SHA_A,
        "size": 10,
        "mtime": f"2026-10-28T10:01:{n:02d}.000Z",
        "fp": "full",
        "stored": True,
        "ts": f"2026-10-28T10:01:{n:02d}.100Z",
    }
    ev.update(kw)
    return ev


def test_file_events_become_artifacts_and_versions(store):
    conn, events = store
    append(
        events / "synth-2026-10-28-watch.jsonl",
        file_event(1, op="created"),
        file_event(2),  # same content rewritten
        file_event(3, sha256=SHA_B, size=12),
        file_event(4, path="/data/proj/fig.png", op="created"),
        file_event(5, op="deleted", sha256=None, size=None, mtime=None, fp=None),
    )
    stats = ingest(conn, events)
    assert stats.files_seen == 5 and stats.malformed == 0
    arts = conn.execute("SELECT host, path, kind FROM artifacts ORDER BY path").fetchall()
    assert [tuple(a) for a in arts] == [
        ("synth", "/data/proj/fig.png", "figure"),
        ("synth", "/data/proj/runs/rmsd.xvg", "data"),
    ]
    versions = conn.execute(
        "SELECT v.sha256, v.size, v.ts, v.fp FROM artifact_versions v JOIN artifacts a"
        " ON a.id = v.artifact_id WHERE a.path LIKE '%rmsd.xvg' ORDER BY v.ts"
    ).fetchall()
    # Identical content is one version, first seen at its first event.
    assert [tuple(v) for v in versions] == [
        (SHA_A, 10, "2026-10-28T10:01:01.100Z", "full"),
        (SHA_B, 12, "2026-10-28T10:01:03.100Z", "full"),
    ]
    changes = conn.execute("SELECT op, version_id IS NULL FROM file_events ORDER BY ts").fetchall()
    assert [tuple(c) for c in changes] == [
        ("created", 0),
        ("modified", 0),
        ("modified", 0),
        ("created", 0),
        ("deleted", 1),
    ]


def test_same_path_on_two_hosts_is_two_artifacts(store):
    conn, events = store
    append(events / "x.jsonl", file_event(1), file_event(2, host="workstation"))
    ingest(conn, events)
    assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 2


def test_stored_flag_follows_local_object_store(store, tmp_path):
    conn, events = store
    objects = tmp_path / "objects"
    (objects / SHA_A[:2]).mkdir(parents=True)
    (objects / SHA_A[:2] / SHA_A).write_text("x")
    append(events / "x.jsonl", file_event(1), file_event(2, sha256=SHA_B, path="/d/other.csv"))
    ingest(conn, events, objects_dir=objects)
    stored = dict(conn.execute("SELECT sha256, stored FROM artifact_versions").fetchall())
    assert stored == {SHA_A: 1, SHA_B: 0}


def test_bad_file_events_are_skipped(store):
    conn, events = store
    append(
        events / "x.jsonl",
        file_event(1, sha256=None),  # a change without a hash
        file_event(2, op="exploded"),
        file_event(3, user=""),
        file_event(4),
    )
    stats = ingest(conn, events)
    assert (stats.malformed, stats.files_seen) == (3, 1)


def test_file_event_reingest_is_idempotent(store):
    conn, events = store
    append(events / "x.jsonl", file_event(1), file_event(2))
    ingest(conn, events)
    (events / "copy.jsonl").write_text((events / "x.jsonl").read_text())
    stats = ingest(conn, events)
    assert stats.duplicates == 2
    assert conn.execute("SELECT COUNT(*) FROM file_events").fetchone()[0] == 2


def test_run_rows_keep_user(store):
    conn, events = store
    append(events / "x.jsonl", cmd_event(1))
    ingest(conn, events)
    assert runs(conn)[0]["user"] == "tester"


@pytest.mark.parametrize(
    "path, kind",
    [
        ("/p/fig.png", "figure"),
        ("/p/plot.SVG", "figure"),
        ("/p/md.mdp", "config"),
        ("/p/params_v2.txt", "config"),
        ("/p/config.yaml", "config"),
        ("/p/fit.py", "script"),
        ("/p/run.sh", "script"),
        ("/p/md.log", "log"),
        ("/p/rmsd.xvg", "data"),
        ("/p/traj.xtc", "data"),
        ("/p/README", "other"),
    ],
)
def test_artifact_kind(path, kind):
    from cairnlab.capture.ingest import artifact_kind

    assert artifact_kind(path) == kind
