"""Prepare figures for vision input: at most 1280 px on the long edge, PNG data URLs."""

import base64
import io

from PIL import Image

MAX_EDGE = 1280
RASTER_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".bmp", ".webp")


def is_raster(path: str) -> bool:
    """True for image formats Pillow can read (SVG and PDF are not rasterised yet)."""
    return path.lower().endswith(RASTER_SUFFIXES)


def to_data_url(data: bytes, max_edge: int = MAX_EDGE) -> str:
    """Downscale ``data`` (any Pillow format) to ``max_edge`` and return a PNG data URL."""
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        if max(im.size) > max_edge:
            scale = max_edge / max(im.size)
            im = im.resize(
                (max(1, round(im.width * scale)), max(1, round(im.height * scale))),
                Image.Resampling.LANCZOS,
            )
        if im.mode not in ("RGB", "RGBA", "L"):
            im = im.convert("RGBA")
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
