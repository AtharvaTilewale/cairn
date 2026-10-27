"""Pydantic models for hook events and database rows.

All timestamps are UTC ISO-8601 strings with millisecond precision and a ``Z`` suffix,
e.g. ``2026-10-28T09:14:03.120Z``.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def format_ts(dt: datetime) -> str:
    """Format an aware datetime as ``YYYY-MM-DDTHH:MM:SS.mmmZ`` in UTC."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def utc_now() -> str:
    """Return the current time in Cairn's timestamp format."""
    return format_ts(datetime.now(UTC))


def normalize_ts(value: str) -> str:
    """Parse an ISO-8601 timestamp with a timezone and return it in Cairn's format.

    Raises ``ValueError`` for unparseable or timezone-naive input.
    """
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp has no timezone: {value!r}")
    return format_ts(dt)


Timestamp = Annotated[str, AfterValidator(normalize_ts)]
NonEmpty = Annotated[str, Field(min_length=1)]


class CmdEvent(BaseModel):
    """One shell command, as written by the bash hook (CLAUDE.md section 5.1)."""

    # Unknown fields are kept so newer hooks do not lose data on older ingesters.
    model_config = ConfigDict(extra="allow")

    v: Literal[1]
    type: Literal["cmd"]
    id: NonEmpty
    session: NonEmpty
    host: NonEmpty
    shell: str
    user: NonEmpty
    cwd: str
    cmd: str
    ts_start: Timestamp
    ts_end: Timestamp
    exit: int


class Event(BaseModel):
    """A row of the ``events`` table."""

    id: str
    type: str
    ts: str
    host: str | None
    payload: str


class Run(BaseModel):
    """A row of the ``runs`` table: one execution of a command."""

    id: str
    session: str | None = None
    host: str | None = None
    shell: str | None = None
    cwd: str | None = None
    cmd: str | None = None
    ts_start: str | None = None
    ts_end: str | None = None
    exit: int | None = None
    git_commit: str | None = None
    git_dirty: int | None = None
    env_hash: str | None = None
    phase: str | None = None
