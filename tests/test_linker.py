"""Tests for provenance linking: argpath, window, wrapper and overlap rules."""

import json
from pathlib import Path

import pytest

from cairnlab.capture.ingest import ingest
from cairnlab.provenance.linker import command_paths, link
from cairnlab.store import db

ROOT = "/data/proj"
_seq = {"n": 0}


def ts(sec: float) -> str:
    whole = int(sec)
    ms = int(round((sec - whole) * 1000))
    return f"2026-10-28T10:{whole // 60:02d}:{whole % 60:02d}.{ms:03d}Z"


def cmd(cmd: str, start: float, end: float, cwd: str = ROOT, **kw) -> dict:
    _seq["n"] += 1
    return {
        "v": 1,
        "type": "cmd",
        "id": f"S-{_seq['n']}",
        "session": "S",
        "host": "hp",
        "shell": "bash",
        "user": "t",
        "cwd": cwd,
        "cmd": cmd,
        "ts_start": ts(start),
        "ts_end": ts(end),
        "exit": 0,
        **kw,
    }


def fev(path: str, at: float, sha: str, op: str = "modified", host: str = "hp") -> dict:
    _seq["n"] += 1
    return {
        "v": 1,
        "type": "file",
        "id": f"F{_seq['n']:025d}",
        "host": host,
        "user": "t",
        "path": f"{ROOT}/{path}",
        "op": op,
        "sha256": sha * 64 if len(sha) == 1 else sha,
        "size": 1,
        "mtime": ts(at),
        "fp": "full",
        "stored": True,
        "ts": ts(at),
    }


@pytest.fixture
def store(tmp_path):
    conn = db.connect(tmp_path / "cairn.db")
    db.migrate(conn)
    events = tmp_path / "events"
    events.mkdir()

    def load(*evs: dict):
        with (events / "e.jsonl").open("a") as fh:
            for e in evs:
                fh.write(json.dumps(e) + "\n")
        ingest(conn, events)
        link(conn)

    return conn, load


def edges(conn, cmd_text: str) -> set[tuple[str, str, str, str]]:
    rows = conn.execute(
        "SELECT e.role, a.path, v.sha256, e.method FROM edges e"
        " JOIN runs r ON r.id = e.run_id JOIN artifact_versions v ON v.id = e.version_id"
        " JOIN artifacts a ON a.id = v.artifact_id WHERE r.cmd = ?",
        (cmd_text,),
    )
    return {(role, path.removeprefix(ROOT + "/"), sha[0], m) for role, path, sha, m in rows}


def conf(conn, cmd_text: str, path: str, role: str) -> float:
    return conn.execute(
        "SELECT e.confidence FROM edges e JOIN runs r ON r.id = e.run_id"
        " JOIN artifact_versions v ON v.id = e.version_id JOIN artifacts a"
        " ON a.id = v.artifact_id WHERE r.cmd = ? AND a.path = ? AND e.role = ?",
        (cmd_text, f"{ROOT}/{path}", role),
    ).fetchone()[0]


def test_argpath_used_and_output_flag_generated(store):
    conn, load = store
    c = "python fit.py data/obs.csv --params=params.txt --out results/fit.json"
    load(
        fev("fit.py", 1, "a", "scanned"),
        fev("data/obs.csv", 2, "b", "scanned"),
        fev("params.txt", 3, "c", "scanned"),
        cmd(c, 10, 12),
        fev("results/fit.json", 11.5, "d", "created"),
    )
    assert edges(conn, c) == {
        ("used", "fit.py", "a", "argpath"),
        ("used", "data/obs.csv", "b", "argpath"),
        ("used", "params.txt", "c", "argpath"),
        ("generated", "results/fit.json", "d", "argpath"),
    }
    assert conf(conn, c, "fit.py", "used") == 0.9


