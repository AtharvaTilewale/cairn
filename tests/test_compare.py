"""Tests for comparing regenerated outputs with recorded versions."""

import io

from PIL import Image, PngImagePlugin

from cairnlab.agent.compare import compare_bytes, compare_file


def png(color, text=None) -> bytes:
    buf = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    if text:
        info.add_text("Creation Time", text)
    Image.new("RGB", (8, 4), color).save(buf, format="PNG", pnginfo=info)
    return buf.getvalue()


def test_identical():
    assert compare_bytes(b"abc", b"abc").outcome == "identical"


def test_png_with_different_metadata_is_pixel_equal():
    c = compare_bytes(png("red", "2026-10-28 09:00"), png("red", "2026-10-29 10:00"))
    assert c.outcome == "pixel_equal" and c.matches
    assert compare_bytes(png("red"), png("blue")).outcome == "different"


def test_json_formatting_is_data_equal():
    c = compare_bytes(b'{"tau": 2.4, "model": "exp"}', b'{\n "model": "exp",\n "tau": 2.4\n}')
    assert c.outcome == "data_equal"


def test_table_formatting_and_number_spelling():
    assert compare_bytes(b"x,y\n1.50,2\n", b"x, y\n1.5, 2.0\n").outcome == "data_equal"


def test_float_noise_is_close_within_tolerance():
    c = compare_bytes(b"energy -1006.5000001\n", b"energy -1006.5\n", rel_tol=1e-6)
    assert c.outcome == "close" and c.max_rel_diff < 1e-9
    assert compare_bytes(b"energy -1006.6\n", b"energy -1006.5\n").outcome == "different"


def test_different_text_and_shapes():
    assert compare_bytes(b"model poly", b"model exp").outcome == "different"
    assert compare_bytes(b"1 2 3", b"1 2").outcome == "different"
    assert compare_bytes(b"\xff\xfe\x00", b"\xff\xfe\x01").outcome == "different"


def test_compare_file_missing_and_unstored(tmp_path):
    produced = tmp_path / "out.csv"
    assert compare_file(produced, tmp_path / "rec").outcome == "different"
    produced.write_text("1")
    assert compare_file(produced, None).outcome == "unverifiable"
