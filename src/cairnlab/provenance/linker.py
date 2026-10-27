"""Infer ``used`` / ``generated`` edges between runs and artefact versions (section 7).

Methods, applied in this order (an edge found by an earlier method is kept):

1. **wrapper (1.0):** ``cairn run`` diffed the tree before and after, so its
   ``generated`` list is exact when nothing else ran meanwhile (0.8 otherwise). A
   change inside a later, nested run's window belongs to that run. For ``--bg`` jobs,
   files the job was seen holding open for writing are its outputs (1.0); other
   changes count only under its ``cwd`` (0.6). Inputs come from the methods below.
2. **argpath (0.9):** command tokens that resolve (from ``cwd``, following ``cd``
   inside the line) to known artefacts are ``used``; targets of output flags
   (``-o``, ``--out``, ``-deffnm NAME`` -> ``NAME.*``), redirections and the last
   argument of ``cp``/``mv``-style commands are ``generated``.
3. **window (0.7):** files under ``cwd`` that changed in ``[ts_start, ts_end + 2 s]``
   are ``generated`` (the grace stops at the next run's start on the host); 0.4 when
   another (non-wrapper) run on the host overlaps. Versions another run claims
   explicitly, and files a background job holds open, are excluded.
   Versions already claimed by a ``cairn run`` wrapper are excluded.
4. **readset (0.6):** files a script reads/writes according to Muse Spark
   (``provenance/readset.py``), when a reader is supplied.

``used`` always points at the version current at ``ts_start``; ``generated`` at the
latest version seen in the run's window. Linking is deterministic and idempotent:
a run's edges are recomputed from the store each time it is linked.
"""

import json
import logging
import shlex
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

from cairnlab.store.models import format_ts

log = logging.getLogger(__name__)

GRACE = timedelta(seconds=2)
CONFIDENCE = {
    "wrapper": 1.0,
    "wrapper_shared": 0.8,  # other runs overlapped the wrapped one
    "wrapper_unseen": 0.6,  # background job's diff, never seen open by the job
    "argpath": 0.9,
    "window": 0.7,
    "ambiguous": 0.4,
    "readset": 0.6,
}

OUTPUT_FLAGS = {"-o", "--out", "--output", "--outfile", "--output-file", "-O"}
PREFIX_FLAGS = {"-deffnm"}  # GROMACS: -deffnm md writes md.xtc, md.edr, md.log, ...
SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "|&", ";;"}
REDIRECT_OUT = {">", ">>", ">|", "&>", "&>>"}
REDIRECT_IN = {"<"}
LAST_ARG_IS_OUTPUT = {"cp", "mv", "rsync", "scp", "ln", "install"}
ALL_ARGS_ARE_OUTPUT = {"tee", "touch"}
NO_PATH_ARGS = {
    "cd",
    "mkdir",
    "rm",
    "rmdir",
    "echo",
    "printf",
    "export",
    "sleep",
    "true",
    "false",
    "cairn",
    "sudo",
    "time",
    "nohup",
    "env",
}


@dataclass
class CommandPaths:
    """Paths a command line names as inputs and outputs (not yet checked to exist)."""

    used: list[PurePosixPath] = field(default_factory=list)
    generated: list[PurePosixPath] = field(default_factory=list)
    prefixes: list[PurePosixPath] = field(default_factory=list)


def _tokens(cmd: str) -> list[str]:
    lexer = shlex.shlex(cmd, posix=True, punctuation_chars="();<>|&")
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:  # unbalanced quotes: fall back to plain words
        return cmd.split()


def _norm(base: PurePosixPath, token: str) -> PurePosixPath | None:
    if not token or token.startswith("-") or token in ("&1", "&2") or token.isdigit():
        return None
    p = PurePosixPath(token)
    p = p if p.is_absolute() else base / p
    parts: list[str] = []
    for part in p.parts:
        if part == "..":
            if len(parts) > 1:
                parts.pop()
        elif part != ".":
            parts.append(part)
    return PurePosixPath(*parts)


