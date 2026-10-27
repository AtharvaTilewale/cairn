"""``cairn run -- <cmd>``: run a command with a before/after snapshot of the project.

Files that are new or changed after the command are its exact outputs (the linker's
``wrapper`` method, confidence 1.0). They are fingerprinted (small ones copied to the
object store) and written as file events, followed by one ``cmd`` event whose
``wrapper.generated`` lists them. The bash hook ignores ``cairn run`` lines, so each
wrapped command is recorded once.
"""

import os
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ulid import ULID

from cairnlab.capture.fingerprint import fingerprint
from cairnlab.capture.watcher import EventWriter, rules_for, walk
from cairnlab.config import events_dir, objects_dir
from cairnlab.store.models import format_ts, utc_now

State = dict[str, tuple[int, int]]  # path -> (size, mtime_ns)


@dataclass
class WrapContext:
    """Where a wrapped run's events go and which trees it snapshots."""

    home: Path
    roots: list[Path]
    host: str
    user: str
    session: str
    rules: object = field(init=False)

    def __post_init__(self) -> None:
        self.rules = rules_for(self.roots)

    def writer(self) -> EventWriter:
        return EventWriter(events_dir(self.home), self.host, self.user, suffix="run")


def tree_state(ctx: WrapContext) -> State:
    """(size, mtime) of every non-ignored file under the context's roots."""
    state: State = {}
    for root in ctx.roots:
        for path in walk(root, ctx.rules):
            try:
                st = path.stat()
            except OSError:
                continue
            state[str(path)] = (st.st_size, st.st_mtime_ns)
    return state


def command_text(argv: list[str]) -> str:
    """How the command is recorded: a single argument is a shell line, else argv."""
    return argv[0] if len(argv) == 1 else shlex.join(argv)


def shell_argv(argv: list[str]) -> list[str]:
    """A single argument runs through bash (so pipes and redirects work)."""
    return ["bash", "-c", argv[0]] if len(argv) == 1 else argv


def record_outputs(
    ctx: WrapContext, before: State, ts: str, since: str | None = None
) -> dict[str, list[str]]:
    """Fingerprint what changed since ``before``; write file events; return the lists.

    Each change is dated by the file's mtime when that falls inside the run
    (``[since, ts]``), so a file written early in a long job is not dated at its end
    (and into another command's window); otherwise it is dated ``ts``.
    """
    after = tree_state(ctx)
    changed = sorted(p for p, sig in after.items() if before.get(p) != sig)
    deleted = sorted(p for p in before if p not in after)
    writer = ctx.writer()
    generated = []
    for path in changed:
        try:
            fp = fingerprint(Path(path), objects_dir=objects_dir(ctx.home))
        except OSError:
            continue  # vanished between the snapshot and the hash
        generated.append(path)
        mtime = format_ts(datetime.fromtimestamp(fp.mtime_ns / 1e9, UTC))
        writer.write(
            {
                "path": path,
                "op": "modified" if path in before else "created",
                "sha256": fp.sha256,
                "size": fp.size,
                "mtime": mtime,
                "fp": fp.method,
                "stored": fp.stored,
                "ts": mtime if since is not None and since <= mtime <= ts else ts,
            }
        )
    for path in deleted:
        writer.write({"path": path, "op": "deleted", "ts": ts})
    return {"generated": generated, "deleted": deleted}


def env_fields() -> dict[str, str]:
    """The environment fields the bash hook records, read from this process."""
    out = {}
    if os.environ.get("CONDA_DEFAULT_ENV"):
        out["conda_env"] = os.environ["CONDA_DEFAULT_ENV"]
    prefix = os.environ.get("VIRTUAL_ENV") or os.environ.get("CONDA_PREFIX")
    if prefix:
        out["env_prefix"] = prefix
    return out


def run_wrapped(ctx: WrapContext, argv: list[str], cwd: Path) -> int:
    """Run ``argv`` in the foreground, then record its exact outputs. Returns its exit code."""
    before = tree_state(ctx)
    ts_start = utc_now()
    try:
        proc = subprocess.Popen(shell_argv(argv), cwd=cwd)
        try:
            rc = proc.wait()
        except KeyboardInterrupt:  # the child got the same SIGINT; let it finish
            rc = proc.wait()
    except FileNotFoundError:
        rc = 127
    ts_end = utc_now()
    outputs = record_outputs(ctx, before, ts_end, since=ts_start)
    ctx.writer().write(
        {
            "type": "cmd",
            "id": f"{ctx.session}-w{ULID()}",
            "session": ctx.session,
            "shell": "cairn-run",
            "cwd": str(cwd),
            "cmd": command_text(argv),
            "ts_start": ts_start,
            "ts_end": ts_end,
            "exit": rc,
            "wrapper": outputs,
            **env_fields(),
        }
    )
    return rc
