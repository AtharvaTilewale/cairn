"""JSONL events to the ``events`` and ``runs`` tables (idempotent).

Each ``events/*.jsonl`` file is read from the byte offset stored in ``ingest_state``,
and only complete lines are consumed, so a hook that is mid-write is never cut off
and re-running ingest adds nothing twice. A unique index on the hook's own event id
guards against duplicates even if offsets are lost or two files repeat an event.
"""

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import ValidationError
from ulid import ULID

from cairnlab.capture.envsnap import EnvSnapshotter
from cairnlab.capture.gitsnap import GitSnapshotter
from cairnlab.provenance.objects import has_object
from cairnlab.store import db, repo
from cairnlab.store.models import (
    CmdEvent,
    FileEvent,
    NoteEvent,
    RunEnd,
    RunStart,
    format_ts,
    utc_now,
)

log = logging.getLogger(__name__)

# Same rule as the bash hook, applied again here as a second line of defence.
_SECRET = re.compile(r"KEY.*=|TOKEN|PASSWORD|SECRET", re.IGNORECASE)
REDACTED = "[redacted]"


def looks_secret(cmd: str) -> bool:
    """True if a command looks like it carries a credential."""
    return bool(_SECRET.search(cmd))


_KINDS = {
    "figure": {".png", ".jpg", ".jpeg", ".svg", ".gif", ".tif", ".tiff", ".eps", ".pdf"},
    "config": {".mdp", ".yaml", ".yml", ".toml", ".json", ".ini", ".cfg", ".conf"},
    "script": {".py", ".sh", ".bash", ".r", ".jl", ".pl", ".ipynb", ".nf", ".smk"},
    "log": {".log", ".out", ".err"},
    "data": {
        ".csv",
        ".tsv",
        ".txt",
        ".dat",
        ".xvg",
        ".npy",
        ".npz",
        ".h5",
        ".hdf5",
        ".parquet",
        ".pdb",
        ".gro",
        ".cif",
        ".mmcif",
        ".sdf",
        ".mol2",
        ".xtc",
        ".trr",
        ".dcd",
        ".edr",
        ".tpr",
        ".top",
        ".itp",
        ".ndx",
        ".cpt",
        ".fasta",
        ".fa",
        ".fastq",
        ".bam",
        ".vcf",
    },
}


def artifact_kind(path: str) -> str:
    """Classify a file as data|figure|config|script|log|other from its name."""
    name = path.rsplit("/", 1)[-1].lower()
    if name.startswith("params") and name.endswith(".txt"):
        return "config"
    suffix = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    for kind, suffixes in _KINDS.items():
        if suffix in suffixes:
            return kind
    return "other"


@dataclass
class Snapshots:
    """Git/env snapshotters for runs recorded on ``host`` (this machine).

    Runs from other hosts are never snapshotted here: their paths are not ours.
    """

    host: str
    git: GitSnapshotter
    env: EnvSnapshotter

    def apply(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        host: str,
        cwd: str,
        ts_start: str,
        ts_end: str,
        env_prefix: str | None,
    ) -> None:
        if host != self.host:
            return
        git = self.git.snapshot(cwd, ts_start, ts_end)
        env_hash = self.env.env_hash(env_prefix, ts_end) if env_prefix else None
        if git is None and env_hash is None:
            return
        conn.execute(
            "UPDATE runs SET git_commit = ?, git_branch = ?, git_dirty = ?, git_diffstat = ?,"
            " env_hash = ? WHERE id = ?",
            (
                git.commit if git else None,
                git.branch if git else None,
                None if git is None or git.dirty is None else int(git.dirty),
                git.diffstat if git else None,
                env_hash,
                run_id,
            ),
        )


@dataclass
class IngestStats:
    """What one ingest pass did."""

    files: int = 0  # JSONL files read
    lines: int = 0
    runs: int = 0  # cmd events and background-run starts stored
    files_seen: int = 0  # file events stored
    notes: int = 0
    earliest: str | None = None  # earliest time any newly stored event touches
    duplicates: int = 0
    malformed: int = 0
    unsupported: int = 0
    lost: int = 0  # background runs found dead without an end event


@dataclass
class _Pass:
    conn: sqlite3.Connection
    stats: IngestStats
    objects_dir: Path | None
    snapshots: Snapshots | None

    def touch(self, ts: str) -> None:
        if self.stats.earliest is None or ts < self.stats.earliest:
            self.stats.earliest = ts


