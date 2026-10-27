"""Tests for figure preparation."""

import base64
import io

from PIL import Image

from cairnlab.llm.figures import is_raster, to_data_url


def png(w: int, h: int, mode: str = "RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, (w, h), "white" if mode != "P" else 0).save(buf, format="PNG")
    return buf.getvalue()


def decode(url: str) -> Image.Image:
    assert url.startswith("data:image/png;base64,")
    return Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))


def test_large_figure_is_downscaled_keeping_aspect():
    im = decode(to_data_url(png(3000, 1500)))
    assert im.size == (1280, 640)


def test_small_figure_is_left_alone():
    assert decode(to_data_url(png(400, 300))).size == (400, 300)


def test_palette_and_jpeg_inputs():
    assert decode(to_data_url(png(10, 10, mode="P"))).size == (10, 10)
    buf = io.BytesIO()
    Image.new("RGB", (2000, 2000)).save(buf, format="JPEG")
    assert decode(to_data_url(buf.getvalue())).size == (1280, 1280)


def test_is_raster():
    assert is_raster("figs/a.PNG") and is_raster("x.jpeg")
    assert not is_raster("plot.svg") and not is_raster("fig.pdf")
