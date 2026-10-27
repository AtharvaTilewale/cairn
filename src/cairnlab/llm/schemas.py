"""JSON schemas for structured outputs (strict mode: every object closed, all keys required)."""

_FILE_REF = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["literal", "flag", "positional"]},
        "value": {"type": "string"},
    },
    "required": ["kind", "value"],
    "additionalProperties": False,
}

READSET = {
    "type": "object",
    "properties": {
        "reads": {"type": "array", "items": _FILE_REF},
        "writes": {"type": "array", "items": _FILE_REF},
        "value_flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["reads", "writes", "value_flags"],
    "additionalProperties": False,
}


def _obj(**props: dict) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


_STR = {"type": "string"}
_IDS = {"type": "array", "items": _STR}

ENTRY = _obj(
    title=_STR,
    goal=_STR,
    phases={
        "type": "array",
        "items": _obj(
            heading=_STR,
            narrative=_STR,
            claims={"type": "array", "items": _obj(text=_STR, evidence=_IDS)},
        ),
    },
    results={"type": "array", "items": _obj(artifact=_STR, description=_STR)},
    decisions={"type": "array", "items": _obj(text=_STR, note=_STR)},
    drift={"type": "array", "items": _obj(text=_STR, evidence=_IDS)},
    open_questions={"type": "array", "items": _STR},
    next_steps={"type": "array", "items": _STR},
)

CHECK_KINDS = [
    "exit_code",
    "command_contains",
    "file_named",
    "param_value",
    "content_contains",
    "none",
]

CLAIMS = _obj(
    claims={
        "type": "array",
        "items": _obj(
            text=_STR,
            evidence=_IDS,
            checks={
                "type": "array",
                "items": _obj(
                    kind={"type": "string", "enum": CHECK_KINDS},
                    run=_STR,
                    path=_STR,
                    key=_STR,
                    value=_STR,
                ),
            },
        ),
    }
)
