"""Tests for replaying a recorded run in a scratch directory."""

import json
import sys

import pytest

from cairnlab.agent.sandbox import Refused, replay
from cairnlab.provenance.graph import resolve_version, trace
from tests.test_gather import ROOT, Store, T

SCRIPT = b"""import sys
rows = open(sys.argv[1]).read().split()
open(sys.argv[2], "w").write(str(sum(float(r) for r in rows)) + "\\n")
"""


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path)
    s.file("sum.py", T("08:00:00"), SCRIPT, op="scanned")
    s.file("data/in.txt", T("08:00:00"), b"1\n2\n3.5\n", op="scanned")
    s.cmd(f"{sys.executable} sum.py data/in.txt out/total.txt", T("09:00:00"), T("09:00:02"))
    s.file("out/total.txt", T("09:00:01"), b"6.5\n", op="created")
    s.cmd("sleep 3600", T("10:00:00"), T("11:00:00"))
    s.load()
    return s


def run_and_inputs(s, path):
    vid = resolve_version(s.conn, "hp", f"{ROOT}/{path}")
    step = trace(s.conn, vid).steps[-1]
    run = s.conn.execute("SELECT * FROM runs WHERE id = ?", (step.run["id"],)).fetchone()
    return run, step.inputs


def test_output_directories_are_recreated(store, tmp_path):
    run, inputs = run_and_inputs(store, "out/total.txt")
    bare = replay(store.conn, run, inputs, store.objects, scratch_parent=tmp_path)
    assert bare.exit != 0 and "No such file" in bare.stderr_tail  # out/ was never an input
    r = replay(
        store.conn,
        run,
        inputs,
        store.objects,
        scratch_parent=tmp_path,
        output_paths=[f"{ROOT}/out/total.txt"],
    )
    assert r.exit == 0 and r.local(f"{ROOT}/out/total.txt").read_text() == "6.5\n"


def test_replay_with_output_dir(tmp_path):
    s = Store(tmp_path)
    s.file(
        "sum.py",
        T("08:00:00"),
        SCRIPT.replace(
            b"open(sys.argv[2]", b"import os; os.makedirs('out', exist_ok=True); open(sys.argv[2]"
        ),
        op="scanned",
    )
    s.file("data/in.txt", T("08:00:00"), b"1\n2\n3.5\n", op="scanned")
    s.cmd(f"{sys.executable} sum.py data/in.txt out/total.txt", T("09:00:00"), T("09:00:02"))
    s.file("out/total.txt", T("09:00:01"), b"6.5\n", op="created")
    s.load()
    run, inputs = run_and_inputs(s, "out/total.txt")
    r = replay(s.conn, run, inputs, s.objects, scratch_parent=tmp_path)
    assert r.exit == 0 and not r.timed_out
    assert r.produced == [f"{ROOT}/out/total.txt"]
    assert r.local(f"{ROOT}/out/total.txt").read_text() == "6.5\n"
    assert r.scratch.parent == tmp_path


def test_refuses_long_runs(store, tmp_path):
    run = store.conn.execute("SELECT * FROM runs WHERE cmd = 'sleep 3600'").fetchone()
    with pytest.raises(Refused, match="60.0 min"):
        replay(store.conn, run, [], store.objects, scratch_parent=tmp_path)


def test_refuses_unstored_inputs(store, tmp_path):
    run, inputs = run_and_inputs(store, "out/total.txt")
    for f in store.objects.rglob("*"):
        if f.is_file():
            f.unlink()
    with pytest.raises(Refused, match="no stored copy"):
        replay(store.conn, run, inputs, store.objects, scratch_parent=tmp_path)


def test_refuses_redacted_and_unfinished(store, tmp_path):
    run = dict(store.conn.execute("SELECT * FROM runs LIMIT 1").fetchone())
    with pytest.raises(Refused, match="redacted"):
        replay(store.conn, {**run, "cmd": "[redacted]"}, [], store.objects)
    with pytest.raises(Refused, match="not finished"):
        replay(store.conn, {**run, "status": "running", "ts_end": None}, [], store.objects)


def test_timeout_kills_the_process_group(tmp_path):
    s = Store(tmp_path)
    s.cmd("sleep 30 & sleep 30; echo never", T("09:00:00"), T("09:00:05"))
    s.load()
    run = s.conn.execute("SELECT * FROM runs").fetchone()
    r = replay(s.conn, run, [], s.objects, timeout_s=1, scratch_parent=tmp_path)
    assert r.timed_out and r.exit is None and r.seconds < 10


def test_absolute_paths_are_redirected_into_scratch(tmp_path):
    s = Store(tmp_path)
    s.file("in.txt", T("08:00:00"), b"x\n", op="scanned")
    s.cmd(f"cp {ROOT}/in.txt {ROOT}/copy.txt", T("09:00:00"), T("09:00:01"))
    s.load()
    run = s.conn.execute("SELECT * FROM runs").fetchone()
    vid = resolve_version(s.conn, "hp", f"{ROOT}/in.txt")
    r = replay(s.conn, run, [vid], s.objects, scratch_parent=tmp_path)
    assert r.exit == 0 and r.produced == [f"{ROOT}/copy.txt"]
    assert r.local(f"{ROOT}/copy.txt").read_text() == "x\n"


def test_api_key_is_not_passed_to_the_command(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-should-not-leak")
    s = Store(tmp_path)
    s.cmd("env > env.txt", T("09:00:00"), T("09:00:01"))
    s.load()
    run = s.conn.execute("SELECT * FROM runs").fetchone()
    r = replay(s.conn, run, [], s.objects, scratch_parent=tmp_path)
    assert "secret-should-not-leak" not in r.local(f"{ROOT}/env.txt").read_text()
    assert json.dumps(r.produced)
