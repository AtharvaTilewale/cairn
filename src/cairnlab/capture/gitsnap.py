"""Git snapshot for a run: commit, branch, dirty flag and short diff stat.

Ingest may happen hours after a command ran, so "current HEAD" would be wrong. The
commit and branch are reconstructed from the HEAD reflog at the run's start time.
Dirty state and diff stat can only be observed now, so they are recorded only when
the run ended within ``recent_s`` (10 min) and HEAD has not moved since; otherwise
they stay unknown (None). Repository state is cached per minute.
"""

import logging
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger(__name__)


class GitSnap(NamedTuple):
    commit: str | None
    branch: str | None
    dirty: bool | None
    diffstat: str | None


class _Reflog(NamedTuple):
    sha: str
    t: int
    msg: str


def _git(repo: str, *args: str) -> str | None:
    try:
        res = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return res.stdout if res.returncode == 0 else None


def _epoch(ts: str) -> float:
    return datetime.fromisoformat(ts).timestamp()


class GitSnapshotter:
    """Snapshots git state for runs, caching repository lookups for a minute."""

    def __init__(self, now: Callable[[], float] = time.time, recent_s: float = 600):
        self.now = now
        self.recent_s = recent_s
        self._toplevel: dict[str, str | None] = {}
        self._repo: dict[str, tuple[float, list[_Reflog], str | None, bool, str]] = {}

    def snapshot(self, cwd: str, ts_start: str, ts_end: str) -> GitSnap | None:
        """Git state for a run that started at ``ts_start`` in ``cwd``; None if not a repo."""
        top = self._top(cwd)
        if top is None:
            return None
        reflog, branch_now, dirty_now, stat_now = self._state(top)
        head_now = reflog[0].sha if reflog else None
        t = _epoch(ts_start)
        commit = next((e.sha for e in reflog if e.t <= t), None)
        branch = self._branch_at(reflog, t, branch_now) if commit else None
        dirty = stat = None
        recent = self.now() - _epoch(ts_end) <= self.recent_s
        if commit and recent and commit == head_now:
            dirty, stat = dirty_now, stat_now
        return GitSnap(commit, branch, dirty, stat)

    def _top(self, cwd: str) -> str | None:
        if cwd not in self._toplevel:
            top = None
            if Path(cwd).is_dir():
                out = _git(cwd, "rev-parse", "--show-toplevel")
                top = out.strip() if out else None
            self._toplevel[cwd] = top
        return self._toplevel[cwd]

    def _state(self, top: str) -> tuple[list[_Reflog], str | None, bool, str]:
        cached = self._repo.get(top)
        if cached and self.now() - cached[0] < 60:
            return cached[1:]
        reflog = []
        out = _git(top, "reflog", "show", "--date=unix", "--format=%H%x09%gd%x09%gs", "HEAD")
        for line in (out or "").splitlines():
            sha, selector, msg = (line.split("\t", 2) + ["", ""])[:3]
            try:  # selector looks like HEAD@{1761556443}
                t = int(selector[selector.index("{") + 1 : selector.index("}")])
            except ValueError:
                continue
            reflog.append(_Reflog(sha, t, msg))
        branch = _git(top, "symbolic-ref", "--short", "-q", "HEAD")
        status = _git(top, "status", "--porcelain", "--untracked-files=no") or ""
        stat = _git(top, "diff", "--shortstat", "HEAD") or ""
        state = (reflog, branch.strip() if branch else None, bool(status.strip()), stat.strip())
        self._repo[top] = (self.now(), *state)
        return state

    @staticmethod
    def _branch_at(reflog: list[_Reflog], t: float, branch_now: str | None) -> str | None:
        """Branch checked out at ``t``: the 'from' of the first checkout after ``t``."""
        for e in reversed(reflog):  # oldest first
            if e.t > t and e.msg.startswith("checkout: moving from "):
                return e.msg.removeprefix("checkout: moving from ").split(" to ", 1)[0]
        return branch_now
