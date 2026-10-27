"""Shared fixtures: a throwaway HOME, no Cairn env vars, no API key, readset off."""

import pytest


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Point HOME at a temp dir and clear CAIRN_* so tests never touch real data."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in ("CAIRN_HOME", "CAIRN_USER", "CAIRN_SESSION", "CAIRN_HOST", "MODEL_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    # Tests never call a model API.
    monkeypatch.setenv("CAIRN_READSET", "0")
    return home


@pytest.fixture
def project(tmp_path, monkeypatch):
    """An empty project directory that is also the current working directory."""
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    return proj
