"""Tests for cairn ask: tools over the store and citation checking (no network)."""

import json
from datetime import UTC
from pathlib import Path
from types import SimpleNamespace

import pytest

from cairnlab.synth.ask import Asker
from tests.test_gather import ROOT, Store, T

BOGUS = "R:01ARZ3NDEKTSV4RRFFQ69G5FZZ"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path)
    s.file("params.txt", T("08:00:00"), b"cutoff = 1.0\n", op="scanned")
    s.file("fit.py", T("08:00:00"), b"fit", op="scanned")
    s.cmd("python fit.py --params params.txt --out fit.json", T("09:00:00"), T("09:00:02"))
    s.file("fit.json", T("09:00:01"), b'{"tau": 2.4}', op="created")
    s.cmd("sed -i s/1.0/1.2/ params.txt", T("09:01:00"), T("09:01:00"))
    s.file("params.txt", T("09:01:00"), b"cutoff = 1.2\n")
    s.cmd("python fit.py missing.csv", T("09:02:00"), T("09:02:01"), exit=1)
    s.load()
    return s


def asker(s, client=None):
    return Asker(client, s.conn, UTC, host="hp", root=Path(ROOT))


def test_search_runs_words_time_and_failures(store):
    a = asker(store)
    got = a.tool_search_runs(words="fit.py params")
    assert [r["cmd"] for r in got["runs"]] == ["python fit.py --params params.txt --out fit.json"]
    assert a.tool_search_runs(failed_only=True)["runs"][0]["exit"] == 1
    assert a.tool_search_runs(since="2026-10-29")["runs"] == []
    assert len(a.tool_search_runs(since="2026-10-28", until="2026-10-28")["runs"]) == 3


def test_search_files_and_trace(store):
    a = asker(store)
    (params,) = a.tool_search_files("params")["files"]
    assert params["versions"] == 2 and params["path"] == "params.txt"
    assert len(params["generated_by"]) == 1  # the sed run
    t = a.tool_trace("fit.json")
    assert t["steps"][-1]["cmd"].startswith("python fit.py")
    assert "error" in a.tool_trace("nope.json")


def test_get_run_and_entry(store):
    a = Asker(None, store.conn, UTC, "hp", Path(ROOT), store.objects)
    rid = a.tool_search_runs(words="sed")["runs"][0]["run_id"]
    run = a.tool_get_run(rid)
    assert {f["path"] for f in run["files"]} == {"params.txt"}
    fit = a.tool_search_runs(words="--out fit.json")["runs"][0]["run_id"]
    out = [f for f in a.tool_get_run(fit)["files"] if f["role"] == "generated"]
    assert out[0]["content"] == '{"tau": 2.4}'  # small outputs inline
    assert a.tool_get_entry("2026-10-28") == {"error": "no entry for 2026-10-28"}


def call(name, **args):
    return SimpleNamespace(
        id=f"c{name}", function=SimpleNamespace(name=name, arguments=json.dumps(args))
    )


class Scripted:
    model = "muse-spark-1.3"

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = []

    def complete(self, *, purpose, messages, tools, reasoning_effort):
        self.calls.append(purpose)
        return self.turns.pop(0)(messages)


def test_answer_with_citations_and_invented_ids_removed(store):
    def answer(messages):
        runs = json.loads(messages[-1]["content"])["runs"]
        rid = runs[0]["run_id"]
        return SimpleNamespace(
            content="",
            tool_calls=[
                call(
                    "answer",
                    text=f"The cutoff changed to 1.2 on 28 Oct [{rid}]. Reason: [{BOGUS}].",
                )
            ],
        )

    client = Scripted(
        [
            lambda m: SimpleNamespace(content="", tool_calls=[call("search_runs", words="sed")]),
            answer,
        ]
    )
    ans = asker(store, client).ask("When did the cutoff change?")
    assert ans.removed == [BOGUS]
    assert ans.markdown.startswith("The cutoff changed to 1.2 on 28 Oct [^1].")
    assert "[^1]: `R:" in ans.markdown and "sed -i" in ans.markdown
    assert ans.tool_calls == 2 and client.calls == ["ask", "ask"]


def test_budget_ends_the_loop(store):
    turns = [lambda m: SimpleNamespace(content="", tool_calls=[call("search_notes")])] * 40
    seen = []

    class Watching(Scripted):
        def complete(self, *, purpose, messages, tools, reasoning_effort):
            seen.append([t["function"]["name"] for t in tools])
            return super().complete(
                purpose=purpose, messages=messages, tools=tools, reasoning_effort=reasoning_effort
            )

    ans = asker(store, Watching(turns)).ask("anything?")
    assert seen[-1] == ["answer"]  # near the budget only `answer` is offered
    assert "could not find" in ans.text


def test_read_file(store):
    a = Asker(None, store.conn, UTC, "hp", Path(ROOT), store.objects)
    vid = a.tool_search_files("fit.json")["files"][0]["latest_version"]
    assert a.tool_read_file(vid)["text"] == '{"tau": 2.4}'
