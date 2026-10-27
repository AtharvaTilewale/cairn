"""Cairn command-line interface: one sub-command per feature."""

import json
import logging
import os
import re
import signal
import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table
from ulid import ULID

from cairnlab import config
from cairnlab.capture.envsnap import EnvSnapshotter
from cairnlab.capture.gitsnap import GitSnapshotter
from cairnlab.capture.hooks import activation_script, deactivation_script
from cairnlab.capture.ingest import Snapshots
from cairnlab.capture.ingest import ingest as ingest_events
from cairnlab.capture.longrun import start_background
from cairnlab.capture.runwrap import WrapContext, run_wrapped
from cairnlab.capture.watcher import EventWriter, FileTracker, rules_for
from cairnlab.capture.watcher import watch as watch_files
from cairnlab.llm.client import Client, LLMError
from cairnlab.provenance.linker import GRACE
from cairnlab.provenance.linker import link as link_runs
from cairnlab.provenance.readset import ReadsetReader
from cairnlab.store import db, repo
from cairnlab.store.models import format_ts, normalize_ts, utc_now
from cairnlab.synth.claims import check_faithfulness
from cairnlab.synth.claims import summarize as summarize_claims
from cairnlab.synth.entry import save_faithfulness, write_entry

app = typer.Typer(
    help="Cairn: the lab notebook that writes itself.",
    no_args_is_help=True,
    add_completion=False,
)
out = Console()
err = Console(stderr=True)
log = logging.getLogger("cairnlab")


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Cairn: the lab notebook that writes itself."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(message)s",
        handlers=[RichHandler(console=err, show_path=False)],
        force=True,
    )


def _todo(name: str) -> None:
    out.print(f"cairn {name}: not implemented yet")


def _require_home() -> Path:
    home = config.find_home()
    if home is None or not config.db_path(home).is_file():
        err.print("[red]No Cairn store here.[/red] Run [bold]cairn init[/bold] in your project.")
        raise typer.Exit(1)
    return home


# --- implemented -------------------------------------------------------------------------


