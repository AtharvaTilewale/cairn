"""Content-addressed copies of small files in ``.cairn/objects``.

An object is stored at ``objects/<sha256[:2]>/<sha256>`` and made read-only, so an
old version of any small input or output can be restored for ``cairn reproduce``.
"""

from pathlib import Path


def object_path(objects_dir: Path, sha256: str) -> Path:
    """Where the object with this hash lives (it may not exist)."""
    return objects_dir / sha256[:2] / sha256


def has_object(objects_dir: Path, sha256: str) -> bool:
    """True if this content is already stored."""
    return object_path(objects_dir, sha256).is_file()
