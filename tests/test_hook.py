"""Unit tests for the bash hook's helper functions and the activation script.

Each test sources bash.sh in a non-interactive bash and calls one helper. The
end-to-end behaviour in a real interactive shell is covered by test_shell.py.
"""

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime

import pytest

from cairnlab.capture.hooks import activation_script, deactivation_script, hook_path

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

HOOK = str(hook_path())


def bash(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run ``script`` in a clean non-interactive bash with the hook library sourced."""
    full_env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "LC_ALL": "C.UTF-8"}
    full_env.update(env or {})
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", f'source "{HOOK}" || exit 99\n{script}'],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )


TRICKY = [
    "",
    "plain",
    'say "hi"',
    "back\\slash",
    "trailing\\",
    '\\"',
    "tab\there",
    "multi\nline\ncommand",
    "cr\rlf\r\n",
    "form\ffeed and back\bspace",
    "esc \x1b[31mred\x1b[0m",
    "bell\x07 and del\x7f",
    "".join(chr(i) for i in range(1, 32)),
    "héllo wörld",
    "日本語のテキスト",
    "emoji 🎉🧬",
    "mixed 'single' \"double\" `back` $dollar &amp; *glob* ?q [br] {x}",
    "percent %s %d %% and printf \\n escape",
    "&& || ; | > < >>",
    "pattern chars * ? [ ] ! ^ \\\\ //",
]


@pytest.mark.parametrize("locale", ["C.UTF-8", "C"])
def test_json_escape_roundtrip(locale):
    env = {f"S{i}": s for i, s in enumerate(TRICKY)}
    env["LC_ALL"] = locale
    script = "\n".join(
        f'__cairn_json_escape "$S{i}"; printf \'"%s"\\n\' "$__cairn_j"' for i in range(len(TRICKY))
    )
    res = bash(script, env)
    assert res.returncode == 0, res.stderr
    lines = res.stdout.split("\n")[:-1]
    assert len(lines) == len(TRICKY)
    for raw, expected in zip(lines, TRICKY, strict=True):
        assert json.loads(raw) == expected
        # Escaped output is a single line with no raw control characters.
        assert not any(ord(c) < 32 for c in raw)


EPOCHS = [
    0,
    59,
    86399,
    86400,
    951782400,  # 2000-02-29
    951868800,  # 2000-03-01
    1709164800,  # 2024-02-29
    1735689599,  # 2024-12-31T23:59:59
    1761556443,  # 2025-10-27
    1793145600,  # 2026-10-27
    4107542400,  # 2100-03-01 (not a leap year)
    4102444799,
]


def test_iso_matches_python():
    script = "\n".join(f'__cairn_iso {t} 042; echo "$__cairn_ts"' for t in EPOCHS)
    res = bash(script)
    assert res.returncode == 0, res.stderr
    got = res.stdout.split()
    want = [datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%S") + ".042Z" for t in EPOCHS]
    assert got == want


def test_now_with_and_without_epochrealtime():
    # Bash 5 path ($EPOCHREALTIME), the locale-comma variant, and the bash 4.2 path (date).
    res = bash(
        "__cairn_now; echo $__cairn_sec $__cairn_ms\n"
        # Once unset, EPOCHREALTIME is an ordinary variable (as on bash 4.2).
        "unset EPOCHREALTIME; __cairn_now; echo $__cairn_sec $__cairn_ms\n"
        "EPOCHREALTIME=1761556443,987654; __cairn_now; echo $__cairn_sec $__cairn_ms\n"
    )
    assert res.returncode == 0, res.stderr
    rows = [line.split() for line in res.stdout.splitlines()]
    assert rows[2] == ["1761556443", "987"]
    for sec, ms in (rows[0], rows[1]):
        assert abs(int(sec) - datetime.now(UTC).timestamp()) < 60
        assert len(ms) == 3 and ms.isdigit()


@pytest.mark.parametrize(
    "cmd, secret",
    [
        ("export MODEL_API_KEY=sk-123", True),
        ("export api_key=abc", True),
        ("curl -H 'X-Token: abc' https://x", True),
        ("GITHUB_TOKEN=abc gh pr list", True),
        ("mysql --password=hunter2", True),
        ("aws configure set aws_secret_access_key x", True),
        ("gmx mdrun -deffnm md_0_1", False),
        ("python fit.py --keyframe 3", False),
        ("ssh-keygen -t ed25519", False),
    ],
)
def test_secret_detection(cmd, secret):
    res = bash('__cairn_secret "$C" && echo yes || echo no', {"C": cmd})
    assert res.stdout.strip() == ("yes" if secret else "no")


@pytest.mark.parametrize(
    "cmd, ignored",
    [
        ("ls", True),
        ("ls -la runs/", True),
        ("  cd ..", True),
        ("pwd", True),
        ("clear", True),
        ("history 20", True),
        ("cairn note changed the cutoff to 1.2 nm", True),
        ("cairn note", True),
        ("cd runs && make", False),
        ("ls | wc -l", False),
        ("lsof -i", False),
        ("cairn ingest", False),
        ("cairn run -- python fit.py", True),
        ("cairn run --bg -- gmx mdrun -deffnm md", True),
        ("cairn run -- python a.py && python b.py", False),
        ("cairn notebook", False),
        ("python fit.py", False),
        # Separators inside quotes do not make a compound command...
        ('cairn note "first fit undershoots; try a larger cutoff"', True),
        ("cairn note 'a && b | c'", True),
        ('ls "odd;name"', True),
        ('cairn note "two\nlines"', True),
        ("cairn note it\\'s fine", True),
        # ...but unquoted ones do, and command substitution always runs something.
        ("cairn note 'x'; python fit.py", False),
        ('cairn note "a" && rm -rf results', False),
        ("ls $(rm -rf x)", False),
        ("ls `rm -rf x`", False),
        ('cairn note "$(date)"', False),
    ],
)
def test_ignore_list(cmd, ignored):
    res = bash('__cairn_ignored "$C" && echo yes || echo no', {"C": cmd})
    assert res.stdout.strip() == ("yes" if ignored else "no")


def test_activation_script_is_valid_bash(tmp_path):
    script = activation_script(home=tmp_path / "it's here", session="01JSESSION")
    res = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "__cairn_session_id=01JSESSION" in script
    assert script.rstrip().endswith("__cairn_activate")
    res = subprocess.run(
        ["bash", "-n"], input=deactivation_script(), capture_output=True, text=True
    )
    assert res.returncode == 0


def test_activation_refuses_without_cairn_user(tmp_path):
    script = activation_script(home=tmp_path / ".cairn", session="01JSESSION")
    res = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-c",
            'eval "$S"; rc=$?; echo "rc=$rc"; trap -p DEBUG;'
            ' echo "pc=${PROMPT_COMMAND-unset}"; declare -F | grep -c __cairn_',
        ],
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "S": script},
        capture_output=True,
        text=True,
    )
    assert "CAIRN_USER is not set" in res.stderr
    lines = res.stdout.splitlines()
    assert lines == ["rc=1", "pc=unset", "0"]
    assert not (tmp_path / ".cairn").exists()
