"""Collect and compress one day of events into the context an entry is written from.

Everything the model may cite gets an ID prefix: ``R:`` runs, ``A:`` artefact
versions, ``N:`` notes. ``DayContext.valid_ids`` is the set of IDs shown, which is what
``synth/claims.py`` validates citations against.

Compression (CLAUDE.md 9.1): ignored commands never reach the store; consecutive
identical runs (same command, cwd, exit code and outputs) collapse into one line
with a count; failed runs carry the tail of their log when one exists; runs are
grouped into phases at gaps over 30 minutes.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path

from cairnlab.llm.figures import is_raster
from cairnlab.provenance.drift import diff, is_config
from cairnlab.provenance.objects import object_path
from cairnlab.store.models import format_ts

PHASE_GAP = timedelta(minutes=30)
TAIL_LINES = 20
TAIL_CHARS = 2000
OUTPUT_MAX_BYTES = 4096  # small text outputs are shown in full (up to OUTPUT_CHARS)
OUTPUT_CHARS = 1500
MAX_OUTPUTS = 15


@dataclass
class FileRef:
    vid: str
    path: str  # display path: relative to the project root when under it
    abspath: str
    kind: str
    sha: str
    stored: bool
    uncertain: bool = False  # only an ambiguous (0.4) edge points here


@dataclass
class RunItem:
    ids: list[str]  # runs collapsed into this line; ids[0] is the one to cite
    ts_start: str
    ts_end: str | None
    exit: int | None
    status: str
    cmd: str
    cwd: str
    used: list[FileRef] = field(default_factory=list)
    generated: list[FileRef] = field(default_factory=list)
    git: str | None = None
    env: str | None = None
    error_tail: str | None = None
    flags: list[str] = field(default_factory=list)

    @property
    def duration_s(self) -> float | None:
        if not self.ts_end:
            return None
        return (
            datetime.fromisoformat(self.ts_end) - datetime.fromisoformat(self.ts_start)
        ).total_seconds()


@dataclass
class Phase:
    index: int
    runs: list[RunItem]

    @property
    def start(self) -> str:
        return self.runs[0].ts_start

    @property
    def end(self) -> str:
        return max((r.ts_end or r.ts_start) for r in self.runs)


@dataclass
class DriftItem:
    path: str
    old: FileRef
    new: FileRef
    changes: list[tuple[str, str | None, str | None]]
    changed_by: str | None  # run id that generated the new version
    used_before: str | None  # last run that used the old version
    used_after: str | None  # first run that used the new version


@dataclass
class NoteItem:
    id: str
    ts: str
    text: str
    run_id: str | None
    source: str


@dataclass
class DayContext:
    day: str
    tz: tzinfo
    start: str
    end: str
    project: str
    phases: list[Phase]
    notes: list[NoteItem]
    drift: list[DriftItem]
    figures: list[tuple[FileRef, str]]  # (figure, run id that generated it)
    outputs: list[tuple[FileRef, str, str]]  # (small text output, run id, its content)
    valid_ids: set[str]
    run_count: int

    @property
    def empty(self) -> bool:
        return self.run_count == 0 and not self.notes


def day_bounds(day: date, tz: tzinfo) -> tuple[str, str]:
    """UTC [start, end) of a local calendar day."""
    start = datetime.combine(day, time(0), tzinfo=tz)
    return format_ts(start), format_ts(start + timedelta(days=1))


def _display(path: str, root: Path | None) -> str:
    if root is not None:
        prefix = str(root).rstrip("/") + "/"
        if path.startswith(prefix):
            return path[len(prefix) :]
        if path == str(root):
            return "."
    return path


def _read_object(objects_dir: Path, sha: str) -> str | None:
    p = object_path(objects_dir, sha)
    if not p.is_file():
        return None
    return p.read_bytes().decode("utf-8", errors="replace")


def _tail(text: str) -> str:
    lines = text.rstrip("\n").splitlines()[-TAIL_LINES:]
    return "\n".join(lines)[-TAIL_CHARS:]


def gather(
    conn: sqlite3.Connection,
    objects_dir: Path,
    day: date,
    tz: tzinfo = UTC,
    root: Path | None = None,
    logs_dir: Path | None = None,
    max_figures: int = 12,
) -> DayContext:
    """Everything recorded on local calendar ``day``, compressed for the prompt."""
    start, end = day_bounds(day, tz)
    rows = conn.execute(
        "SELECT r.*, e.payload FROM runs r JOIN events e ON e.id = r.id"
        " WHERE r.ts_start >= ? AND r.ts_start < ? ORDER BY r.ts_start, r.id",
        (start, end),
    ).fetchall()
    valid: set[str] = set()
    items: list[RunItem] = []
    last_git = last_env = None
    used_by: dict[str, list[tuple[str, str]]] = {}  # vid -> [(ts, run id)]
    generated_by: dict[str, str] = {}  # vid -> run id
    figure_runs: list[tuple[FileRef, str, str]] = []  # (figure, run id, ts)
    output_runs: dict[str, tuple[FileRef, str]] = {}  # path -> (latest output, run id)

    for r in rows:
        payload = json.loads(r["payload"])
        item = RunItem(
            ids=[r["id"]],
            ts_start=r["ts_start"],
            ts_end=r["ts_end"],
            exit=r["exit"],
            status=r["status"],
            cmd=r["cmd"] or "",
            cwd=_display(r["cwd"] or "", root),
        )
        if payload.get("ts_start_approx"):
            item.flags.append("start time approximate")
        if payload.get("hist_repeat"):
            item.flags.append("identical rerun")
        if r["shell"] == "cairn-run":
            item.flags.append(
                "background job"
                if r["status"] != "done" or payload.get("type") == "run_start"
                else "wrapped"
            )
        for e in conn.execute(
            "SELECT e.role, e.confidence, v.id, v.sha256, v.stored, a.path, a.kind"
            " FROM edges e JOIN artifact_versions v ON v.id = e.version_id"
            " JOIN artifacts a ON a.id = v.artifact_id WHERE e.run_id = ?"
            " ORDER BY e.role DESC, a.path",
            (r["id"],),
        ):
            ref = FileRef(
                vid=e["id"],
                path=_display(e["path"], root),
                abspath=e["path"],
                kind=e["kind"] or "other",
                sha=e["sha256"],
                stored=bool(e["stored"]),
                uncertain=e["confidence"] < 0.5,
            )
            if e["role"] == "used":
                item.used.append(ref)
                used_by.setdefault(ref.vid, []).append((r["ts_start"], r["id"]))
            else:
                item.generated.append(ref)
                generated_by[ref.vid] = r["id"]
                if ref.kind == "figure" and is_raster(ref.path):
                    figure_runs.append((ref, r["id"], r["ts_start"]))
                elif ref.kind in ("data", "config", "log", "other") and ref.stored:
                    output_runs[ref.abspath] = (ref, r["id"])  # latest version wins
        git = None
        if r["git_commit"]:
            git = f"{r['git_branch'] or 'detached'}@{r['git_commit'][:7]}"
            git += " (uncommitted changes)" if r["git_dirty"] else ""
        if git != last_git:
            item.git, last_git = git, git
        if r["env_name"] != last_env:
            item.env, last_env = r["env_name"], r["env_name"]
        if r["exit"] not in (0, None):
            item.error_tail = _error_tail(r, item, objects_dir, logs_dir)
        if items and _same(items[-1], item):
            items[-1].ids.append(r["id"])
        else:
            items.append(item)
        valid.add(f"R:{r['id']}")

    phases: list[Phase] = []
    for item in items:
        if phases:
            prev_end = datetime.fromisoformat(phases[-1].end)
            if datetime.fromisoformat(item.ts_start) - prev_end <= PHASE_GAP:
                phases[-1].runs.append(item)
                continue
        phases.append(Phase(len(phases) + 1, [item]))

    for item in items:
        valid |= {f"A:{f.vid}" for f in item.used + item.generated}

    notes = [
        NoteItem(n["id"], n["ts"], n["text"] or "", n["run_id"], n["source"] or "typed")
        for n in conn.execute(
            "SELECT * FROM notes WHERE ts >= ? AND ts < ? ORDER BY ts", (start, end)
        )
    ]
    valid |= {f"N:{n.id}" for n in notes}

    drift = _drift(conn, objects_dir, items, used_by, generated_by, start, end, root)
    for d in drift:
        valid |= {f"A:{d.old.vid}", f"A:{d.new.vid}"}

    latest: dict[str, tuple[FileRef, str, str]] = {}
    for ref, run_id, ts in figure_runs:
        if ref.stored and (ref.abspath not in latest or latest[ref.abspath][2] <= ts):
            latest[ref.abspath] = (ref, run_id, ts)
    figures = sorted(latest.values(), key=lambda x: x[2])[-max_figures:]

    outputs = _small_outputs(conn, objects_dir, output_runs)
    valid |= {f"A:{f.vid}" for f, _, _ in outputs}

    return DayContext(
        outputs=outputs,
        day=day.isoformat(),
        tz=tz,
        start=start,
        end=end,
        project=root.name if root else "",
        phases=phases,
        notes=notes,
        drift=drift,
        figures=[(f, rid) for f, rid, _ in figures],
        valid_ids=valid,
        run_count=len(rows),
    )


def _small_outputs(conn, objects_dir: Path, output_runs: dict) -> list[tuple[FileRef, str, str]]:
    """Text content of the day's small outputs (final versions), so values can be cited."""
    out = []
    for ref, run_id in list(output_runs.values())[-MAX_OUTPUTS:]:
        p = object_path(objects_dir, ref.sha)
        if not p.is_file() or p.stat().st_size > OUTPUT_MAX_BYTES:
            continue
        data = p.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue  # binary
        if "\x00" in text:
            continue
        if len(text) > OUTPUT_CHARS:
            text = text[:OUTPUT_CHARS] + "\n[... truncated ...]"
        out.append((ref, run_id, text))
    return out


