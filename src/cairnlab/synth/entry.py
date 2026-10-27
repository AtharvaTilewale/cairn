"""The daily entry pipeline (CLAUDE.md 9.1): gather -> prompt -> Muse Spark -> validate ->
render -> store.

The system prompt is fixed text, so the API's prompt cache applies across days. The
Glimmer path shrinks the input first (at most 4 figures, about 60K tokens of log),
because its context is 128K rather than 1M.
"""

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, tzinfo
from importlib import resources
from pathlib import Path

from jinja2 import Environment, PackageLoader, StrictUndefined
from ulid import ULID

from cairnlab.llm.client import LLMError
from cairnlab.llm.figures import to_data_url
from cairnlab.llm.schemas import ENTRY
from cairnlab.provenance.objects import object_path
from cairnlab.store.models import utc_now
from cairnlab.synth.claims import CITE, Dropped, validate_entry
from cairnlab.synth.gather import DayContext, gather, to_prompt

log = logging.getLogger(__name__)

LIMITS = {  # backend -> (max figures, max prompt characters)
    "spark": (12, 600_000),
    "glimmer": (4, 200_000),
}


@dataclass
class EntryResult:
    day: str
    markdown: str
    entry: dict
    dropped: list[Dropped]
    model: str
    context: DayContext
    prompt: str
    path: Path | None = None
    faithfulness: dict | None = None
    verdicts: list = field(default_factory=list)


def _system_prompt() -> str:
    return resources.files("cairnlab.llm").joinpath("prompts/entry.md").read_text()


def build_messages(ctx: DayContext, prompt: str, objects_dir: Path) -> list[dict]:
    """System prompt plus one user message: the log text, then each figure image."""
    content: list[dict] = [{"type": "text", "text": prompt}]
    attached: set[str] = set()
    for fig, _run in ctx.figures:
        if fig.sha in attached:
            continue  # byte-identical copy, already attached (the log says so)
        attached.add(fig.sha)
        data = object_path(objects_dir, fig.sha).read_bytes()
        try:
            url = to_data_url(data)
        except OSError as exc:
            log.warning("skipping figure %s: %s", fig.path, exc)
            continue
        content.append({"type": "text", "text": f"Figure [A:{fig.vid}] {fig.path}:"})
        content.append({"type": "image_url", "image_url": {"url": url}})
    return [{"role": "system", "content": _system_prompt()}, {"role": "user", "content": content}]


# --- rendering ------------------------------------------------------------------------


class _Footnotes:
    """Numbers each cited ID once, in order of first use, with a one-line description."""

    def __init__(self, conn: sqlite3.Connection, ctx: DayContext):
        self.conn = conn
        self.ctx = ctx
        self.order: dict[str, int] = {}

    def ref(self, ident: str) -> str:
        if ident not in self.order:
            self.order[ident] = len(self.order) + 1
        return f"[^{self.order[ident]}]"

    def refs(self, ids: list[str]) -> str:
        return "".join(self.ref(i) for i in dict.fromkeys(ids))

    def cite(self, text: str) -> str:
        return CITE.sub(lambda m: self.ref(m.group(1)), text or "")

    def label(self, ident: str) -> str:
        row = self.conn.execute(
            "SELECT a.path FROM artifact_versions v JOIN artifacts a ON a.id = v.artifact_id"
            " WHERE v.id = ?",
            (ident.removeprefix("A:"),),
        ).fetchone()
        return _short(row[0], self.ctx) if row else ident

    def describe(self, ident: str) -> str:
        kind, key = ident.split(":", 1)
        if kind == "R":
            r = self.conn.execute("SELECT * FROM runs WHERE id = ?", (key,)).fetchone()
            if r:
                when = datetime.fromisoformat(r["ts_start"]).astimezone(self.ctx.tz)
                cmd = (r["cmd"] or "").replace("`", "'")
                cmd = cmd if len(cmd) <= 90 else cmd[:87] + "..."
                status = f"exit {r['exit']}" if r["exit"] is not None else r["status"]
                return f"{when:%H:%M:%S} `{cmd}` ({status})"
        elif kind == "A":
            r = self.conn.execute(
                "SELECT a.path, v.sha256 FROM artifact_versions v JOIN artifacts a"
                " ON a.id = v.artifact_id WHERE v.id = ?",
                (key,),
            ).fetchone()
            if r:
                return f"{_short(r[0], self.ctx)} (sha256 {r[1][:12]})"
        elif kind == "N":
            r = self.conn.execute("SELECT text FROM notes WHERE id = ?", (key,)).fetchone()
            if r:
                text = r[0] or ""
                return f"note: “{text[:80]}{'...' if len(text) > 80 else ''}”"
        return ""

    def items(self) -> list[tuple[int, str, str]]:
        return [(n, i, self.describe(i)) for i, n in self.order.items()]


