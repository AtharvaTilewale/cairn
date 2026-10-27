"""Tests for provenance traces: walking generated <- run <- used to raw inputs."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cairnlab.provenance.graph import input_status, producer, resolve_version, trace
from tests.test_gather import ROOT, Store, T


@pytest.fixture
def chain(tmp_path):
    """gen -> data.csv; sed edits params; fit(data, params) -> fit.json; plot -> fig.png."""
    s = Store(tmp_path)
    s.file("gen.py", T("08:00:00"), b"gen", op="scanned")
    s.file("fit.py", T("08:00:00"), b"fit", op="scanned")
    s.file("plot.py", T("08:00:00"), b"plot", op="scanned")
    s.file("params.txt", T("08:00:00"), b"cutoff = 1.0\n", op="scanned")
    s.cmd("python gen.py --out data.csv", T("09:00:00"), T("09:00:01"))
    s.file("data.csv", T("09:00:01"), b"1,2\n", op="created")
    s.cmd("sed -i s/1.0/1.2/ params.txt", T("09:01:00"), T("09:01:00"))
    s.file("params.txt", T("09:01:00"), b"cutoff = 1.2\n")
    s.cmd("python fit.py data.csv --params params.txt --out fit.json", T("09:02:00"), T("09:02:02"))
    s.file("fit.json", T("09:02:01"), b'{"tau": 2.4}', op="created")
    s.cmd("python plot.py fit.json --out fig.png", T("09:03:00"), T("09:03:02"))
    s.file("fig.png", T("09:03:01"), b"PNG", op="created")
    s.load()
    return s


def test_trace_walks_to_raw_inputs(chain):
    vid = resolve_version(chain.conn, "hp", f"{ROOT}/fig.png")
    t = trace(chain.conn, vid)
    assert [s.run["cmd"].split()[1] for s in t.steps] == [
        "gen.py",
        "-i",
        "fit.py",
        "plot.py",
    ]  # topological: every step after the steps that made its inputs
    assert t.steps[-1].outputs == [vid]
    raw = {t.nodes[v].path.rsplit("/", 1)[-1] for v in t.raw_inputs}
    assert raw == {"gen.py", "fit.py", "plot.py", "params.txt"}
    # The edited params.txt traces back to the version before the edit.
    params = [n for n in t.nodes.values() if n.path.endswith("params.txt")]
    assert len(params) == 2


def test_raw_input_has_no_steps(chain):
    vid = resolve_version(chain.conn, "hp", f"{ROOT}/gen.py")
    t = trace(chain.conn, vid)
    assert t.steps == [] and t.raw_inputs == [vid]


def test_resolve_version_at_time_and_sha(chain):
    old = resolve_version(chain.conn, "hp", f"{ROOT}/params.txt", at=T("09:00:30"))
    new = resolve_version(chain.conn, "hp", f"{ROOT}/params.txt")
    assert old != new
    sha = chain.conn.execute(
        "SELECT sha256 FROM artifact_versions WHERE id = ?", (old,)
    ).fetchone()[0]
    assert resolve_version(chain.conn, "hp", f"{ROOT}/params.txt", sha=sha[:10]) == old
    assert resolve_version(chain.conn, "hp", f"{ROOT}/nope.txt") is None


def test_producer_prefers_strong_evidence(chain):
    vid = resolve_version(chain.conn, "hp", f"{ROOT}/fit.json")
    run, method, conf = producer(chain.conn, vid)
    assert "fit.py" in run["cmd"] and method == "argpath" and conf == 0.9


def test_input_status_against_disk(chain, tmp_path):
    root = tmp_path / "disk"
    root.mkdir()
    (root / "data.csv").write_text("1,2\n")  # unchanged
    (root / "params.txt").write_text("cutoff = 9.9\n")  # edited since
    vid = resolve_version(chain.conn, "hp", f"{ROOT}/fit.json")
    t = trace(chain.conn, vid)
    fit = t.steps[-1]
    status = {Path(p).name: s for p, s in input_status(chain.conn, fit, root_map=(ROOT, root))}
    assert status == {"data.csv": "unchanged", "params.txt": "changed since", "fit.py": "missing"}


def test_cli_trace(chain, tmp_path, monkeypatch):
    from cairnlab.cli import app

    home = tmp_path / "h" / ".cairn"
    home.mkdir(parents=True)
    (home / "cairn.db").symlink_to(tmp_path / "cairn.db")
    monkeypatch.setenv("CAIRN_HOME", str(home))
    monkeypatch.setenv("CAIRN_HOST", "hp")
    runner = CliRunner()
    res = runner.invoke(app, ["trace", f"{ROOT}/fig.png"])
    assert res.exit_code == 0, res.output
    assert "python plot.py fit.json --out fig.png" in res.output
    assert "python gen.py --out data.csv" in res.output
    assert "raw input" in res.output
    assert "Regenerate" in res.output and f"cd {ROOT}" in res.output
    js = runner.invoke(app, ["trace", f"{ROOT}/fig.png", "--json"])
    data = json.loads(js.output)
    assert len(data["steps"]) == 4 and data["target"]["path"].endswith("fig.png")
    assert runner.invoke(app, ["trace", f"{ROOT}/unknown.png"]).exit_code == 1
