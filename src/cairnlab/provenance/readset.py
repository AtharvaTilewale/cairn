"""Readset: Muse Spark reads a script and lists the files it reads and writes (7.4).

Command-line paths are already covered by argpath; readset adds paths a script names
in its own code (``open("results/fit.json")``) and maps option/positional references
onto the run's actual arguments. Answers are cached by the script's SHA-256, so each
version of a script is read once. A failed call is logged and not cached.
"""

import json
import logging
import shlex
import sqlite3
from importlib import resources
from pathlib import Path, PurePosixPath

from cairnlab.llm.client import LLMError
from cairnlab.llm.schemas import READSET
from cairnlab.provenance.objects import object_path
from cairnlab.store.models import utc_now

log = logging.getLogger(__name__)

MAX_SOURCE_BYTES = 20_000
INTERPRETERS = {"python", "python3", "Rscript", "bash", "sh", "perl", "julia"}
SCRIPT_SUFFIXES = (".py", ".R", ".r", ".sh", ".pl", ".jl")
SEPARATORS = {";", "&&", "||", "|", "&"}


def _prompt() -> str:
    return resources.files("cairnlab.llm").joinpath("prompts/readset.md").read_text()


def script_call(cmd: str) -> tuple[str, list[str]] | None:
    """(script path, its arguments) if the line's first command runs a script file."""
    try:
        lexer = shlex.shlex(cmd, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    first: list[str] = []
    for t in tokens:
        if t in SEPARATORS:
            break
        first.append(t)
    while first and "=" in first[0] and not first[0].startswith(("-", "/", ".")):
        first.pop(0)  # VAR=value environment assignments
    if not first:
        return None
    prog = first[0]
    name = PurePosixPath(prog).name
    if name in INTERPRETERS or name.startswith("python3."):
        rest = first[1:]
        while rest and rest[0].startswith("-"):
            opt = rest.pop(0)
            if opt in ("-c", "-m"):
                return None  # inline code or a module, not a script file
            if opt in ("-W", "-X") and rest:
                rest.pop(0)
        if rest and rest[0].endswith(SCRIPT_SUFFIXES):
            return rest[0], rest[1:]
        return None
    if prog.startswith(("./", "/")) and prog.endswith(SCRIPT_SUFFIXES):
        return prog, first[1:]
    return None


def resolve(readset: dict, args: list[str], cwd: PurePosixPath) -> tuple[list[str], list[str]]:
    """Map a readset answer onto concrete paths for one invocation."""
    value_flags = set(readset.get("value_flags", []))
    flags: dict[str, str] = {}
    positional: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-") and "=" in a:
            k, v = a.split("=", 1)
            flags[k] = v
        elif a.startswith("-"):
            if a in value_flags and i + 1 < len(args):
                flags[a] = args[i + 1]
                i += 1
        else:
            positional.append(a)
        i += 1

    def one(ref: dict) -> str | None:
        kind, value = ref.get("kind"), str(ref.get("value", ""))
        if kind == "literal":
            raw = value
        elif kind == "flag":
            raw = flags.get(value)
        elif kind == "positional":
            try:
                raw = positional[int(value) - 1]
            except (ValueError, IndexError):
                raw = None
        else:
            raw = None
        if not raw or any(c in raw for c in "*?{$"):
            return None
        p = PurePosixPath(raw)
        return str(p if p.is_absolute() else cwd / p)

    used = [p for p in map(one, readset.get("reads", [])) if p]
    generated = [p for p in map(one, readset.get("writes", [])) if p]
    return used, generated


class ReadsetReader:
    """The linker's readset method, backed by a model client and the readset cache."""

    def __init__(self, client, objects_dir: Path):
        self.client = client
        self.objects_dir = objects_dir

    def __call__(self, conn: sqlite3.Connection, run: sqlite3.Row):
        call = script_call(run["cmd"] or "")
        if call is None:
            return None
        script, args = call
        cwd = PurePosixPath(run["cwd"] or "/")
        script_path = str(PurePosixPath(script) if script.startswith("/") else cwd / script)
        row = conn.execute(
            "SELECT v.sha256 FROM file_events fe JOIN artifacts a ON a.id = fe.artifact_id"
            " JOIN artifact_versions v ON v.id = fe.version_id"
            " WHERE a.host = ? AND a.path = ? AND fe.ts <= ? ORDER BY fe.ts DESC LIMIT 1",
            (run["host"], script_path, run["ts_start"]),
        ).fetchone()
        if row is None:
            return None
        readset = self._readset(conn, row[0], script_path)
        if readset is None:
            return None
        return resolve(readset, args, cwd)

    def _readset(self, conn: sqlite3.Connection, sha: str, name: str) -> dict | None:
        cached = conn.execute("SELECT json FROM readsets WHERE sha256 = ?", (sha,)).fetchone()
        if cached:
            return json.loads(cached[0])
        obj = object_path(self.objects_dir, sha)
        if not obj.is_file():
            return None
        source = obj.read_bytes()[:MAX_SOURCE_BYTES].decode("utf-8", errors="replace")
        try:
            answer = self.client.structured(
                purpose="readset",
                messages=[
                    {"role": "system", "content": _prompt()},
                    {
                        "role": "user",
                        "content": f"Script: {PurePosixPath(name).name}\n\n```\n{source}\n```",
                    },
                ],
                schema_name="Readset",
                schema=READSET,
                reasoning_effort="low",
            )
        except LLMError as exc:
            log.warning("readset for %s failed: %s", name, exc)
            return None
        conn.execute(
            "INSERT OR REPLACE INTO readsets (sha256, model, created, json) VALUES (?, ?, ?, ?)",
            (sha, self.client.model, utc_now(), json.dumps(answer)),
        )
        return answer