def test_used_points_at_version_current_at_start(store):
    conn, load = store
    load(
        fev("params.txt", 1, "a", "scanned"),
        fev("params.txt", 5, "b"),  # edited before the run
        cmd("cat params.txt", 10, 11),
        fev("params.txt", 20, "c"),  # edited after
    )
    assert edges(conn, "cat params.txt") == {("used", "params.txt", "b", "argpath")}


def test_in_place_edit_uses_old_and_generates_new(store):
    conn, load = store
    c = "sed -i 's/a/b/' params.txt"
    load(fev("params.txt", 1, "a", "scanned"), cmd(c, 10, 10.2), fev("params.txt", 10.1, "b"))
    assert edges(conn, c) == {
        ("used", "params.txt", "a", "argpath"),
        ("generated", "params.txt", "b", "window"),
    }


def test_window_generated_under_cwd_only(store):
    conn, load = store
    c = "python simulate.py"
    load(
        fev("simulate.py", 1, "a", "scanned"),
        cmd(c, 10, 20, cwd=f"{ROOT}/runs"),
        fev("runs/out.xvg", 15, "b", "created"),
        fev("other/unrelated.csv", 16, "c", "created"),
        fev("runs/late.log", 21.5, "d", "created"),  # within the 2 s grace
        fev("runs/too_late.log", 23, "e", "created"),
    )
    got = edges(conn, c)
    assert ("generated", "runs/out.xvg", "b", "window") in got
    assert ("generated", "runs/late.log", "d", "window") in got
    assert not any(p in ("other/unrelated.csv", "runs/too_late.log") for _, p, _, _ in got)
    assert conf(conn, c, "runs/out.xvg", "generated") == 0.7


def test_overlapping_runs_get_ambiguous_window_confidence(store):
    conn, load = store
    load(
        cmd("python a.py", 10, 30),
        cmd("python b.py", 20, 25),
        fev("x.csv", 22, "a", "created"),
    )
    assert conf(conn, "python a.py", "x.csv", "generated") == 0.4
    assert conf(conn, "python b.py", "x.csv", "generated") == 0.4


def test_deleted_file_is_not_used(store):
    conn, load = store
    load(
        fev("old.csv", 1, "a", "scanned"),
        {**fev("old.csv", 5, "a", "deleted"), "sha256": None, "size": None},
        cmd("cat old.csv", 10, 11),
    )
    assert edges(conn, "cat old.csv") == set()


def test_copy_destination_is_output_not_input(store):
    conn, load = store
    c = "cp figs/a.png figs/final.png"
    load(
        fev("figs/a.png", 1, "a", "scanned"),
        fev("figs/final.png", 2, "z", "scanned"),  # an older final.png existed
        cmd(c, 10, 10.1),
        fev("figs/final.png", 10.05, "a"),
    )
    assert edges(conn, c) == {
        ("used", "figs/a.png", "a", "argpath"),
        ("generated", "figs/final.png", "a", "argpath"),
    }


def test_redirect_and_deffnm(store):
    conn, load = store
    load(
        fev("md.tpr", 1, "a", "scanned"),
        cmd("gmx mdrun -deffnm md > run.out 2>&1", 10, 30),
        fev("md.xtc", 20, "b", "created"),
        fev("md.edr", 21, "c", "created"),
        fev("run.out", 29, "d", "created"),
    )
    got = edges(conn, "gmx mdrun -deffnm md > run.out 2>&1")
    assert {(r, p, m) for r, p, _, m in got} >= {
        ("generated", "md.xtc", "argpath"),
        ("generated", "md.edr", "argpath"),
        ("generated", "run.out", "argpath"),
    }


def test_subshell_cd_resolves_paths(store):
    conn, load = store
    c = "(cd results && wc -l fit.json fit2.json)"
    load(
        fev("results/fit.json", 1, "a", "scanned"),
        fev("results/fit2.json", 2, "b", "scanned"),
        cmd(c, 10, 10.1),
    )
    assert {p for _, p, _, _ in edges(conn, c)} == {"results/fit.json", "results/fit2.json"}


