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
    assert "0 artefacts, 0 versions, 0 changes, 0 edges" in result.output
    assert "runs: 0" in result.output.lower()


@pytest.mark.parametrize(
    "args",
    [
        ["ask", "what changed?"],
        ["serve"],
    ],
)
def test_stubs_exit_zero(args, project):
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "not implemented yet" in result.output


def test_activate_prints_hook_for_this_store(project):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["activate"])
    assert result.exit_code == 0
    assert f"__cairn_default_home={project / '.cairn'}" in result.stdout
    assert result.stdout.rstrip().endswith("__cairn_activate")


def test_activate_outside_a_store_leaves_home_to_the_hook(project):
    result = runner.invoke(app, ["activate"])
    assert result.exit_code == 0
    assert "__cairn_default_home=" not in result.stdout


def test_activate_uses_a_fresh_ulid_session(project):
    first = runner.invoke(app, ["activate"]).stdout.splitlines()[0]
    second = runner.invoke(app, ["activate"]).stdout.splitlines()[0]
    assert first.startswith("__cairn_session_id=") and len(first) == len("__cairn_session_id=") + 26
    assert first != second


def test_deactivate_prints_restore_code(project):
    result = runner.invoke(app, ["deactivate"])
    assert result.exit_code == 0
    assert "__cairn_deactivate" in result.stdout


def test_ingest_then_status(project):
    runner.invoke(app, ["init"])
    events = project / ".cairn" / "events"
    line = (
        '{"v":1,"type":"cmd","id":"S-1","session":"S","host":"synth","shell":"bash",'
        '"user":"tester","cwd":"/p","cmd":"echo hi","ts_start":"2026-10-28T09:00:00.000Z",'
        '"ts_end":"2026-10-28T09:00:01.000Z","exit":0}\n'
    )
    (events / "synth-2026-10-28.jsonl").write_text(line)
    assert "Not yet ingested" in runner.invoke(app, ["status"]).output
    result = runner.invoke(app, ["ingest"])
    assert result.exit_code == 0, result.output
    assert "1 new runs" in result.output
    status = runner.invoke(app, ["status"]).output
    assert "Runs: 1" in status
    assert "synth" in status and "2026-10-28T09:00:00.000Z" in status
    assert "Recent runs" in status and "echo hi" in status
    assert "Not yet ingested" not in status
    assert "0 new runs" in runner.invoke(app, ["ingest"]).output


def test_ingest_without_store(project):
    assert runner.invoke(app, ["ingest"]).exit_code == 1


def test_scan_writes_file_events(project, monkeypatch):
    runner.invoke(app, ["init"])
    (project / "data.csv").write_text("1,2\n")
    (project / "__pycache__").mkdir()
    (project / "__pycache__" / "m.pyc").write_bytes(b"x")
    monkeypatch.setenv("CAIRN_USER", "tester")
    monkeypatch.setenv("CAIRN_HOST", "synth")
    result = runner.invoke(app, ["scan"])
    assert result.exit_code == 0, result.output
    # data.csv and .cairnignore; never .cairn/ or __pycache__/
    assert "2 file events" in result.output
    assert "0 file events" in runner.invoke(app, ["scan"]).output
    files = list((project / ".cairn" / "events").glob("synth-*-watch.jsonl"))
    assert len(files) == 1
    result = runner.invoke(app, ["scan", "--since", "2999-01-01T00:00:00Z"])
    assert "0 file events" in result.output


def test_scan_requires_cairn_user(project):
    runner.invoke(app, ["init"])
    assert runner.invoke(app, ["scan"]).exit_code == 1


def test_scan_rejects_bad_since(project, monkeypatch):
    runner.invoke(app, ["init"])
    monkeypatch.setenv("CAIRN_USER", "tester")
    assert runner.invoke(app, ["scan", "--since", "yesterday"]).exit_code == 2


def test_watch_refuses_on_eddie(project, monkeypatch):
    runner.invoke(app, ["init"])
    monkeypatch.setenv("CAIRN_USER", "tester")
    monkeypatch.setenv("CAIRN_NO_DAEMON", "1")
    result = runner.invoke(app, ["watch"])
    assert result.exit_code == 2
    assert "never runs on the cluster" in result.output


def test_readset_setting(project, monkeypatch):
    from cairnlab import config

    runner.invoke(app, ["init"])
    home = project / ".cairn"
    monkeypatch.delenv("CAIRN_READSET")
    assert config.readset_enabled(home) is True
    cfg = home / "config.toml"
    cfg.write_text(cfg.read_text().replace("readset = true", "readset = false"))
    assert config.readset_enabled(home) is False
    monkeypatch.setenv("CAIRN_READSET", "1")
    assert config.readset_enabled(home) is True


def test_entry_dry_run_and_show(project, monkeypatch):
    runner.invoke(app, ["init"])
    events = project / ".cairn" / "events"
    run = (
        '{"v":1,"type":"cmd","id":"S-1","session":"S","host":"synth","shell":"bash",'
        '"user":"tester","cwd":"/p","cmd":"python fit.py","ts_start":"2026-10-28T09:00:00.000Z",'
        '"ts_end":"2026-10-28T09:00:01.000Z","exit":3}\n'
    )
    (events / "synth-2026-10-28.jsonl").write_text(run)
    runner.invoke(app, ["ingest"])
    cfg = project / ".cairn" / "config.toml"
    cfg.write_text(cfg.read_text().replace('# timezone = "Asia/Kolkata"', 'timezone = "UTC"'))
    result = runner.invoke(app, ["entry", "2026-10-28", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "$ python fit.py" in result.output and "exit 3" in result.output
    import re

    rid = re.search(r"\[R:([0-9A-Z]{26})\]", result.output).group(1)
    shown = runner.invoke(app, ["show", f"R:{rid}"])
    assert shown.exit_code == 0 and "python fit.py" in shown.output and "exit 3" in shown.output
    assert runner.invoke(app, ["show", rid.lower()]).exit_code == 0
    assert runner.invoke(app, ["show", "R:01ARZ3NDEKTSV4RRFFQ69G5FAV"]).exit_code == 1
    assert runner.invoke(app, ["entry", "someday", "--dry-run"]).exit_code == 2


def test_entry_without_api_key_fails_cleanly(project):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["entry", "today"])
    assert result.exit_code == 1 and "MODEL_API_KEY" in result.output


def test_link_command_and_malformed_warning(project):
    runner.invoke(app, ["init"])
    (project / ".cairn" / "events" / "bad.jsonl").write_text("not json\n")
    result = runner.invoke(app, ["ingest"])
    assert result.exit_code == 0 and "Malformed lines were skipped" in result.output
    linked = runner.invoke(app, ["link"])
    assert linked.exit_code == 0, linked.output
    assert "Linked 0 runs" in linked.output