@app.command()
def init(
    path: Annotated[Path, typer.Argument(help="Project directory.")] = Path("."),
) -> None:
    """Create .cairn/ (config, database, event dir) and .cairnignore. Touches no dotfiles."""
    project = path.expanduser().resolve()
    if project == Path.home().resolve():
        err.print(
            "[red]Refusing to initialise your home directory[/red] "
            "(it would put .cairn and .cairnignore in $HOME). Use a project directory."
        )
        raise typer.Exit(2)
    if not project.is_dir():
        err.print(f"[red]Not a directory:[/red] {project}")
        raise typer.Exit(2)

    home = project / config.CAIRN_DIR
    for sub in ("", "events", "objects", "entries", "notes"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    # Keeps the whole store out of the project's git without editing its .gitignore.
    gitignore = home / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n")
    cfg = home / "config.toml"
    if not cfg.exists():
        cfg.write_text(config.default_config(project.name, utc_now()))
    ignore = project / config.IGNORE_FILE
    if not ignore.exists():
        ignore.write_text(config.DEFAULT_IGNORE)

    conn = db.connect(config.db_path(home))
    applied = db.migrate(conn)
    conn.close()

    out.print(f"Cairn store ready: {home}")
    if applied:
        out.print(f"  applied migrations: {', '.join(map(str, applied))}")
    out.print(
        'Start capture in this terminal:  export CAIRN_USER=<you>; eval "$(cairn activate)"',
        soft_wrap=True,
    )


@app.command()
def status() -> None:
    """Show the database path, event and run counts, and the last event time per host."""
    home = _require_home()
    conn = db.connect(config.db_path(home))
    db.migrate(conn)
    out.print(f"Store:    {home}")
    out.print(f"Database: {config.db_path(home)}")
    out.print(f"Events: {repo.count(conn, 'events')}   Runs: {repo.count(conn, 'runs')}")
    out.print(
        f"Files: {repo.count(conn, 'artifacts')} artefacts,"
        f" {repo.count(conn, 'artifact_versions')} versions,"
        f" {repo.count(conn, 'file_events')} changes, {repo.count(conn, 'edges')} edges"
    )

    hosts = repo.hosts(conn)
    if hosts:
        table = Table("host", "events", "last event (UTC)")
        for h in hosts:
            table.add_row(h.host, str(h.events), h.last_ts)
        out.print(table)

    recent = repo.recent_runs(conn, limit=10)
    if recent:
        table = Table("start (UTC)", "host", "exit", "command", title="Recent runs")
        for r in recent:
            exit_code = r.status if r.status != "done" else "?" if r.exit is None else str(r.exit)
            style = "red" if r.exit not in (0, None) else None
            table.add_row(r.ts_start, r.host, exit_code, r.cmd, style=style)
        out.print(table)

    notes = conn.execute(
        "SELECT n.ts, n.text, r.cmd FROM notes n LEFT JOIN runs r ON r.id = n.run_id"
        " ORDER BY n.ts DESC LIMIT 5"
    ).fetchall()
    if notes:
        table = Table("time (UTC)", "note", "about", title="Recent notes")
        for ts, text, about in reversed(notes):
            table.add_row(ts, text, about or "-")
        out.print(table)

    offsets = repo.ingest_offsets(conn)
    events = config.events_dir(home)
    pending = 0
    if events.is_dir():
        for f in events.glob("*.jsonl"):
            pending += max(0, f.stat().st_size - offsets.get(f.name, 0))
    if pending:
        out.print(f"Not yet ingested: {pending} bytes of JSONL (run [bold]cairn ingest[/bold])")
    conn.close()


@app.command()
def activate() -> None:
    """Print the bash capture hook; use as: eval "$(cairn activate)".

    Needs CAIRN_USER set in the shell. Events go to $CAIRN_HOME, else the nearest
    .cairn/ above the current directory, else ~/.cairn-$CAIRN_USER.
    """
    if sys.stdout.isatty():
        err.print('Use this as: [bold]eval "$(cairn activate)"[/bold]')
        raise typer.Exit(1)
    sys.stdout.write(activation_script(home=config.find_home(), session=str(ULID())))


@app.command()
def deactivate() -> None:
    """Remove the capture hook from this shell (restores PROMPT_COMMAND, DEBUG trap, PS1)."""
    if sys.stdout.isatty():
        err.print(
            "Capture is not active in this shell, or was started elsewhere. "
            "In an activated shell run [bold]cairn deactivate[/bold] or "
            '[bold]eval "$(cairn deactivate)"[/bold].'
        )
        return
    sys.stdout.write(deactivation_script())


@app.command()
def ingest() -> None:
    """Load new JSONL events into the database (idempotent)."""
    home = config.find_home()
    if home is None:
        err.print("[red]No Cairn store here.[/red] Run [bold]cairn init[/bold] in your project.")
        raise typer.Exit(1)
    conn = db.connect(config.db_path(home))
    db.migrate(conn)
    snapshots = Snapshots(host=config.local_host(), git=GitSnapshotter(), env=EnvSnapshotter())
    stats = ingest_events(
        conn,
        config.events_dir(home),
        config.objects_dir(home),
        snapshots,
        lost_check_host=config.local_host(),
    )
    linked = None
    if stats.earliest is not None:
        # New events can change the edges of any run whose window reaches them.
        linked = link_runs(conn, since=_minus_grace(stats.earliest), reader=_reader(home))
    conn.close()
    out.print(
        f"Ingested {stats.runs} new runs and {stats.files_seen} file events"
        + (f", {stats.notes} notes" if stats.notes else "")
        + f" from {stats.lines} lines in {stats.files} files"
        f" ({stats.duplicates} duplicates, {stats.malformed} malformed,"
        f" {stats.unsupported} unsupported).",
        soft_wrap=True,
    )
    if linked:
        out.print(f"Linked {linked.runs} runs: {_edge_summary(linked.edges)}.", soft_wrap=True)


def _reader(home: Path) -> ReadsetReader | None:
    """The readset method for linking, or None when disabled or no API key is set."""
    if not config.readset_enabled(home):
        return None
    try:
        client = Client(home=home)
    except LLMError as exc:
        log.info("readset off: %s", exc)
        return None
    return ReadsetReader(client, config.objects_dir(home))


def _minus_grace(ts: str) -> str:
    return format_ts(datetime.fromisoformat(ts) - GRACE)


def _edge_summary(edges: dict[str, int]) -> str:
    if not edges:
        return "no edges"
    return ", ".join(f"{n} {method}" for method, n in sorted(edges.items()))


@app.command()
def link() -> None:
    """Recompute provenance edges for every run (after a linker change)."""
    home = _require_home()
    conn = db.connect(config.db_path(home))
    db.migrate(conn)
    stats = link_runs(conn, reader=_reader(home))
    conn.close()
    out.print(f"Linked {stats.runs} runs: {_edge_summary(stats.edges)}.", soft_wrap=True)
    if stats.malformed:
        err.print("Malformed lines were skipped; run with --verbose for details.")


# --- not implemented yet -----------------------------------------------------------------


RootOpt = Annotated[
    list[Path] | None,
    typer.Option("--root", help="Directory to cover (repeatable). Default: the project."),
]


def _cairn_user() -> str:
    user = os.environ.get("CAIRN_USER", "")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", user):
        err.print("[red]Set CAIRN_USER[/red] (letters, digits, '.', '_', '-') first.")
        raise typer.Exit(1)
    return user


def _file_tracker(home: Path, roots: RootOpt) -> tuple[FileTracker, list[Path]]:
    user = _cairn_user()
    if roots:
        roots = [r.expanduser().resolve() for r in roots]
    else:
        project = config.project_root(home)
        if project is None:
            err.print("[red]This store is not inside a project;[/red] pass --root DIR.")
            raise typer.Exit(2)
        roots = [project]
    host = config.local_host()
    tracker = FileTracker(
        rules=rules_for(roots),
        writer=EventWriter(config.events_dir(home), host=host, user=user),
        objects_dir=config.objects_dir(home),
        state_path=config.state_dir(home) / f"files-{host}.json",
    )
    return tracker, roots


@app.command()
def watch(root: RootOpt = None) -> None:
    """Run the file watcher in the foreground (HP, workstation; never the cluster). Ctrl-C stops."""
    if config.is_eddie():
        err.print("[red]cairn watch never runs on the cluster[/red] (no daemons on login nodes).")
        err.print("Use [bold]cairn scan[/bold]; [bold]cairn sync cluster[/bold] runs it for you.")
        raise typer.Exit(2)
    home = _require_home()
    tracker, roots = _file_tracker(home, root)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    def ready(n: int) -> None:
        err.print(f"Baseline: {n} new or changed files fingerprinted.")
        err.print(f"Watching {', '.join(map(str, roots))}  (Ctrl-C to stop)")

    err.print("Scanning for changes made while nothing was watching...")
    worker = threading.Thread(
        target=watch_files,
        args=(tracker, roots, stop),
        kwargs={"baseline": True, "on_ready": ready},
        daemon=True,
    )
    worker.start()
    try:
        while worker.is_alive():
            worker.join(0.5)
    except KeyboardInterrupt:
        pass
    stop.set()
    worker.join(timeout=15)
    err.print("Watcher stopped.")


@app.command()
def scan(
    since: Annotated[
        str | None, typer.Option(help="Only files modified after this ISO-8601 time.")
    ] = None,
    root: RootOpt = None,
) -> None:
    """One-shot fingerprint pass that exits immediately (used on the cluster by sync)."""
    # A spoke (the cluster) has a store directory for events but no database.
    home = config.find_home()
    if home is None:
        err.print("[red]No Cairn store:[/red] set CAIRN_HOME or run [bold]cairn init[/bold].")
        raise typer.Exit(1)
    tracker, roots = _file_tracker(home, root)
    since_epoch = None
    if since:
        try:
            since_epoch = datetime.fromisoformat(normalize_ts(since)).timestamp()
        except ValueError:
            err.print(f"[red]Bad --since:[/red] {since!r} (use e.g. 2026-10-28T09:00:00Z)")
            raise typer.Exit(2) from None
    n = sum(tracker.scan(r, since=since_epoch) for r in roots)
    tracker.save_state()
    out.print(f"Scanned {', '.join(map(str, roots))}: {n} file events written.")


@app.command()
def sync(aliases: Annotated[list[str] | None, typer.Argument(help="Host aliases.")] = None) -> None:
    """Pull spoke JSONL over SSH, scan the cluster, reconcile SGE jobs, ingest."""
    _todo("sync")


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def run(
    ctx: typer.Context,
    bg: Annotated[bool, typer.Option("--bg", help="Detached long run.")] = False,
) -> None:
    """Run a command and record exactly which files it wrote: cairn run [--bg] -- <cmd>.

    A single quoted argument runs through bash (pipes and redirects work). With --bg
    the command runs detached, its output goes to .cairn/logs/<run_id>.log, and its
    start and end are recorded even if this terminal closes.
    """
    argv = list(ctx.args)
    if not argv:
        err.print("Usage: [bold]cairn run [--bg] -- <command> [args...][/bold]")
        raise typer.Exit(2)
    home = config.find_home()
    if home is None:
        err.print("[red]No Cairn store:[/red] set CAIRN_HOME or run [bold]cairn init[/bold].")
        raise typer.Exit(1)
    user = _cairn_user()
    cwd = Path.cwd()
    project = config.project_root(home)
    roots = [project] if project and cwd.is_relative_to(project) else [cwd]
    wctx = WrapContext(
        home=home,
        roots=roots,
        host=config.local_host(),
        user=user,
        session=os.environ.get("CAIRN_SESSION") or str(ULID()),
    )
    if bg:
        run_id, log = start_background(wctx, argv, cwd)
        err.print(f"Started run {run_id} in the background. Output: {log}")
        return
    raise typer.Exit(run_wrapped(wctx, argv, cwd))


@app.command()
def note(
    text: Annotated[list[str] | None, typer.Argument(help="Note text.")] = None,
    run_id: Annotated[
        str | None, typer.Option("--run", help="The run it is about (default: the last one).")
    ] = None,
) -> None:
    """Record why: a note pinned to the last command in this terminal (or --run R:<id>)."""
    words = " ".join(text or []).strip()
    if not words:
        _todo("note (voice)")
        return
    home = config.find_home()
    if home is None:
        err.print("[red]No Cairn store:[/red] set CAIRN_HOME or run [bold]cairn init[/bold].")
        raise typer.Exit(1)
    user = _cairn_user()
    host = config.local_host()
    event = {
        "type": "note",
        "id": str(ULID()),
        "session": os.environ.get("CAIRN_SESSION"),
        "ts": utc_now(),
        "text": words,
        "source": "typed",
    }
    if run_id:
        event["run"] = run_id if run_id.startswith("R:") else f"R:{run_id}"
    EventWriter(config.events_dir(home), host, user, suffix="note").write(event)
    err.print("Noted.")


def _parse_day(text: str, tz) -> date:
    today = datetime.now(tz).date()
    if text == "today":
        return today
    if text == "yesterday":
        return today - timedelta(days=1)
    try:
        return date.fromisoformat(text)
    except ValueError:
        err.print(f"[red]Bad day:[/red] {text!r} (use today, yesterday or YYYY-MM-DD)")
        raise typer.Exit(2) from None


def _usage_cost(home: Path, since_bytes: int) -> float:
    usage = home / "usage.jsonl"
    if not usage.is_file():
        return 0.0
    with usage.open("rb") as fh:
        fh.seek(since_bytes)
        return sum(json.loads(line)["cost_usd"] for line in fh.read().splitlines() if line)


@app.command()
def entry(
    day: Annotated[str, typer.Argument(help="today, yesterday or YYYY-MM-DD.")] = "today",
    backend: Annotated[str, typer.Option(help="spark (Meta Model API) or glimmer.")] = "spark",
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would be sent; call nothing.")
    ] = False,
    check: Annotated[
        bool, typer.Option("--check/--no-check", help="Verify the entry's claims afterwards.")
    ] = True,
) -> None:
    """Write the day's notebook entry with Muse Spark; every claim cites the log."""
    home = _require_home()
    conn = db.connect(config.db_path(home))
    db.migrate(conn)
    tz = config.timezone(home)
    the_day = _parse_day(day, tz)
    root = config.project_root(home)
    if dry_run:
        res = write_entry(conn, home, the_day, tz=tz, root=root, backend=backend, dry_run=True)
        out.print(res.prompt, markup=False, highlight=False, soft_wrap=True)
        err.print(
            f"{len(res.context.figures)} figures would be attached;"
            f" about {len(res.prompt) // 4:,} tokens of log text."
        )
        return
    try:
        client = Client(home=home, backend=backend)
    except LLMError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    usage = home / "usage.jsonl"
    before = usage.stat().st_size if usage.is_file() else 0
    err.print(f"Writing the entry for {the_day} with {client.model}...")
    try:
        res = write_entry(conn, home, the_day, client=client, tz=tz, root=root, backend=backend)
        if check and res.entry:
            err.print("Checking its claims against the store...")
            res.verdicts = check_faithfulness(
                client, conn, config.objects_dir(home), res.markdown, res.prompt
            )
            res.faithfulness = summarize_claims(res.verdicts)
            save_faithfulness(conn, res)
    except LLMError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    finally:
        conn.close()
    out.print(f"Entry written: {res.path}")
    if res.dropped:
        out.print(f"  {len(res.dropped)} uncited or invalid items dropped (see --verbose)")
    if res.faithfulness:
        f = res.faithfulness
        score = "n/a" if f["faithfulness"] is None else f"{f['faithfulness']:.0%}"
        out.print(
            f"  claims: {f['supported']} supported, {f['unsupported']} unsupported,"
            f" {f['unverifiable']} unverifiable (faithfulness {score})"
        )
        for v in res.verdicts:
            if v.verdict == "unsupported":
                out.print(f"  [yellow]unsupported:[/yellow] {v.text}", soft_wrap=True)
    out.print(f"  model cost: ${_usage_cost(home, before):.4f}")


