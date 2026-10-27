"""The reproduce agent: Muse Spark plans, the researcher approves, then it re-runs and checks.

Phase 1 (plan) offers read-only tools: ``get_trace``, ``read_file``, ``estimate_cost``,
``list_files`` and ``propose_plan``. The plan is shown to the researcher; nothing runs
until it is approved (or ``--yes``). Phase 2 (execute) offers ``run_in_sandbox``
(approved runs only), ``compare``, ``read_file`` and ``finish``.

The model never decides whether a file was reproduced: the verdict comes from the
``compare`` results. Planning may make ``MAX_PLAN_CALLS`` tool calls and execution
``MAX_TOOL_CALLS``.
"""

import json
import logging
import os
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from cairnlab.agent.compare import Comparison, compare_file
from cairnlab.agent.sandbox import (
    DEFAULT_TIMEOUT_S,
    Refused,
    Replay,
    check_replayable,
    duration_s,
    replay,
    version_paths,
)
from cairnlab.provenance.graph import node, trace
from cairnlab.provenance.objects import object_path

log = logging.getLogger(__name__)

MAX_TOOL_CALLS = 15  # per phase: planning reads files, execution re-runs and compares
MAX_PLAN_CALLS = 12
READ_LIMIT = 20_000


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


_STR = {"type": "string"}
_IDS = {"type": "array", "items": {"type": "string"}}

PLAN_TOOLS = [
    _fn("get_trace", "The chain of recorded runs and file versions behind the target.", {}, []),
    _fn(
        "read_file",
        "Text of a stored file version (first 20 KB).",
        {"version_id": _STR},
        ["version_id"],
    ),
    _fn(
        "estimate_cost",
        "A run's original duration and whether it may be replayed.",
        {"run_id": _STR},
        ["run_id"],
    ),
    _fn(
        "list_files",
        "Stored file versions that existed when a run started (max 200).",
        {"run_id": _STR},
        ["run_id"],
    ),
    _fn(
        "propose_plan",
        "Propose the steps to re-run; the researcher must approve them.",
        {
            "summary": _STR,
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "run_id": _STR,
                        "check": {**_IDS, "description": "recorded output version ids to compare"},
                        "extra_inputs": {
                            **_IDS,
                            "description": "stored versions to add, if needed",
                        },
                        "timeout_s": {"type": "number"},
                        "why": _STR,
                    },
                    "required": ["run_id", "check", "why"],
                },
            },
        },
        ["summary", "steps"],
    ),
]
EXEC_TOOLS = [
    _fn(
        "run_in_sandbox",
        "Re-run an approved recorded run in a scratch directory.",
        {"run_id": _STR, "extra_inputs": _IDS},
        ["run_id"],
    ),
    _fn(
        "compare",
        "Compare a re-run's output with a recorded version.",
        {"run_id": _STR, "version_id": _STR},
        ["run_id", "version_id"],
    ),
    PLAN_TOOLS[1],
    _fn(
        "finish",
        "End with a short report for the researcher.",
        {"explanation": _STR},
        ["explanation"],
    ),
]


@dataclass
class PlanStep:
    run_id: str
    check: list[str]
    why: str
    extra_inputs: list[str] = field(default_factory=list)
    timeout_s: float = DEFAULT_TIMEOUT_S
    cmd: str = ""


@dataclass
class Plan:
    summary: str
    steps: list[PlanStep]
    rejected: list[str] = field(default_factory=list)  # steps dropped by policy, with why


@dataclass
class CheckResult:
    run_id: str
    version_id: str
    path: str
    outcome: str
    detail: str


@dataclass
class Report:
    target: str
    verdict: str  # reproduced | reproduced_close | not_reproduced | refused | inconclusive
    plan: Plan | None
    checks: list[CheckResult]
    replays: list[dict]
    explanation: str
    tool_calls: int

    def to_dict(self) -> dict:
        return asdict(self)


def verdict_from(checks: list[CheckResult], plan: Plan | None, approved: bool) -> str:
    """The verdict, from compare results only."""
    if plan is None or not plan.steps:
        return "refused" if plan is not None and plan.rejected else "inconclusive"
    if not approved:
        return "inconclusive"
    if not checks:
        return "inconclusive"
    if any(c.outcome in ("different", "unverifiable") for c in checks):
        return "not_reproduced"
    if any(c.outcome == "close" for c in checks):
        return "reproduced_close"
    return "reproduced"


