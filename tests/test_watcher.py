"""Tests for the file watcher core, ignore rules, scan and the watchdog adapter."""

import json
import os
import threading
import time
from pathlib import Path

import pytest

from cairnlab.capture.watcher import EventWriter, FileTracker, IgnoreRules, watch


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    home = root / ".cairn"
    events = home / "events"
    clock = Clock()
    tracker = FileTracker(
        rules=IgnoreRules(root),
        writer=EventWriter(events, host="synth", user="tester"),
        objects_dir=home / "objects",
        state_path=home / "state" / "files-synth.json",
        clock=clock,
        is_open=lambda p: False,
    )
    return root, events, tracker, clock


def read_events(events: Path) -> list[dict]:
    lines = []
    for f in sorted(events.glob("*.jsonl")):
        lines += [json.loads(x) for x in f.read_text().splitlines()]
    return lines


def write(path: Path, data: str | bytes, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data)
    else:
        path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_ignore_rules(tmp_path):
    (tmp_path / ".cairnignore").write_text("scratch/\n*.log\n!keep.log\n")
    rules = IgnoreRules(tmp_path)
    ignored = [
        ".git/HEAD",
        ".cairn/cairn.db",
        "a/__pycache__/m.pyc",
        "x.swp",
        "#md.xtc.1#",
        "runs/#rmsd.xvg.2#",
        "state_prev.cpt",
        "scratch/out.txt",
        "run.log",
        "notes.txt~",
    ]
    kept = ["runs/v4/rmsd.xvg", "md.cpt", "keep.log", "fig.png"]
    assert [p for p in ignored if not rules.ignored(tmp_path / p)] == []
    assert [p for p in kept if rules.ignored(tmp_path / p)] == []
    assert rules.ignored(tmp_path.parent / "elsewhere.txt")


def test_burst_is_debounced_into_one_event(env):
    root, events, tracker, clock = env
    f = root / "rmsd.xvg"
    for i in range(5):
        write(f, f"v{i}")
        tracker.notify(f, "modified")
        clock.advance(0.1)
    assert tracker.process_due() == 0  # still inside the 0.5 s window
    clock.advance(0.5)
    assert tracker.process_due() == 1
    (ev,) = read_events(events)
    assert ev["op"] == "created" and ev["path"] == str(f)
    assert ev["fp"] == "full" and ev["stored"] is True
    assert ev["size"] == 2 and ev["host"] == "synth" and ev["user"] == "tester"
    assert (root / ".cairn" / "objects" / ev["sha256"][:2] / ev["sha256"]).read_text() == "v4"


def test_modify_then_delete(env):
    root, events, tracker, clock = env
    f = root / "a.csv"
    write(f, "1")
    tracker.notify(f, "created")
    clock.advance(1)
    tracker.process_due()
    write(f, "22")
    tracker.notify(f, "modified")
    clock.advance(1)
    tracker.process_due()
    f.unlink()
    tracker.notify(f, "deleted")
    clock.advance(1)
    tracker.process_due()
    assert [e["op"] for e in read_events(events)] == ["created", "modified", "deleted"]


def test_unchanged_file_is_not_re_emitted(env):
    root, events, tracker, clock = env
    f = root / "a.txt"
    write(f, "x")
    tracker.notify(f, "modified")
    clock.advance(1)
    tracker.process_due()
    tracker.notify(f, "modified")  # e.g. a chmod: same size and mtime
    clock.advance(1)
    assert tracker.process_due() == 0


def test_temp_file_created_and_removed_leaves_no_trace(env):
    root, events, tracker, clock = env
    f = root / "tmp.out"
    write(f, "x")
    tracker.notify(f, "created")
    f.unlink()
    tracker.notify(f, "deleted")
    tracker.notify(f, "created")
    clock.advance(1)
    tracker.process_due()
    assert read_events(events) == []


def test_ignored_paths_never_emit(env):
    root, events, tracker, clock = env
    f = root / "__pycache__" / "x.pyc"
    write(f, "x")
    tracker.notify(f, "created")
    clock.advance(1)
    assert tracker.process_due() == 0


def test_large_file_waits_until_settled(env):
    root, events, tracker, clock = env
    tracker.large_bytes = 1024
    f = root / "traj.xtc"
    write(f, b"x" * 4096, mtime=clock.t)
    tracker.notify(f, "created")
    clock.advance(1)
    assert tracker.process_due() == 0  # mtime is only 1 s old
    clock.advance(31)
    assert tracker.process_due() == 1
    (ev,) = read_events(events)
    assert ev["fp"] == "fast" and ev["stored"] is False
    assert not (root / ".cairn" / "objects").exists() or not any(
        (root / ".cairn" / "objects").rglob("*")
    )


def test_large_file_open_by_a_writer_waits(env):
    root, events, tracker, clock = env
    tracker.large_bytes = 1024
    busy = {"open": True}
    tracker.is_open = lambda p: busy["open"]
    f = root / "md.trr"
    write(f, b"x" * 4096, mtime=clock.t - 3600)
    tracker.notify(f, "modified")
    clock.advance(1)
    assert tracker.process_due() == 0
    busy["open"] = False
    clock.advance(6)
    assert tracker.process_due() == 1


def test_checkpoint_waits_for_long_quiet(env):
    root, events, tracker, clock = env
    f = root / "md.cpt"
    for _ in range(3):  # mdrun rewrites the checkpoint every 15 min
        write(f, os.urandom(64))
        tracker.notify(f, "modified")
        clock.advance(15 * 60)
        assert tracker.process_due() == 0
    clock.advance(6 * 60)
    assert tracker.process_due() == 1
    assert len(read_events(events)) == 1


