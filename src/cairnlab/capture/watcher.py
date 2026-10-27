"""File events with fingerprints (CLAUDE.md section 5.2).

``FileTracker`` is the pure core: it is told about changes, waits until each path is
quiet, fingerprints it and appends one JSON line per settled change. ``cairn watch``
feeds it from watchdog; ``cairn scan`` feeds it a one-shot directory walk.

Settling rules:
- every path waits ``debounce`` (0.5 s) after its last event, so bursts become one event;
  a close-after-write (the writer is done) settles in 0.1 s, so outputs of commands run
  in quick succession stay separate events;
- files over 50 MB also wait until their mtime is 30 s old and no process has them
  open, then get the fast fingerprint once;
- GROMACS checkpoints (``*.cpt``) wait ``cpt_quiet`` (20 min, longer than mdrun's
  default 15 min checkpoint interval), so only the final checkpoint is recorded.
"""

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pathspec
from ulid import ULID

from cairnlab.capture.fingerprint import LARGE_BYTES, fingerprint
from cairnlab.config import IGNORE_FILE
from cairnlab.store.models import format_ts, utc_now

log = logging.getLogger(__name__)

# Always ignored, whatever .cairnignore says.
BUILTIN_IGNORE = [
    ".git/",
    ".cairn/",
    ".cairn-*/",
    "__pycache__/",
    ".ipynb_checkpoints/",
    "*.tmp",
    "*.swp",
    "*.swx",
    "*~",
    ".#*",
    "\\#*#",  # editor autosaves and GROMACS backups (#md.xtc.1#)
    "4913",  # vim's write test file
    "*_prev.cpt",  # GROMACS keeps the previous checkpoint alongside the current one
]

DEBOUNCE_S = 0.5
CLOSED_S = 0.1  # after IN_CLOSE_WRITE the writer is done; just absorb trailing events
SETTLE_S = 30.0
CPT_QUIET_S = 20 * 60.0
RECHECK_S = 5.0
MTIME_TRUST_S = 5.0  # an mtime this close before the event is the real write time


class IgnoreRules:
    """Built-in ignore patterns plus the project's ``.cairnignore`` (gitignore syntax)."""

    def __init__(self, root: Path, extra: Iterable[str] = ()):
        self.root = root
        lines = list(BUILTIN_IGNORE)
        ignore_file = root / IGNORE_FILE
        if ignore_file.is_file():
            lines += ignore_file.read_text(encoding="utf-8", errors="replace").splitlines()
        lines += list(extra)
        self.spec = pathspec.GitIgnoreSpec.from_lines(lines)

    def ignored(self, path: Path, is_dir: bool = False) -> bool:
        """True if ``path`` (absolute, under root) must not be recorded."""
        try:
            rel = path.relative_to(self.root).as_posix()
        except ValueError:
            return True
        if rel in ("", "."):
            return False
        return self.spec.match_file(rel + "/" if is_dir else rel)


class MultiRootRules:
    """Ignore rules for several roots: each path uses the rules of the root it is under."""

    def __init__(self, roots: list[Path]):
        self.by_root = [IgnoreRules(r) for r in roots]

    def ignored(self, path: Path, is_dir: bool = False) -> bool:
        for rules in self.by_root:
            if path.is_relative_to(rules.root):
                return rules.ignored(path, is_dir)
        return True


def rules_for(roots: list[Path]) -> "IgnoreRules | MultiRootRules":
    """Ignore rules covering ``roots``."""
    return IgnoreRules(roots[0]) if len(roots) == 1 else MultiRootRules(roots)


def walk(root: Path, rules: "IgnoreRules | MultiRootRules") -> Iterator[Path]:
    """Every non-ignored regular file under ``root``, skipping ignored directories."""
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not rules.ignored(here / d, is_dir=True)]
        for name in filenames:
            p = here / name
            if not rules.ignored(p) and p.is_file() and not p.is_symlink():
                yield p


def open_by_any_process(path: Path) -> bool:
    """True if any process we can see holds ``path`` open (Linux /proc)."""
    target = str(path)
    proc = Path("/proc")
    if not proc.is_dir():
        return False
    for pid_dir in proc.iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            for fd in (pid_dir / "fd").iterdir():
                try:
                    if os.readlink(fd) == target:
                        return True
                except OSError:
                    continue
        except OSError:
            continue
    return False