MODELS = {
    "cmd": CmdEvent,
    "file": FileEvent,
    "run_start": RunStart,
    "run_end": RunEnd,
    "note": NoteEvent,
}
NOTE_SESSION_WINDOW = timedelta(hours=6)  # a note explains a run at most this long before it
NOTE_HOST_WINDOW = timedelta(hours=1)


def ingest(
    conn: sqlite3.Connection,
    events_dir: Path,
    objects_dir: Path | None = None,
    snapshots: Snapshots | None = None,
    lost_check_host: str | None = None,
) -> IngestStats:
    """Ingest every new complete line of every ``*.jsonl`` file in ``events_dir``.

    ``objects_dir`` is this store's object store; versions found there are marked stored.
    ``snapshots`` adds git and environment state to runs recorded on this machine.
    ``lost_check_host``: background runs on this host whose monitor is gone are
    marked ``lost``.
    """
    stats = IngestStats()
    ctx = _Pass(conn, stats, objects_dir, snapshots)
    if events_dir.is_dir():
        offsets = repo.ingest_offsets(conn)
        # Recursive: synced spokes live in events/remote/<alias>/. Offsets are keyed by
        # the path relative to events_dir (a bare name for the hub's own files).
        for path in sorted(events_dir.rglob("*.jsonl")):
            key = path.relative_to(events_dir).as_posix()
            _ingest_file(ctx, path, key, offsets.get(key, 0))
    if lost_check_host:
        stats.lost = _mark_lost(conn, lost_check_host)
    _attach_notes(conn)
    return stats


def _ingest_file(ctx: _Pass, path: Path, key: str, offset: int) -> None:
    size = path.stat().st_size
    if size < offset:
        log.warning("%s shrank (%d < %d bytes); re-reading from the start", path.name, size, offset)
        offset = 0
    if size == offset:
        return
    with path.open("rb") as fh:
        fh.seek(offset)
        data = fh.read(size - offset)
    end = data.rfind(b"\n")
    if end < 0:
        return  # only a partial line so far
    ctx.stats.files += 1
    with db.transaction(ctx.conn):
        pos = offset
        for n, raw in enumerate(data[: end + 1].splitlines(keepends=True), start=1):
            _ingest_line(ctx, raw, f"{key} line {n} of new data (byte {pos})")
            pos += len(raw)
        repo.set_ingest_offset(ctx.conn, key, offset + end + 1, utc_now())


def _ingest_line(ctx: _Pass, raw: bytes, where: str) -> None:
    stats = ctx.stats
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return
    stats.lines += 1
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        stats.malformed += 1
        log.warning("%s: not JSON (%s); skipped", where, exc.msg)
        return
    if not isinstance(obj, dict):
        stats.malformed += 1
        log.warning("%s: not a JSON object; skipped", where)
        return
    kind = obj.get("type")
    model = MODELS.get(kind)
    if model is None:
        stats.unsupported += 1
        log.info("%s: event type %r not ingested yet; skipped", where, kind)
        return
    try:
        event = model.model_validate(obj)
    except ValidationError as exc:
        stats.malformed += 1
        problems = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or 'event'}: {e['msg']}" for e in exc.errors()
        )
        log.warning("%s: invalid %s event (%s); skipped", where, kind, problems)
        return

    stored = {
        CmdEvent: _on_cmd,
        FileEvent: _on_file,
        RunStart: _on_run_start,
        RunEnd: _on_run_end,
        NoteEvent: _on_note,
    }[type(event)](ctx, event)
    if not stored:
        stats.duplicates += 1


def _on_cmd(ctx: _Pass, event: CmdEvent) -> bool:
    if looks_secret(event.cmd):
        event.cmd = REDACTED
    run_id = _store_cmd(ctx.conn, event)
    if not run_id:
        return False
    ctx.stats.runs += 1
    ctx.touch(event.ts_start)
    if ctx.snapshots:
        ctx.snapshots.apply(
            ctx.conn, run_id, event.host, event.cwd, event.ts_start, event.ts_end, event.env_prefix
        )
    return True


def _on_file(ctx: _Pass, event: FileEvent) -> bool:
    if not _store_file(ctx.conn, event, ctx.objects_dir):
        return False
    ctx.stats.files_seen += 1
    ctx.touch(event.ts)
    return True


