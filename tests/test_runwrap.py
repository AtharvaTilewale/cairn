"""Tests for `cairn run` (exact before/after outputs) and `cairn run --bg`."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cairnlab.capture.ingest import ingest
from cairnlab.capture.longrun import start_background
from cairnlab.capture.runwrap import WrapContext, run_wrapped
from cairnlab.provenance.linker import link
from cairnlab.store import db

PY = sys.executable


@pytest.fixture
def ctx(tmp_path):
    root = tmp_path / "proj"
    home = root / ".cairn"
    (home / "events").mkdir(parents=True)
    (root / "in.csv").write_text("1,2\n")
    return WrapContext(home=home, roots=[root], host="synth", user="tester", session="SESS")


def events(home: Path) -> list[dict]:
    out = []
    for f in sorted((home / "events").glob("*.jsonl")):
        out += [json.loads(x) for x in f.read_text().splitlines()]
    return out


def load(home: Path):
    conn = db.connect(home / "cairn.db")
    db.migrate(conn)
    ingest(conn, home / "events", home / "objects")
    link(conn)
    return conn


def edge_rows(conn):
    return sorted(
        (role, Path(path).name, method, conf)
        for role, path, method, conf in conn.execute(
            "SELECT e.role, a.path, e.method, e.confidence FROM edges e"
            " JOIN artifact_versions v ON v.id = e.version_id"
            " JOIN artifacts a ON a.id = v.artifact_id"
        )
    )


def test_wrapped_run_records_exact_outputs(ctx):
    root = ctx.roots[0]
    code = "import shutil; shutil.copy('in.csv', 'out.csv'); open('log.txt','w').write('ok')"
    rc = run_wrapped(ctx, [PY, "-c", code], cwd=root)
    assert rc == 0
    evs = events(ctx.home)
    (cmd,) = [e for e in evs if e["type"] == "cmd"]
    assert cmd["shell"] == "cairn-run" and cmd["exit"] == 0 and cmd["user"] == "tester"
    assert cmd["id"].startswith("SESS-w")
    assert sorted(Path(p).name for p in cmd["wrapper"]["generated"]) == ["log.txt", "out.csv"]
    files = [e for e in evs if e["type"] == "file"]
    assert {Path(e["path"]).name for e in files} == {"log.txt", "out.csv"}
    assert all(e["stored"] for e in files)

    conn = load(ctx.home)
    assert edge_rows(conn) == [
        ("generated", "log.txt", "wrapper", 1.0),
        ("generated", "out.csv", "wrapper", 1.0),
    ]


def test_wrapped_run_inputs_come_from_argpath(ctx):
    root = ctx.roots[0]
    (root / "copy.py").write_text("import sys, shutil; shutil.copy(sys.argv[1], sys.argv[2])\n")
    # Fingerprint the inputs first (as the watcher's baseline scan would).
    from cairnlab.capture.watcher import EventWriter, FileTracker, IgnoreRules

    FileTracker(
        IgnoreRules(root), EventWriter(ctx.home / "events", "synth", "tester"), ctx.home / "objects"
    ).scan(root)
    time.sleep(0.01)
    assert run_wrapped(ctx, [PY, "copy.py", "in.csv", "out.csv"], cwd=root) == 0
    conn = load(ctx.home)
    assert ("used", "in.csv", "argpath", 0.9) in edge_rows(conn)
    assert ("used", "copy.py", "argpath", 0.9) in edge_rows(conn)
    assert ("generated", "out.csv", "wrapper", 1.0) in edge_rows(conn)


def test_exit_code_and_deletions(ctx):
    root = ctx.roots[0]
    rc = run_wrapped(ctx, [PY, "-c", "import os, sys; os.remove('in.csv'); sys.exit(3)"], cwd=root)
    assert rc == 3
    evs = events(ctx.home)
    (cmd,) = [e for e in evs if e["type"] == "cmd"]
    assert cmd["exit"] == 3
    assert [Path(p).name for p in cmd["wrapper"]["deleted"]] == ["in.csv"]
    assert any(e["type"] == "file" and e["op"] == "deleted" for e in evs)


def test_single_string_runs_through_bash(ctx):
    root = ctx.roots[0]
    assert run_wrapped(ctx, ["echo hi > greeting.txt && test -s greeting.txt"], cwd=root) == 0
    assert (root / "greeting.txt").read_text() == "hi\n"


def test_ignored_paths_are_not_outputs(ctx):
    root = ctx.roots[0]
    run_wrapped(
        ctx, [PY, "-c", "open('x.tmp','w').write('1'); open('y.csv','w').write('2')"], cwd=root
    )
    (cmd,) = [e for e in events(ctx.home) if e["type"] == "cmd"]
    assert [Path(p).name for p in cmd["wrapper"]["generated"]] == ["y.csv"]


def wait_for(pred, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return False


def test_background_run(ctx):
    root = ctx.roots[0]
    run_id, log = start_background(
        ctx,
        # Holds its output open while working, as mdrun does with md.log.
        [
            PY,
            "-c",
            "import time; f = open('bg.csv', 'w'); f.write('x'); f.flush();"
            " time.sleep(0.6); f.close(); print('done')",
        ],
        cwd=root,
    )
    assert wait_for(lambda: any(e["type"] == "run_start" for e in events(ctx.home)))
    conn = load(ctx.home)
    row = conn.execute("SELECT status, exit, ts_end FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert tuple(row) == ("running", None, None)

    assert wait_for(lambda: any(e["type"] == "run_end" for e in events(ctx.home)))
    conn = load(ctx.home)
    row = conn.execute("SELECT status, exit, cmd FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert row["status"] == "done" and row["exit"] == 0 and "time.sleep" in row["cmd"]
    assert ("generated", "bg.csv", "wrapper", 1.0) in edge_rows(conn)
    assert "done" in Path(log).read_text()
    end = [e for e in events(ctx.home) if e["type"] == "run_end"][0]
    assert any(p.endswith("/bg.csv") for p in end["wrapper"]["written"])


def test_lost_background_run(ctx):
    """A run_start whose monitor is gone (e.g. after a reboot) is marked lost."""
    from ulid import ULID

    lost_id = str(ULID())
    dead = subprocess.Popen([PY, "-c", "pass"])
    dead.wait()
    start = {
        "v": 1,
        "type": "run_start",
        "id": f"{lost_id}-start",
        "run_id": lost_id,
        "pid": dead.pid,
        "monitor_pid": dead.pid,
        "session": "S",
        "host": "synth",
        "user": "tester",
        "cwd": str(ctx.roots[0]),
        "cmd": "gmx mdrun -deffnm md",
        "ts_start": "2026-10-28T10:00:00.000Z",
    }
    (ctx.home / "events" / "synth-2026-10-28-run.jsonl").write_text(json.dumps(start) + "\n")
    conn = db.connect(ctx.home / "cairn.db")
    db.migrate(conn)
    ingest(conn, ctx.home / "events", lost_check_host="synth")
    row = conn.execute("SELECT status, exit FROM runs").fetchone()
    assert tuple(row) == ("lost", None)


def test_cli_run_passes_exit_code_and_ignores_hook(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    env = {**os.environ, "HOME": str(tmp_path), "CAIRN_USER": "tester", "CAIRN_HOST": "synth"}
    env.pop("CAIRN_HOME", None)
    bin_dir = Path(sys.executable).parent
    subprocess.run([bin_dir / "cairn", "init"], cwd=proj, env=env, check=True, capture_output=True)
    res = subprocess.run(
        [bin_dir / "cairn", "run", "--", PY, "-c", "import sys; print('out'); sys.exit(4)"],
        cwd=proj,
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 4
    assert "out" in res.stdout
