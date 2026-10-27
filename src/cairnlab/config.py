"""Locate the Cairn store and load ``.cairn/config.toml``.

The store directory (``CAIRN_HOME``) is, in order: the ``CAIRN_HOME`` environment
variable, or the nearest ``.cairn/`` directory at or above the current directory.
"""

import os
import socket
import tomllib
from pathlib import Path
from typing import Any

CAIRN_DIR = ".cairn"
IGNORE_FILE = ".cairnignore"

DEFAULT_IGNORE = """\
# Paths Cairn never records (gitignore syntax).
.git/
.cairn/
__pycache__/
.ipynb_checkpoints/
*.tmp
*.swp
*~
.#*
# Editor autosaves and GROMACS backups (#md.xtc.1#); the backslash stops "#" being a comment.
\\#*#
"""


def find_home(start: Path | None = None) -> Path | None:
    """Return the active Cairn store directory, or ``None`` if there is none."""
    env = os.environ.get("CAIRN_HOME")
    if env:
        return Path(env).expanduser()
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / CAIRN_DIR
        if candidate.is_dir():
            return candidate
    return None


def local_host() -> str:
    """This machine's host name as stored in events: $CAIRN_HOST, else the short name.

    Matches the bash hook, which uses ``${CAIRN_HOST:-${HOSTNAME%%.*}}``.
    """
    return os.environ.get("CAIRN_HOST") or socket.gethostname().split(".")[0]


def is_eddie() -> bool:
    """True on the an HPC cluster, where no daemons may run."""
    if os.environ.get("CAIRN_NO_DAEMON"):
        return True
    return socket.getfqdn().endswith(".cluster.example.org")


def project_root(home: Path) -> Path | None:
    """The project directory a store belongs to (``<project>/.cairn``), if any."""
    return home.parent if home.name == CAIRN_DIR else None


def db_path(home: Path) -> Path:
    """Path of the SQLite database inside a store."""
    return home / "cairn.db"


def events_dir(home: Path) -> Path:
    """Directory the shell hook appends JSONL events to."""
    return home / "events"


def objects_dir(home: Path) -> Path:
    """Content-addressed copies of small files."""
    return home / "objects"


def state_dir(home: Path) -> Path:
    """Small bookkeeping files (watcher state), never synced."""
    return home / "state"


def load_config(home: Path) -> dict[str, Any]:
    """Parse ``config.toml`` in the store; an absent file gives an empty config."""
    path = home / "config.toml"
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def readset_enabled(home: Path) -> bool:
    """Whether linking may send scripts to Muse Spark (config [link] readset, CAIRN_READSET)."""
    env = os.environ.get("CAIRN_READSET")
    if env is not None:
        return env.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(load_config(home).get("link", {}).get("readset", True))


def default_config(project_name: str, created: str) -> str:
    """Text of a fresh ``config.toml``."""
    name = project_name.replace("\\", "\\\\").replace('"', '\\"')
    return f"""\
# Cairn project configuration.
[project]
name = "{name}"
created = "{created}"

[llm]
backend = "spark"  # spark | glimmer

[link]
# Send each new script version to Muse Spark once to learn which files it reads and
# writes (cached by hash). Set to false, or CAIRN_READSET=0, to keep scripts local.
readset = true
"""
