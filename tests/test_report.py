"""Tests for the weekly supervisor report (no network)."""

from datetime import date

from cairnlab.store import db
from cairnlab.synth.report import MAX_WORDS, render, week_bounds, write_report


class Fake:
    model = "muse-spark-1.3"

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def structured(self, **kw):
        self.calls.append(kw)
        return self.answer


ANSWER = {
    "subject": "Decay fits: exp vs poly",
    "moved": ["The exp fit with cutoff 1.2 gave tau = 2.49 ns."],
    "blocked": ["A fit on data/missing.csv failed (exit 1)."],
    "decisions": ["The model changed from exp to poly; no reason was recorded."],
    "next_week": ["Check the poly fit's divergence beyond t = 6."],
}


def store(tmp_path):
    conn = db.connect(tmp_path / "cairn.db")
    db.migrate(conn)
    return conn


def add_entry(conn, day, md="# Entry\n- tau 2.49 ns[^1]\n", model="muse-spark-1.3"):
    conn.execute(
        "INSERT INTO entries (id, day, created, model, json, markdown)"
        " VALUES (?, ?, 't', ?, '{}', ?)",
        (f"E{day}", day, model, md),
    )


def add_run(conn, day):
    conn.execute(
        "INSERT INTO runs (id, ts_start, cmd) VALUES (?, ?, 'x')",
        (f"R{day}", f"{day}T10:00:00.000Z"),
    )


def test_week_bounds():
    assert week_bounds(date(2026, 10, 30)) == (date(2026, 10, 24), date(2026, 10, 30))
    assert week_bounds(date(2026, 11, 4), start=date(2026, 10, 26)) == (
        date(2026, 10, 26),
        date(2026, 11, 1),
    )


def test_report_from_entries(tmp_path):
    conn = store(tmp_path)
    add_entry(conn, "2026-10-27")
    add_entry(conn, "2026-10-29")
    add_entry(conn, "2026-10-28", md="# empty", model="none")  # nothing recorded that day
    add_run(conn, "2026-10-30")  # a day with runs but no entry
    client = Fake(ANSWER)
    rep = write_report(conn, tmp_path, client, today=date(2026, 10, 30))
    (call,) = client.calls
    sent = call["messages"][1]["content"]
    assert "=== Entry for 2026-10-27 ===" in sent and "=== Entry for 2026-10-29 ===" in sent
    assert "2026-10-28" not in sent
    assert call["schema_name"] == "WeeklyReport"
    assert rep.text.startswith("Subject: Decay fits: exp vs poly\nWeek 2026-10-24 to 2026-10-30")
    assert "What moved:\n- The exp fit" in rep.text and "Next week:" in rep.text
    assert "No notebook entry was written for 2026-10-30." in rep.text
    assert rep.path.read_text() == rep.text
    assert len(rep.text.split()) <= MAX_WORDS


def test_empty_week_needs_no_model(tmp_path):
    conn = store(tmp_path)
    add_run(conn, "2026-10-29")
    rep = write_report(conn, tmp_path, client=None, today=date(2026, 10, 30))
    assert "No notebook entries were written this week" in rep.text
    assert "cairn entry" in rep.text


def test_word_limit_trims_last_items():
    long = {
        k: [f"Item {i} " + "word " * 20 for i in range(10)]
        for k in ("moved", "blocked", "decisions", "next_week")
    }
    text = render({"subject": "S", **long}, date(2026, 10, 26), date(2026, 11, 1), [])
    assert len(text.split()) <= MAX_WORDS
    assert "Item 0" in text and "What moved:" in text
