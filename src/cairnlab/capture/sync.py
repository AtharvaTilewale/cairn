"""Pull spoke stores into the hub with rsync over SSH, then ingest (CLAUDE.md 5.4).

Hosts are defined in ``~/.config/cairn/hosts.toml`` (or ``$CAIRN_HOSTS_FILE``)::

    [workstation]
    ssh = "workstation"         # an SSH target or ~/.ssh/config Host alias
    home = "~/.cairn-alice"   # CAIRN_HOME on that machine

``ssh = ""`` means ``home`` is a local directory (another store on this machine, and
the tests). Sync only ever reads from the spoke: no ``--delete``, nothing written
there, no passwords stored. A spoke's events land in ``events/remote/<alias>/`` so
they can never collide with the hub's own files; content-addressed objects merge
into the hub's object store; background-job logs join the hub's logs.
"""

import os
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

SSH_OPTIONS = "ssh -o ConnectTimeout=15 -o ServerAliveInterval=30"


class SyncError(RuntimeError):
    """A transfer failed (unreachable host, missing directory, rsync error)."""


@dataclass(frozen=True)
class Host:
    alias: str
    ssh: str
    home: str


def hosts_file() -> Path:
    """Where host aliases are defined."""
    env = os.environ.get("CAIRN_HOSTS_FILE")
    return Path(env) if env else Path.home() / ".config" / "cairn" / "hosts.toml"


def load_hosts() -> dict[str, Host]:
    """Host aliases from the hosts file ({} if there is none)."""
    path = hosts_file()
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return {
        alias: Host(alias, str(cfg.get("ssh", "")), str(cfg["home"]))
        for alias, cfg in data.items()
        if isinstance(cfg, dict) and "home" in cfg
    }


def _source(host: Host, sub: str) -> str:
    if host.ssh:
        return f"{host.ssh}:{host.home.rstrip('/')}/{sub}/"
    return f"{Path(host.home).expanduser()}/{sub}/"


def sync_commands(host: Host, hub_home: Path) -> list[list[str]]:
    """The rsync commands that pull ``host`` into the hub (events, objects, logs)."""
    base = ["rsync", "-az", "--partial", "--timeout=120"]
    if host.ssh:
        base += ["-e", SSH_OPTIONS]
    events = hub_home / "events" / "remote" / host.alias
    return [
        [*base, _source(host, "events"), f"{events}/"],
        # Objects never change once written; logs grow while their job runs.
        [*base, "--ignore-existing", _source(host, "objects"), f"{hub_home / 'objects'}/"],
        [*base, _source(host, "logs"), f"{hub_home / 'logs'}/"],
    ]


def sync(host: Host, hub_home: Path, dry_run: bool = False) -> list[list[str]]:
    """Pull ``host`` into ``hub_home``. Returns the commands run (or that would run)."""
    cmds = sync_commands(host, hub_home)
    if dry_run:
        return cmds
    (hub_home / "events" / "remote" / host.alias).mkdir(parents=True, exist_ok=True)
    (hub_home / "objects").mkdir(parents=True, exist_ok=True)
    (hub_home / "logs").mkdir(parents=True, exist_ok=True)
    for i, cmd in enumerate(cmds):
        res = subprocess.run(cmd, capture_output=True, text=True)
        # Objects and logs may not exist on a spoke yet (rsync exit 23: some files missing).
        if res.returncode != 0 and not (i > 0 and res.returncode == 23):
            raise SyncError(f"{host.alias}: rsync exited {res.returncode}: {res.stderr.strip()}")
    return cmds
