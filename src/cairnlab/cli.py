"""Cairn command-line interface: one sub-command per feature."""

import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
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
from cairnlab.agent.reproduce import ReproduceAgent
from cairnlab.capture.envsnap import EnvSnapshotter
from cairnlab.capture.gitsnap import GitSnapshotter
from cairnlab.capture.hooks import activation_script, deactivation_script
from cairnlab.capture.ingest import Snapshots
from cairnlab.capture.ingest import ingest as ingest_events
from cairnlab.capture.longrun import start_background
from cairnlab.capture.runwrap import WrapContext, run_wrapped
from cairnlab.capture.sync import SyncError, hosts_file, load_hosts
from cairnlab.capture.sync import sync as sync_spoke
from cairnlab.capture.watcher import EventWriter, FileTracker, rules_for
from cairnlab.capture.watcher import watch as watch_files
from cairnlab.llm.client import Client, LLMError
from cairnlab.provenance.graph import input_status, resolve_version
from cairnlab.provenance.graph import node as graph_node
from cairnlab.provenance.graph import trace as trace_graph
from cairnlab.provenance.linker import GRACE
from cairnlab.provenance.linker import link as link_runs
from cairnlab.provenance.readset import ReadsetReader
from cairnlab.store import db, repo
from cairnlab.store.models import format_ts, normalize_ts, utc_now
from cairnlab.synth.ask import Asker
from cairnlab.synth.claims import check_faithfulness
from cairnlab.synth.claims import summarize as summarize_claims
from cairnlab.synth.entry import save_faithfulness, write_entry
from cairnlab.synth.report import write_report as write_weekly
from cairnlab.voice.record import MAX_SECONDS as VOICE_MAX_SECONDS
from cairnlab.voice.record import RATE as VOICE_RATE
from cairnlab.voice.record import Toggle, to_wav
from cairnlab.voice.record import record as record_audio
from cairnlab.voice.transcribe import transcribe_wav
from cairnlab.web.export import Exporter

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

    usage = _usage_summary(home)
    if usage:
        table = Table("model", "calls", "cost (USD)", "generation speed", title="Model usage")
        for model, (n, cost, speeds) in sorted(usage.items()):
            speed = f"{sum(speeds) / len(speeds):.1f} tokens/s" if speeds else "-"
            table.add_row(model, str(n), f"{cost:.4f}", speed)
        out.print(table)

    offsets = repo.ingest_offsets(conn)
    events = config.events_dir(home)
    pending = 0
    if events.is_dir():
        for f in events.rglob("*.jsonl"):
            key = f.relative_to(events).as_posix()
            pending += max(0, f.stat().st_size - offsets.get(key, 0))
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
    sys.stdout.write(
        activation_script(
            home=config.find_home(),
            session=str(ULID()),
            user=config.user_default("CAIRN_USER"),
            host=config.user_default("CAIRN_HOST"),
        )
    )


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
        + (f", {stats.notes} note{'s' if stats.notes != 1 else ''}" if stats.notes else "")
        + f" from {stats.lines} lines in {stats.files} files"
        f" ({stats.duplicates} duplicates, {stats.malformed} malformed,"
        f" {stats.unsupported} unsupported).",
        soft_wrap=True,
    )
    if linked:
        out.print(f"Linked {linked.runs} runs: {_edge_summary(linked.edges)}.", soft_wrap=True)
    if stats.malformed:
        err.print("Malformed lines were skipped; run with --verbose for details.")


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


# --- not implemented yet -----------------------------------------------------------------


RootOpt = Annotated[
    list[Path] | None,
    typer.Option("--root", help="Directory to cover (repeatable). Default: the project."),
]


