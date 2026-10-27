"""Parameter-file diffs between versions (CLAUDE.md section 7).

Config-like files (``.mdp``, ``.yaml``/``.yml``, ``.toml``, ``.json``, ``.ini``,
``.cfg``, ``params*.txt``) are parsed into flat ``key -> value`` maps (nested keys
joined with ``.``), so two versions can be compared key by key.
"""

import json
import re
import tomllib

CONFIG_SUFFIXES = (".mdp", ".yaml", ".yml", ".toml", ".json", ".ini", ".cfg")


def is_config(path: str) -> bool:
    """True for files whose versions are compared as parameter sets."""
    name = path.rsplit("/", 1)[-1].lower()
    return name.endswith(CONFIG_SUFFIXES) or (name.startswith("params") and name.endswith(".txt"))


def _flatten(obj, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}{k}."))
    else:
        out[prefix[:-1]] = json.dumps(obj) if isinstance(obj, list) else str(obj)
    return out


def _key_values(text: str) -> dict[str, str]:
    """``key = value`` lines (mdp, ini, cfg, params.txt); ``;`` and ``#`` start comments."""
    out = {}
    for line in text.splitlines():
        line = re.split(r"\s[;#]|^[;#]", line, maxsplit=1)[0].strip()
        if not line or line.startswith("[") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip("'\"")
    return out


def _yaml(text: str) -> dict[str, str]:
    """Indentation-nested ``key: value`` YAML (no lists, anchors or multi-line strings)."""
    out: dict[str, str] = {}
    stack: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#") or ":" not in raw:
            continue
        indent = len(raw) - len(raw.lstrip())
        key, value = raw.strip().split(":", 1)
        value = value.split(" #", 1)[0].strip().strip("'\"")
        while stack and stack[-1][0] >= indent:
            stack.pop()
        full = ".".join([k for _, k in stack] + [key.strip()])
        if value:
            out[full] = value
        else:
            stack.append((indent, key.strip()))
    return out


def parse(path: str, text: str) -> dict[str, str]:
    """Flat parameter map of a config file's text ({} if it cannot be parsed)."""
    name = path.lower()
    try:
        if name.endswith(".json"):
            return _flatten(json.loads(text))
        if name.endswith(".toml"):
            return _flatten(tomllib.loads(text))
        if name.endswith((".yaml", ".yml")):
            return _yaml(text)
        return _key_values(text)
    except (ValueError, tomllib.TOMLDecodeError):
        return {}


def diff(path: str, old_text: str, new_text: str) -> list[tuple[str, str | None, str | None]]:
    """(key, old, new) for every key whose value differs; None marks added/removed."""
    old, new = parse(path, old_text), parse(path, new_text)
    return [
        (k, old.get(k), new.get(k))
        for k in sorted(old.keys() | new.keys())
        if old.get(k) != new.get(k)
    ]
