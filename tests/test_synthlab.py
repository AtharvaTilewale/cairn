"""synthlab end to end: real watcher, real hook, ingest, linking, metrics (about 20 s)."""

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
    assert m["ok"], m
    assert m["run_recall"] == 1.0 and m["run_precision"] == 1.0
    assert m["fit_json_versions"] == 3 and m["params_versions"] == 3
    # Linking (argpath + window): 1.0 / 1.0 today; allow a little timing slack.
    assert m["edge_precision"] >= 0.95 and m["edge_recall"] >= 0.95, m