def _short(path: str, ctx: DayContext) -> str:
    for fig, _ in ctx.figures:
        if fig.abspath == path:
            return fig.path
    for phase in ctx.phases:
        for run in phase.runs:
            for f in run.used + run.generated:
                if f.abspath == path:
                    return f.path
    return path


def render(entry: dict, ctx: DayContext, conn: sqlite3.Connection, model: str) -> str:
    """Markdown for a validated entry; evidence IDs become numbered footnotes."""
    env = Environment(
        loader=PackageLoader("cairnlab.synth", "templates"),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,
    )
    notes = _Footnotes(conn, ctx)
    template = env.get_template("entry.md.j2")
    # Render twice: the first pass numbers footnotes in reading order.
    kwargs = dict(
        entry=entry,
        day=ctx.day,
        project=ctx.project,
        runs=ctx.run_count,
        model=model,
        cite=notes.cite,
        refs=notes.refs,
        label=notes.label,
    )
    template.render(**kwargs, footnotes=[])
    text = template.render(**kwargs, footnotes=notes.items())
    return re.sub(r"\n{3,}", "\n\n", text).rstrip() + "\n"


def _empty_markdown(ctx: DayContext) -> str:
    return f"# {ctx.day}\n\nNothing was recorded on this day.\n"


# --- pipeline -------------------------------------------------------------------------


def write_entry(
    conn: sqlite3.Connection,
    home: Path,
    day: date,
    client=None,
    tz: tzinfo | None = None,
    root: Path | None = None,
    backend: str = "spark",
    dry_run: bool = False,
) -> EntryResult:
    """Write (and store) the entry for local calendar ``day``.

    With ``dry_run`` no model is called: the result carries the prompt that would be sent.
    """
    tz = tz or datetime.now().astimezone().tzinfo
    max_figures, max_chars = LIMITS[backend]
    objects = home / "objects"
    ctx = gather(
        conn, objects, day, tz=tz, root=root, logs_dir=home / "logs", max_figures=max_figures
    )
    prompt = to_prompt(ctx, max_chars=max_chars)
    model = getattr(client, "model", "none")
    if ctx.empty or dry_run:
        md = _empty_markdown(ctx) if ctx.empty else ""
        result = EntryResult(ctx.day, md, {}, [], "none" if ctx.empty else model, ctx, prompt)
        if ctx.empty and not dry_run:
            _store(conn, home, result)
        return result
    messages = build_messages(ctx, prompt, objects)
    try:
        raw = client.structured(
            purpose="entry",
            messages=messages,
            schema_name="Entry",
            schema=ENTRY,
            reasoning_effort="medium",
        )
    except LLMError:
        if backend != "glimmer" or not ctx.figures:
            raise
        # A self-hosted server built without vision rejects images: write from text.
        log.warning("the Glimmer server rejected the figures; writing the entry from text")
        text = prompt + (
            "\n\n(The figures could not be shown to the model; describe them"
            " only from their file names and the log.)"
        )
        raw = client.structured(
            purpose="entry",
            messages=[messages[0], {"role": "user", "content": text}],
            schema_name="Entry",
            schema=ENTRY,
            reasoning_effort="medium",
        )
    entry, dropped = validate_entry(raw, ctx.valid_ids)
    for d in dropped:
        log.info("dropped %s (%s): %s", d.where, d.reason, d.text[:120])
    md = render(entry, ctx, conn, model)
    result = EntryResult(ctx.day, md, entry, dropped, model, ctx, prompt)
    _store(conn, home, result, raw)
    return result


def _store(
    conn: sqlite3.Connection, home: Path, result: EntryResult, raw: dict | None = None
) -> None:
    payload = {
        "entry": result.entry,
        "raw": raw,
        "dropped": [d.__dict__ for d in result.dropped],
        "faithfulness": result.faithfulness,
    }
    conn.execute(
        "INSERT INTO entries (id, day, created, model, json, markdown) VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(day) DO UPDATE SET created = excluded.created, model = excluded.model,"
        " json = excluded.json, markdown = excluded.markdown",
        (str(ULID()), result.day, utc_now(), result.model, json.dumps(payload), result.markdown),
    )
    out = home / "entries" / f"{result.day}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(result.markdown, encoding="utf-8")
    result.path = out


def save_faithfulness(conn: sqlite3.Connection, result: EntryResult) -> None:
    """Record the faithfulness check with the stored entry."""
    row = conn.execute("SELECT json FROM entries WHERE day = ?", (result.day,)).fetchone()
    if row is None:
        return
    payload = json.loads(row[0])
    payload["faithfulness"] = result.faithfulness
    payload["claims"] = [v.__dict__ for v in result.verdicts]
    conn.execute("UPDATE entries SET json = ? WHERE day = ?", (json.dumps(payload), result.day))