def _same(a: RunItem, b: RunItem) -> bool:
    return (
        (a.cmd, a.cwd, a.exit, a.status) == (b.cmd, b.cwd, b.exit, b.status)
        and {f.sha for f in a.generated} == {f.sha for f in b.generated}
        and not b.git
        and not b.env
    )


def _error_tail(r, item: RunItem, objects_dir: Path, logs_dir: Path | None) -> str | None:
    if logs_dir is not None:
        log = logs_dir / f"{r['id']}.log"
        if log.is_file():
            return _tail(log.read_text(errors="replace"))
    for ref in item.generated:
        if ref.kind == "log" and ref.stored:
            text = _read_object(objects_dir, ref.sha)
            if text:
                return _tail(text)
    return None


EDITING_COMMANDS = {
    "sed",
    "vim",
    "vi",
    "nvim",
    "nano",
    "emacs",
    "code",
    "gedit",
    "perl",
    "awk",
    "ed",
    "patch",
    "cp",
    "mv",
    "tee",
    "echo",
    "printf",
    "cat",
    "git",
    "rsync",
    "scp",
}


def _program(cmd: str) -> str:
    words = [w for w in cmd.split() if "=" not in w or w.startswith("-")]
    return Path(words[0]).name if words else ""


def _ref(v, like: FileRef) -> FileRef:
    return FileRef(v["id"], like.path, like.abspath, like.kind, v["sha256"], bool(v["stored"]))


