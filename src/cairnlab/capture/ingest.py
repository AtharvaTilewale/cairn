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
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError
from ulid import ULID

from cairnlab.capture.envsnap import EnvSnapshotter
from cairnlab.capture.gitsnap import GitSnapshotter
from cairnlab.provenance.objects import has_object
from cairnlab.store import db, repo
from cairnlab.store.models import CmdEvent, FileEvent, utc_now

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

    def apply(self, conn: sqlite3.Connection, run_id: str, event: CmdEvent) -> None:
        if event.host != self.host:
            return
        git = self.git.snapshot(event.cwd, event.ts_start, event.ts_end)
        env_hash = self.env.env_hash(event.env_prefix, event.ts_end) if event.env_prefix else None
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
    runs: int = 0  # cmd events stored
    files_seen: int = 0  # file events stored
    duplicates: int = 0
    malformed: int = 0
    unsupported: int = 0


def ingest(
    conn: sqlite3.Connection,
    events_dir: Path,
    objects_dir: Path | None = None,
    snapshots: Snapshots | None = None,
) -> IngestStats:
    """Ingest every new complete line of every ``*.jsonl`` file in ``events_dir``.

    ``objects_dir`` is this store's object store; versions found there are marked stored.
    ``snapshots`` adds git and environment state to runs recorded on this machine.
    """
    stats = IngestStats()
    if not events_dir.is_dir():
        return stats
    offsets = repo.ingest_offsets(conn)
    for path in sorted(events_dir.glob("*.jsonl")):
        offset = offsets.get(path.name, 0)
        size = path.stat().st_size
        if size < offset:
            log.warning(
                "%s shrank (%d < %d bytes); re-reading from the start", path.name, size, offset
            )
            offset = 0
        if size == offset:
            continue
        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read(size - offset)
        end = data.rfind(b"\n")
        if end < 0:
            continue  # only a partial line so far
        stats.files += 1
        with db.transaction(conn):
            pos = offset
            for n, raw in enumerate(data[: end + 1].splitlines(keepends=True), start=1):
                where = f"{path.name} line {n} of new data (byte {pos})"
                _ingest_line(conn, raw, where, stats, objects_dir, snapshots)
                pos += len(raw)
            repo.set_ingest_offset(conn, path.name, offset + end + 1, utc_now())
    return stats


def _ingest_line(
    conn: sqlite3.Connection,
    raw: bytes,
    where: str,
    stats: IngestStats,
    objects_dir: Path | None,
    snapshots: Snapshots | None,
) -> None:
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
    model = {"cmd": CmdEvent, "file": FileEvent}.get(kind)
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

    if isinstance(event, CmdEvent):
        if looks_secret(event.cmd):
            event.cmd = REDACTED
        run_id = _store_cmd(conn, event)
        if run_id:
            stats.runs += 1
            if snapshots:
                snapshots.apply(conn, run_id, event)
        else:
            stats.duplicates += 1
    elif _store_file(conn, event, objects_dir):
        stats.files_seen += 1
    else:
        stats.duplicates += 1


def _insert_event(conn: sqlite3.Connection, kind: str, ts: str, host: str, obj: dict) -> str | None:
    """Insert an events row; returns its new ULID, or None if the hook id was seen before."""
    event_id = str(ULID.from_datetime(datetime.fromisoformat(ts)))
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
