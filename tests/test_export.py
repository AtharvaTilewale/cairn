"""Tests for the static HTML export."""

from datetime import UTC, date
from pathlib import Path

from cairnlab.synth.entry import write_entry
from cairnlab.web.export import Exporter
from tests.test_entry import FakeClient, _entry_from_log
from tests.test_gather import ROOT, day  # noqa: F401


def test_export_entry_items_and_images(day, tmp_path):  # noqa: F811
    home = tmp_path / "store"
    home.mkdir()
    (home / "objects").symlink_to(day.objects)
    write_entry(
        day.conn,
        home,
        date(2026, 10, 28),
        client=FakeClient(_entry_from_log),
        tz=UTC,
        root=Path(ROOT),
    )
    out = tmp_path / "site"
    index = Exporter(day.conn, day.objects, out, UTC, Path(ROOT)).export()
    text = index.read_text()
    assert "Exponential fits of the decay data" in text and "entries/2026-10-28.html" in text
    entry = (out / "entries" / "2026-10-28.html").read_text()
    assert 'class="footnote"' in entry and "../items/R_" in entry
    run_pages = list((out / "items").glob("R_*.html"))
    fig_pages = [p for p in (out / "items").glob("A_*.html") if "fig.png" in p.read_text()]
    assert run_pages and fig_pages
    assert "<img" in fig_pages[0].read_text() and list((out / "files").glob("*.png"))
    assert "provenance" in fig_pages[0].read_text()
    # Pages are self-contained: no scripts, no external requests.
    for page in out.rglob("*.html"):
        body = page.read_text()
        assert "<script" not in body and "http://" not in body and "https://" not in body


def test_export_empty_store(tmp_path):
    from cairnlab.store import db

    conn = db.connect(tmp_path / "cairn.db")
    db.migrate(conn)
    index = Exporter(conn, tmp_path / "objects", tmp_path / "site", UTC).export()
    assert "No entries yet" in index.read_text()
