"""File fingerprints (CLAUDE.md section 5.2).

- Files up to ``large_bytes`` (50 MB): full SHA-256, streamed in 1 MB chunks. With an
  object store, the bytes are copied in the same pass, so the stored copy always
  matches the hash even if the file changes while it is read.
- Larger files (trajectories, big .edr): a *fast* fingerprint, the SHA-256 of
  (size, mtime, first 1 MB, last 1 MB). Never fully hashed, never copied; well under
  a second even for a 50 GB file.
"""

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cairnlab.provenance.objects import has_object, object_path

CHUNK = 1024 * 1024
LARGE_BYTES = 50 * 1024 * 1024
FULL = "full"
FAST = "fast"


@dataclass(frozen=True)
class Fingerprint:
    """Identity of one file's content at one moment."""

    sha256: str
    size: int
    mtime_ns: int
    method: str  # FULL or FAST
    stored: bool  # a copy exists in the object store


def fingerprint(
    path: Path, objects_dir: Path | None = None, large_bytes: int = LARGE_BYTES
) -> Fingerprint:
    """Fingerprint ``path``; store a copy in ``objects_dir`` if it is small enough."""
    st = os.stat(path)
    if st.st_size > large_bytes:
        return Fingerprint(_fast(path, st), st.st_size, st.st_mtime_ns, FAST, False)
    if objects_dir is None:
        return Fingerprint(_full(path), st.st_size, st.st_mtime_ns, FULL, False)
    sha, size = _full_and_store(path, objects_dir)
    return Fingerprint(sha, size, st.st_mtime_ns, FULL, True)


def _full(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _fast(path: Path, st: os.stat_result) -> str:
    h = hashlib.sha256()
    h.update(f"cairn-fast-v1:{st.st_size}:{st.st_mtime_ns}:".encode())
    with open(path, "rb") as fh:
        h.update(fh.read(CHUNK))
        fh.seek(max(0, st.st_size - CHUNK))
        h.update(fh.read(CHUNK))
    return h.hexdigest()


def _full_and_store(path: Path, objects_dir: Path) -> tuple[str, int]:
    """Hash while copying to a temp file, then move it into place under its hash."""
    objects_dir.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    size = 0
    fd, tmp = tempfile.mkstemp(dir=objects_dir, prefix=".incoming-")
    try:
        with open(path, "rb") as src, os.fdopen(fd, "wb") as dst:
            while chunk := src.read(CHUNK):
                h.update(chunk)
                dst.write(chunk)
                size += len(chunk)
        sha = h.hexdigest()
        if has_object(objects_dir, sha):
            os.unlink(tmp)
        else:
            final = object_path(objects_dir, sha)
            final.parent.mkdir(exist_ok=True)
            os.chmod(tmp, 0o444)
            os.replace(tmp, final)
        return sha, size
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
