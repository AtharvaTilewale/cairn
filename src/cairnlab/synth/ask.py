"""Q&A over the log (CLAUDE.md 9.3): Muse Spark searches the store with tools and
answers with citations.

Every ID in the answer must exist in the store; IDs that do not are removed and
reported. Citations render as numbered footnotes, like entries.
"""

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, time, tzinfo
from importlib import resources
from pathlib import Path
from typing import Any

from cairnlab.provenance.graph import node, resolve_version, trace
from cairnlab.provenance.objects import object_path
from cairnlab.store.models import format_ts

log = logging.getLogger(__name__)

MAX_TURNS = 6  # the model batches several tool calls per turn; the last turn must answer
MAX_TOOL_CALLS = 30
CITE = re.compile(r"\[([RAN]):([0-9A-HJKMNP-TV-Z]{26})\]")


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


_S = {"type": "string"}
TOOLS = [
    _fn(
        "search_runs",
        "Runs whose command or directory contains all the words.",
        {
            "words": {**_S, "description": "space-separated words; empty for any"},
            "since": {**_S, "description": "YYYY-MM-DD or ISO time (local); optional"},
            "until": {**_S, "description": "YYYY-MM-DD or ISO time (local); optional"},
            "failed_only": {"type": "boolean"},
            "limit": {"type": "integer"},
        },
        [],
    ),
    _fn(
        "search_files",
        "Files whose path contains the text, with versions and producers.",
        {"path_contains": _S},
        ["path_contains"],
    ),
    _fn("search_notes", "Notes containing all the words (empty for all).", {"words": _S}, []),
    _fn(
        "get_run",
        "A run's command, times, exit code, inputs, outputs and notes.",
        {"run_id": _S},
        ["run_id"],
    ),
    _fn(
        "get_entry",
        "The notebook entry for a day.",
        {"day": {**_S, "description": "YYYY-MM-DD"}},
        ["day"],
    ),
    _fn(
        "trace",
        "How a file (path or A:version) was produced, back to raw inputs.",
        {"path": _S},
        ["path"],
    ),
    _fn(
        "read_file",
        "Text of a stored file version (first 8 KB): results, parameters.",
        {"version_id": _S},
        ["version_id"],
    ),
    _fn(
        "answer",
        "Give the final answer, with [R:..]/[A:..]/[N:..] citations.",
        {"text": _S},
        ["text"],
    ),
]


@dataclass
class Answer:
    question: str
    text: str
    markdown: str
    citations: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)  # cited IDs not in the store
    tool_calls: int = 0


