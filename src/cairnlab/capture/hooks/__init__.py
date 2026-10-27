"""Shell hooks emitted by ``cairn activate``. They are never installed into startup files."""

import shlex
from importlib import resources
from pathlib import Path


def hook_path() -> Path:
    """Filesystem path of the bundled bash hook library."""
    return Path(str(resources.files(__name__).joinpath("bash.sh")))


def activation_script(home: Path | None, session: str) -> str:
    """Bash code for ``eval "$(cairn activate)"``.

    ``home`` is the store to write events to when ``CAIRN_HOME`` is unset in the shell;
    ``None`` lets the hook fall back to ``~/.cairn-$CAIRN_USER``. ``session`` is the
    session id (a ULID) shared by every command in this terminal.
    """
    lines = [f"__cairn_session_id={shlex.quote(session)}"]
    if home is not None:
        lines.append(f"__cairn_default_home={shlex.quote(str(home))}")
    lines.append(hook_path().read_text(encoding="utf-8"))
    # Must run at top level: inside a function bash hides the current DEBUG trap.
    lines.append("__cairn_debug_probe=$(trap -p DEBUG)")
    lines.append("__cairn_activate")
    return "\n".join(lines) + "\n"


def deactivation_script() -> str:
    """Bash code for ``eval "$(cairn deactivate)"``."""
    return (
        "if declare -F __cairn_deactivate >/dev/null 2>&1; then __cairn_deactivate;"
        " else echo 'cairn: capture is not active in this shell' >&2; fi\n"
    )