def _drift(conn, objects_dir, items, used_by, generated_by, start, end, root) -> list[DriftItem]:
    """Parameter changes in config files the day's runs touched.

    Only edits count: the new version came from no run (an editor save) or from an
    editing command. A config-like file a script wrote (results.json) is an output.
    """
    run_cmds = {i: item.cmd for item in items for i in item.ids}
    touched: dict[str, FileRef] = {}
    for item in items:
        for f in item.used + item.generated:
            if is_config(f.path):
                touched.setdefault(f.abspath, f)
    out = []
    for abspath, ref in touched.items():
        versions = conn.execute(
            "SELECT DISTINCT v.id, v.sha256, v.stored, MIN(fe.ts) AS first"
            " FROM file_events fe JOIN artifact_versions v ON v.id = fe.version_id"
            " JOIN artifacts a ON a.id = fe.artifact_id"
            " WHERE a.path = ? AND fe.ts < ? GROUP BY v.id ORDER BY first",
            (abspath, end),
        ).fetchall()
        # The version current when the day began, then each new one during the day.
        chain = [v for v in versions if v["first"] < start][-1:] + [
            v for v in versions if v["first"] >= start
        ]
        for old, new in zip(chain, chain[1:], strict=False):
            old_text = _read_object(objects_dir, old["sha256"])
            new_text = _read_object(objects_dir, new["sha256"])
            if old_text is None or new_text is None:
                continue
            changes = diff(abspath, old_text, new_text)
            if not changes:
                continue
            changer = generated_by.get(new["id"])
            if changer and _program(run_cmds.get(changer, "")) not in EDITING_COMMANDS:
                continue  # a script's output (results.json), not a parameter edit
            # The editing run itself is not interesting as a user of either version.
            before = sorted(u for u in used_by.get(old["id"], []) if u[1] != changer)
            after = sorted(u for u in used_by.get(new["id"], []) if u[1] != changer)
            out.append(
                DriftItem(
                    path=ref.path,
                    old=_ref(old, ref),
                    new=_ref(new, ref),
                    changes=changes,
                    changed_by=generated_by.get(new["id"]),
                    used_before=before[-1][1] if before else None,
                    used_after=after[0][1] if after else None,
                )
            )
    return out


# --- prompt text --------------------------------------------------------------------


def _clock(ts: str, tz: tzinfo) -> str:
    return datetime.fromisoformat(ts).astimezone(tz).strftime("%H:%M:%S")


def _dur(s: float | None) -> str:
    if s is None:
        return "still running"
    if s < 60:
        return f"{s:.1f} s"
    if s < 3600:
        return f"{s / 60:.1f} min"
    return f"{s / 3600:.1f} h"


