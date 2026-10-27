"""Pydantic models for hook events and database rows.

All timestamps are UTC ISO-8601 strings with millisecond precision and a ``Z`` suffix,
e.g. ``2026-10-28T09:14:03.120Z``.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


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
    conda_env: str | None = None  # $CONDA_DEFAULT_ENV at the time
    env_prefix: str | None = None  # $VIRTUAL_ENV, else $CONDA_PREFIX


Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class FileEvent(BaseModel):
    """One settled file change, as written by the watcher or scan (section 5.2)."""

    model_config = ConfigDict(extra="allow")

    v: Literal[1]
    type: Literal["file"]
    id: NonEmpty
    host: NonEmpty
    user: NonEmpty
    path: NonEmpty
    op: Literal["created", "modified", "deleted", "moved", "scanned"]
    sha256: Sha256 | None = None
    size: int | None = None
    mtime: Timestamp | None = None
    fp: Literal["full", "fast"] | None = None
    stored: bool = False
    ts: Timestamp
    src_path: str | None = None

    @model_validator(mode="after")
    def _content_unless_deleted(self) -> "FileEvent":
        if self.op != "deleted" and (self.sha256 is None or self.size is None):
            raise ValueError(f"a {self.op!r} file event needs sha256 and size")
        return self


RunId = Annotated[str, Field(pattern=r"^[0-9A-HJKMNP-TV-Z]{26}$")]


class RunStart(BaseModel):
    """A ``cairn run --bg`` job began (section 5.5)."""

    model_config = ConfigDict(extra="allow")

    v: Literal[1]
    type: Literal["run_start"]
    id: NonEmpty
    run_id: RunId
    pid: int | None
    monitor_pid: int
    session: str
    host: NonEmpty
    user: NonEmpty
    cwd: str
    cmd: str
    ts_start: Timestamp
    conda_env: str | None = None
    env_prefix: str | None = None


class RunEnd(BaseModel):
    """A ``cairn run --bg`` job ended, with its exact outputs."""

    model_config = ConfigDict(extra="allow")

    v: Literal[1]
    type: Literal["run_end"]
    id: NonEmpty
    run_id: RunId
    host: NonEmpty
    user: NonEmpty
    exit: int | None
    ts_end: Timestamp
    wrapper: dict = Field(default_factory=dict)


class NoteEvent(BaseModel):
    """Why: a typed or voice note, pinned at ingest to the run it explains."""

    model_config = ConfigDict(extra="allow")

    v: Literal[1]
    type: Literal["note"]
    id: RunId
    session: str | None = None
    host: NonEmpty
    user: NonEmpty
    ts: Timestamp
    text: str
    source: Literal["typed", "voice"] = "typed"
    audio: str | None = None
    run: str | None = None  # explicit "R:<id>" the note is about


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
    user: str | None = None
    git_branch: str | None = None
    git_diffstat: str | None = None
    env_name: str | None = None
    status: str = "done"  # done|running|lost
