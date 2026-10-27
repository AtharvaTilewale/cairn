"""Tests for readset: script detection, argument mapping, caching, linking. No network."""

import json
from pathlib import PurePosixPath

import pytest

from cairnlab.capture.ingest import ingest
from cairnlab.provenance.linker import link
from cairnlab.provenance.readset import ReadsetReader, resolve, script_call
from cairnlab.store import db


@pytest.mark.parametrize(
    "cmd, script, args",
    [
        ("python fit.py a.csv --out b.json", "fit.py", ["a.csv", "--out", "b.json"]),
        ("python3 -u tools/run.py x", "tools/run.py", ["x"]),
        ("python3.12 -W ignore s.py", "s.py", []),
        ("Rscript plot.R in.tsv", "plot.R", ["in.tsv"]),
        ("bash pipeline.sh 3", "pipeline.sh", ["3"]),
        ("./analyse.py --n 3", "./analyse.py", ["--n", "3"]),
        ("OMP_NUM_THREADS=4 python fit.py", "fit.py", []),
        ("python -c 'print(1)'", None, None),
        ("python -m pip install x", None, None),
        ("gmx mdrun -deffnm md", None, None),
        ("python a.py && python b.py", "a.py", []),
    ],
)
def test_script_call(cmd, script, args):
    got = script_call(cmd)
    if script is None:
        assert got is None
    else:
        assert got == (script, args)


def test_resolve_maps_flags_positionals_and_literals():
    readset = {
        "reads": [
            {"kind": "positional", "value": "1"},
            {"kind": "flag", "value": "--params"},
            {"kind": "literal", "value": "ref/table.csv"},
        ],
        "writes": [{"kind": "flag", "value": "--out"}, {"kind": "positional", "value": "2"}],
        "value_flags": ["--params", "--out", "--n"],
    }
    args = ["--n", "5", "data.csv", "--params=p.txt", "--out", "o.json", "second.csv"]
    used, generated = resolve(readset, args, PurePosixPath("/proj"))
    assert used == ["/proj/data.csv", "/proj/p.txt", "/proj/ref/table.csv"]
    assert generated == ["/proj/o.json", "/proj/second.csv"]


def test_resolve_ignores_missing_arguments():
    readset = {
        "reads": [{"kind": "flag", "value": "--absent"}, {"kind": "positional", "value": "4"}],
        "writes": [],
        "value_flags": [],
    }
    assert resolve(readset, ["a"], PurePosixPath("/p")) == ([], [])


class FakeClient:
    model = "muse-spark-1.3"

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def structured(self, **kw):
        self.calls.append(kw)
        return self.answer


SUMMARIZE = """import json
a = json.load(open("results/fit.json"))
b = json.load(open("results/fit2.json"))
open("results/summary.txt", "w").write(str(a) + str(b))
"""


def _store(tmp_path):
    root = tmp_path / "proj"
    objects = tmp_path / "objects"
    sha = "c" * 64
    (objects / sha[:2]).mkdir(parents=True)
    (objects / sha[:2] / sha).write_text(SUMMARIZE)
    events = tmp_path / "events"
    events.mkdir()

    def fev(path, ts, h, op="scanned"):
        return {
            "v": 1,
            "type": "file",
            "id": f"F-{path}-{ts}",
            "host": "hp",
            "user": "t",
            "path": f"{root}/{path}",
            "op": op,
            "sha256": h,
            "size": 1,
            "fp": "full",
            "stored": True,
            "ts": ts,
        }

    lines = [
        fev("summarize.py", "2026-10-28T10:00:00.000Z", sha),
        fev("results/fit.json", "2026-10-28T10:00:01.000Z", "a" * 64),
        fev("results/fit2.json", "2026-10-28T10:00:02.000Z", "b" * 64),
        {
            "v": 1,
            "type": "cmd",
            "id": "S-1",
            "session": "S",
            "host": "hp",
            "shell": "bash",
            "user": "t",
            "cwd": str(root),
            "cmd": "python summarize.py",
            "ts_start": "2026-10-28T10:01:00.000Z",
            "ts_end": "2026-10-28T10:01:01.000Z",
            "exit": 0,
        },
        fev("results/summary.txt", "2026-10-28T10:01:00.800Z", "d" * 64, op="created"),
    ]
    (events / "e.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    conn = db.connect(tmp_path / "cairn.db")
    db.migrate(conn)
    ingest(conn, events, objects)
    return conn, objects


ANSWER = {
    "reads": [
        {"kind": "literal", "value": "results/fit.json"},
        {"kind": "literal", "value": "results/fit2.json"},
    ],
    "writes": [{"kind": "literal", "value": "results/summary.txt"}],
    "value_flags": [],
}


def edges(conn):
    return sorted(
        (role, path.rsplit("/", 1)[-1], method)
        for role, path, method in conn.execute(
            "SELECT e.role, a.path, e.method FROM edges e JOIN artifact_versions v"
            " ON v.id = e.version_id JOIN artifacts a ON a.id = v.artifact_id"
        )
    )


def test_readset_adds_hidden_inputs_and_is_cached(tmp_path):
    conn, objects = _store(tmp_path)
    client = FakeClient(ANSWER)
    reader = ReadsetReader(client, objects)
    link(conn, reader=reader)
    assert edges(conn) == [
        ("generated", "summary.txt", "window"),  # window wins: found first
        ("used", "fit.json", "readset"),
        ("used", "fit2.json", "readset"),
        ("used", "summarize.py", "argpath"),
    ]
    assert len(client.calls) == 1
    sent = json.dumps(client.calls[0]["messages"])
    assert "results/fit2.json" in sent  # the script source was sent
    assert client.calls[0]["reasoning_effort"] == "low"
    # Cached by script hash: relinking does not call the model again.
    link(conn, reader=ReadsetReader(client, objects))
    assert len(client.calls) == 1
    assert conn.execute("SELECT model FROM readsets").fetchone()[0] == "muse-spark-1.3"


def test_readset_failure_is_not_fatal_and_not_cached(tmp_path):
    from cairnlab.llm.client import LLMError

    conn, objects = _store(tmp_path)

    class Broken(FakeClient):
        def structured(self, **kw):
            raise LLMError("network down")

    link(conn, reader=ReadsetReader(Broken(None), objects))
    assert ("used", "summarize.py", "argpath") in edges(conn)
    assert conn.execute("SELECT COUNT(*) FROM readsets").fetchone()[0] == 0


def test_script_without_stored_object_is_skipped(tmp_path):
    conn, objects = _store(tmp_path)
    for f in objects.rglob("*"):
        if f.is_file():
            f.unlink()
    client = FakeClient(ANSWER)
    link(conn, reader=ReadsetReader(client, objects))
    assert client.calls == []