def _prompt() -> str:
    return resources.files("cairnlab.llm").joinpath("prompts/reproduce.md").read_text()


class ReproduceAgent:
    """One reproduce session for one target file version."""

    def __init__(
        self,
        client,
        conn: sqlite3.Connection,
        objects_dir: Path,
        target_vid: str,
        confirm: Callable[[Plan], bool],
        say: Callable[[str], None] = lambda s: None,
        scratch_parent: Path | None = None,
        rel_tol: float = 1e-6,
    ):
        self.client = client
        self.conn = conn
        self.objects = objects_dir
        self.target = target_vid
        self.confirm = confirm
        self.say = say
        self.scratch_parent = scratch_parent
        self.rel_tol = rel_tol
        self.trace = trace(conn, target_vid)
        self.plan: Plan | None = None
        self.approved = False
        self.replays: dict[str, Replay] = {}
        self.checks: list[CheckResult] = []
        self.calls = 0
        self.explanation = ""

    # -- tools -----------------------------------------------------------------------

    def _run(self, run_id: str):
        return self.conn.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id.removeprefix("R:"),)
        ).fetchone()

    def _step(self, run_id: str):
        return next((s for s in self.trace.steps if s.run["id"] == run_id.removeprefix("R:")), None)

    def tool_get_trace(self) -> dict:
        def ref(vid):
            n = self.trace.nodes.get(vid) or node(self.conn, vid)
            return {"version_id": vid, "path": n.path, "stored": n.stored} if n else {}

        steps = []
        for s in self.trace.steps:
            run = self._run(s.run["id"])
            ok, why = self._replayable(run, s.inputs)
            steps.append(
                {
                    "run_id": s.run["id"],
                    "cmd": s.run["cmd"],
                    "cwd": s.run["cwd"],
                    "exit": s.run["exit"],
                    "duration_s": duration_s(run),
                    "inputs": [ref(v) for v in s.inputs],
                    "outputs_on_path": [ref(v) for v in s.outputs],
                    "notes": s.notes,
                    "replayable": ok,
                    "why_not": why,
                }
            )
        return {
            "target": ref(self.target),
            "steps": steps,
            "raw_inputs": [ref(v) for v in self.trace.raw_inputs],
        }

    def _replayable(self, run, inputs: list[str]) -> tuple[bool, str]:
        try:
            check_replayable(run, version_paths(self.conn, inputs), self.objects)
        except Refused as exc:
            return False, str(exc)
        return True, ""

    def tool_read_file(self, version_id: str) -> dict:
        n = node(self.conn, version_id.removeprefix("A:"))
        if n is None:
            return {"error": "unknown version"}
        obj = object_path(self.objects, n.sha)
        if not obj.is_file():
            return {"error": f"{n.path}: no stored copy"}
        data = obj.read_bytes()[:READ_LIMIT]
        if b"\x00" in data:
            return {"path": n.path, "error": "binary file"}
        return {"path": n.path, "text": data.decode("utf-8", errors="replace")}

    def tool_estimate_cost(self, run_id: str) -> dict:
        run = self._run(run_id)
        if run is None:
            return {"error": "unknown run"}
        step = self._step(run_id)
        ok, why = self._replayable(run, step.inputs if step else [])
        return {
            "run_id": run["id"],
            "original_duration_s": duration_s(run),
            "replayable": ok,
            "why_not": why,
            "env": run["env_name"],
            "git": run["git_commit"],
        }

    def tool_list_files(self, run_id: str) -> dict:
        run = self._run(run_id)
        if run is None:
            return {"error": "unknown run"}
        rows = self.conn.execute(
            "SELECT a.path, fe.version_id, v.stored, MAX(fe.ts) FROM file_events fe"
            " JOIN artifacts a ON a.id = fe.artifact_id"
            " JOIN artifact_versions v ON v.id = fe.version_id"
            " WHERE a.host = ? AND fe.ts < ? AND a.path LIKE ? ESCAPE '\\' GROUP BY a.id"
            " ORDER BY a.path"
            " LIMIT 200",
            (run["host"], run["ts_start"], _like(self._project_dir(run)) + "/%"),
        ).fetchall()
        return {"files": [{"path": p, "version_id": v} for p, v, stored, _ in rows if stored]}

    def _existing_files(self, run) -> set[str]:
        """Version ids an extra input may be: stored files present when the run started."""
        return {f["version_id"] for f in self.tool_list_files(run["id"])["files"]}

    def _project_dir(self, run) -> str:
        """The directory shared by the target and the run: where helper files would be."""
        return os.path.commonpath([run["cwd"] or "/", self.trace.nodes[self.target].path])

    def tool_propose_plan(self, summary: str = "", steps: list[dict] | str | None = None) -> dict:
        # Tolerate a JSON string or a missing field: a malformed call must not lose the plan.
        if isinstance(steps, str):
            try:
                steps = json.loads(steps)
            except ValueError:
                steps = []
        steps = [st for st in (steps or []) if isinstance(st, dict)]
        accepted, rejected = [], []
        for raw in steps:
            run = self._run(str(raw.get("run_id", "")))
            step = self._step(str(raw.get("run_id", "")))
            if run is None or step is None:
                rejected.append(f"{raw.get('run_id')}: not a run in this trace")
                continue
            extras = [e.removeprefix("A:") for e in raw.get("extra_inputs", []) or []]
            if not set(extras) <= self._existing_files(run):
                rejected.append(f"{run['cmd']}: extra inputs must come from list_files")
                continue
            ok, why = self._replayable(run, step.inputs + extras)
            if not ok:
                rejected.append(f"{run['cmd']}: {why}")
                continue
            check = [c.removeprefix("A:") for c in raw.get("check", []) or []]
            generated = {
                v
                for (v,) in self.conn.execute(
                    "SELECT version_id FROM edges WHERE run_id = ? AND role = 'generated'",
                    (run["id"],),
                )
            }
            check = [c for c in check if c in generated] or list(step.outputs)
            accepted.append(
                PlanStep(
                    run["id"],
                    check,
                    str(raw.get("why", "")),
                    extras,
                    float(raw.get("timeout_s") or DEFAULT_TIMEOUT_S),
                    run["cmd"],
                )
            )
        self.plan = Plan(summary, accepted, rejected)
        self.approved = bool(accepted) and self.confirm(self.plan)
        if not accepted:
            return {
                "approved": False,
                "rejected": rejected,
                "note": "nothing can be replayed; call finish to explain",
            }
        if not self.approved:
            return {"approved": False, "note": "the researcher declined; call finish"}
        return {
            "approved": True,
            "rejected": rejected,
            "steps": [s.run_id for s in accepted],
            "note": "now run each step with run_in_sandbox, then compare its outputs",
        }

    def tool_run_in_sandbox(self, run_id: str, extra_inputs: list[str] | None = None) -> dict:
        planned = next(
            (
                s
                for s in (self.plan.steps if self.plan else [])
                if s.run_id == run_id.removeprefix("R:")
            ),
            None,
        )
        if not self.approved or planned is None:
            return {"error": "only approved plan steps can be run"}
        run, step = self._run(run_id), self._step(run_id)
        extras = list(
            dict.fromkeys(planned.extra_inputs + [e.removeprefix("A:") for e in extra_inputs or []])
        )
        bad = [e for e in extras if e not in self._existing_files(run)]
        if bad:
            return {
                "error": "extra inputs must be stored files that existed when the run"
                f" started (see list_files): {', '.join(bad)}"
            }
        outputs = [p for p, _ in version_paths(self.conn, planned.check).values()]
        self.say(f"Re-running in a scratch directory: {run['cmd']}")
        try:
            r = replay(
                self.conn,
                run,
                step.inputs + extras,
                self.objects,
                timeout_s=planned.timeout_s,
                scratch_parent=self.scratch_parent,
                output_paths=outputs,
            )
        except Refused as exc:
            return {"error": str(exc)}
        self.replays[run["id"]] = r
        return {
            "exit": r.exit,
            "timed_out": r.timed_out,
            "seconds": round(r.seconds, 2),
            "produced": r.produced,
            "stderr_tail": r.stderr_tail[-1500:],
            "stdout_tail": r.stdout_tail[-800:],
        }

    def tool_compare(self, run_id: str, version_id: str) -> dict:
        r = self.replays.get(run_id.removeprefix("R:"))
        n = node(self.conn, version_id.removeprefix("A:"))
        if r is None or n is None:
            return {"error": "run that step with run_in_sandbox first" if n else "unknown version"}
        try:
            produced = r.local(n.path)
        except ValueError:
            return {"error": f"{n.path} is outside the replayed directory"}
        recorded = object_path(self.objects, n.sha) if n.stored else None
        c: Comparison = compare_file(produced, recorded, self.rel_tol)
        self.checks.append(CheckResult(r.run_id, n.vid, n.path, c.outcome, c.detail))
        self.say(f"  {n.path}: {c.outcome}{f' ({c.detail})' if c.detail else ''}")
        return {"outcome": c.outcome, "detail": c.detail, "matches": c.matches}

    def tool_finish(self, explanation: str) -> dict:
        self.explanation = explanation
        return {"ok": True}

    # -- loop ------------------------------------------------------------------------

    def run(self) -> Report:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _prompt()},
            {
                "role": "user",
                "content": f"Can {self.trace.nodes[self.target].path} (version"
                f" A:{self.target}) be reproduced? Start with get_trace.",
            },
        ]
        tools = PLAN_TOOLS
        finished = False
        self.plan_calls = 0
        while not finished:
            if self.plan is None and self.plan_calls >= MAX_PLAN_CALLS:
                break
            if self.plan is not None and self.calls - self.plan_calls >= MAX_TOOL_CALLS:
                break
            offered = tools
            if self.plan is None and self.plan_calls >= MAX_PLAN_CALLS - 4:
                # Planning budget nearly spent: the only move left is to propose a plan.
                offered = [PLAN_TOOLS[-1]]
                messages.append({"role": "user", "content": "Propose the plan now."})
            msg = self.client.complete(
                purpose="reproduce", messages=messages, tools=offered, reasoning_effort="high"
            )
            calls = list(getattr(msg, "tool_calls", None) or [])
            log.info(
                "reproduce turn (offered %s): %s",
                [t["function"]["name"] for t in offered],
                [(c.function.name, (c.function.arguments or "")[:200]) for c in calls]
                or (msg.content or "")[:200],
            )
            messages.append(_assistant(msg, calls))
            if not calls:
                if self.plan is not None and (not self.approved or self.checks):
                    self.explanation = self.explanation or (msg.content or "")
                    break
                messages.append(
                    {
                        "role": "user",
                        "content": "Use the tools; end with "
                        + ("finish." if tools is EXEC_TOOLS else "propose_plan."),
                    }
                )
                continue
            for call in calls:
                self.calls += 1
                if self.plan is None:
                    self.plan_calls += 1
                name = call.function.name
                try:
                    args = json.loads(call.function.arguments or "{}")
                except ValueError:
                    args = {}
                allowed = {t["function"]["name"] for t in offered}
                if name not in allowed:
                    result = {"error": f"{name} is not available now"}
                else:
                    try:
                        result = getattr(self, f"tool_{name}")(**args)
                    except TypeError as exc:
                        result = {"error": f"bad arguments: {exc}"}
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)}
                )
                if name == "propose_plan" and name in allowed:
                    tools = EXEC_TOOLS
                if name == "finish" and name in allowed:
                    finished = True
                if self.plan is None and self.plan_calls >= MAX_PLAN_CALLS:
                    break
                if self.plan is not None and self.calls - self.plan_calls >= MAX_TOOL_CALLS:
                    break
            if self.plan is not None and not self.approved and not finished:
                # Declined or nothing replayable: one more turn to explain, then stop.
                tools = [EXEC_TOOLS[-1]]
        verdict = verdict_from(self.checks, self.plan, self.approved)
        return Report(
            target=self.target,
            verdict=verdict,
            plan=self.plan,
            checks=self.checks,
            replays=[
                {
                    "run_id": r.run_id,
                    "exit": r.exit,
                    "timed_out": r.timed_out,
                    "seconds": round(r.seconds, 2),
                    "scratch": str(r.scratch),
                }
                for r in self.replays.values()
            ],
            explanation=self.explanation,
            tool_calls=self.calls,
        )


def _like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _assistant(msg, calls) -> dict:
    out: dict[str, Any] = {"role": "assistant", "content": getattr(msg, "content", None) or ""}
    if calls:
        out["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"},
            }
            for c in calls
        ]
    return out