@app.command()
def trace(path: Annotated[str, typer.Argument(help="Artefact path.")]) -> None:
    """Show the provenance chain and regenerate command for a file."""
    _todo("trace")


@app.command()
def reproduce(
    path: Annotated[str, typer.Argument(help="Artefact path.")],
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask before executing.")] = False,
) -> None:
    """Re-run the cheap final steps for a file and verify the output matches."""
    _todo("reproduce")


@app.command()
def ask(question: Annotated[str, typer.Argument(help="Question about the log.")]) -> None:
    """Answer a question over the log, with citations."""
    _todo("ask")


@app.command()
def report(period: Annotated[str, typer.Argument(help="week")] = "week") -> None:
    """Write the weekly supervisor update."""
    _todo("report")


@app.command()
def show(ident: Annotated[str, typer.Argument(help="R:<run>, A:<version>, N:<note> or a ULID.")]):
    """Print a run, a file version or a note (the IDs cited in entries)."""
    home = _require_home()
    conn = db.connect(config.db_path(home))
    db.migrate(conn)
    tz = config.timezone(home)
    kind, _, key = ident.rpartition(":")
    key = key.upper()
    try:
        if kind in ("", "R") and _show_run(conn, key, tz):
            return
        if kind in ("", "A") and _show_version(conn, key, tz):
            return
        if kind in ("", "N") and _show_note(conn, key, tz):
            return
    finally:
        conn.close()
    err.print(f"[red]Nothing with id {ident!r} in this store.[/red]")
    raise typer.Exit(1)


