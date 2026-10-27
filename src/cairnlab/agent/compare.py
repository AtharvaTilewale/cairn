"""Compare a regenerated file with the recorded version.

Outcomes, from strongest to weakest:

- ``identical``: same SHA-256.
- ``pixel_equal``: both decode as images with the same size and pixels (PNG metadata
  such as a timestamp differs).
- ``data_equal``: same parsed data (JSON values; tables with the same tokens and
  numbers), formatting aside.
- ``close``: numbers agree within ``rel_tol`` and all other tokens match.
- ``different``; or ``unverifiable`` when there is no stored copy to compare with.
"""

import hashlib
import io
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

_NUMBER = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_TOKEN = re.compile(r"[^\s,;:\t\[\]{}()\"']+")


@dataclass
class Comparison:
    outcome: str  # identical|pixel_equal|data_equal|close|different|unverifiable
    detail: str = ""
    max_rel_diff: float | None = None

    @property
    def matches(self) -> bool:
        return self.outcome in ("identical", "pixel_equal", "data_equal", "close")


def _pixels(data: bytes):
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            return im.size, im.convert("RGBA").tobytes()
    except Exception:  # noqa: BLE001 - not an image Pillow can read
        return None


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text)


def _rel(a: float, b: float) -> float:
    if a == b:
        return 0.0
    scale = max(abs(a), abs(b))
    return abs(a - b) / scale if scale else 0.0


def compare_bytes(produced: bytes, recorded: bytes, rel_tol: float = 1e-6) -> Comparison:
    """Compare two contents (see module docstring for the outcomes)."""
    if hashlib.sha256(produced).digest() == hashlib.sha256(recorded).digest():
        return Comparison("identical")
    a_img, b_img = _pixels(produced), _pixels(recorded)
    if a_img is not None and b_img is not None:
        if a_img == b_img:
            return Comparison("pixel_equal", "same image; file bytes differ (metadata)")
        if a_img[0] != b_img[0]:
            return Comparison("different", f"image size {a_img[0]} vs {b_img[0]}")
        return Comparison("different", "pixels differ")
    try:
        a_text, b_text = produced.decode("utf-8"), recorded.decode("utf-8")
    except UnicodeDecodeError:
        return Comparison("different", "binary content differs")
    try:
        if json.loads(a_text) == json.loads(b_text):
            return Comparison("data_equal", "same JSON values")
    except ValueError:
        pass
    ta, tb = _tokens(a_text), _tokens(b_text)
    if len(ta) != len(tb):
        return Comparison("different", f"{len(ta)} vs {len(tb)} tokens")
    worst = 0.0
    exact_numbers = True
    for x, y in zip(ta, tb, strict=True):
        if x == y:
            continue
        if _NUMBER.match(x) and _NUMBER.match(y):
            fx, fy = float(x), float(y)
            if fx == fy:
                continue  # 1.50 vs 1.5: same number, different formatting
            exact_numbers = False
            worst = max(worst, _rel(fx, fy))
            if math.isnan(worst):
                return Comparison("different", f"{x!r} vs {y!r}")
        else:
            return Comparison("different", f"{x!r} vs {y!r}")
    if exact_numbers:
        return Comparison("data_equal", "same tokens and numbers; formatting differs")
    if worst <= rel_tol:
        return Comparison("close", f"numbers within rel_tol {rel_tol:g}", worst)
    return Comparison("different", f"largest relative difference {worst:.3g}", worst)


def compare_file(produced: Path, recorded: Path | None, rel_tol: float = 1e-6) -> Comparison:
    """Compare a produced file with a stored recorded copy (``None``: not stored)."""
    if not produced.is_file():
        return Comparison("different", "the command did not produce this file")
    if recorded is None or not recorded.is_file():
        return Comparison("unverifiable", "no stored copy of the recorded version")
    return compare_bytes(produced.read_bytes(), recorded.read_bytes(), rel_tol)
