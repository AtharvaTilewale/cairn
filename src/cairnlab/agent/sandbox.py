"""Replay one recorded run in a scratch directory (CLAUDE.md section 10).

This is a **scratch directory, not a security sandbox**: the command runs as you,
with your environment (minus API keys), and could touch anything you can. Safety
comes from what may be replayed:

- only recorded commands, never free-form ones;
- only runs that finished and took at most ``MAX_ORIGINAL_S`` (10 min) originally;
- only when every input version has a stored copy in ``.cairn/objects``;
- a hard timeout (default 300 s, never above 600 s), killing the whole process group;
- absolute paths into the project in the command are rewritten to the scratch copy,
  so a replay cannot overwrite the real files.
"""

import os
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from cairnlab.provenance.objects import object_path

MAX_ORIGINAL_S = 600
DEFAULT_TIMEOUT_S = 300
TAIL = 3000
SCRUBBED_ENV = ("MODEL_API_KEY", "CAIRN_SESSION", "CAIRN_HOME", "OPENAI_API_KEY")


class Refused(Exception):
    """This run must not be replayed; the message says why."""


@dataclass
class Replay:
    run_id: str
    exit: int | None
    timed_out: bool
    seconds: float
    stdout_tail: str
    stderr_tail: str
    scratch: Path
    base: Path  # the recorded directory that scratch stands in for
    produced: list[str] = field(default_factory=list)  # recorded-style absolute paths

    def local(self, recorded_path: str) -> Path:
        """Where a recorded absolute path lives inside the scratch directory."""
        return self.scratch / Path(recorded_path).relative_to(self.base)


def duration_s(run) -> float | None:
    if not run["ts_end"]:
        return None
    return (
        datetime.fromisoformat(run["ts_end"]) - datetime.fromisoformat(run["ts_start"])
    ).total_seconds()


def version_paths(conn: sqlite3.Connection, vids: list[str]) -> dict[str, tuple[str, str]]:
    """version id -> (recorded path, sha256)."""
    out = {}
    for vid in vids:
        r = conn.execute(
            "SELECT a.path, v.sha256 FROM artifact_versions v JOIN artifacts a"
            " ON a.id = v.artifact_id WHERE v.id = ?",
            (vid,),
        ).fetchone()
        if r:
            out[vid] = (r[0], r[1])
    return out


def check_replayable(run, inputs: dict[str, tuple[str, str]], objects_dir: Path) -> None:
    """Raise Refused unless the run may be replayed under the rules above."""
    if run["status"] != "done" or run["ts_end"] is None:
        raise Refused(f"run {run['id']} has not finished (status {run['status']})")
    if (run["cmd"] or "").strip() in ("", "[redacted]"):
        raise Refused("the command was redacted (it looked like it held a secret)")
    secs = duration_s(run) or 0.0
    if secs > MAX_ORIGINAL_S:
        raise Refused(
            f"the original run took {secs / 60:.1f} min; only steps under"
            f" {MAX_ORIGINAL_S // 60} min are re-run (no MD or docking jobs)"
        )
    missing = [p for p, sha in inputs.values() if not object_path(objects_dir, sha).is_file()]
    if missing:
        raise Refused(
            "inputs have no stored copy (large or never fingerprinted): "
            + ", ".join(sorted(missing))
        )


def _base(run, paths: list[str]) -> Path:
    return Path(os.path.commonpath([run["cwd"], *paths])) if paths else Path(run["cwd"])


def _state(root: Path) -> dict[str, tuple[int, int]]:
    out = {}
    for p in root.rglob("*"):
        if p.is_file():
            st = p.stat()
            out[str(p)] = (st.st_size, st.st_mtime_ns)
    return out


def _tail(b: bytes) -> str:
    return b.decode("utf-8", errors="replace")[-TAIL:]


def replay(
    conn: sqlite3.Connection,
    run,
    input_vids: list[str],
    objects_dir: Path,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    scratch_parent: Path | None = None,
    output_paths: list[str] = (),
) -> Replay:
    """Restore the inputs into a fresh scratch dir and run the recorded command there.

    ``output_paths`` (the run's recorded outputs) get their parent directories made,
    as they existed when the run happened (e.g. made by an earlier ``mkdir``).
    """
    inputs = version_paths(conn, input_vids)
    check_replayable(run, inputs, objects_dir)
    timeout_s = max(1.0, min(float(timeout_s), float(MAX_ORIGINAL_S)))
    base = _base(run, [p for p, _ in inputs.values()] + list(output_paths))
    scratch = Path(tempfile.mkdtemp(prefix="cairn-reproduce-", dir=scratch_parent))
    for path, sha in inputs.values():
        dest = scratch / Path(path).relative_to(base)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(object_path(objects_dir, sha), dest)
    for out in output_paths:
        (scratch / Path(out).relative_to(base)).parent.mkdir(parents=True, exist_ok=True)
    cwd = scratch / Path(run["cwd"]).relative_to(base)
    cwd.mkdir(parents=True, exist_ok=True)
    # Absolute paths into the recorded tree point at the scratch copy instead.
    cmd = (run["cmd"] or "").replace(str(base).rstrip("/") + "/", str(scratch) + "/")
    env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV}
    before = _state(scratch)
    t0 = time.monotonic()
    proc = subprocess.Popen(
        ["bash", "-c", cmd],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
    seconds = time.monotonic() - t0
    after = _state(scratch)
    produced = sorted(
        str(base / Path(p).relative_to(scratch)) for p, sig in after.items() if before.get(p) != sig
    )
    return Replay(
        run["id"],
        None if timed_out else proc.returncode,
        timed_out,
        seconds,
        _tail(out),
        _tail(err),
        scratch,
        base,
        produced,
    )
