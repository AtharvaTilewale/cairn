"""Cairn command-line interface: one sub-command per feature."""

import logging
import os
import re
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table
from ulid import ULID

from cairnlab import config
from cairnlab.capture.hooks import activation_script, deactivation_script
from cairnlab.capture.ingest import ingest as ingest_events
from cairnlab.capture.watcher import EventWriter, FileTracker, IgnoreRules
from cairnlab.capture.watcher import watch as watch_files
from cairnlab.store import db, repo
from cairnlab.store.models import normalize_ts, utc_now

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
            exit_code = "?" if r.exit is None else str(r.exit)
            style = "red" if r.exit not in (0, None) else None
            table.add_row(r.ts_start, r.host, exit_code, r.cmd, style=style)
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
    stats = ingest_events(conn, config.events_dir(home), config.objects_dir(home))
    conn.close()
    out.print(
        f"Ingested {stats.runs} new runs and {stats.files_seen} file events"
        f" from {stats.lines} lines in {stats.files} files"
        f" ({stats.duplicates} duplicates, {stats.malformed} malformed,"
        f" {stats.unsupported} unsupported).",
        soft_wrap=True,
    )
    if stats.malformed:
        err.print("Malformed lines were skipped; run with --verbose for details.")


# --- not implemented yet -----------------------------------------------------------------


RootOpt = Annotated[
    list[Path] | None,
    typer.Option("--root", help="Directory to cover (repeatable). Default: the project."),
]


def _file_tracker(home: Path, roots: RootOpt) -> tuple[FileTracker, list[Path]]:
    user = os.environ.get("CAIRN_USER", "")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", user):
        err.print("[red]Set CAIRN_USER[/red] (letters, digits, '.', '_', '-') first.")
        raise typer.Exit(1)
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
        rules=IgnoreRules(roots[0]) if len(roots) == 1 else _MultiRootRules(roots),
        writer=EventWriter(config.events_dir(home), host=host, user=user),
        objects_dir=config.objects_dir(home),
        state_path=config.state_dir(home) / f"files-{host}.json",
    )
    return tracker, roots


class _MultiRootRules:
    """Ignore rules for several roots: each path uses the rules of the root it is under."""

    def __init__(self, roots: list[Path]):
        self.by_root = [IgnoreRules(r) for r in roots]

    def ignored(self, path: Path, is_dir: bool = False) -> bool:
        for rules in self.by_root:
            if path.is_relative_to(rules.root):
                return rules.ignored(path, is_dir)
        return True


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
    """Run a command with before/after snapshots: cairn run [--bg] -- <cmd>."""
    _todo("run")


@app.command()
def note(text: Annotated[list[str] | None, typer.Argument(help="Note text.")] = None) -> None:
    """Add a typed note; with no text, record a voice note."""
    _todo("note")


@app.command()
def entry(
    day: Annotated[str, typer.Argument(help="today or YYYY-MM-DD.")] = "today",
    backend: Annotated[str, typer.Option(help="spark or glimmer.")] = "spark",
) -> None:
    """Write the daily notebook entry."""
    _todo("entry")


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
def show(id: Annotated[str, typer.Argument(help="Event, run or artefact id.")]) -> None:
    """Print an event, run or artefact."""
    _todo("show")


@app.command()
def serve() -> None:
    """Start the optional web viewer."""
    _todo("serve")