def command_paths(cmd: str, cwd: Path | PurePosixPath) -> CommandPaths:
    """Split a shell line into candidate input and output paths, resolved from ``cwd``."""
    out = CommandPaths()
    base = PurePosixPath(str(cwd))
    segments: list[list[str]] = [[]]
    redirects: list[tuple[str, str]] = []
    toks = _tokens(cmd)
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in REDIRECT_OUT or t in REDIRECT_IN or t in (">&",):
            if i + 1 < len(toks):
                redirects.append((t, toks[i + 1]))
                segments[-1].append(f"\0redir{len(redirects) - 1}")
            i += 2
            continue
        if t in SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(t)
        i += 1

    here = base
    for seg in segments:
        words = [w for w in seg if not w.startswith("\0redir")]
        # Leading VAR=value assignments are environment, not the program.
        while words and "=" in words[0] and not words[0].startswith(("-", "/", ".")):
            words.pop(0)
        for w in seg:
            if w.startswith("\0redir"):
                op, target = redirects[int(w[6:])]
                p = _norm(here, target)
                if p is not None and op != ">&":
                    (out.used if op in REDIRECT_IN else out.generated).append(p)
        if not words:
            continue
        prog, args = PurePosixPath(words[0]).name, words[1:]
        if prog == "cd":
            target = args[0] if args else None
            if target and target != "-":
                here = _norm(here, target) or here
            continue
        if prog in NO_PATH_ARGS:
            continue
        if words[0].startswith(("./", "/")):
            p = _norm(here, words[0])
            if p:
                out.used.append(p)
        positional: list[PurePosixPath] = []
        j = 0
        while j < len(args):
            a = args[j]
            if a in OUTPUT_FLAGS or a in PREFIX_FLAGS:
                if j + 1 < len(args):
                    p = _norm(here, args[j + 1])
                    if p:
                        (out.generated if a in OUTPUT_FLAGS else out.prefixes).append(p)
                j += 2
                continue
            if a.startswith("-") and "=" in a:
                flag, value = a.split("=", 1)
                p = _norm(here, value)
                if p:
                    (out.generated if flag in OUTPUT_FLAGS else out.used).append(p)
            elif not a.startswith("-"):
                value = a.split("=", 1)[1] if "=" in a and not a.startswith("/") else a
                p = _norm(here, value)
                if p:
                    positional.append(p)
            j += 1
        if prog in ALL_ARGS_ARE_OUTPUT:
            out.generated += positional
        elif prog in LAST_ARG_IS_OUTPUT and len(positional) >= 2:
            out.used += positional[:-1]
            out.generated.append(positional[-1])
        else:
            out.used += positional
    return out


@dataclass
class LinkStats:
    """What one linking pass did."""

    runs: int = 0
    edges: dict[str, int] = field(default_factory=dict)


# A readset reader returns (used paths, generated paths) for a run, or None.
Reader = Callable[[sqlite3.Connection, sqlite3.Row], tuple[list[str], list[str]] | None]


def _plus(ts: str, delta: timedelta) -> str:
    return format_ts(datetime.fromisoformat(ts) + delta)


def _artifact(conn: sqlite3.Connection, host: str, path: str) -> str | None:
    row = conn.execute("SELECT id FROM artifacts WHERE host = ? AND path = ?", (host, path))
    found = row.fetchone()
    return found[0] if found else None


def _current_version(conn: sqlite3.Connection, artifact_id: str, at: str) -> str | None:
    """The version of an artefact current at ``at`` (None if absent or deleted)."""
    row = conn.execute(
        "SELECT version_id FROM file_events WHERE artifact_id = ? AND ts <= ?"
        " ORDER BY ts DESC, event_id DESC LIMIT 1",
        (artifact_id, at),
    ).fetchone()
    return row[0] if row else None


def _window_change(
    conn: sqlite3.Connection, artifact_id: str, start: str, end: str
) -> tuple[str, str] | None:
    """(version, time) of the artefact's last change within ``[start, end]``."""
    row = conn.execute(
        "SELECT version_id, ts FROM file_events WHERE artifact_id = ? AND ts >= ? AND ts <= ?"
        " AND version_id IS NOT NULL ORDER BY ts DESC, event_id DESC LIMIT 1",
        (artifact_id, start, end),
    ).fetchone()
    return (row[0], row[1]) if row else None