def _local(ts: str | None, tz) -> str:
    if not ts:
        return "-"
    return datetime.fromisoformat(ts).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z")


def _show_run(conn, key: str, tz) -> bool:
    r = conn.execute("SELECT * FROM runs WHERE id = ?", (key,)).fetchone()
    if r is None:
        return False
    out.print(f"[bold]Run R:{r['id']}[/bold]")
    out.print(f"  $ {r['cmd']}", markup=False, soft_wrap=True)
    status = f"exit {r['exit']}" if r["exit"] is not None else r["status"]
    out.print(f"  {_local(r['ts_start'], tz)} → {_local(r['ts_end'], tz)}  ({status})")
    out.print(f"  {r['user']}@{r['host']}  cwd {r['cwd']}", markup=False)
    if r["git_commit"]:
        dirty = {1: ", uncommitted changes", 0: ", clean"}.get(r["git_dirty"], "")
        out.print(f"  git {r['git_branch']}@{r['git_commit'][:12]}{dirty}")
        if r["git_diffstat"]:
            out.print(f"      {r['git_diffstat']}")
    if r["env_name"]:
        out.print(f"  env {r['env_name']}" + (f" ({r['env_hash'][:12]})" if r["env_hash"] else ""))
    table = Table("role", "file", "version", "method", "confidence")
    for e in conn.execute(
        "SELECT e.role, a.path, v.id, v.sha256, e.method, e.confidence FROM edges e"
        " JOIN artifact_versions v ON v.id = e.version_id JOIN artifacts a"
        " ON a.id = v.artifact_id WHERE e.run_id = ? ORDER BY e.role DESC, a.path",
        (key,),
    ):
        table.add_row(
            e["role"],
            e["path"],
            f"A:{e['id']} ({e['sha256'][:8]})",
            e["method"],
            f"{e['confidence']:.1f}",
        )
    if table.row_count:
        out.print(table)
    return True