def _on_run_start(ctx: _Pass, event: RunStart) -> bool:
    """A background run began: a run row with no end yet (status running)."""
    if looks_secret(event.cmd):
        event.cmd = REDACTED
    if (
        _insert_event(
            ctx.conn,
            "run_start",
            event.ts_start,
            event.host,
            event.model_dump(),
            event_id=event.run_id,
        )
        is None
    ):
        return False
    ctx.conn.execute(
        "INSERT INTO runs (id, session, host, shell, cwd, cmd, ts_start, user, env_name, status)"
        " VALUES (?, ?, ?, 'cairn-run', ?, ?, ?, ?, ?, 'running')",
        (
            event.run_id,
            event.session,
            event.host,
            event.cwd,
            event.cmd,
            event.ts_start,
            event.user,
            event.conda_env or _env_basename(event.env_prefix),
        ),
    )
    ctx.stats.runs += 1
    ctx.touch(event.ts_start)
    if ctx.snapshots:
        ctx.snapshots.apply(
            ctx.conn,
            event.run_id,
            event.host,
            event.cwd,
            event.ts_start,
            event.ts_start,
            event.env_prefix,
        )
    # The end may have been ingested first (e.g. from another file).
    row = ctx.conn.execute(
        "SELECT payload FROM events WHERE type = 'run_end'"
        " AND json_extract(payload, '$.run_id') = ?",
        (event.run_id,),
    ).fetchone()
    if row:
        _finish_run(ctx, RunEnd.model_validate_json(row[0]))
    return True


def _on_run_end(ctx: _Pass, event: RunEnd) -> bool:
    if _insert_event(ctx.conn, "run_end", event.ts_end, event.host, event.model_dump()) is None:
        return False
    _finish_run(ctx, event)
    return True


def _finish_run(ctx: _Pass, event: RunEnd) -> None:
    cur = ctx.conn.execute(
        "UPDATE runs SET ts_end = ?, exit = ?, status = 'done' WHERE id = ?",
        (event.ts_end, event.exit, event.run_id),
    )
    if not cur.rowcount:
        return  # the start has not arrived yet; _on_run_start will finish it
    # The linker reads the wrapper's exact outputs from the run's own event payload.
    ctx.conn.execute(
        "UPDATE events SET payload = json_set(payload, '$.wrapper', json(?)) WHERE id = ?",
        (json.dumps(event.wrapper), event.run_id),
    )
    (start,) = ctx.conn.execute(
        "SELECT ts_start FROM runs WHERE id = ?", (event.run_id,)
    ).fetchone()
    ctx.touch(start)


def _on_note(ctx: _Pass, event: NoteEvent) -> bool:
    if (
        _insert_event(ctx.conn, "note", event.ts, event.host, event.model_dump(), event_id=event.id)
        is None
    ):
        return False
    ctx.conn.execute(
        "INSERT INTO notes (id, ts, run_id, text, source, audio_path) VALUES (?, ?, NULL, ?, ?, ?)",
        (event.id, event.ts, event.text, event.source, event.audio),
    )
    ctx.stats.notes += 1
    ctx.touch(event.ts)
    return True


def _attach_notes(conn: sqlite3.Connection) -> None:
    """Pin each unattached note to the run it explains.

    Notes and runs live in different files and arrive in any order, so this runs after
    every ingest. The explicit ``run`` wins; else the last run of the same terminal
    session that finished before the note (within 6 h); else the same for the user on
    the host (within 1 h). A run still going at the note's time is a last resort.
    """
    rows = conn.execute(
        "SELECT n.id, n.ts, e.payload FROM notes n JOIN events e ON e.id = n.id"
        " WHERE n.run_id IS NULL"
    ).fetchall()
    for note_id, ts, payload in rows:
        p = json.loads(payload)
        run_id = None
        explicit = (p.get("run") or "").removeprefix("R:")
        if explicit and conn.execute("SELECT 1 FROM runs WHERE id = ?", (explicit,)).fetchone():
            run_id = explicit
        if run_id is None and p.get("session"):
            run_id = _latest_run(conn, "session = ?", p["session"], ts, NOTE_SESSION_WINDOW)
        if run_id is None:
            run_id = _latest_run(
                conn, "host = ? AND user = ?", (p["host"], p["user"]), ts, NOTE_HOST_WINDOW
            )
        if run_id is not None:
            conn.execute("UPDATE notes SET run_id = ? WHERE id = ?", (run_id, note_id))


