"""Database connection and numbered migrations."""

import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

from cairnlab.store.models import utc_now

log = logging.getLogger(__name__)

_MIGRATION_NAME = re.compile(r"^(\d+)_[\w-]+\.sql$")


def connect(path: Path | str) -> sqlite3.Connection:
    """Open the Cairn database at ``path`` in WAL mode with foreign keys on.

    The connection is in autocommit mode; use :func:`transaction` to group writes.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run the block in one transaction: commit on success, roll back on error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def available_migrations() -> list[tuple[int, str, str]]:
    """Return ``(version, filename, sql)`` for every bundled migration, in order."""
    found = []
    for entry in resources.files("cairnlab.store.migrations").iterdir():
        m = _MIGRATION_NAME.match(entry.name)
        if m:
            found.append((int(m.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    return sorted(found)


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply every migration not yet recorded in ``schema_migrations``.

    Returns the versions applied by this call (empty when already up to date).
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations"
        " (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied TEXT NOT NULL)"
    )
    done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    applied = []
    for version, name, sql in available_migrations():
        if version in done:
            continue
        log.info("applying migration %s", name)
        # executescript() commits implicitly, so wrap the script in its own BEGIN/COMMIT.
        now = utc_now()
        try:
            conn.executescript(
                f"BEGIN;\n{sql}\n"
                f"INSERT INTO schema_migrations (version, name, applied)"
                f" VALUES ({version}, '{name}', '{now}');\nCOMMIT;"
            )
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(version)
    return applied
