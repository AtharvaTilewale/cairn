"""Environment snapshot for a run: which packages were installed.

The hash is the SHA-256 of the environment's package manifest: the names of the
``conda-meta/*.json`` records (name-version-build) plus every pip ``*.dist-info`` /
``*.egg-info`` under ``lib/python*/site-packages``. That is the information in
``conda list --export``, read in milliseconds instead of seconds.

The manifest can only be read now. If the environment changed after the run ended
(conda history or site-packages modified later), the hash is left unknown (None).
Hashes are cached per prefix for 10 minutes.
"""

import hashlib
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path


class EnvSnapshotter:
    """Hashes conda/venv package manifests, cached per prefix."""

    def __init__(self, now: Callable[[], float] = time.time, cache_s: float = 600):
        self.now = now
        self.cache_s = cache_s
        self._cache: dict[str, tuple[float, str | None, float]] = {}

    def env_hash(self, prefix: str, ts_end: str) -> str | None:
        """Manifest hash of ``prefix`` if it has not changed since ``ts_end``."""
        cached = self._cache.get(prefix)
        if cached is None or self.now() - cached[0] >= self.cache_s:
            cached = (self.now(), *self._manifest(Path(prefix)))
            self._cache[prefix] = cached
        _, digest, changed = cached
        if digest is None or changed > datetime.fromisoformat(ts_end).timestamp():
            return None
        return digest

    @staticmethod
    def _manifest(prefix: Path) -> tuple[str | None, float]:
        """(hash, last change time) of the environment at ``prefix``."""
        if not prefix.is_dir():
            return None, 0.0
        names: list[str] = []
        changed = 0.0
        meta = prefix / "conda-meta"
        if meta.is_dir():
            names += sorted(f"conda:{p.stem}" for p in meta.glob("*.json"))
            changed = max(changed, meta.stat().st_mtime)
            history = meta / "history"
            if history.is_file():
                changed = max(changed, history.stat().st_mtime)
        for site in sorted(prefix.glob("lib/python*/site-packages")):
            names += sorted(
                f"pip:{p.name}" for p in site.iterdir() if p.suffix in (".dist-info", ".egg-info")
            )
            changed = max(changed, site.stat().st_mtime)
        if not names:
            return None, changed
        return hashlib.sha256("\n".join(names).encode()).hexdigest(), changed