def test_other_hosts_files_never_match(store):
    conn, load = store
    load(fev("data.csv", 1, "a", "scanned", host="workstation"), cmd("cat data.csv", 10, 11))
    assert edges(conn, "cat data.csv") == set()


def test_relinking_is_idempotent(store):
    conn, load = store
    load(fev("a.csv", 1, "a", "scanned"), cmd("cat a.csv", 10, 11))
    link(conn)
    link(conn)
    assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 1


def test_late_file_events_are_linked_on_next_ingest(store):
    """A big file settles 30 s after the run; its event arrives in a later ingest."""
    conn, load = store
    load(cmd("python sim.py", 10, 20))
    assert edges(conn, "python sim.py") == set()
    load(fev("traj.xtc", 19, "a", "created"))
    assert ("generated", "traj.xtc", "a", "window") in edges(conn, "python sim.py")


@pytest.mark.parametrize(
    "command, used, generated",
    [
        ("python fit.py a.csv --out b.json", ["fit.py", "a.csv"], ["b.json"]),
        ("python fit.py --out=b.json a.csv", ["fit.py", "a.csv"], ["b.json"]),
        ("sort -o out.txt in.txt", ["in.txt"], ["out.txt"]),
        ("cat a.txt > b.txt", ["a.txt"], ["b.txt"]),
        ("cat a.txt >> b.txt", ["a.txt"], ["b.txt"]),
        ("tool in=a.csv", ["a.csv"], []),
        ("mv x.csv y.csv", ["x.csv"], ["y.csv"]),
        ("head -3 'my file.csv' | tee copy.csv", ["my file.csv"], ["copy.csv"]),
    ],
)
def test_command_paths(command, used, generated):
    got = command_paths(command, Path("/p"))
    assert [p.name for p in got.used] == [Path(u).name for u in used]
    assert [p.name for p in got.generated] == [Path(g).name for g in generated]


def test_grace_stops_at_the_next_run(store):
    """Quick successive commands: the 2 s grace never reaches into the next run."""
    conn, load = store
    load(
        cmd("mkdir -p data", 10, 10.1),
        cmd("python gen.py --n 5", 10.6, 11.4),
        fev("data/obs.csv", 11.2, "a", "created"),
        cmd("python other.py", 12.0, 12.5),
        fev("late.csv", 13.0, "b", "created"),  # after other.py: within its grace
    )
    assert edges(conn, "mkdir -p data") == set()
    assert ("generated", "data/obs.csv", "a", "window") in edges(conn, "python gen.py --n 5")
    assert ("generated", "late.csv", "b", "window") in edges(conn, "python other.py")
    assert not any(p == "late.csv" for _, p, _, _ in edges(conn, "python gen.py --n 5"))


def wrapped(cmd_text: str, start: float, end: float, generated: list[str], **kw) -> dict:
    return {
        **cmd(
            cmd_text,
            start,
            end,
            shell="cairn-run",
            wrapper={"generated": [f"{ROOT}/{g}" for g in generated], "deleted": []},
        ),
        **kw,
    }


def test_wrapper_does_not_claim_a_nested_runs_output(store):
    """A long background run's tree diff also sees what other commands wrote meanwhile."""
    conn, load = store
    load(
        wrapped("python long.py", 10, 60, ["long.out", "summary.txt"]),
        cmd("python summarize.py", 30, 31),
        fev("summary.txt", 30.5, "a", "created"),
        fev("long.out", 59, "b", "created"),
    )
    long_edges = edges(conn, "python long.py")
    assert ("generated", "long.out", "b", "wrapper") in long_edges
    assert not any(p == "summary.txt" for _, p, _, _ in long_edges)
    assert ("generated", "summary.txt", "a", "window") in edges(conn, "python summarize.py")
    # Others ran during the wrapped run, so its claim is strong but not certain.
    assert conf(conn, "python long.py", "long.out", "generated") == 0.8


def test_wrapper_alone_is_exact(store):
    conn, load = store
    load(wrapped("python a.py", 10, 12, ["out.csv"]), fev("out.csv", 12, "a", "created"))
    assert conf(conn, "python a.py", "out.csv", "generated") == 1.0