class EventWriter:
    """Appends events, one line per ``write()``, to ``<host>-<day>-<suffix>.jsonl``.

    Each writer (watcher, ``cairn run``) has its own file per day, so its lines never
    interleave with the shell hook's. Events default to ``type: file``.
    """

    def __init__(self, events_dir: Path, host: str, user: str, suffix: str = "watch"):
        self.events_dir = events_dir
        self.host = host
        self.user = user
        self.suffix = suffix

    def write(self, event: dict) -> None:
        event = {
            "v": 1,
            "type": "file",
            "id": str(ULID()),
            "host": self.host,
            "user": self.user,
            **event,
        }
        line = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        self.events_dir.mkdir(parents=True, exist_ok=True)
        when = event.get("ts") or event.get("ts_end") or event.get("ts_start") or utc_now()
        path = self.events_dir / f"{self.host}-{when[:10]}-{self.suffix}.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)


@dataclass
class _Pending:
    op: str
    first: float
    last: float
    src: str | None = None
    not_before: float = 0.0
    closed: bool = False


@dataclass
class FileTracker:
    """Debounces change notifications and emits settled, fingerprinted file events."""

    rules: IgnoreRules
    writer: EventWriter
    objects_dir: Path | None
    state_path: Path | None = None
    clock: Callable[[], float] = time.time
    debounce: float = DEBOUNCE_S
    settle: float = SETTLE_S
    cpt_quiet: float = CPT_QUIET_S
    large_bytes: int = LARGE_BYTES
    is_open: Callable[[Path], bool] = open_by_any_process
    pending: dict[str, _Pending] = field(default_factory=dict)
    known: dict[str, list] = field(default_factory=dict)  # path -> [size, mtime_ns, sha]
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.state_path and self.state_path.is_file():
            try:
                self.known = json.loads(self.state_path.read_text())
            except (OSError, ValueError):
                log.warning("could not read %s; starting with no file state", self.state_path)

    # -- input -----------------------------------------------------------------------

    def notify(self, path: Path, op: str, src: Path | None = None, closed: bool = False) -> None:
        """Record that ``path`` changed (op: created|modified|deleted|moved).

        ``closed`` marks a close-after-write: the change is complete, so it settles in
        0.1 s instead of the full debounce, keeping quick successive runs apart.
        """
        if self.rules.ignored(path):
            return
        now = self.clock()
        key = str(path)
        with self._lock:
            p = self.pending.get(key)
            if p is None:
                p = self.pending[key] = _Pending(op, now, now, str(src) if src else None)
            else:
                p.last = now
                p.not_before = 0.0
                p.closed = False
                if op in ("deleted", "moved", "created"):
                    p.op = op
                if src:
                    p.src = str(src)
            if closed:
                p.closed = True

    # -- output ----------------------------------------------------------------------

    def process_due(self, flush: bool = False) -> int:
        """Emit every pending path that has settled. Returns the number emitted.

        ``flush`` (on shutdown) stops waiting for quiet periods, but large files that
        are still being written are left alone: the next start's baseline scan
        records them once they are complete.
        """
        now = self.clock()
        with self._lock:
            due = [
                (k, p)
                for k, p in self.pending.items()
                if flush or (now - p.last >= self._quiet_for(k, p) and now >= p.not_before)
            ]
            for k, _ in due:
                del self.pending[k]
        emitted = 0
        for key, p in due:
            if self._emit(Path(key), p, now, skip_settle=False):
                emitted += 1
        return emitted

    def _quiet_for(self, key: str, p: _Pending) -> float:
        if key.endswith(".cpt"):
            return self.cpt_quiet
        return min(self.debounce, CLOSED_S) if p.closed else self.debounce

    def _requeue(self, key: str, p: _Pending, now: float) -> None:
        p.not_before = now + RECHECK_S
        with self._lock:
            self.pending.setdefault(key, p)

    def _emit(self, path: Path, p: _Pending, now: float, skip_settle: bool) -> bool:
        key = str(path)
        observed = format_ts(datetime.fromtimestamp(p.last, UTC))
        try:
            st = path.stat()
        except FileNotFoundError:
            # Only files we have seen: sed -i and editors delete temp files we never knew.
            if key in self.known:
                self.known.pop(key, None)
                self.writer.write({"path": key, "op": "deleted", "ts": observed})
                return True
            return False  # created and gone again before it settled
        except OSError as exc:
            log.warning("cannot stat %s: %s", path, exc)
            return False
        if not path.is_file():
            return False

        large = st.st_size > self.large_bytes
        if large and not skip_settle and (now - st.st_mtime < self.settle or self.is_open(path)):
            self._requeue(key, p, now)
            return False

        prev = self.known.get(key)
        if prev and prev[0] == st.st_size and prev[1] == st.st_mtime_ns and p.op != "moved":
            return False  # nothing was written (e.g. an attribute change)
        try:
            fp = fingerprint(path, objects_dir=self.objects_dir, large_bytes=self.large_bytes)
        except OSError as exc:
            log.warning("cannot fingerprint %s: %s", path, exc)
            return False
        self.known[key] = [fp.size, fp.mtime_ns, fp.sha256]
        op = "created" if prev is None and p.op == "modified" else p.op
        event = {
            "path": key,
            "op": op,
            "sha256": fp.sha256,
            "size": fp.size,
            "mtime": format_ts(datetime.fromtimestamp(fp.mtime_ns / 1e9, UTC)),
            "fp": fp.method,
            "stored": fp.stored,
            "ts": self._change_time(p, fp.mtime_ns / 1e9, observed),
        }
        if p.src:
            event["src_path"] = p.src
        self.writer.write(event)
        return True

    @staticmethod
    def _change_time(p: _Pending, mtime: float, observed: str) -> str:
        """When the change happened: the file's mtime if it is fresh, else when we saw it.

        Event delivery can lag the write by a fraction of a second, enough to land in
        the next command's window. A fresh mtime is the kernel's write time. An mtime
        much older than the event is a preserved timestamp (cp -p, rsync -a, tar), so
        the observation time is used instead.
        """
        if p.op != "scanned" and p.last - MTIME_TRUST_S <= mtime <= p.last + 0.5:
            return format_ts(datetime.fromtimestamp(mtime, UTC))
        return observed

    def save_state(self) -> None:
        """Persist what has been fingerprinted, so a restart does not re-hash everything."""
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.known))
        os.replace(tmp, self.state_path)

    # -- scanning --------------------------------------------------------------------

    def scan(self, root: Path, since: float | None = None) -> int:
        """Fingerprint every file under ``root`` that changed since it was last seen.

        With ``since`` (epoch seconds), only files modified after it are considered.
        Large files are fingerprinted immediately (a scan does not wait for settling).
        Returns the number of events emitted.
        """
        emitted = 0
        for path in walk(root, self.rules):
            try:
                st = path.stat()
            except OSError:
                continue
            if since is not None and st.st_mtime <= since:
                continue
            prev = self.known.get(str(path))
            if prev and prev[0] == st.st_size and prev[1] == st.st_mtime_ns:
                continue
            p = _Pending("scanned", st.st_mtime, st.st_mtime)
            if self._emit(path, p, self.clock(), skip_settle=True):
                emitted += 1
        return emitted


