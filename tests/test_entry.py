"""Tests for entry validation, rendering, the pipeline and the faithfulness verifier."""

import json
from datetime import date
from pathlib import Path

import pytest

from cairnlab.synth.claims import Verifier, _sentences, summarize, validate_entry
from cairnlab.synth.entry import write_entry
from cairnlab.synth.gather import gather
from tests.test_gather import ROOT, day  # noqa: F401  (the synthetic day fixture)

R1 = "R:01ARZ3NDEKTSV4RRFFQ69G5FAV"
R2 = "R:01ARZ3NDEKTSV4RRFFQ69G5FAW"
A1 = "A:01ARZ3NDEKTSV4RRFFQ69G5FAX"
BOGUS = "R:01ARZ3NDEKTSV4RRFFQ69G5FZZ"
VALID = {R1, R2, A1}


def entry_with(**kw) -> dict:
    base = {
        "title": "T",
        "goal": "Not recorded.",
        "phases": [],
        "results": [],
        "decisions": [],
        "drift": [],
        "open_questions": [],
        "next_steps": [],
    }
    return {**base, **kw}


def test_sentences_keep_trailing_citations():
    text = f"The fit ran on obs.csv [{R1}]. The cutoff changed. [{R2}] Then tau was 2.4 ns [{A1}]."
    assert _sentences(text) == [
        f"The fit ran on obs.csv [{R1}].",
        f"The cutoff changed. [{R2}]",
        f"Then tau was 2.4 ns [{A1}].",
    ]
    assert _sentences("Values 1.2 and 3.5 were used [R:x].") == [
        "Values 1.2 and 3.5 were used [R:x]."
    ]


def test_validation_drops_uncited_and_invented():
    raw = entry_with(
        goal=f"Fit the decay [{BOGUS}].",
        phases=[
            {
                "heading": "Fitting",
                "narrative": f"The fit ran [{R1}]. It was great. It used [{BOGUS}].",
                "claims": [
                    {"text": "fit ran", "evidence": [R1]},
                    {"text": "no evidence", "evidence": []},
                    {"text": "made up", "evidence": [BOGUS]},
                    {"text": "half made up", "evidence": [R1, BOGUS]},
                ],
            },
            {"heading": "Empty", "narrative": "Nothing cited.", "claims": []},
        ],
        results=[
            {"artifact": A1, "description": "a plot"},
            {"artifact": R1, "description": "a run is not a file"},
        ],
        decisions=[{"text": "chose poly", "note": "N:01ARZ3NDEKTSV4RRFFQ69G5FAV"}],
        drift=[{"text": "cutoff", "evidence": [R1, R2]}, {"text": "x", "evidence": [BOGUS]}],
        next_steps=[f"rerun [{BOGUS}]"],
    )
    entry, dropped = validate_entry(raw, VALID)
    (phase,) = entry["phases"]  # the uncited phase disappeared
    assert phase["narrative"] == f"The fit ran [{R1}]."
    assert [c["text"] for c in phase["claims"]] == ["fit ran"]
    assert [r["artifact"] for r in entry["results"]] == [A1]
    assert entry["decisions"] == []
    assert [d["text"] for d in entry["drift"]] == ["cutoff"]
    assert entry["goal"] == "Not recorded."
    assert entry["next_steps"] == ["rerun"]
    reasons = {d.where: d.reason for d in dropped}
    assert "cites nothing" in reasons["phases[0].claims[1]"]
    assert BOGUS in reasons["phases[0].claims[2]"]
    assert "phases[0].narrative.sentence[1]" in reasons
    assert len(dropped) == 10


class FakeClient:
    model = "muse-spark-1.3"

    def __init__(self, make):
        self.make = make
        self.calls = []

    def structured(self, **kw):
        self.calls.append(kw)
        return self.make(kw)


def _entry_from_log(kw):
    """A plausible model answer built from the IDs in the prompt (plus one invented)."""
    import re

    text = kw["messages"][1]["content"][0]["text"]
    runs = re.findall(r"\[(R:[0-9A-Z]{26})\]", text)
    figs = re.findall(r"- \[(A:[0-9A-Z]{26})\] fig.png", text)
    return entry_with(
        title="Exponential fits of the decay data",
        phases=[
            {
                "heading": "Fitting and plotting",
                "narrative": f"fit.py was run on data.csv [{runs[0]}].",
                "claims": [
                    {"text": "The first fit exited 0.", "evidence": [runs[0]]},
                    {"text": "Invented.", "evidence": [BOGUS]},
                ],
            }
        ],
        results=[{"artifact": figs[0], "description": "A blue square."}],
    )


@pytest.fixture
def home(tmp_path, day):  # noqa: F811
    h = tmp_path / "home_store"
    h.mkdir()
    (h / "objects").symlink_to(day.objects)
    (h / "logs").mkdir()
    return h