def _window_version(conn: sqlite3.Connection, artifact_id: str, start: str, end: str):
    hit = _window_change(conn, artifact_id, start, end)
    return hit[0] if hit else None


def link(
    conn: sqlite3.Connection, since: str | None = None, reader: Reader | None = None
) -> LinkStats:
    """(Re)compute edges for runs that ended at or after ``since`` (all runs if None).

    Runs still in progress are always included.
    """
    stats = LinkStats()
    sql = "SELECT r.*, e.payload, e.type AS etype FROM runs r JOIN events e ON e.id = r.id"
    params: tuple = ()
    if since is not None:
        sql += " WHERE r.ts_end IS NULL OR r.ts_end >= ?"
        params = (since,)
    runs = conn.execute(sql + " ORDER BY r.ts_start", params).fetchall()
    # Wrapper runs first, so their exact outputs are claimed before windows are drawn.
    runs.sort(key=lambda r: 0 if _wrapper(r) is not None else 1)
    conn.execute("BEGIN IMMEDIATE")
    try:
        for run in runs:
            _link_run(conn, run, reader, stats)
            stats.runs += 1
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return stats


def _wrapper(run: sqlite3.Row) -> dict | None:
    payload = json.loads(run["payload"]) if run["payload"] else {}
    w = payload.get("wrapper")
    return w if isinstance(w, dict) else None


def _add(conn, stats, run_id, version_id, role, method, confidence) -> None:
    cur = conn.execute(
        "INSERT OR IGNORE INTO edges (run_id, version_id, role, method, confidence)"
        " VALUES (?, ?, ?, ?, ?)",
        (run_id, version_id, role, method, confidence),
    )
    if cur.rowcount:
        stats.edges[method] = stats.edges.get(method, 0) + 1