def _cairn_user() -> str:
    user = config.user_default("CAIRN_USER") or ""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", user):
        err.print(
            "[red]Set CAIRN_USER[/red] (letters, digits, '.', '_', '-'): export it, or put"
            f" CAIRN_USER=<you> in {config.user_env_file()}."
        )
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
def sync(
    aliases: Annotated[list[str] | None, typer.Argument(help="Host aliases to pull.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print the commands only.")] = False,
) -> None:
    """Pull spoke machines' events, objects and logs over SSH, then ingest. Never pushes."""
    hosts = load_hosts()
    if not aliases:
        out.print(f"Hosts file: {hosts_file()}")
        if not hosts:
            out.print(
                '  none yet; add e.g.\n  [workstation]\n  ssh = "workstation"\n'
                '  home = "~/.cairn-alice"',
                markup=False,
            )
        for h in hosts.values():
            out.print(f"  {h.alias}: {h.ssh or '(local)'}:{h.home}", markup=False)
        return
    unknown = [a for a in aliases if a not in hosts]
    if unknown:
        err.print(f"[red]Unknown host(s):[/red] {', '.join(unknown)} (see {hosts_file()})")
        raise typer.Exit(2)
    home = _require_home()
    for alias in aliases:
        try:
            cmds = sync_spoke(hosts[alias], home, dry_run=dry_run)
        except SyncError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from None
        if dry_run:
            for c in cmds:
                out.print(" ".join(shlex.quote(x) for x in c), markup=False, soft_wrap=True)
        else:
            err.print(f"Pulled {alias}.")
    if not dry_run:
        ingest()


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
    voice: Annotated[bool, typer.Option("--voice", help="Record a spoken note.")] = False,
) -> None:
    """Record why: a note pinned to the last command in this terminal (or --run R:<id>).

    With no text (or --voice), record a spoken note: run it once to start and again to
    stop (bind it to a keyboard shortcut), or press Enter in a terminal.
    """
    words = " ".join(text or []).strip()
    home = config.find_home()
    if home is None:
        err.print("[red]No Cairn store:[/red] set CAIRN_HOME or run [bold]cairn init[/bold].")
        raise typer.Exit(1)
    user = _cairn_user()
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
    if voice or not words:
        _voice_note(home, event)
    _write_note(home, user, event)
    err.print("Noted." if event["text"] else "Saved the recording.")


def _write_note(home: Path, user: str, event: dict) -> None:
    EventWriter(config.events_dir(home), config.local_host(), user, suffix="note").write(event)


def _desktop_notify(message: str) -> None:
    """A desktop notification, for notes started from a keyboard shortcut."""
    graphical = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    if graphical and shutil.which("notify-send"):
        subprocess.run(
            ["notify-send", "-a", "Cairn", "Cairn", message], check=False, capture_output=True
        )


def _voice_note(home: Path, event: dict) -> None:
    """Record, save and transcribe; fills ``event`` in place (or exits if only stopping)."""
    toggle = Toggle(config.state_dir(home) / "voice.pid")
    if toggle.stop_other():
        err.print("Stopped the recording.")
        raise typer.Exit(0)
    stop = threading.Event()
    toggle.claim(stop)
    if sys.stdin.isatty():
        threading.Thread(target=lambda: (sys.stdin.readline(), stop.set()), daemon=True).start()
        err.print("Recording... press Enter (or run cairn note again) to stop.")
    _desktop_notify("Recording a note: press the shortcut again to stop.")
    try:
        pcm = record_audio(stop, max_seconds=VOICE_MAX_SECONDS)
    except RuntimeError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    finally:
        toggle.release()
    if len(pcm) < VOICE_RATE * 2 * 0.3:  # under 0.3 s of audio
        err.print("Too short; nothing saved.")
        raise typer.Exit(0)
    audio = Path("notes") / f"{event['id']}.wav"
    wav = to_wav(pcm)
    (home / audio).parent.mkdir(parents=True, exist_ok=True)
    (home / audio).write_bytes(wav)
    event.update(source="voice", audio=audio.as_posix())
    try:
        event["text"] = transcribe_wav(wav, home=home)
    except LLMError as exc:
        event["untranscribed"] = True
        _write_note(home, _cairn_user(), event)
        err.print(f"[red]Transcription failed:[/red] {exc}. The audio is kept at {home / audio}.")
        raise typer.Exit(1) from None
    _desktop_notify(f"Noted: {event['text'] or '(no speech heard)'}")


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


