"""Shared fixtures: every test runs with a throwaway HOME and no Cairn env vars."""

import pytest


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Point HOME at a temp dir and clear CAIRN_* so tests never touch real data."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in ("CAIRN_HOME", "CAIRN_USER", "CAIRN_SESSION", "CAIRN_HOST"):
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture
def project(tmp_path, monkeypatch):
    """An empty project directory that is also the current working directory."""
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    return proj
