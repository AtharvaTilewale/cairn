"""Tests for parameter-file parsing and diffs."""

import pytest

from cairnlab.provenance.drift import diff, is_config, parse


def test_mdp_and_params_txt():
    text = """; GROMACS run parameters
integrator = md
dt         = 0.002   ; ps
nsteps     = 500000
tcoupl = V-rescale
"""
    assert parse("md.mdp", text) == {
        "integrator": "md",
        "dt": "0.002",
        "nsteps": "500000",
        "tcoupl": "V-rescale",
    }
    assert parse("params.txt", "model = exp\ncutoff = 1.0\n# note\n") == {
        "model": "exp",
        "cutoff": "1.0",
    }


def test_yaml_json_toml():
    yaml = "model: exp\nfit:\n  cutoff: 1.2\n  tol: 1e-6\n# c\nname: 'run 4'\n"
    assert parse("config.yaml", yaml) == {
        "model": "exp",
        "fit.cutoff": "1.2",
        "fit.tol": "1e-6",
        "name": "run 4",
    }
    assert parse("c.json", '{"a": 1, "b": {"c": [1, 2]}}') == {"a": "1", "b.c": "[1, 2]"}
    assert parse("c.toml", '[fit]\ncutoff = 1.5\n[io]\nout = "x.csv"\n') == {
        "fit.cutoff": "1.5",
        "io.out": "x.csv",
    }


def test_diff_reports_changed_added_removed():
    old = "model = exp\ncutoff = 1.0\nseed = 1\n"
    new = "model = poly\ncutoff = 1.0\norder = 3\n"
    assert diff("params.txt", old, new) == [
        ("model", "exp", "poly"),
        ("order", None, "3"),
        ("seed", "1", None),
    ]


def test_unparseable_gives_empty():
    assert parse("c.json", "{not json") == {}
    assert diff("c.json", "{", "}") == []


@pytest.mark.parametrize(
    "path, ok",
    [
        ("md.mdp", True),
        ("params_v2.txt", True),
        ("x.yaml", True),
        ("a.yml", True),
        ("c.toml", True),
        ("s.json", True),
        ("x.ini", True),
        ("y.cfg", True),
        ("data.csv", False),
        ("notes.txt", False),
    ],
)
def test_is_config(path, ok):
    assert is_config(path) is ok