def test_pipeline_validates_renders_and_stores(day, home):  # noqa: F811
    client = FakeClient(_entry_from_log)
    res = write_entry(
        day.conn,
        home,
        date(2026, 10, 28),
        client=client,
        tz=__import__("datetime").UTC,
        root=Path(ROOT),
    )
    (call,) = client.calls
    assert call["schema_name"] == "Entry" and call["reasoning_effort"] == "medium"
    content = call["messages"][1]["content"]
    assert [c["type"] for c in content] == ["text", "text", "image_url"]
    assert content[2]["image_url"]["url"].startswith("data:image/png;base64,")
    assert call["messages"][0]["role"] == "system"

    md = res.markdown
    assert md.startswith("# Exponential fits of the decay data")
    assert "The first fit exited 0.[^1]" in md
    assert "fit.py was run on data.csv [^1]." in md
    assert "Invented" not in md
    assert "[^1]: `R:" in md and "cairn show R:" in md
    assert "**fig.png**[^2]: A blue square." in md
    assert len(res.dropped) == 1
    assert (home / "entries" / "2026-10-28.md").read_text() == md
    row = day.conn.execute("SELECT model, json FROM entries WHERE day = '2026-10-28'").fetchone()
    assert row[0] == "muse-spark-1.3"
    assert json.loads(row[1])["dropped"][0]["text"] == "Invented."

    # Writing the same day again replaces the entry.
    write_entry(
        day.conn,
        home,
        date(2026, 10, 28),
        client=client,
        tz=__import__("datetime").UTC,
        root=Path(ROOT),
    )
    assert day.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1


def test_dry_run_calls_nothing(day, home):  # noqa: F811
    client = FakeClient(_entry_from_log)
    res = write_entry(
        day.conn,
        home,
        date(2026, 10, 28),
        client=client,
        dry_run=True,
        tz=__import__("datetime").UTC,
        root=Path(ROOT),
    )
    assert client.calls == [] and "## Phase 1" in res.prompt
    assert not (home / "entries" / "2026-10-28.md").exists()


def test_empty_day_needs_no_model(day, home):  # noqa: F811
    res = write_entry(day.conn, home, date(2026, 1, 1), client=None, tz=__import__("datetime").UTC)
    assert "Nothing was recorded" in res.markdown and res.model == "none"


def test_glimmer_limits_figures(day, home):  # noqa: F811
    res = write_entry(
        day.conn,
        home,
        date(2026, 10, 28),
        client=None,
        dry_run=True,
        backend="glimmer",
        tz=__import__("datetime").UTC,
        root=Path(ROOT),
    )
    assert len(res.context.figures) <= 4


def test_verifier(day):  # noqa: F811
    ctx = gather(day.conn, day.objects, date(2026, 10, 28), root=Path(ROOT))
    v = Verifier(day.conn, day.objects)
    first = f"R:{ctx.phases[0].runs[0].ids[0]}"
    failed = f"R:{ctx.phases[1].runs[0].ids[0]}"
    new_params = f"A:{ctx.drift[0].new.vid}"

    def claim(*checks, evidence=()):
        return {
            "text": "c",
            "evidence": list(evidence),
            "checks": [
                {"kind": k, "run": r, "path": p, "key": key, "value": val}
                for k, r, p, key, val in checks
            ],
        }

    ok = v.verdict(
        claim(
            ("exit_code", first, "", "", "0"),
            ("command_contains", first, "", "", "--params params.txt"),
            ("file_named", "", "fit.json", "", ""),
            ("param_value", "", "params.txt", "cutoff", "1.20"),
            evidence=[first, new_params],
        )
    )
    assert ok.verdict == "supported", ok.checks
    assert v.verdict(claim(("exit_code", failed, "", "", "0"))).verdict == "unsupported"
    assert (
        v.verdict(
            claim(("param_value", "", "params.txt", "cutoff", "1.5"), evidence=[new_params])
        ).verdict
        == "unsupported"
    )
    assert v.verdict(claim(("file_named", "", "nonexistent.csv", "", ""))).verdict == "unsupported"
    assert v.verdict(claim(("none", "", "", "", ""))).verdict == "unverifiable"
    # The param value used by a run, found through the run's edges.
    assert (
        v.verdict(
            claim(("param_value", first, "params.txt", "cutoff", "1.0"), evidence=[first])
        ).verdict
        == "supported"
    )
    fit_json = next(f for f in ctx.phases[0].runs[3].generated if f.path == "fit.json")
    assert (
        v.verdict(
            claim(
                ("content_contains", "", "fit.json", "", '"tau":  2.4'),
                evidence=[f"A:{fit_json.vid}"],
            )
        ).verdict
        == "supported"
    )
    assert (
        v.verdict(
            claim(
                ("content_contains", "", "fit.json", "", "tau 9.9"), evidence=[f"A:{fit_json.vid}"]
            )
        ).verdict
        == "unsupported"
    )
    s = summarize([ok, v.verdict(claim(("exit_code", failed, "", "", "0")))])
    assert s["faithfulness"] == 0.5 and s["total"] == 2


def test_param_value_matches_nested_keys_by_last_component():
    from cairnlab.synth.claims import _param_matches

    params = {"model": "poly", "coef.c0": "-0.0106", "coef.c1": "0.175", "a.x": "1", "b.x": "2"}
    assert _param_matches(params, "c0", "-0.0106")
    assert _param_matches(params, "coef.c1", "0.1750")
    assert not _param_matches(params, "c1", "0.2")
    assert not _param_matches(params, "x", "1")  # ambiguous: a.x or b.x