def _show_version(conn, key: str, tz) -> bool:
    v = conn.execute(
        "SELECT v.*, a.path, a.host, a.kind FROM artifact_versions v JOIN artifacts a"
        " ON a.id = v.artifact_id WHERE v.id = ?",
        (key,),
    ).fetchone()
    if v is None:
        return False
    out.print(f"[bold]File version A:{v['id']}[/bold]")
    out.print(f"  {v['host']}:{v['path']}  ({v['kind']})", markup=False)
    out.print(f"  sha256 {v['sha256']} ({v['fp']})  {v['size']} bytes")
    out.print(
        f"  first seen {_local(v['ts'], tz)}; "
        + ("copy stored in .cairn/objects" if v["stored"] else "no stored copy")
    )
    for role in ("generated", "used"):
        rows = conn.execute(
            "SELECT r.id, r.ts_start, r.cmd FROM edges e JOIN runs r ON r.id = e.run_id"
            " WHERE e.version_id = ? AND e.role = ? ORDER BY r.ts_start",
            (key, role),
        ).fetchall()
        for r in rows:
            label = "generated by" if role == "generated" else "used by"
            out.print(
                f"  {label} R:{r['id']} {_local(r['ts_start'], tz)}: {r['cmd']}",
                markup=False,
                soft_wrap=True,
            )
    return True


def _show_note(conn, key: str, tz) -> bool:
    n = conn.execute("SELECT * FROM notes WHERE id = ?", (key,)).fetchone()
    if n is None:
        return False
    out.print(f"[bold]Note N:{n['id']}[/bold] ({n['source']}, {_local(n['ts'], tz)})")
    out.print(f"  {n['text']}", markup=False, soft_wrap=True)
    if n["run_id"]:
        out.print(f"  about run R:{n['run_id']}")
    return True


@app.command()
def serve() -> None:
    """Start the optional web viewer."""
    _todo("serve")
