"""Tests for the SQLite connection and migration runner."""

import sqlite3

import pytest

from cairnlab.store import db

EXPECTED_TABLES = {
    "events",
    "runs",
    "artifacts",
    "artifact_versions",
    "edges",
    "notes",
    "entries",
    "ingest_state",
    "file_events",
    "schema_migrations",
}


def tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def test_migrate_creates_all_tables(tmp_path):
    conn = db.connect(tmp_path / "cairn.db")
    applied = db.migrate(conn)
    assert applied == [v for v, _, _ in db.available_migrations()]
    assert tables(conn) >= EXPECTED_TABLES


def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "cairn.db"
    conn = db.connect(path)
    db.migrate(conn)
    conn.close()
    conn = db.connect(path)
    assert db.migrate(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == len(
        db.available_migrations()
    )


def test_wal_and_foreign_keys(tmp_path):
    conn = db.connect(tmp_path / "cairn.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_event_source_id_is_unique(tmp_path):
    """The original hook id inside the payload may only be stored once."""
    conn = db.connect(tmp_path / "cairn.db")
    db.migrate(conn)
    sql = "INSERT INTO events (id, type, ts, host, payload) VALUES (?, 'cmd', 't', 'h', ?)"
    conn.execute(sql, ("A", '{"id": "s-1"}'))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, ("B", '{"id": "s-1"}'))


def test_migrations_are_numbered_and_ordered():
    migs = db.available_migrations()
    versions = [v for v, _name, _sql in migs]
    assert versions == sorted(versions)
    assert versions[0] == 1
    assert len(set(versions)) == len(versions)


def test_transaction_rolls_back(tmp_path):
    conn = db.connect(tmp_path / "cairn.db")
    db.migrate(conn)
    with pytest.raises(RuntimeError), db.transaction(conn):
        conn.execute("INSERT INTO ingest_state (file, offset, updated) VALUES ('f', 1, 't')")
        raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM ingest_state").fetchone()[0] == 0


def test_migration_002_backfills_run_user(tmp_path):
    """A store created at 001 keeps its data and gains runs.user from event payloads."""
    conn = db.connect(tmp_path / "cairn.db")
    conn.execute(
        "CREATE TABLE schema_migrations"
        " (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied TEXT NOT NULL)"
    )
    version, name, sql = db.available_migrations()[0]
    conn.executescript(sql)
    conn.execute("INSERT INTO schema_migrations VALUES (1, ?, 't')", (name,))
    conn.execute(
        "INSERT INTO events VALUES ('R1', 'cmd', 't', 'hp', ?)",
        ('{"id": "s-1", "user": "alice"}',),
    )
    conn.execute("INSERT INTO runs (id, cmd) VALUES ('R1', 'echo hi')")
    assert db.migrate(conn) == [v for v, _, _ in db.available_migrations()[1:]]
    assert tuple(conn.execute("SELECT user, cmd FROM runs").fetchone()) == ("alice", "echo hi")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(artifacts)")}
    assert {"host", "path", "kind"} <= cols