def bg_start(cmd_text: str, start: float, run_id: str) -> dict:
    return {
        "v": 1,
        "type": "run_start",
        "id": f"{run_id}-start",
        "run_id": run_id,
        "pid": 1,
        "monitor_pid": 1,
        "session": "S",
        "host": "hp",
        "user": "t",
        "cwd": ROOT,
        "cmd": cmd_text,
        "ts_start": ts(start),
    }


def bg_end(run_id: str, end: float, generated: list[str], written: list[str]) -> dict:
    return {
        "v": 1,
        "type": "run_end",
        "id": f"{run_id}-end",
        "run_id": run_id,
        "host": "hp",
        "user": "t",
        "exit": 0,
        "ts_end": ts(end),
        "wrapper": {
            "generated": [f"{ROOT}/{g}" for g in generated],
            "deleted": [],
            "written": [f"{ROOT}/{w}" for w in written],
        },
    }


def test_background_job_owns_files_it_held_open(store):
    """mdrun writes md.log for hours while the user analyses in the same directory."""
    from ulid import ULID

    conn, load = store
    rid = str(ULID())
    load(
        bg_start("gmx mdrun -v", 10, rid),
        fev("md.log", 20, "a", "created"),
        cmd("python analyse.py", 30, 32),
        fev("md.log", 31, "b"),  # mdrun keeps writing during the analysis
        fev("plot.png", 31.5, "c", "created"),
        fev("md.log", 59, "d"),
        bg_end(rid, 60, ["md.log", "plot.png"], ["md.log"]),
    )
    assert edges(conn, "python analyse.py") == {("generated", "plot.png", "c", "window")}
    md = edges(conn, "gmx mdrun -v")
    assert ("generated", "md.log", "d", "wrapper") in md
    assert not any(p == "plot.png" for _, p, _, _ in md)
    assert conf(conn, "gmx mdrun -v", "md.log", "generated") == 1.0


def test_background_unseen_change_outside_cwd_is_not_claimed(store):
    from ulid import ULID

    conn, load = store
    rid = str(ULID())
    start = {**bg_start("python job.py", 10, rid), "cwd": f"{ROOT}/runs"}
    load(
        start,
        fev("runs/out.csv", 15, "a", "created"),
        fev("notes/todo.md", 16, "b", "created"),  # edited in an editor meanwhile
        bg_end(rid, 20, ["runs/out.csv", "notes/todo.md"], []),
    )
    got = edges(conn, "python job.py")
    assert ("generated", "runs/out.csv", "a", "wrapper") in got
    assert not any(p == "notes/todo.md" for _, p, _, _ in got)
    assert conf(conn, "python job.py", "runs/out.csv", "generated") == 0.6


def test_back_to_back_runs_are_not_ambiguous(store):
    conn, load = store
    load(
        cmd("sed -i s/a/b/ p.txt", 10, 10.1),
        fev("p.txt", 10.05, "a"),
        cmd("python next.py", 10.6, 11),
    )
    assert conf(conn, "sed -i s/a/b/ p.txt", "p.txt", "generated") == 0.7


def test_background_job_started_just_after_the_writer_does_not_claim_its_output(store):
    """The --bg monitor records its start ~10 ms after the user's next command began."""
    from ulid import ULID

    conn, load = store
    rid = str(ULID())
    load(
        cmd("python summarize.py", 10.662, 10.679),
        bg_start("python simulate.py", 10.671, rid),
        fev("summary.txt", 10.676, "a", "created"),
        fev("sim.log", 12.5, "b", "created"),
        bg_end(rid, 12.794, ["summary.txt", "sim.log"], ["sim.log"]),
    )
    assert ("generated", "summary.txt", "a", "window") in edges(conn, "python summarize.py")
    assert not any(p == "summary.txt" for _, p, _, _ in edges(conn, "python simulate.py"))