def watch(
    tracker: FileTracker,
    roots: list[Path],
    stop: threading.Event,
    baseline: bool = False,
    on_ready: Callable[[int], None] | None = None,
) -> None:
    """Run watchdog on ``roots`` until ``stop`` is set, feeding ``tracker``.

    With ``baseline``, first fingerprint files that changed while nothing was watching
    (changes made during that scan are queued and handled right after it).
    ``on_ready`` is called with the number of baseline events once watching is live.
    """
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer

    ops = {
        "created": "created",
        "modified": "modified",
        "deleted": "deleted",
        "moved": "moved",
        "closed": "modified",  # IN_CLOSE_WRITE
    }

    class Handler(FileSystemEventHandler):
        def on_any_event(self, event: FileSystemEvent) -> None:
            if event.is_directory or event.event_type not in ops:
                return
            op = ops[event.event_type]
            if op == "moved":
                src = Path(os.fsdecode(event.src_path))
                tracker.notify(src, "deleted")
                tracker.notify(Path(os.fsdecode(event.dest_path)), "moved", src=src)
            else:
                closed = event.event_type == "closed"
                tracker.notify(Path(os.fsdecode(event.src_path)), op, closed=closed)

    observer = Observer()
    for root in roots:
        observer.schedule(Handler(), str(root), recursive=True)
    observer.start()
    found = 0
    if baseline:
        for root in roots:
            found += tracker.scan(root)
        tracker.save_state()
    if on_ready:
        on_ready(found)
    last_save = time.monotonic()
    try:
        while not stop.wait(0.05):
            if tracker.process_due() or time.monotonic() - last_save > 60:
                tracker.save_state()
                last_save = time.monotonic()
    finally:
        observer.stop()
        observer.join(timeout=5)
        tracker.process_due(flush=True)
        tracker.save_state()
