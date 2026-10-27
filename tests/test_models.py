"""Tests for the hook event and row models."""

import pytest
from pydantic import ValidationError

from cairnlab.store.models import CmdEvent, normalize_ts

GOOD = {
    "v": 1,
    "type": "cmd",
    "id": "01JABCDEF-3",
    "session": "01JABCDEF",
    "host": "hpc-login2",
    "shell": "bash",
    "user": "alice",
    "cwd": "/home/alice/proj",
    "cmd": "gmx mdrun -deffnm md_0_1",
    "ts_start": "2026-10-28T09:14:03.120Z",
    "ts_end": "2026-10-28T09:14:05.871Z",
    "exit": 0,
}


def test_valid_cmd_event():
    ev = CmdEvent.model_validate(GOOD)
    assert ev.exit == 0
    assert ev.ts_start == "2026-10-28T09:14:03.120Z"


@pytest.mark.parametrize("missing", ["user", "host", "id", "cmd", "ts_start", "exit"])
def test_required_fields(missing):
    bad = {k: v for k, v in GOOD.items() if k != missing}
    with pytest.raises(ValidationError):
        CmdEvent.model_validate(bad)


def test_empty_user_rejected():
    with pytest.raises(ValidationError):
        CmdEvent.model_validate({**GOOD, "user": ""})


def test_wrong_type_rejected():
    with pytest.raises(ValidationError):
        CmdEvent.model_validate({**GOOD, "type": "file"})


def test_exit_must_be_integer():
    with pytest.raises(ValidationError):
        CmdEvent.model_validate({**GOOD, "exit": "zero"})


def test_unknown_fields_are_kept():
    ev = CmdEvent.model_validate({**GOOD, "pipestatus": [0, 1]})
    assert ev.model_dump()["pipestatus"] == [0, 1]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026-10-28T09:14:03.120Z", "2026-10-28T09:14:03.120Z"),
        ("2026-10-28T09:14:03Z", "2026-10-28T09:14:03.000Z"),
        ("2026-10-28T14:44:03.5+05:30", "2026-10-28T09:14:03.500Z"),
        ("2026-10-28T09:14:03.123456Z", "2026-10-28T09:14:03.123Z"),
    ],
)
def test_normalize_ts(raw, expected):
    assert normalize_ts(raw) == expected


@pytest.mark.parametrize("raw", ["yesterday", "2026-10-28T09:14:03", ""])
def test_normalize_ts_rejects_bad_or_naive(raw):
    with pytest.raises(ValueError):
        normalize_ts(raw)
