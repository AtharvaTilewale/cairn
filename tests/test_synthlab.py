"""synthlab end to end: real watcher, real hook, ingest, linking, metrics (about 20 s)."""

import json
import shutil

import pytest

pytestmark = [
    pytest.mark.shell,
    pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash"),
]


def test_synthlab_capture_is_complete(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("matplotlib")
    from eval.metrics import compute
    from eval.synthlab import build, play

    project = tmp_path / "proj"
    build(project)
    play(project, tmp_path / "home")
    m = compute(project)
    assert m["ok"], json.dumps(m, default=str)
    assert m["run_recall"] == 1.0 and m["run_precision"] == 1.0
    assert m["fit_json_versions"] == 3 and m["params_versions"] == 3
    # Linking without readset (no network in tests): precision 1.0; recall misses only
    # the two inputs summarize.py names in its own code.
    assert m["edge_precision"] >= 0.95, m
    assert m["edge_recall"] >= 0.9, m
    assert ("used", "results/fit2.json") in {(r, p) for _, r, p in m["edge_false_negatives"]}
