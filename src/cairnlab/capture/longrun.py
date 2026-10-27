"""``cairn run --bg -- <cmd>``: a detached long run with start and end events (5.5).

The CLI starts a monitor (``python -m cairnlab.capture.longrun <spec>``) in its own
session with stdout/stderr going to ``.cairn/logs/<run_id>.log``, and returns at once.
The monitor snapshots the project, starts the command, writes ``run_start`` (with
both PIDs), waits, records the exact outputs like ``cairn run``, and writes
``run_end``. If the machine reboots mid-run, ingest finds a ``run_start`` whose
monitor is gone and marks the run ``lost``.
"""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from ulid import ULID

from cairnlab.capture.runwrap import (
    WrapContext,
    command_text,
    env_fields,
    record_outputs,
    shell_argv,
    tree_state,
)
from cairnlab.config import state_dir
from cairnlab.store.models import utc_now


def log_path(home: Path, run_id: str) -> Path:
    """Where a background run's stdout and stderr go."""
    return home / "logs" / f"{run_id}.log"


def start_background(ctx: WrapContext, argv: list[str], cwd: Path) -> tuple[str, Path]:
    """Start ``argv`` detached under a monitor. Returns (run_id, log path)."""
    run_id = str(ULID())
    log = log_path(ctx.home, run_id)
    log.parent.mkdir(parents=True, exist_ok=True)
    spec_dir = state_dir(ctx.home) / "bg"
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = spec_dir / f"{run_id}.json"
    spec.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "argv": argv,
                "cwd": str(cwd),
                "home": str(ctx.home),
                "roots": [str(r) for r in ctx.roots],
                "host": ctx.host,
                "user": ctx.user,
                "session": ctx.session,
                "env": env_fields(),
            }
        )
    )
    with open(log, "ab") as fh:
        subprocess.Popen(
            [sys.executable, "-m", "cairnlab.capture.longrun", str(spec)],
            stdin=subprocess.DEVNULL,
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # survives the terminal closing
            close_fds=True,
        )
    return run_id, log


def monitor_alive(pid: int, run_id: str) -> bool:
    """True if ``pid`` is still the monitor for ``run_id`` (guards against PID reuse)."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return run_id.encode() in cmdline


def _monitor(spec_path: Path) -> int:
    spec = json.loads(spec_path.read_text())
    ctx = WrapContext(
        home=Path(spec["home"]),
        roots=[Path(r) for r in spec["roots"]],
        host=spec["host"],
        user=spec["user"],
        session=spec["session"],
    )
    run_id, argv, cwd = spec["run_id"], spec["argv"], Path(spec["cwd"])
    before = tree_state(ctx)
    ts_start = utc_now()
    writer = ctx.writer()
    try:
        child = subprocess.Popen(shell_argv(argv), cwd=cwd, stdin=subprocess.DEVNULL)
    except FileNotFoundError as exc:
        print(f"cairn: cannot start {argv[0]!r}: {exc}", flush=True)
        child = None

    def forward(signum, _frame):
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, forward)

    writer.write(
        {
            "type": "run_start",
            "id": f"{run_id}-start",
            "run_id": run_id,
            "pid": child.pid if child else None,
            "monitor_pid": os.getpid(),
            "session": ctx.session,
            "cwd": str(cwd),
            "cmd": command_text(argv),
            "ts_start": ts_start,
            **spec.get("env", {}),
        }
    )
    rc = child.wait() if child else 127
    ts_end = utc_now()
    outputs = record_outputs(ctx, before, ts_end)
    writer.write(
        {
            "type": "run_end",
            "id": f"{run_id}-end",
            "run_id": run_id,
            "exit": rc,
            "ts_end": ts_end,
            "wrapper": outputs,
        }
    )
    spec_path.unlink(missing_ok=True)
    print(f"cairn: run {run_id} finished with exit code {rc}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(_monitor(Path(sys.argv[1])))
