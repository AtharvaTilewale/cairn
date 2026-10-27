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