def _usage_summary(home: Path) -> dict[str, tuple[int, float, list[float]]]:
    """model -> (calls, cost, tokens/s samples) from usage.jsonl."""
    usage = home / "usage.jsonl"
    out: dict[str, tuple[int, float, list[float]]] = {}
    if not usage.is_file():
        return out
    for line in usage.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        n, cost, speeds = out.get(rec.get("model", "?"), (0, 0.0, []))
        if "tokens_per_s" in rec:
            speeds = [*speeds, rec["tokens_per_s"]]
        out[rec.get("model", "?")] = (n + 1, cost + rec.get("cost_usd", 0.0), speeds)
    return out


def _usage_cost(home: Path, since_bytes: int) -> float:
    usage = home / "usage.jsonl"
    if not usage.is_file():
        return 0.0
    with usage.open("rb") as fh:
        fh.seek(since_bytes)
        return sum(json.loads(line)["cost_usd"] for line in fh.read().splitlines() if line)


QUEUE_DAYS = 14


def _days_needing_entries(conn, tz, today: date) -> list[date]:
    """Local days in the last QUEUE_DAYS with runs but no written entry, oldest first."""
    have = {d for (d,) in conn.execute("SELECT day FROM entries WHERE model != 'none'")}
    days = set()
    for (ts,) in conn.execute("SELECT ts_start FROM runs WHERE ts_start IS NOT NULL"):
        d = datetime.fromisoformat(ts).astimezone(tz).date()
        if today - timedelta(days=QUEUE_DAYS) <= d <= today and d.isoformat() not in have:
            days.add(d)
    return sorted(days)


def _write_one(conn, home, client, the_day, tz, root, backend, check) -> None:
    err.print(f"Writing the entry for {the_day} with {client.model}...")
    res = write_entry(conn, home, the_day, client=client, tz=tz, root=root, backend=backend)
    if check and res.entry:
        err.print("Checking its claims against the store...")
        res.verdicts = check_faithfulness(
            client, conn, config.objects_dir(home), res.markdown, res.prompt
        )
        res.faithfulness = summarize_claims(res.verdicts)
        save_faithfulness(conn, res)
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


