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

from cairnlab.store import db, repo
from cairnlab.store.models import CmdEvent, utc_now

log = logging.getLogger(__name__)

# Same rule as the bash hook, applied again here as a second line of defence.
_SECRET = re.compile(r"KEY.*=|TOKEN|PASSWORD|SECRET", re.IGNORECASE)
REDACTED = "[redacted]"


def looks_secret(cmd: str) -> bool:
    """True if a command looks like it carries a credential."""
    return bool(_SECRET.search(cmd))


@dataclass
class IngestStats:
    """What one ingest pass did."""

    files: int = 0
    lines: int = 0
    runs: int = 0
    duplicates: int = 0
    malformed: int = 0
    unsupported: int = 0


def ingest(conn: sqlite3.Connection, events_dir: Path) -> IngestStats:
    """Ingest every new complete line of every ``*.jsonl`` file in ``events_dir``."""
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
                _ingest_line(conn, raw, f"{path.name} line {n} of new data (byte {pos})", stats)
                pos += len(raw)
            repo.set_ingest_offset(conn, path.name, offset + end + 1, utc_now())
    return stats


def _ingest_line(conn: sqlite3.Connection, raw: bytes, where: str, stats: IngestStats) -> None:
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
    if obj.get("type") != "cmd":
        stats.unsupported += 1
        log.info("%s: event type %r not ingested yet; skipped", where, obj.get("type"))
        return
    try:
        event = CmdEvent.model_validate(obj)
    except ValidationError as exc:
        stats.malformed += 1
        problems = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or 'event'}: {e['msg']}" for e in exc.errors()
        )
        log.warning("%s: invalid cmd event (%s); skipped", where, problems)
        return

    if looks_secret(event.cmd):
        event.cmd = REDACTED
    if _store_cmd(conn, event):
        stats.runs += 1
    else:
        stats.duplicates += 1


def _store_cmd(conn: sqlite3.Connection, event: CmdEvent) -> bool:
    """Insert the event and its run. Returns False if the event was already stored."""
    run_id = str(ULID.from_datetime(datetime.fromisoformat(event.ts_start)))
    payload = json.dumps(event.model_dump(), ensure_ascii=False, separators=(",", ":"))
    cur = conn.execute(
        "INSERT OR IGNORE INTO events (id, type, ts, host, payload) VALUES (?, 'cmd', ?, ?, ?)",
        (run_id, event.ts_start, event.host, payload),
    )
    if cur.rowcount == 0:
        return False
    conn.execute(
        "INSERT INTO runs (id, session, host, shell, cwd, cmd, ts_start, ts_end, exit)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        ),
    )
    return True