class Asker:
    """One question, answered with tools over one store."""

    def __init__(
        self,
        client,
        conn: sqlite3.Connection,
        tz: tzinfo,
        host: str,
        root: Path | None = None,
        objects_dir: Path | None = None,
    ):
        self.objects_dir = objects_dir
        self.client = client
        self.conn = conn
        self.tz = tz
        self.host = host
        self.root = root

    # -- helpers -----------------------------------------------------------------------

    def _rel(self, path: str) -> str:
        if self.root and path.startswith(str(self.root).rstrip("/") + "/"):
            return path[len(str(self.root).rstrip("/")) + 1 :]
        return path

    def _local(self, ts: str | None) -> str:
        if not ts:
            return ""
        return datetime.fromisoformat(ts).astimezone(self.tz).strftime("%Y-%m-%d %H:%M:%S")

    def _bound(self, text: str, end: bool) -> str | None:
        text = (text or "").strip()
        if not text:
            return None
        try:
            if len(text) == 10:
                d = datetime.fromisoformat(text).date()
                dt = datetime.combine(d, time.max if end else time.min, tzinfo=self.tz)
            else:
                dt = datetime.fromisoformat(text)
                dt = dt if dt.tzinfo else dt.replace(tzinfo=self.tz)
        except ValueError:
            return None
        return format_ts(dt)

    def _exists(self, ident: str) -> bool:
        kind, key = ident.split(":", 1)
        table = {"R": "runs", "A": "artifact_versions", "N": "notes"}[kind]
        return (
            self.conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (key,)).fetchone() is not None
        )

    # -- tools -------------------------------------------------------------------------

    def tool_search_runs(
        self,
        words: str = "",
        since: str = "",
        until: str = "",
        failed_only: bool = False,
        limit: int = 20,
    ) -> dict:
        sql = "SELECT id, ts_start, ts_end, exit, status, cwd, cmd FROM runs WHERE 1=1"
        args: list[Any] = []
        for w in (words or "").split():
            sql += " AND (cmd LIKE ? OR cwd LIKE ?)"
            args += [f"%{w}%", f"%{w}%"]
        if (lo := self._bound(since, False)) is not None:
            sql += " AND ts_start >= ?"
            args.append(lo)
        if (hi := self._bound(until, True)) is not None:
            sql += " AND ts_start <= ?"
            args.append(hi)
        if failed_only:
            sql += " AND exit IS NOT NULL AND exit != 0"
        sql += " ORDER BY ts_start DESC LIMIT ?"
        args.append(max(1, min(int(limit or 20), 50)))
        rows = self.conn.execute(sql, args).fetchall()
        return {
            "runs": [
                {
                    "run_id": f"R:{r[0]}",
                    "time": self._local(r[1]),
                    "exit": r[3],
                    "status": r[4],
                    "cwd": self._rel(r[5] or ""),
                    "cmd": r[6],
                }
                for r in rows
            ]
        }

    def tool_search_files(self, path_contains: str) -> dict:
        rows = self.conn.execute(
            "SELECT a.id, a.path, a.kind, COUNT(v.id) FROM artifacts a"
            " LEFT JOIN artifact_versions v ON v.artifact_id = a.id"
            " WHERE a.path LIKE ? GROUP BY a.id ORDER BY a.path LIMIT 30",
            (f"%{path_contains}%",),
        ).fetchall()
        out = []
        for art_id, path, kind, n in rows:
            latest = self.conn.execute(
                "SELECT v.id, v.ts FROM artifact_versions v WHERE v.artifact_id = ?"
                " ORDER BY v.ts DESC LIMIT 1",
                (art_id,),
            ).fetchone()
            makers = [
                f"R:{r}"
                for (r,) in self.conn.execute(
                    "SELECT DISTINCT e.run_id FROM edges e JOIN artifact_versions v"
                    " ON v.id = e.version_id WHERE v.artifact_id = ? AND e.role = 'generated'"
                    " LIMIT 5",
                    (art_id,),
                )
            ]
            out.append(
                {
                    "path": self._rel(path),
                    "kind": kind,
                    "versions": n,
                    "latest_version": f"A:{latest[0]}" if latest else None,
                    "latest_time": self._local(latest[1]) if latest else None,
                    "generated_by": makers,
                }
            )
        return {"files": out}

    def tool_search_notes(self, words: str = "") -> dict:
        sql, args = "SELECT id, ts, text, run_id FROM notes WHERE 1=1", []
        for w in (words or "").split():
            sql += " AND text LIKE ?"
            args.append(f"%{w}%")
        rows = self.conn.execute(sql + " ORDER BY ts DESC LIMIT 30", args).fetchall()
        return {
            "notes": [
                {
                    "note_id": f"N:{i}",
                    "time": self._local(ts),
                    "text": t,
                    "about_run": f"R:{r}" if r else None,
                }
                for i, ts, t, r in rows
            ]
        }

    def tool_get_run(self, run_id: str) -> dict:
        rid = run_id.removeprefix("R:")
        r = self.conn.execute("SELECT * FROM runs WHERE id = ?", (rid,)).fetchone()
        if r is None:
            return {"error": "unknown run"}
        files = [
            {"role": e[0], "path": self._rel(e[1]), "version": f"A:{e[2]}", "method": e[3]}
            for e in self.conn.execute(
                "SELECT e.role, a.path, v.id, e.method FROM edges e JOIN artifact_versions v"
                " ON v.id = e.version_id JOIN artifacts a ON a.id = v.artifact_id"
                " WHERE e.run_id = ? ORDER BY e.role DESC, a.path",
                (rid,),
            )
        ]
        notes = [
            {"note_id": f"N:{i}", "text": t}
            for i, t in self.conn.execute("SELECT id, text FROM notes WHERE run_id = ?", (rid,))
        ]
        for f in files:  # small outputs inline: saves a read_file round trip
            if f["role"] == "generated":
                content = self.tool_read_file(f["version"])
                if "text" in content and len(content["text"]) <= 1024:
                    f["content"] = content["text"]
        return {
            "run_id": f"R:{rid}",
            "cmd": r["cmd"],
            "cwd": self._rel(r["cwd"] or ""),
            "start": self._local(r["ts_start"]),
            "end": self._local(r["ts_end"]),
            "exit": r["exit"],
            "status": r["status"],
            "host": r["host"],
            "git": r["git_commit"],
            "env": r["env_name"],
            "files": files,
            "notes": notes,
        }

    def tool_get_entry(self, day: str) -> dict:
        row = self.conn.execute("SELECT markdown FROM entries WHERE day = ?", (day,)).fetchone()
        if row is None:
            return {"error": f"no entry for {day}"}
        return {"day": day, "markdown": row[0][:20_000]}

    def tool_read_file(self, version_id: str) -> dict:
        n = node(self.conn, version_id.removeprefix("A:"))
        if n is None:
            return {"error": "unknown version"}
        obj = object_path(self.objects_dir, n.sha) if self.objects_dir else None
        if obj is None or not obj.is_file():
            return {"path": self._rel(n.path), "error": "no stored copy"}
        data = obj.read_bytes()[:8192]
        if b"\x00" in data:
            return {"path": self._rel(n.path), "error": "binary file"}
        return {
            "version": f"A:{n.vid}",
            "path": self._rel(n.path),
            "text": data.decode("utf-8", errors="replace"),
        }

    def tool_trace(self, path: str) -> dict:
        vid = path[2:] if path.startswith("A:") else None
        if vid is None:
            candidates = [path] if path.startswith("/") else [str((self.root or Path.cwd()) / path)]
            vid = next(
                (v for c in candidates if (v := resolve_version(self.conn, self.host, c))), None
            )
        if vid is None:
            rows = self.tool_search_files(Path(path).name)["files"]
            return {"error": "no recorded version with that path", "similar": rows[:5]}
        t = trace(self.conn, vid)
        return {
            "target": {"version": f"A:{vid}", "path": self._rel(t.nodes[vid].path)},
            "steps": [
                {
                    "run_id": f"R:{s.run['id']}",
                    "cmd": s.run["cmd"],
                    "time": self._local(s.run["ts_start"]),
                    "inputs": [
                        f"A:{v} {self._rel(t.nodes[v].path)}" for v in s.inputs if v in t.nodes
                    ],
                    "outputs": [f"A:{v}" for v in s.outputs],
                    "notes": s.notes,
                }
                for s in t.steps
            ],
            "raw_inputs": [f"A:{v} {self._rel(t.nodes[v].path)}" for v in t.raw_inputs],
        }

    # -- loop --------------------------------------------------------------------------

    def ask(self, question: str) -> Answer:
        system = resources.files("cairnlab.llm").joinpath("prompts/ask.md").read_text()
        now = datetime.now(self.tz).strftime("%Y-%m-%d %H:%M %Z")
        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"(Now: {now}.) {question}"},
        ]
        calls, turns, final = 0, 0, None
        while final is None and calls < MAX_TOOL_CALLS:
            # Near the end of the budget only `answer` is offered, so there is always one.
            turns += 1
            last_turn = turns >= MAX_TURNS or calls >= MAX_TOOL_CALLS - 4
            if last_turn:
                messages.append({"role": "user", "content": "Answer now with what you have."})
            msg = self.client.complete(
                purpose="ask",
                messages=messages,
                tools=[TOOLS[-1]] if last_turn else TOOLS,
                reasoning_effort="medium",
            )
            log.info(
                "ask turn: %s",
                [c.function.name for c in (msg.tool_calls or [])]
                if getattr(msg, "tool_calls", None)
                else "text",
            )
            tool_calls = list(getattr(msg, "tool_calls", None) or [])
            messages.append(
                {
                    "role": "assistant",
                    "content": getattr(msg, "content", None) or "",
                    **(
                        {
                            "tool_calls": [
                                {
                                    "id": c.id,
                                    "type": "function",
                                    "function": {
                                        "name": c.function.name,
                                        "arguments": c.function.arguments or "{}",
                                    },
                                }
                                for c in tool_calls
                            ]
                        }
                        if tool_calls
                        else {}
                    ),
                }
            )
            if not tool_calls:
                final = msg.content or ""
                break
            for c in tool_calls:
                calls += 1
                try:
                    args = json.loads(c.function.arguments or "{}")
                except ValueError:
                    args = {}
                if c.function.name == "answer":
                    final = str(args.get("text", ""))
                    result = {"ok": True}
                elif hasattr(self, f"tool_{c.function.name}"):
                    try:
                        result = getattr(self, f"tool_{c.function.name}")(**args)
                    except TypeError as exc:
                        result = {"error": f"bad arguments: {exc}"}
                else:
                    result = {"error": "unknown tool"}
                messages.append(
                    {"role": "tool", "tool_call_id": c.id, "content": json.dumps(result)}
                )
        if final is None:
            final = "I could not find an answer within the search budget."
        return self._finish(question, final, calls)

    def _finish(self, question: str, text: str, calls: int) -> Answer:
        removed: list[str] = []

        def keep(m: re.Match) -> str:
            ident = f"{m.group(1)}:{m.group(2)}"
            if self._exists(ident):
                return m.group(0)
            removed.append(ident)
            return ""

        text = CITE.sub(keep, text).strip()
        order: dict[str, int] = {}

        def ref(m: re.Match) -> str:
            ident = f"{m.group(1)}:{m.group(2)}"
            order.setdefault(ident, len(order) + 1)
            return f"[^{order[ident]}]"

        md = CITE.sub(ref, text)
        if order:
            md += "\n\n" + "\n".join(
                f"[^{n}]: `{i}` {self._describe(i)} (`cairn show {i}`)" for i, n in order.items()
            )
        return Answer(question, text, md.strip() + "\n", list(order), removed, calls)

    def _describe(self, ident: str) -> str:
        kind, key = ident.split(":", 1)
        if kind == "R":
            r = self.conn.execute(
                "SELECT ts_start, cmd, exit FROM runs WHERE id = ?", (key,)
            ).fetchone()
            return f"{self._local(r[0])} `{(r[1] or '')[:80]}` (exit {r[2]})" if r else ""
        if kind == "A":
            n = node(self.conn, key)
            return f"{self._rel(n.path)} (sha256 {n.sha[:12]})" if n else ""
        r = self.conn.execute("SELECT text FROM notes WHERE id = ?", (key,)).fetchone()
        return f"note: “{(r[0] or '')[:80]}”" if r else ""