@app.command()
def entry(
    day: Annotated[str, typer.Argument(help="today, yesterday or YYYY-MM-DD.")] = "today",
    backend: Annotated[
        str, typer.Option(help="spark (Meta Model API) or glimmer (self-hosted, private).")
    ] = "spark",
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would be sent; call nothing.")
    ] = False,
    check: Annotated[
        bool, typer.Option("--check/--no-check", help="Verify the entry's claims afterwards.")
    ] = True,
    queue: Annotated[
        bool,
        typer.Option(
            "--queue",
            help=f"Write every day of the last {QUEUE_DAYS} that has runs"
            " but no entry (e.g. overnight with --backend glimmer).",
        ),
    ] = False,
) -> None:
    """Write the day's notebook entry; every claim cites the log."""
    if backend not in ("spark", "glimmer"):
        err.print("--backend must be spark or glimmer")
        raise typer.Exit(2)
    home = _require_home()
    conn = db.connect(config.db_path(home))
    db.migrate(conn)
    tz = config.timezone(home)
    root = config.project_root(home)
    days = (
        _days_needing_entries(conn, tz, datetime.now(tz).date()) if queue else [_parse_day(day, tz)]
    )
    if queue and not days:
        out.print("Every recent day with runs already has an entry.")
        return
    if dry_run:
        for d in days:
            res = write_entry(conn, home, d, tz=tz, root=root, backend=backend, dry_run=True)
            out.print(res.prompt, markup=False, highlight=False, soft_wrap=True)
            err.print(
                f"{d}: {len(res.context.figures)} figures would be attached;"
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
    failed = 0
    try:
        for d in days:
            try:
                _write_one(conn, home, client, d, tz, root, backend, check)
            except LLMError as exc:
                err.print(f"[red]{d}: {exc}[/red]")
                failed += 1
                if not queue:
                    raise typer.Exit(1) from None
    finally:
        conn.close()
    out.print(f"  model cost: ${_usage_cost(home, before):.4f}")
    if failed:
        raise typer.Exit(1)


@app.command()
def trace(
    path: Annotated[str, typer.Argument(help="File (or A:<version>) to explain.")],
    at: Annotated[str | None, typer.Option(help="The version current at this time.")] = None,
    sha: Annotated[str | None, typer.Option(help="The version with this hash prefix.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Show the chain of runs and inputs that produced a file, and how to regenerate it."""
    home = _require_home()
    conn = db.connect(config.db_path(home))
    db.migrate(conn)
    tz = config.timezone(home)
    host = config.local_host()
    if path.startswith("A:"):
        vid = path[2:]
    else:
        at_ts = normalize_ts(at) if at else None
        vid = None
        for candidate in dict.fromkeys([os.path.abspath(path), str(Path(path).resolve())]):
            vid = resolve_version(conn, host, candidate, at=at_ts, sha=sha)
            if vid:
                break
    if not vid or graph_node(conn, vid) is None:
        err.print(f"[red]No recorded version of {path!r}[/red] on {host}.")
        raise typer.Exit(1)
    t = trace_graph(conn, vid)
    if as_json:
        sys.stdout.write(json.dumps(t.to_dict(), indent=1) + "\n")
        return
    out.print(_trace_tree(t, tz, config.project_root(home)))
    final = t.producer_of(vid)
    if final is None:
        out.print("\nThis version was not produced by any recorded run (a raw input).")
        return
    r = final.run
    out.print("\n[bold]Regenerate the final step:[/bold]")
    out.print(f"  cd {r['cwd']}", markup=False)
    out.print(f"  {r['cmd']}", markup=False, soft_wrap=True)
    context = []
    if r.get("git_commit"):
        dirty = " with uncommitted changes" if r.get("git_dirty") else ""
        context.append(f"recorded at git {r.get('git_branch')}@{r['git_commit'][:7]}{dirty}")
    if r.get("env_name"):
        context.append(f"env {r['env_name']}")
    if context:
        out.print(f"  ({'; '.join(context)})")
    if r.get("host") == host:
        for p, status in input_status(conn, final):
            colour = {"unchanged": "green", "changed since": "yellow"}.get(status, "red")
            out.print(
                f"  input {_rel(p, config.project_root(home))}: [{colour}]{status}[/{colour}]"
            )


def _rel(path: str, root: Path | None) -> str:
    if root and path.startswith(str(root).rstrip("/") + "/"):
        return path[len(str(root).rstrip("/")) + 1 :]
    return path


def _trace_tree(t, tz, root: Path | None):
    from rich.markup import escape
    from rich.tree import Tree

    def label(vid: str) -> str:
        n = t.nodes[vid]
        return (
            f"[bold]{escape(_rel(n.path, root))}[/bold] [dim]A:{vid} sha {n.sha[:8]}"
            f" {_local(n.ts, tz)}[/dim]"
        )

    shown: set[str] = set()

    def add(tree, vid: str) -> None:
        branch = tree.add(label(vid))
        step = t.producer_of(vid)
        if step is None:
            branch.add("[dim]raw input (not produced by a recorded run)[/dim]")
            return
        r = step.run
        status = f"exit {r['exit']}" if r["exit"] is not None else r["status"]
        run_branch = branch.add(
            f"[cyan]{escape(r['cmd'] or '')}[/cyan] [dim]R:{r['id']} {_local(r['ts_start'], tz)}"
            f" {status} · {step.method} {step.confidence:.1f}[/dim]"
        )
        for text in step.notes:
            run_branch.add(f"[magenta]note:[/magenta] {escape(text)}")
        if r["id"] in shown:
            run_branch.add("[dim](inputs shown above)[/dim]")
            return
        shown.add(r["id"])
        for inp in step.inputs:
            add(run_branch, inp)

    tree = Tree(f"[bold]Provenance of {escape(_rel(t.nodes[t.target].path, root))}[/bold]")
    add(tree, t.target)
    return tree


def _resolve_target(conn, path: str, at: str | None, sha: str | None) -> str:
    host = config.local_host()
    if path.startswith("A:"):
        vid = path[2:]
    else:
        vid = None
        at_ts = normalize_ts(at) if at else None
        for candidate in dict.fromkeys([os.path.abspath(path), str(Path(path).resolve())]):
            vid = resolve_version(conn, host, candidate, at=at_ts, sha=sha)
            if vid:
                break
    if not vid or graph_node(conn, vid) is None:
        err.print(f"[red]No recorded version of {path!r}[/red] on {host}.")
        raise typer.Exit(1)
    return vid


@app.command()
def reproduce(
    path: Annotated[str, typer.Argument(help="File (or A:<version>) to regenerate.")],
    yes: Annotated[bool, typer.Option("--yes", help="Run the plan without asking.")] = False,
    at: Annotated[str | None, typer.Option(help="The version current at this time.")] = None,
    sha: Annotated[str | None, typer.Option(help="The version with this hash prefix.")] = None,
    rel_tol: Annotated[float, typer.Option(help="Tolerance for numbers.")] = 1e-6,
    keep_scratch: Annotated[
        bool, typer.Option("--keep-scratch", help="Keep the scratch directories afterwards.")
    ] = False,
) -> None:
    """Re-run the cheap final steps for a file in a scratch directory and check the result.

    Muse Spark reads the trace and proposes a plan; nothing runs until you approve it.
    Only recorded commands under 10 minutes with stored inputs can be re-run. The
    scratch directory is not a security sandbox: commands run as you.
    """
    home = _require_home()
    conn = db.connect(config.db_path(home))
    db.migrate(conn)
    vid = _resolve_target(conn, path, at, sha)
    try:
        client = Client(home=home)
    except LLMError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None

    def confirm(plan) -> bool:
        out.print(f"\n[bold]Plan:[/bold] {plan.summary}", soft_wrap=True)
        for i, st in enumerate(plan.steps, 1):
            out.print(f"  {i}. re-run [cyan]{st.cmd}[/cyan]", soft_wrap=True, highlight=False)
            out.print(f"     check {len(st.check)} output(s); {st.why}", soft_wrap=True)
            if st.extra_inputs:
                out.print(f"     extra inputs: {', '.join(st.extra_inputs)}")
        for r in plan.rejected:
            out.print(f"  [yellow]not re-run:[/yellow] {r}", soft_wrap=True)
        if yes:
            return True
        return typer.confirm("Run these steps in a scratch directory?", default=False)

    usage = home / "usage.jsonl"
    before = usage.stat().st_size if usage.is_file() else 0
    err.print(f"Asking {client.model} to plan a reproduction of {path}...")
    # Inside .cairn/, which the watcher never records, so replays are not mistaken for work.
    scratch = home / "scratch"
    scratch.mkdir(exist_ok=True)
    agent = ReproduceAgent(
        client,
        conn,
        config.objects_dir(home),
        vid,
        confirm,
        say=lambda m: err.print(m, soft_wrap=True),
        rel_tol=rel_tol,
        scratch_parent=scratch,
    )
    try:
        report = agent.run()
    except LLMError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    finally:
        conn.close()
    colour = {"reproduced": "green", "reproduced_close": "green", "not_reproduced": "red"}.get(
        report.verdict, "yellow"
    )
    out.print(f"\n[bold {colour}]{report.verdict.replace('_', ' ')}[/bold {colour}]")
    for c in report.checks:
        out.print(
            f"  {_rel(c.path, config.project_root(home))}: {c.outcome}"
            + (f" ({c.detail})" if c.detail else ""),
            soft_wrap=True,
        )
    if report.explanation:
        out.print(report.explanation, soft_wrap=True)
    if not keep_scratch:
        for r in report.replays:
            shutil.rmtree(r["scratch"], ignore_errors=True)
    out_dir = home / "reproductions"
    out_dir.mkdir(exist_ok=True)
    saved = out_dir / f"{ULID()}.json"
    saved.write_text(json.dumps(report.to_dict(), indent=1))
    out.print(
        f"[dim]{report.tool_calls} tool calls, model cost"
        f" ${_usage_cost(home, before):.4f}; report saved to {saved}[/dim]"
    )
    raise typer.Exit(
        {"reproduced": 0, "reproduced_close": 0, "not_reproduced": 1}.get(report.verdict, 3)
    )


@app.command()
def ask(question: Annotated[list[str], typer.Argument(help="Question about your work.")]):
    """Answer a question from the record (runs, files, notes, entries), with citations."""
    home = _require_home()
    conn = db.connect(config.db_path(home))
    db.migrate(conn)
    try:
        client = Client(home=home)
    except LLMError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    usage = home / "usage.jsonl"
    before = usage.stat().st_size if usage.is_file() else 0
    asker = Asker(
        client,
        conn,
        config.timezone(home),
        config.local_host(),
        config.project_root(home),
        config.objects_dir(home),
    )
    try:
        ans = asker.ask(" ".join(question))
    except LLMError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    finally:
        conn.close()
    out.print(ans.markdown, markup=False, highlight=False, soft_wrap=True)
    if ans.removed:
        err.print(f"[yellow]Removed {len(ans.removed)} cited IDs not in the store.[/yellow]")
    err.print(
        f"[dim]{ans.tool_calls} tool calls, model cost ${_usage_cost(home, before):.4f}[/dim]"
    )


@app.command()
def report(
    period: Annotated[str, typer.Argument(help="week")] = "week",
    start: Annotated[
        str | None, typer.Option(help="First day (YYYY-MM-DD); default: the last 7 days.")
    ] = None,
) -> None:
    """Write the weekly supervisor update (under 300 words) from the week's entries."""
    if period != "week":
        err.print("Only 'cairn report week' is supported.")
        raise typer.Exit(2)
    home = _require_home()
    conn = db.connect(config.db_path(home))
    db.migrate(conn)
    tz = config.timezone(home)
    first = _parse_day(start, tz) if start else None
    try:
        client = Client(home=home)
    except LLMError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    usage = home / "usage.jsonl"
    before = usage.stat().st_size if usage.is_file() else 0
    try:
        rep = write_weekly(conn, home, client, today=datetime.now(tz).date(), start=first, tz=tz)
    except LLMError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    finally:
        conn.close()
    out.print(rep.text, markup=False, highlight=False, soft_wrap=True)
    if rep.path:
        err.print(
            f"Saved to {rep.path} ({len(rep.text.split())} words,"
            f" model cost ${_usage_cost(home, before):.4f})"
        )


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
def export(
    out_dir: Annotated[
        Path | None, typer.Option("--out", help="Where to write the site (default .cairn/site).")
    ] = None,
) -> None:
    """Write the notebook as a static HTML site: entries, reports, cited runs and files."""
    home = _require_home()
    conn = db.connect(config.db_path(home))
    db.migrate(conn)
    target = out_dir or home / "site"
    index = Exporter(
        conn, config.objects_dir(home), target, config.timezone(home), config.project_root(home)
    ).export()
    conn.close()
    out.print(f"Wrote {index}")


@app.command()
def serve() -> None:
    """Start the optional web viewer (use cairn export for a static site)."""
    _todo("serve")