def _latest_run(conn, where: str, args, ts: str, window: timedelta) -> str | None:
    """The run a note typed at ``ts`` explains: the last one started among those that
    finished before it (the command the user typed last).

    A background job still running when the note is typed is not "the last command",
    even if its monitor recorded a later start, and neither is an older job that
    happened to finish just before the note; a running job is only chosen when
    nothing finished before the note.
    """
    args = args if isinstance(args, tuple) else (args,)
    earliest = format_ts(datetime.fromisoformat(ts) - window)
    row = conn.execute(
        f"SELECT id FROM runs WHERE {where} AND ts_end IS NOT NULL AND ts_end <= ?"
        " AND ts_start >= ? ORDER BY ts_start DESC LIMIT 1",
        (*args, ts, earliest),
    ).fetchone()
    if row is None:
        row = conn.execute(
            f"SELECT id FROM runs WHERE {where} AND ts_start <= ? AND ts_start >= ?"
            " ORDER BY ts_start DESC LIMIT 1",
            (*args, ts, earliest),
        ).fetchone()
    return row[0] if row else None


def _mark_lost(conn: sqlite3.Connection, host: str) -> int:
    """Mark running background runs on ``host`` whose monitor process is gone."""
    from cairnlab.capture.longrun import monitor_alive

    lost = 0
    rows = conn.execute(
        "SELECT r.id, json_extract(e.payload, '$.monitor_pid') FROM runs r"
        " JOIN events e ON e.id = r.id WHERE r.status = 'running' AND r.host = ?",
        (host,),
    ).fetchall()
    for run_id, pid in rows:
        if pid is None or not monitor_alive(int(pid), run_id):
            conn.execute("UPDATE runs SET status = 'lost' WHERE id = ?", (run_id,))
            lost += 1
    return lost


def _insert_event(
    conn: sqlite3.Connection,
    kind: str,
    ts: str,
    host: str,
    obj: dict,
    event_id: str | None = None,
) -> str | None:
    """Insert an events row; returns its id, or None if the hook id was seen before."""
    event_id = event_id or str(ULID.from_datetime(datetime.fromisoformat(ts)))
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    cur = conn.execute(
        "INSERT OR IGNORE INTO events (id, type, ts, host, payload) VALUES (?, ?, ?, ?, ?)",
        (event_id, kind, ts, host, payload),
    )
    return event_id if cur.rowcount else None


def _store_file(conn: sqlite3.Connection, event: FileEvent, objects_dir: Path | None) -> bool:
    """Record a file change: artifact, version (deduplicated by hash) and change row."""
    event_id = _insert_event(conn, "file", event.ts, event.host, event.model_dump())
    if event_id is None:
        return False
    conn.execute(
        "INSERT OR IGNORE INTO artifacts (id, host, path, kind) VALUES (?, ?, ?, ?)",
        (str(ULID()), event.host, event.path, artifact_kind(event.path)),
    )
    (artifact_id,) = conn.execute(
        "SELECT id FROM artifacts WHERE host = ? AND path = ?", (event.host, event.path)
    ).fetchone()
    version_id = None
    if event.sha256 is not None:
        stored = int(objects_dir is not None and has_object(objects_dir, event.sha256))
        conn.execute(
            "INSERT OR IGNORE INTO artifact_versions"
            " (id, artifact_id, sha256, size, ts, stored, fp, mtime)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(ULID()),
                artifact_id,
                event.sha256,
                event.size,
                event.ts,
                stored,
                event.fp or "full",
                event.mtime,
            ),
        )
        version_id, was_stored = conn.execute(
            "SELECT id, stored FROM artifact_versions WHERE artifact_id = ? AND sha256 = ?",
            (artifact_id, event.sha256),
        ).fetchone()
        if stored and not was_stored:
            conn.execute("UPDATE artifact_versions SET stored = 1 WHERE id = ?", (version_id,))
    conn.execute(
        "INSERT INTO file_events (event_id, artifact_id, version_id, op, ts, mtime)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (event_id, artifact_id, version_id, event.op, event.ts, event.mtime),
    )
    return True


def _store_cmd(conn: sqlite3.Connection, event: CmdEvent) -> str | None:
    """Insert the event and its run. Returns the run id, or None if already stored."""
    run_id = _insert_event(conn, "cmd", event.ts_start, event.host, event.model_dump())
    if run_id is None:
        return None
    conn.execute(
        "INSERT INTO runs (id, session, host, shell, cwd, cmd, ts_start, ts_end, exit, user,"
        " env_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            event.session,
            event.host,
            event.shell,
            event.cwd,
            event.cmd,
            event.ts_start,
            event.ts_end,
            event.exit,
            event.user,
            event.conda_env or _env_basename(event.env_prefix),
        ),
    )
    return run_id


def _env_basename(prefix: str | None) -> str | None:
    return prefix.rstrip("/").rsplit("/", 1)[-1] if prefix else None
