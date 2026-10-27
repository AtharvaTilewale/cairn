"""Cairn command-line interface: one sub-command per feature."""

import logging
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from cairnlab import config
from cairnlab.store import db, repo
from cairnlab.store.models import utc_now

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
    out.print('Start capture in this terminal:  export CAIRN_USER=<you>; eval "$(cairn activate)"')


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
    """Print the bash capture hook; use as: eval "$(cairn activate)"."""
    _todo("activate")


@app.command()
def deactivate() -> None:
    """Remove the capture hook from this shell."""
    _todo("deactivate")


@app.command()
def ingest() -> None:
    """Load new JSONL events into the database (idempotent)."""
    _todo("ingest")


# --- not implemented yet -----------------------------------------------------------------


@app.command()
def watch() -> None:
    """Run the file watcher (HP, workstation; never the cluster)."""
    _todo("watch")


@app.command()
def scan(
    since: Annotated[str | None, typer.Option(help="Only files changed after this time.")] = None,
) -> None:
    """One-shot fingerprint pass (used on the cluster by sync)."""
    _todo("scan")


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
