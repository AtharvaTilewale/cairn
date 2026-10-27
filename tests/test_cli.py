"""Tests for the Typer CLI: init, status and the not-yet-implemented stubs."""

import tomllib

import pytest
from typer.testing import CliRunner

from cairnlab.cli import app

runner = CliRunner()


def snapshot(directory):
    """Every path under ``directory`` (relative), to prove nothing was written there."""
    return sorted(str(p.relative_to(directory)) for p in directory.rglob("*"))


def test_init_creates_layout(project):
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    cairn = project / ".cairn"
    assert (cairn / "cairn.db").is_file()
    assert (cairn / "events").is_dir()
    assert (cairn / ".gitignore").read_text().strip() == "*"
    config = tomllib.loads((cairn / "config.toml").read_text())
    assert config["project"]["name"] == "proj"
    assert config["llm"]["backend"] == "spark"
    ignore = (project / ".cairnignore").read_text()
    assert "__pycache__/" in ignore


def test_init_touches_no_dotfiles(project, isolated_env):
    before = snapshot(isolated_env)
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert snapshot(isolated_env) == before
    # Only .cairn/ and .cairnignore appear in the project.
    assert sorted(p.name for p in project.iterdir()) == [".cairn", ".cairnignore"]


def test_init_is_idempotent_and_keeps_edits(project):
    assert runner.invoke(app, ["init"]).exit_code == 0
    (project / ".cairnignore").write_text("mine\n")
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert (project / ".cairnignore").read_text() == "mine\n"


def test_init_refuses_home_directory(isolated_env, monkeypatch):
    monkeypatch.chdir(isolated_env)
    result = runner.invoke(app, ["init"])
    assert result.exit_code != 0
    assert not (isolated_env / ".cairn").exists()
    assert not (isolated_env / ".cairnignore").exists()


def test_status_without_store(project):
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 1
    assert "cairn init" in result.output


def test_status_on_empty_store(project):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "cairn.db" in result.output
    assert "events: 0" in result.output.lower()
    assert "runs: 0" in result.output.lower()


@pytest.mark.parametrize(
    "args",
    [
        ["watch"],
        ["scan", "--since", "2026-10-27T00:00:00Z"],
        ["sync", "cluster"],
        ["run", "--", "python", "fit.py"],
        ["run", "--bg", "--", "sleep", "1"],
        ["note", "why I changed the cutoff"],
        ["entry", "today"],
        ["trace", "fig.png"],
        ["reproduce", "fig.png", "--yes"],
        ["ask", "what changed?"],
        ["report", "week"],
        ["show", "01JABC"],
        ["serve"],
    ],
)
def test_stubs_exit_zero(args, project):
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "not implemented yet" in result.output
