"""Typed queries over the Cairn store."""

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class HostSummary:
    """Event count and latest event time for one host."""

    host: str
    events: int
    last_ts: str


def count(conn: sqlite3.Connection, table: str) -> int:
    """Number of rows in ``table`` (one of the known Cairn tables)."""
    if table not in {"events", "runs", "artifacts", "artifact_versions", "edges", "notes"}:
        raise ValueError(f"unknown table {table!r}")
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def hosts(conn: sqlite3.Connection) -> list[HostSummary]:
    """Per-host event counts and the time of each host's latest event."""
    rows = conn.execute(
        "SELECT COALESCE(host, '?'), COUNT(*), MAX(ts) FROM events GROUP BY host ORDER BY host"
    )
    return [HostSummary(host=h, events=n, last_ts=ts) for h, n, ts in rows]


def ingest_offsets(conn: sqlite3.Connection) -> dict[str, int]:
    """Byte offset reached so far in each ingested JSONL file, keyed by file name."""
    return {f: off for f, off in conn.execute("SELECT file, offset FROM ingest_state")}


def set_ingest_offset(conn: sqlite3.Connection, file: str, offset: int, updated: str) -> None:
    """Record how far ``file`` has been ingested."""
    conn.execute(
        "INSERT INTO ingest_state (file, offset, updated) VALUES (?, ?, ?)"
        " ON CONFLICT(file) DO UPDATE SET offset = excluded.offset, updated = excluded.updated",
        (file, offset, updated),
    )