def test_flush_on_shutdown_skips_unsettled_large_files(env):
    root, events, tracker, clock = env
    tracker.large_bytes = 1024
    small, big = root / "s.txt", root / "b.xtc"
    write(small, "x")
    write(big, b"x" * 4096, mtime=clock.t)
    tracker.notify(small, "created")
    tracker.notify(big, "created")
    assert tracker.process_due(flush=True) == 1
    assert [e["path"] for e in read_events(events)] == [str(small)]


def test_moved_file(env):
    root, events, tracker, clock = env
    a, b = root / "a.png", root / "b.png"
    write(a, "img")
    tracker.notify(a, "created")
    clock.advance(1)
    tracker.process_due()
    a.rename(b)
    tracker.notify(a, "deleted")
    tracker.notify(b, "moved", src=a)
    clock.advance(1)
    tracker.process_due()
    evs = read_events(events)
    assert [(e["op"], Path(e["path"]).name) for e in evs] == [
        ("created", "a.png"),
        ("deleted", "a.png"),
        ("moved", "b.png"),
    ]
    assert evs[2]["src_path"] == str(a) and evs[2]["sha256"] == evs[0]["sha256"]


def test_scan_and_state_survive_restart(env, tmp_path):
    root, events, tracker, clock = env
    write(root / "data" / "x.csv", "1,2")
    write(root / "fit.py", "print(1)")
    write(root / ".git" / "HEAD", "ref")
    assert tracker.scan(root) == 2
    tracker.save_state()
    assert {e["op"] for e in read_events(events)} == {"scanned"}

    again = FileTracker(
        rules=IgnoreRules(root),
        writer=tracker.writer,
        objects_dir=tracker.objects_dir,
        state_path=tracker.state_path,
        clock=clock,
    )
    assert again.scan(root) == 0  # nothing changed since the saved state
    write(root / "fit.py", "print(2)")
    assert again.scan(root) == 1


def test_scan_since(env):
    root, events, tracker, clock = env
    write(root / "old.txt", "o", mtime=1_000)
    write(root / "new.txt", "n", mtime=3_000)
    assert tracker.scan(root, since=2_000) == 1
    assert [Path(e["path"]).name for e in read_events(events)] == ["new.txt"]


def test_real_watchdog(tmp_path):
    root = tmp_path / "proj"
    (root / "runs").mkdir(parents=True)
    events = root / ".cairn" / "events"
    tracker = FileTracker(
        rules=IgnoreRules(root),
        writer=EventWriter(events, host="synth", user="tester"),
        objects_dir=root / ".cairn" / "objects",
        debounce=0.2,
    )
    stop = threading.Event()
    t = threading.Thread(target=watch, args=(tracker, [root], stop), daemon=True)
    t.start()
    time.sleep(0.5)
    (root / "runs" / "out.csv").write_text("a,b\n1,2\n")
    (root / "runs" / "junk.swp").write_text("x")
    deadline = time.time() + 10
    while time.time() < deadline and not list(events.glob("*.jsonl")):
        time.sleep(0.1)
    time.sleep(0.5)
    stop.set()
    t.join(timeout=10)
    evs = read_events(events)
    assert [Path(e["path"]).name for e in evs] == ["out.csv"]
    assert evs[0]["op"] == "created"


def test_close_after_write_settles_quickly(env):
    """IN_CLOSE_WRITE means the writer is done: no need to wait the full debounce."""
    root, events, tracker, clock = env
    f = root / "fit.json"
    write(f, "v1")
    tracker.notify(f, "modified")
    tracker.notify(f, "modified", closed=True)
    clock.advance(0.15)
    assert tracker.process_due() == 1
    write(f, "v2")  # a second run rewrites it 0.6 s later: a separate event
    tracker.notify(f, "modified")
    tracker.notify(f, "modified", closed=True)
    clock.advance(0.15)
    assert tracker.process_due() == 1
    assert len(read_events(events)) == 2


def test_close_does_not_shortcut_checkpoints(env):
    root, events, tracker, clock = env
    f = root / "md.cpt"
    write(f, "x")
    tracker.notify(f, "modified", closed=True)
    clock.advance(1)
    assert tracker.process_due() == 0


def test_event_time_is_the_write_time_when_mtime_is_fresh(env):
    """Late delivery must not push a write into the next run's window."""
    root, events, tracker, clock = env
    f = root / "fig.png"
    write(f, "png", mtime=clock.t - 0.8)  # written 0.8 s before the event was observed
    tracker.notify(f, "created", closed=True)
    clock.advance(0.2)
    tracker.process_due()
    (ev,) = read_events(events)
    assert ev["ts"] == ev["mtime"]


def test_preserved_old_mtime_keeps_observed_time(env):
    """cp -p / rsync -a keep an old mtime: the change still happened now."""
    root, events, tracker, clock = env
    f = root / "copied.csv"
    write(f, "x", mtime=clock.t - 86400)
    tracker.notify(f, "created", closed=True)
    observed = clock.t
    clock.advance(0.2)
    tracker.process_due()
    (ev,) = read_events(events)
    assert ev["ts"] != ev["mtime"]
    from datetime import datetime

    assert abs(datetime.fromisoformat(ev["ts"]).timestamp() - observed) < 0.01
