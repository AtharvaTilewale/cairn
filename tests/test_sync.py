"""Tests for pulling a spoke's events and objects into the hub (local transport)."""

import json
import shutil

import pytest
from typer.testing import CliRunner

from cairnlab.capture.ingest import ingest
from cairnlab.capture.sync import Host, load_hosts, sync, sync_commands
from cairnlab.cli import app
from cairnlab.store import db

pytestmark = pytest.mark.skipif(shutil.which("rsync") is None, reason="needs rsync")


def spoke_event(i: int) -> dict:
    return {
        "v": 1,
        "type": "cmd",
        "id": f"WS-{i}",
        "session": "WS",
        "host": "workstation",
        "shell": "bash",
        "user": "alice",
        "cwd": "/home/labuser/md",
        "cmd": f"gmx step {i}",
        "ts_start": f"2026-10-28T10:00:{i:02d}.000Z",
        "ts_end": f"2026-10-28T10:00:{i:02d}.500Z",
        "exit": 0,
    }


@pytest.fixture
def stores(tmp_path):
    spoke = tmp_path / "spoke" / ".cairn-alice"
    (spoke / "events").mkdir(parents=True)
    (spoke / "objects" / "ab").mkdir(parents=True)
    (spoke / "objects" / "ab" / ("ab" + "0" * 62)).write_text("an mdp file")
    (spoke / "logs").mkdir()
    (spoke / "logs" / "01ARZ3NDEKTSV4RRFFQ69G5FAV.log").write_text("step 1\n")
    f = spoke / "events" / "workstation-2026-10-28.jsonl"
    f.write_text("".join(json.dumps(spoke_event(i)) + "\n" for i in range(3)))
    hub = tmp_path / "hub" / ".cairn"
    (hub / "events").mkdir(parents=True)
    return spoke, hub, f


def test_commands_for_ssh_and_local():
    remote = Host("workstation", ssh="lab-ws", home="~/.cairn-alice")
    cmds = sync_commands(remote, hub_home=__import__("pathlib").Path("/hub/.cairn"))
    flat = [" ".join(c) for c in cmds]
    assert any(
        "lab-ws:~/.cairn-alice/events/" in c and "/hub/.cairn/events/remote/workstation/" in c
        for c in flat
    )
    assert any("lab-ws:~/.cairn-alice/objects/" in c and "--ignore-existing" in c for c in flat)
    assert all("--delete" not in c for c in flat)  # pull only, never delete


def test_sync_then_ingest_is_idempotent(stores):
    spoke, hub, f = stores
    host = Host("workstation", ssh="", home=str(spoke))
    conn = db.connect(hub / "cairn.db")
    db.migrate(conn)
    sync(host, hub)
    assert ingest(conn, hub / "events").runs == 3
    assert (hub / "objects" / "ab" / ("ab" + "0" * 62)).read_text() == "an mdp file"
    assert (hub / "logs" / "01ARZ3NDEKTSV4RRFFQ69G5FAV.log").exists()

    sync(host, hub)
    assert ingest(conn, hub / "events").runs == 0
    with f.open("a") as fh:  # the spoke keeps working
        fh.write(json.dumps(spoke_event(3)) + "\n")
    sync(host, hub)
    assert ingest(conn, hub / "events").runs == 1
    hosts = [h for (h,) in conn.execute("SELECT DISTINCT host FROM runs")]
    assert hosts == ["workstation"]
    # Offsets are kept per relative path, apart from the hub's own files.
    keys = [k for (k,) in conn.execute("SELECT file FROM ingest_state")]
    assert keys == ["remote/workstation/workstation-2026-10-28.jsonl"]


def test_failed_transfer_raises(tmp_path):
    from cairnlab.capture.sync import SyncError

    host = Host("ghost", ssh="", home=str(tmp_path / "does-not-exist"))
    with pytest.raises(SyncError):
        sync(host, tmp_path / "hub")


def test_hosts_file(tmp_path, monkeypatch):
    f = tmp_path / "hosts.toml"
    f.write_text(
        '[workstation]\nssh = "lab-ws"\nhome = "~/.cairn-alice"\n[laptop]\nhome = "/srv/cairn"\n'
    )
    monkeypatch.setenv("CAIRN_HOSTS_FILE", str(f))
    hosts = load_hosts()
    assert hosts["workstation"] == Host("workstation", "lab-ws", "~/.cairn-alice")
    assert hosts["laptop"].ssh == ""


def test_cli_sync(stores, tmp_path, monkeypatch):
    spoke, hub, _ = stores
    f = tmp_path / "hosts.toml"
    f.write_text(f'[workstation]\nssh = ""\nhome = "{spoke}"\n')
    monkeypatch.setenv("CAIRN_HOSTS_FILE", str(f))
    monkeypatch.setenv("CAIRN_HOME", str(hub))
    runner = CliRunner()
    assert runner.invoke(app, ["init", str(tmp_path / "hub")]).exit_code == 0
    dry = runner.invoke(app, ["sync", "workstation", "--dry-run"])
    assert dry.exit_code == 0 and "rsync" in dry.output
    assert not (hub / "events" / "remote").exists()
    res = runner.invoke(app, ["sync", "workstation"])
    assert res.exit_code == 0, res.output
    assert "3 new runs" in res.output
    assert runner.invoke(app, ["sync", "nowhere"]).exit_code == 2
    listing = runner.invoke(app, ["sync"])
    assert "workstation" in listing.output
