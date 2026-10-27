"""End-to-end tests of the capture hook in a real interactive bash.

Commands are piped to ``bash --noprofile --norc --noediting -i``. Interactive mode
makes PROMPT_COMMAND and history work; ``--noediting`` turns readline off so tabs and
escape characters reach the shell literally instead of triggering completion.
Activation goes through the installed ``cairn`` console script, exactly as by hand.
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.shell,
    pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash"),
]

TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
ACTIVATE = 'export CAIRN_USER=alice; eval "$(cairn activate)"'


def bash_version() -> tuple[int, int]:
    out = subprocess.run(
        ["bash", "-c", 'echo "${BASH_VERSINFO[0]} ${BASH_VERSINFO[1]}"'],
        capture_output=True,
        text=True,
    ).stdout.split()
    return int(out[0]), int(out[1])


@dataclass
class Session:
    stdout: str
    stderr: str
    events: list[dict]
    raw: str
    cwd: Path


def run_shell(tmp_path: Path, lines: list[str], env: dict[str, str] | None = None) -> Session:
    """Pipe ``lines`` into an interactive bash in a fresh project with a Cairn store."""
    work = tmp_path / "work"
    store = work / ".cairn"
    (store / "events").mkdir(parents=True, exist_ok=True)
    full_env = {
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "HISTFILE": str(tmp_path / "bash_history"),
        "LC_ALL": "C.UTF-8",
        "TERM": "dumb",
        "CAIRN_HOST": "testhost",
    }
    full_env.update(env or {})
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "--noediting", "-i"],
        input="\n".join(lines) + "\nexit\n",
        cwd=work,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    raw = "".join(p.read_text() for p in sorted((store / "events").glob("*.jsonl")))
    events = [json.loads(line) for line in raw.splitlines()]
    return Session(proc.stdout, proc.stderr, events, raw, work)


def cmds(session: Session) -> list[str]:
    return [e["cmd"] for e in session.events]


def test_records_exit_codes_cwd_and_ignores(tmp_path):
    s = run_shell(
        tmp_path,
        [ACTIVATE, "ls", "echo hi", "false", "pwd", "mkdir sub", "cd sub", "true", "(exit 7)"],
    )
    assert cmds(s) == ["echo hi", "false", "mkdir sub", "true", "(exit 7)"]
    assert [e["exit"] for e in s.events] == [0, 1, 0, 0, 7]
    # A bare subshell never fires this shell's DEBUG trap: start is the prompt time.
    assert [e.get("ts_start_approx", False) for e in s.events] == [False] * 4 + [True]
    assert s.events[0]["cwd"] == str(s.cwd)
    assert s.events[3]["cwd"] == str(s.cwd / "sub")

    session = s.events[0]["session"]
    assert len(session) == 26  # a ULID from `cairn activate`
    assert [e["id"] for e in s.events] == [f"{session}-{i}" for i in range(1, 6)]
    for e in s.events:
        assert e["v"] == 1 and e["type"] == "cmd" and e["shell"] == "bash"
        assert e["user"] == "alice" and e["host"] == "testhost"
        assert TS.match(e["ts_start"]) and TS.match(e["ts_end"])
        assert e["ts_start"] <= e["ts_end"]
    assert "[cairn] " in s.stderr  # the prompt marker


def test_event_file_name_is_host_and_utc_day(tmp_path):
    s = run_shell(tmp_path, [ACTIVATE, "echo x"])
    files = list((s.cwd / ".cairn" / "events").glob("*.jsonl"))
    assert [f.name for f in files] == [f"testhost-{s.events[0]['ts_end'][:10]}.jsonl"]


def test_duration_covers_the_command(tmp_path):
    s = run_shell(tmp_path, [ACTIVATE, "sleep 0.4"])
    start, end = (datetime.fromisoformat(s.events[0][k]) for k in ("ts_start", "ts_end"))
    assert 0.35 <= (end - start).total_seconds() < 5


TRICKY_COMMANDS = [
    'echo "say \\"hi\\""',
    "echo 'back\\slash' \\\\ end",
    "printf '%s\\n' 'a\tb'",  # a literal tab inside the command
    "echo héllo 日本語 🎉",
    "echo '\x1b[31mred\x1b[0m'",  # a literal ESC
    "echo 'it''s' \"$HOME\" `true` $((1+1))",
]


def test_tricky_escaping(tmp_path):
    multi = "echo 'line one\nline two'"
    s = run_shell(tmp_path, [ACTIVATE, *TRICKY_COMMANDS, multi])
    assert cmds(s) == [*TRICKY_COMMANDS, multi]
    # One JSON object per physical line, even for the multi-line command.
    assert len(s.raw.splitlines()) == len(TRICKY_COMMANDS) + 1


def test_redaction(tmp_path):
    s = run_shell(
        tmp_path,
        [
            ACTIVATE,
            "export MY_API_KEY=abc123secretvalue",
            "GH_TOKEN=tok999 true",
            "echo password=hunter2",
            "echo fine",
        ],
    )
    assert cmds(s) == ["[redacted]", "[redacted]", "[redacted]", "echo fine"]
    for leaked in ("abc123secretvalue", "tok999", "hunter2"):
        assert leaked not in s.raw


def test_history_control(tmp_path):
    s = run_shell(
        tmp_path,
        [
            ACTIVATE,
            "echo one",
            " echo hidden",
            "",
            "python3 -c 'print(1)'",
            "python3 -c 'print(1)'",
            "false",
            "false",
            "echo two",
        ],
        env={"HISTCONTROL": "ignoreboth"},
    )
    # Leading space and empty Enter are skipped. An identical rerun adds no history
    # entry under ignoredups but is still a real run, so it is recorded and flagged.
    assert cmds(s) == ["echo one", *["python3 -c 'print(1)'"] * 2, "false", "false", "echo two"]
    assert [e.get("hist_repeat", False) for e in s.events] == [
        False,
        False,
        True,
        False,
        True,
        False,
    ]
    assert [e["exit"] for e in s.events] == [0, 0, 0, 1, 1, 0]


def test_hidden_command_is_not_mistaken_for_a_repeat(tmp_path):
    s = run_shell(
        tmp_path,
        [ACTIVATE, "echo visible", " echo secret-ish", " true", "echo after"],
        env={"HISTCONTROL": "ignoreboth"},
    )
    assert cmds(s) == ["echo visible", "echo after"]


def test_repeats_without_ignoredups_are_ordinary(tmp_path):
    s = run_shell(tmp_path, [ACTIVATE, "echo dup", "echo dup"], env={"HISTCONTROL": ""})
    assert cmds(s) == ["echo dup", "echo dup"]
    assert not any(e.get("hist_repeat") for e in s.events)


@pytest.mark.parametrize("user", ["alice,", "a b", "x;rm", ""])
def test_refuses_malformed_cairn_user(tmp_path, user):
    s = run_shell(
        tmp_path,
        [
            f"export CAIRN_USER='{user}'",
            'eval "$(cairn activate)"; echo "rc=$?" > rc.txt',
            "echo not-recorded",
        ],
    )
    assert (s.cwd / "rc.txt").read_text() == "rc=1\n"
    assert "CAIRN_USER" in s.stderr
    assert s.events == []


def test_preserves_and_restores_prompt_command_trap_and_ps1(tmp_path):
    s = run_shell(
        tmp_path,
        [
            "PROMPT_COMMAND='echo \"rc=$?\" >> pc.log'",
            "trap 'dbg=1' DEBUG",
            "PS1='my> '",
            '{ declare -p PROMPT_COMMAND; trap -p DEBUG; echo "PS1=$PS1"; } > before.txt',
            ACTIVATE,
            "false",
            "echo in",
            "cairn deactivate",
            "echo after",
            '{ declare -p PROMPT_COMMAND; trap -p DEBUG; echo "PS1=$PS1"; } > after.txt',
            "{ declare -F | grep __cairn_; set | grep '^__cairn_'; type -t cairn; } > left.txt",
        ],
    )
    assert cmds(s) == ["false", "echo in"]
    before = (s.cwd / "before.txt").read_text()
    assert before == (s.cwd / "after.txt").read_text()
    assert "PS1=my> " in before
    # The old PROMPT_COMMAND kept running and saw the user's exit status.
    pc = (s.cwd / "pc.log").read_text().split()
    assert "rc=1" in pc
    assert (s.cwd / "left.txt").read_text().strip() in ("", "file")
    assert "command not found" not in s.stderr


def test_eval_deactivate_from_subshell_form(tmp_path):
    s = run_shell(
        tmp_path,
        [
            ACTIVATE,
            "echo in",
            'eval "$(cairn deactivate)"',
            "echo after",
            '{ trap -p DEBUG; echo "pc=${PROMPT_COMMAND-unset}"; } > after.txt',
        ],
    )
    assert cmds(s) == ["echo in"]
    assert (s.cwd / "after.txt").read_text() == "pc=unset\n"


def test_activate_twice_installs_once(tmp_path):
    s = run_shell(
        tmp_path,
        [ACTIVATE, ACTIVATE, "echo once", "printf '%s' \"$PROMPT_COMMAND\" > pc.txt"],
    )
    # The second activation line is an ordinary recorded command; nothing is doubled.
    assert cmds(s) == [ACTIVATE, "echo once", "printf '%s' \"$PROMPT_COMMAND\" > pc.txt"]
    assert (s.cwd / "pc.txt").read_text().count("__cairn_precmd") == 1
    assert "already active" in s.stderr


def test_refuses_without_cairn_user(tmp_path):
    s = run_shell(
        tmp_path,
        [
            'eval "$(cairn activate)"; echo "rc=$?" > rc.txt',
            "echo not-recorded",
            '{ trap -p DEBUG; echo "pc=${PROMPT_COMMAND-unset}"; } > state.txt',
        ],
    )
    assert (s.cwd / "rc.txt").read_text() == "rc=1\n"
    assert "CAIRN_USER is not set" in s.stderr
    assert (s.cwd / "state.txt").read_text() == "pc=unset\n"
    assert s.events == []


def test_safe_under_nounset(tmp_path):
    s = run_shell(tmp_path, ["set -u", ACTIVATE, "echo a", "false", "cairn deactivate", "true"])
    assert cmds(s) == ["echo a", "false"]
    assert "unbound variable" not in s.stderr


def test_write_failure_is_silent(tmp_path):
    s = run_shell(
        tmp_path,
        [
            ACTIVATE,
            "chmod a-w .cairn/events",
            "echo still-works > ok.txt",
            "chmod u+w .cairn/events",
        ],
    )
    assert (s.cwd / "ok.txt").read_text() == "still-works\n"
    assert "Permission denied" not in s.stderr


@pytest.mark.skipif(bash_version() < (5, 1), reason="array PROMPT_COMMAND needs bash 5.1")
def test_array_prompt_command(tmp_path):
    s = run_shell(
        tmp_path,
        [
            "PROMPT_COMMAND=('echo \"a=$?\" >> a.log' 'echo b >> b.log')",
            "declare -p PROMPT_COMMAND > before.txt",
            ACTIVATE,
            "false",
            "echo hi",
            "cairn deactivate",
            "declare -p PROMPT_COMMAND > after.txt",
        ],
    )
    assert cmds(s) == ["false", "echo hi"]
    assert (s.cwd / "before.txt").read_text() == (s.cwd / "after.txt").read_text()
    assert "a=1" in (s.cwd / "a.log").read_text()


def test_overhead_per_prompt_is_small(tmp_path):
    n = 200
    body = ["true"] * n

    def timed(lines):
        t0 = time.perf_counter()
        run_shell(tmp_path, lines)
        return time.perf_counter() - t0

    base = timed(body)
    hooked = timed([ACTIVATE, *body])
    per_command_ms = (hooked - base) / n * 1000
    assert per_command_ms < 20, f"{per_command_ms:.1f} ms per command"


def test_done_when_scenario(tmp_path):
    """The Day 1 acceptance check, one command per prompt, then ingest and status.

    Typed as a single line, the activation line is one history entry and is never
    recorded, so the check is driven the way it is used: one command per prompt.
    """
    s = run_shell(
        tmp_path,
        [
            "cairn init > /dev/null",
            "export CAIRN_USER=alice",
            'eval "$(cairn activate)"',
            "ls",
            "echo hi",
            "false",
            "cairn ingest > ingest.txt",
            "COLUMNS=200 cairn status > status.txt",
        ],
    )
    assert [(e["cmd"], e["exit"]) for e in s.events][:2] == [("echo hi", 0), ("false", 1)]
    assert "2 new runs" in (s.cwd / "ingest.txt").read_text()
    status = (s.cwd / "status.txt").read_text()
    assert "Runs: 2" in status and "testhost" in status
    assert re.search(r"echo hi", status) and re.search(r"\b1\b.*false", status)

    conn = sqlite3.connect(s.cwd / ".cairn" / "cairn.db")
    rows = conn.execute("SELECT cmd, exit FROM runs ORDER BY ts_start").fetchall()
    assert rows == [("echo hi", 0), ("false", 1)]


def test_cairn_watch_process(tmp_path):
    """`cairn watch` as a real process: baseline scan, live events, clean SIGTERM."""
    import signal

    proj = tmp_path / "proj"
    proj.mkdir()
    env = {
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "CAIRN_USER": "tester",
        "CAIRN_HOST": "synth",
    }
    subprocess.run(["cairn", "init"], cwd=proj, env=env, check=True, capture_output=True)
    (proj / "before.txt").write_text("existed before the watcher")
    proc = subprocess.Popen(
        ["cairn", "watch"], cwd=proj, env=env, stderr=subprocess.PIPE, text=True
    )
    try:
        for line in proc.stderr:
            if line.startswith("Watching"):
                break
        (proj / "fig.png").write_bytes(b"\x89PNG fake")
        time.sleep(1.5)
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)
    assert proc.returncode == 0
    events = [
        json.loads(x)
        for f in (proj / ".cairn" / "events").glob("*-watch.jsonl")
        for x in f.read_text().splitlines()
    ]
    by_name = {Path(e["path"]).name: e["op"] for e in events}
    assert by_name["before.txt"] == "scanned"
    assert by_name["fig.png"] == "created"


def test_records_environment(tmp_path):
    s = run_shell(
        tmp_path,
        [
            ACTIVATE,
            "echo none",
            'export CONDA_DEFAULT_ENV=md CONDA_PREFIX="/opt/conda/envs/md"',
            "echo conda",
            'export VIRTUAL_ENV="/home/x/my \\"venv\\""',
            "echo venv",
        ],
    )
    by_cmd = {e["cmd"]: e for e in s.events}
    assert "conda_env" not in by_cmd["echo none"] and "env_prefix" not in by_cmd["echo none"]
    assert by_cmd["echo conda"]["conda_env"] == "md"
    assert by_cmd["echo conda"]["env_prefix"] == "/opt/conda/envs/md"
    assert by_cmd["echo venv"]["env_prefix"] == '/home/x/my "venv"'