def _link_run(conn, run, reader: Reader | None, stats: LinkStats) -> None:
    run_id, host, cwd = run["id"], run["host"], run["cwd"] or "/"
    start = run["ts_start"]
    end = _window_end(conn, run)
    conn.execute("DELETE FROM edges WHERE run_id = ?", (run_id,))

    # 1. wrapper
    wrapper = _wrapper(run)
    if wrapper is not None and end is not None:
        # The tree diff also sees writes by other commands that ran meanwhile (a
        # background run lasts hours): a change inside a later, nested run's window is
        # that run's, and any overlap makes this run's claim less than certain.
        others = conn.execute(
            "SELECT ts_start, ts_end FROM runs WHERE host = ? AND id != ? AND ts_start > ?"
            " AND ts_start <= ?",
            (host, run_id, start, end),
        ).fetchall()
        background = run["etype"] == "run_start"
        written = set(wrapper.get("written", []))
        under_cwd = cwd.rstrip("/") + "/"
        shared = CONFIDENCE["wrapper"] if not others else CONFIDENCE["wrapper_shared"]
        for path in wrapper.get("generated", []):
            art = _artifact(conn, host, path)
            hit = _window_change(conn, art, start, end) if art else None
            if hit is None:
                continue
            v, t = hit
            if path in written:  # the job itself held it open for writing
                _add(conn, stats, run_id, v, "generated", "wrapper", CONFIDENCE["wrapper"])
                continue
            nested = any(
                o_start <= t and o_end is not None and _plus(o_end, GRACE) >= t
                for o_start, o_end in others
            )
            if nested:
                continue
            if not background:
                _add(conn, stats, run_id, v, "generated", "wrapper", shared)
            elif path.startswith(under_cwd):
                _add(conn, stats, run_id, v, "generated", "wrapper", CONFIDENCE["wrapper_unseen"])

    # 2. argpath
    paths = command_paths(run["cmd"] or "", PurePosixPath(cwd))
    out_set = {str(p) for p in paths.generated}
    for p in paths.used:
        if str(p) in out_set:
            continue
        art = _artifact(conn, host, str(p))
        v = _current_version(conn, art, start) if art else None
        if v:
            _add(conn, stats, run_id, v, "used", "argpath", CONFIDENCE["argpath"])
    if end is not None:
        for p in paths.generated:
            art = _artifact(conn, host, str(p))
            v = _window_version(conn, art, start, end) if art else None
            if v:
                _add(conn, stats, run_id, v, "generated", "argpath", CONFIDENCE["argpath"])
        for prefix in paths.prefixes:
            rows = conn.execute(
                "SELECT id FROM artifacts WHERE host = ? AND path LIKE ? ESCAPE '\\'",
                (host, _like_prefix(str(prefix)) + ".%"),
            )
            for (art,) in rows.fetchall():
                v = _window_version(conn, art, start, end)
                if v:
                    _add(conn, stats, run_id, v, "generated", "argpath", CONFIDENCE["argpath"])

    # 3. window
    if end is not None and wrapper is None:
        overlap = conn.execute(
            "SELECT 1 FROM runs r JOIN events e ON e.id = r.id WHERE r.host = ? AND r.id != ?"
            " AND r.ts_start <= ? AND COALESCE(r.ts_end, ?) >= ?"
            " AND json_extract(e.payload, '$.wrapper') IS NULL LIMIT 1",
            (host, run_id, end, end, start),
        ).fetchone()
        confidence = CONFIDENCE["ambiguous" if overlap else "window"]
        under = cwd.rstrip("/") + "/"
        rows = conn.execute(
            "SELECT fe.artifact_id, fe.version_id, a.path FROM file_events fe"
            " JOIN artifacts a ON a.id = fe.artifact_id"
            " WHERE a.host = ? AND fe.ts >= ? AND fe.ts <= ? AND fe.version_id IS NOT NULL"
            " AND substr(a.path, 1, ?) = ?"
            " AND fe.version_id NOT IN (SELECT version_id FROM edges WHERE role = 'generated'"
            "   AND method IN ('wrapper', 'argpath', 'readset') AND run_id != ?)"
            " ORDER BY fe.ts, fe.event_id",
            (host, start, end, len(under), under, run_id),
        ).fetchall()
        # A background job that held a file open for writing owns all its changes.
        held = set()
        for (payload,) in conn.execute(
            "SELECT e.payload FROM runs r JOIN events e ON e.id = r.id"
            " WHERE r.host = ? AND r.id != ? AND e.type = 'run_start' AND r.ts_start <= ?"
            " AND COALESCE(r.ts_end, ?) >= ?",
            (host, run_id, end, end, start),
        ):
            held |= set((json.loads(payload).get("wrapper") or {}).get("written", []))
        latest = {art: (v, path) for art, v, path in rows}  # last version per artefact
        for v, path in latest.values():
            if path not in held:
                _add(conn, stats, run_id, v, "generated", "window", confidence)

    # 4. readset
    if reader is not None:
        found = reader(conn, run)
        if found:
            used, generated = found
            for path in used:
                art = _artifact(conn, host, path)
                v = _current_version(conn, art, start) if art else None
                if v:
                    _add(conn, stats, run_id, v, "used", "readset", CONFIDENCE["readset"])
            if end is not None:
                for path in generated:
                    art = _artifact(conn, host, path)
                    v = _window_version(conn, art, start, end) if art else None
                    if v:
                        _add(conn, stats, run_id, v, "generated", "readset", CONFIDENCE["readset"])


def _window_end(conn: sqlite3.Connection, run: sqlite3.Row) -> str | None:
    """``ts_end`` plus the grace, clipped at the next run's start on the same host.

    The grace catches writes that land just after the prompt returns; it must not
    hand a later command's outputs to this one when commands follow quickly.
    """
    if not run["ts_end"]:
        return None
    end = _plus(run["ts_end"], GRACE)
    nxt = conn.execute(
        "SELECT MIN(ts_start) FROM runs WHERE host = ? AND ts_start > ? AND id != ?",
        (run["host"], run["ts_start"], run["id"]),
    ).fetchone()[0]
    if nxt is not None and nxt < end:
        end = max(nxt, run["ts_end"])
    return end


def _like_prefix(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
