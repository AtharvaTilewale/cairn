"""Tests for git and environment snapshots attached to runs at ingest."""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from cairnlab.capture.envsnap import EnvSnapshotter
from cairnlab.capture.gitsnap import GitSnapshotter
from cairnlab.capture.ingest import Snapshots, ingest
from cairnlab.store import db

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

T0 = 1_793_000_000  # 2026-10-27, epoch seconds


def iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


def git(repo: Path, *args: str, at: int) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_AUTHOR_DATE": f"@{at} +0000",
        "GIT_COMMITTER_DATE": f"@{at} +0000",
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args], env=env, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """main: c1 @T0, c2 @T0+100; then feat (checkout @T0+200): c3 @T0+300."""
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main", at=T0)
    (r / "fit.py").write_text("print(1)\n")
    git(r, "add", "fit.py", at=T0)
    git(r, "commit", "-q", "-m", "c1", at=T0)
    (r / "fit.py").write_text("print(2)\n")
    git(r, "commit", "-q", "-am", "c2", at=T0 + 100)
    git(r, "checkout", "-q", "-b", "feat", at=T0 + 200)
    (r / "fit.py").write_text("print(3)\n")
    git(r, "commit", "-q", "-am", "c3", at=T0 + 300)
    shas = git(r, "log", "--format=%H", "--all", at=T0).split()
    return r, {"c3": shas[0], "c2": shas[1], "c1": shas[2]}


def test_commit_and_branch_at_run_time(repo):
    r, sha = repo
    snap = GitSnapshotter(now=lambda: T0 + 100_000)
    assert snap.snapshot(str(r), iso(T0 + 50), iso(T0 + 51))[:2] == (sha["c1"], "main")
    assert snap.snapshot(str(r), iso(T0 + 150), iso(T0 + 151))[:2] == (sha["c2"], "main")
    assert snap.snapshot(str(r), iso(T0 + 250), iso(T0 + 251))[:2] == (sha["c2"], "feat")
    assert snap.snapshot(str(r), iso(T0 + 400), iso(T0 + 401))[:2] == (sha["c3"], "feat")


def test_subdirectory_cwd(repo):
    r, sha = repo
    (r / "runs").mkdir()
    snap = GitSnapshotter(now=lambda: T0 + 100_000)
    assert snap.snapshot(str(r / "runs"), iso(T0 + 400), iso(T0 + 401)).commit == sha["c3"]


def test_before_repo_history_is_unknown(repo):
    r, _ = repo
    snap = GitSnapshotter(now=lambda: T0 + 100_000)
    assert snap.snapshot(str(r), iso(T0 - 10), iso(T0 - 9)).commit is None


def test_dirty_only_for_recent_runs(repo):
    r, sha = repo
    (r / "fit.py").write_text("print('edited')\n")
    (r / "untracked_output.csv").write_text("1\n")
    recent = GitSnapshotter(now=lambda: T0 + 460).snapshot(str(r), iso(T0 + 400), iso(T0 + 401))
    assert recent.dirty is True
    assert "1 file changed" in recent.diffstat
    old = GitSnapshotter(now=lambda: T0 + 100_000).snapshot(str(r), iso(T0 + 400), iso(T0 + 401))
    assert old.commit == sha["c3"] and old.dirty is None and old.diffstat is None


def test_clean_tree_ignores_untracked(repo):
    r, _ = repo
    (r / "untracked_output.csv").write_text("1\n")
    snap = GitSnapshotter(now=lambda: T0 + 460).snapshot(str(r), iso(T0 + 400), iso(T0 + 401))
    assert snap.dirty is False and snap.diffstat == ""


def test_not_a_repo_or_missing_cwd(tmp_path):
    snap = GitSnapshotter()
    assert snap.snapshot(str(tmp_path), iso(T0), iso(T0)) is None
    assert snap.snapshot(str(tmp_path / "gone"), iso(T0), iso(T0)) is None


def fake_env(tmp_path: Path, packages: list[str], pip: list[str], at: float | None = None) -> Path:
    prefix = tmp_path / "envs" / "md"
    (prefix / "conda-meta").mkdir(parents=True)
    for p in packages:
        (prefix / "conda-meta" / f"{p}.json").write_text("{}")
    (prefix / "conda-meta" / "history").write_text("")
    site = prefix / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    for p in pip:
        (site / f"{p}.dist-info").mkdir()
    old = time.time() - 3600 if at is None else at
    for p in [prefix / "conda-meta", prefix / "conda-meta" / "history", site]:
        os.utime(p, (old, old))
    return prefix


def test_env_hash_tracks_installed_packages(tmp_path):
    prefix = fake_env(tmp_path, ["numpy-2.1.0-py312_0", "gromacs-2024.1-0"], ["mdtraj-1.10.0"])
    now = time.time()
    h1 = EnvSnapshotter().env_hash(str(prefix), iso(now))
    assert h1 and len(h1) == 64
    assert EnvSnapshotter().env_hash(str(prefix), iso(now)) == h1  # deterministic
    (prefix / "lib" / "python3.12" / "site-packages" / "rich-14.0.dist-info").mkdir()
    h2 = EnvSnapshotter().env_hash(str(prefix), iso(time.time() + 1))
    assert h2 and h2 != h1


def test_env_changed_after_run_gives_no_hash(tmp_path):
    prefix = fake_env(tmp_path, ["numpy-2.1.0-py312_0"], [])
    run_end = time.time() - 600
    (prefix / "conda-meta" / "scipy-1.14-0.json").write_text("{}")
    (prefix / "conda-meta" / "history").write_text("==> later install <==")
    assert EnvSnapshotter().env_hash(str(prefix), iso(run_end)) is None


def test_env_hash_missing_prefix(tmp_path):
    assert EnvSnapshotter().env_hash(str(tmp_path / "nope"), iso(time.time())) is None


def test_ingest_fills_snapshots_for_local_runs(repo, tmp_path):
    r, sha = repo
    prefix = fake_env(tmp_path, ["numpy-2.1.0-py312_0"], [], at=T0)
    events = tmp_path / "events"
    events.mkdir()
    base = {
        "v": 1,
        "type": "cmd",
        "session": "S",
        "shell": "bash",
        "user": "tester",
        "cwd": str(r),
        "cmd": "python fit.py",
        "exit": 0,
        "conda_env": "md",
        "env_prefix": str(prefix),
    }
    lines = [
        {**base, "id": "S-1", "host": "hp", "ts_start": iso(T0 + 400), "ts_end": iso(T0 + 401)},
        {
            **base,
            "id": "S-2",
            "host": "workstation",
            "ts_start": iso(T0 + 400),
            "ts_end": iso(T0 + 401),
        },
    ]
    (events / "x.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    conn = db.connect(tmp_path / "cairn.db")
    db.migrate(conn)
    snaps = Snapshots(host="hp", git=GitSnapshotter(now=lambda: T0 + 100_000), env=EnvSnapshotter())
    ingest(conn, events, snapshots=snaps)
    rows = {
        row["host"]: row
        for row in conn.execute("SELECT host, git_commit, git_branch, env_name, env_hash FROM runs")
    }
    assert rows["hp"]["git_commit"] == sha["c3"] and rows["hp"]["git_branch"] == "feat"
    assert rows["hp"]["env_name"] == "md" and len(rows["hp"]["env_hash"]) == 64
    # Another host's paths are not this machine's: no snapshot is guessed.
    assert rows["workstation"]["git_commit"] is None and rows["workstation"]["env_hash"] is None
    assert rows["workstation"]["env_name"] == "md"
