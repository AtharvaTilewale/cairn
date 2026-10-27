"""Provenance graph queries: trace(), ancestors(), descendants() (CLAUDE.md section 7).

``trace`` walks ``generated <- run <- used`` backwards from one file version to the raw
inputs (versions no recorded run generated). The producer of a version is the run
with the strongest ``generated`` edge (earliest on ties: the run that first made that
content). Steps come back in topological order, which for recorded runs is start-time
order: a run can only use versions that existed before it started.
"""

import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

from cairnlab.capture.fingerprint import fingerprint


@dataclass
class Node:
    vid: str
    path: str
    host: str
    sha: str
    ts: str
    stored: bool
    kind: str
    fp: str


@dataclass
class Step:
    run: dict
    method: str  # how the producing edge was found
    confidence: float
    inputs: list[str] = field(default_factory=list)  # version ids used
    outputs: list[str] = field(default_factory=list)  # version ids on the traced path
    notes: list[str] = field(default_factory=list)


@dataclass
class Trace:
    target: str
    steps: list[Step]
    nodes: dict[str, Node]
    raw_inputs: list[str]

    def producer_of(self, vid: str) -> Step | None:
        return next((s for s in self.steps if vid in s.outputs), None)

    def to_dict(self) -> dict:
        return {
            "target": asdict(self.nodes[self.target]),
            "steps": [asdict(s) for s in self.steps],
            "nodes": {k: asdict(v) for k, v in self.nodes.items()},
            "raw_inputs": self.raw_inputs,
        }


def resolve_version(
    conn: sqlite3.Connection, host: str, path: str, at: str | None = None, sha: str | None = None
) -> str | None:
    """The version of ``host:path`` with hash prefix ``sha``, current at ``at``, or now."""
    row = conn.execute("SELECT id FROM artifacts WHERE host = ? AND path = ?", (host, path))
    art = row.fetchone()
    if art is None:
        return None
    if sha:
        found = conn.execute(
            "SELECT id FROM artifact_versions WHERE artifact_id = ? AND sha256 LIKE ?"
            " ORDER BY ts LIMIT 1",
            (art[0], sha.lower() + "%"),
        ).fetchone()
        return found[0] if found else None
    sql = "SELECT version_id FROM file_events WHERE artifact_id = ?"
    params: list = [art[0]]
    if at:
        sql += " AND ts <= ?"
        params.append(at)
    found = conn.execute(sql + " ORDER BY ts DESC, event_id DESC LIMIT 1", params).fetchone()
    return found[0] if found else None


def node(conn: sqlite3.Connection, vid: str) -> Node | None:
    r = conn.execute(
        "SELECT v.id, a.path, a.host, v.sha256, v.ts, v.stored, a.kind, v.fp"
        " FROM artifact_versions v JOIN artifacts a ON a.id = v.artifact_id WHERE v.id = ?",
        (vid,),
    ).fetchone()
    return Node(r[0], r[1], r[2], r[3], r[4], bool(r[5]), r[6] or "other", r[7]) if r else None


def producer(conn: sqlite3.Connection, vid: str) -> tuple[dict, str, float] | None:
    """(run, method, confidence) of the run that generated this version, if any."""
    r = conn.execute(
        "SELECT r.*, e.method, e.confidence FROM edges e JOIN runs r ON r.id = e.run_id"
        " WHERE e.version_id = ? AND e.role = 'generated'"
        " ORDER BY e.confidence DESC, r.ts_start ASC LIMIT 1",
        (vid,),
    ).fetchone()
    if r is None:
        return None
    # sqlite3.Row iterates over values, so .keys() is needed here.
    run = {k: r[k] for k in r.keys() if k not in ("method", "confidence")}  # noqa: SIM118
    return run, r["method"], r["confidence"]


def _inputs(conn: sqlite3.Connection, run_id: str) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT e.version_id FROM edges e JOIN artifact_versions v ON v.id = e.version_id"
            " JOIN artifacts a ON a.id = v.artifact_id WHERE e.run_id = ? AND e.role = 'used'"
            " ORDER BY a.path",
            (run_id,),
        )
    ]


def trace(conn: sqlite3.Connection, vid: str, max_steps: int = 500) -> Trace:
    """The chain of runs and versions that produced ``vid``."""
    steps: dict[str, Step] = {}
    nodes: dict[str, Node] = {}
    raw: list[str] = []
    stack, seen = [vid], set()
    while stack and len(steps) < max_steps:
        v = stack.pop()
        if v in seen:
            continue
        seen.add(v)
        n = node(conn, v)
        if n is None:
            continue
        nodes[v] = n
        found = producer(conn, v)
        if found is None:
            raw.append(v)
            continue
        run, method, confidence = found
        step = steps.get(run["id"])
        if step is None:
            inputs = [i for i in _inputs(conn, run["id"]) if i != v]
            notes = [
                t
                for (t,) in conn.execute(
                    "SELECT text FROM notes WHERE run_id = ? ORDER BY ts", (run["id"],)
                )
            ]
            step = steps[run["id"]] = Step(run, method, confidence, inputs, [], notes)
            stack.extend(reversed(inputs))
        if v not in step.outputs:
            step.outputs.append(v)
    ordered = sorted(steps.values(), key=lambda s: (s.run["ts_start"], s.run["id"]))
    return Trace(vid, ordered, nodes, sorted(raw, key=lambda v: nodes[v].path))


def ancestors(conn: sqlite3.Connection, vid: str) -> set[str]:
    """Every version ``vid`` was derived from."""
    t = trace(conn, vid)
    return set(t.nodes) - {vid}


def descendants(conn: sqlite3.Connection, vid: str, limit: int = 10_000) -> set[str]:
    """Every version derived (directly or not) from ``vid``."""
    out: set[str] = set()
    frontier = [vid]
    while frontier and len(out) < limit:
        v = frontier.pop()
        for (g,) in conn.execute(
            "SELECT DISTINCT g.version_id FROM edges u JOIN edges g ON g.run_id = u.run_id"
            " WHERE u.version_id = ? AND u.role = 'used' AND g.role = 'generated'",
            (v,),
        ):
            if g not in out and g != vid:
                out.add(g)
                frontier.append(g)
    return out


def input_status(
    conn: sqlite3.Connection, step: Step, root_map: tuple[str, Path] | None = None
) -> list[tuple[str, str]]:
    """For each input of ``step``: unchanged | changed since | missing, against disk now.

    ``root_map`` = (recorded prefix, local directory) relocates paths (tests, moved
    projects).
    """
    out = []
    for vid in step.inputs:
        n = node(conn, vid)
        if n is None:
            continue
        path = Path(n.path)
        if root_map and n.path.startswith(root_map[0].rstrip("/") + "/"):
            path = root_map[1] / n.path[len(root_map[0].rstrip("/")) + 1 :]
        if not path.is_file():
            out.append((n.path, "missing"))
            continue
        try:
            now = fingerprint(path)
        except OSError:
            out.append((n.path, "missing"))
            continue
        out.append((n.path, "unchanged" if now.sha256 == n.sha else "changed since"))
    return out