def _files(refs: list[FileRef]) -> str:
    return ", ".join(f"[A:{f.vid}] {f.path}{' (uncertain)' if f.uncertain else ''}" for f in refs)


def render_run(item: RunItem, tz: tzinfo, with_used: bool = True) -> str:
    """One run as prompt text."""
    head = f"[R:{item.ids[0]}] {_clock(item.ts_start, tz)} ({_dur(item.duration_s)})"
    head += f" exit {item.exit}" if item.exit is not None else f" status {item.status}"
    head += f" · cwd {item.cwd or '.'}"
    if item.env:
        head += f" · env {item.env}"
    if item.git:
        head += f" · git {item.git}"
    if len(item.ids) > 1:
        others = ", ".join(f"[R:{i}]" for i in item.ids[1:])
        head += f" · ran {len(item.ids)} times identically (also {others})"
    if item.flags:
        head += f" · {', '.join(item.flags)}"
    lines = [head, f"  $ {item.cmd}"]
    if with_used and item.used:
        lines.append(f"  used: {_files(item.used)}")
    if item.generated:
        lines.append(f"  generated: {_files(item.generated)}")
    if item.error_tail:
        lines.append("  error output (last lines):")
        lines += [f"  | {ln}" for ln in item.error_tail.splitlines()]
    return "\n".join(lines)


def to_prompt(ctx: DayContext, max_chars: int = 400_000) -> str:
    """The day as text for the entry prompt, shrunk to ``max_chars`` if needed."""
    for with_used in (True, False):
        text = _prompt_text(ctx, with_used)
        if len(text) <= max_chars:
            return text
    return text[:max_chars] + "\n[... log truncated to fit the model's context ...]"


def _prompt_text(ctx: DayContext, with_used: bool) -> str:
    tzname = datetime.fromisoformat(ctx.start).astimezone(ctx.tz).tzname()
    out = [
        f"Project: {ctx.project or '(unnamed)'}",
        f"Day: {ctx.day} (times are local, {tzname})",
        f"Runs recorded: {ctx.run_count}",
        "",
    ]
    for ph in ctx.phases:
        out.append(
            f"## Phase {ph.index}: {_clock(ph.start, ctx.tz)}–{_clock(ph.end, ctx.tz)}"
            f" ({sum(len(r.ids) for r in ph.runs)} runs)"
        )
        out += [render_run(r, ctx.tz, with_used) for r in ph.runs]
        out.append("")
    out.append("## Notes")
    if ctx.notes:
        for n in ctx.notes:
            about = f" (about [R:{n.run_id}])" if n.run_id else ""
            out.append(f"[N:{n.id}] {_clock(n.ts, ctx.tz)}{about} ({n.source}): {n.text}")
    else:
        out.append("No notes were recorded today.")
    out.append("")
    out.append("## Parameter changes")
    if ctx.drift:
        for d in ctx.drift:
            changes = "; ".join(
                f"{k}: {o if o is not None else '(absent)'} → {n if n is not None else '(removed)'}"
                for k, o, n in d.changes
            )
            ctx_bits = []
            if d.changed_by:
                ctx_bits.append(f"changed by [R:{d.changed_by}]")
            if d.used_before:
                ctx_bits.append(f"old version last used by [R:{d.used_before}]")
            if d.used_after:
                ctx_bits.append(f"new version first used by [R:{d.used_after}]")
            out.append(
                f"- {d.path} [A:{d.old.vid}] → [A:{d.new.vid}]: {changes}"
                + (f" ({'; '.join(ctx_bits)})" if ctx_bits else "")
            )
    else:
        out.append("No parameter-file changes were detected today.")
    out.append("")
    out.append("## Small outputs (final version of each, content shown)")
    if ctx.outputs:
        for f, run_id, text in ctx.outputs:
            out.append(f"- [A:{f.vid}] {f.path}, generated by [R:{run_id}]:")
            out += [f"    {ln}" for ln in text.rstrip("\n").splitlines()]
    else:
        out.append("No small text outputs were generated today.")
    out.append("")
    out.append("## Figures (images attached in this order)")
    if ctx.figures:
        first: dict[str, str] = {}
        for f, run_id in ctx.figures:
            if f.sha in first:
                out.append(
                    f"- [A:{f.vid}] {f.path}, generated by [R:{run_id}]:"
                    f" byte-identical to {first[f.sha]} (not attached again)"
                )
            else:
                first[f.sha] = f.path
                out.append(f"- [A:{f.vid}] {f.path}, generated by [R:{run_id}]")
    else:
        out.append("No figures were generated today.")
    return "\n".join(out)
