"""Tests for gathering and compressing a day of events."""

import hashlib
import io
import json
from datetime import UTC, date, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

from cairnlab.capture.ingest import ingest
from cairnlab.provenance.linker import link
from cairnlab.provenance.objects import object_path
from cairnlab.store import db
from cairnlab.synth.gather import day_bounds, gather, to_prompt

ROOT = "/data/proj"
IST = timezone(timedelta(hours=5, minutes=30))


class Store:
    def __init__(self, tmp_path: Path):
        self.objects = tmp_path / "objects"
        self.events = tmp_path / "events"
        self.events.mkdir()
        self.logs = tmp_path / "logs"
        self.logs.mkdir()
        self.conn = db.connect(tmp_path / "cairn.db")
        db.migrate(self.conn)
        self.lines: list[dict] = []
        self.n = 0

    def blob(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        p = object_path(self.objects, sha)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return sha

    def file(self, path: str, at: str, data: bytes, op: str = "modified"):
        self.n += 1
        self.lines.append(
            {
                "v": 1,
                "type": "file",
                "id": f"F{self.n:025d}",
                "host": "hp",
                "user": "t",
                "path": f"{ROOT}/{path}",
                "op": op,
                "sha256": self.blob(data),
                "size": len(data),
                "fp": "full",
                "stored": True,
                "ts": at,
            }
        )

    def cmd(self, cmd: str, start: str, end: str, exit: int = 0, **kw):
        self.n += 1
        self.lines.append(
            {
                "v": 1,
                "type": "cmd",
                "id": f"S-{self.n}",
                "session": "S",
                "host": "hp",
                "shell": "bash",
                "user": "t",
                "cwd": ROOT,
                "cmd": cmd,
                "ts_start": start,
                "ts_end": end,
                "exit": exit,
                **kw,
            }
        )

    def load(self):
        (self.events / "e.jsonl").write_text("".join(json.dumps(x) + "\n" for x in self.lines))
        ingest(self.conn, self.events, self.objects)
        link(self.conn)


def T(hhmmss: str, day: str = "2026-10-28") -> str:
    return f"{day}T{hhmmss}.000Z"


def png_bytes(color: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (20, 10), color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def day(tmp_path):
    s = Store(tmp_path)
    s.file("params.txt", T("08:00:00"), b"model = exp\ncutoff = 1.0\n", op="scanned")
    s.file("fit.py", T("08:00:00"), b"print('fit')\n", op="scanned")
    s.file("data.csv", T("08:00:00"), b"1,2\n", op="scanned")
    s.cmd("python fit.py data.csv --params params.txt --out fit.json", T("09:00:00"), T("09:00:02"))
    s.file("fit.json", T("09:00:01"), b'{"tau": 2.0}')
    s.cmd("python plot.py fit.json --out fig.png", T("09:01:00"), T("09:01:03"))
    s.file("fig.png", T("09:01:02"), png_bytes("red"))
    s.cmd("sed -i s/1.0/1.2/ params.txt", T("09:02:00"), T("09:02:00"))
    s.file("params.txt", T("09:02:00"), b"model = exp\ncutoff = 1.2\n")
    s.cmd("python fit.py data.csv --params params.txt --out fit.json", T("09:03:00"), T("09:03:02"))
    s.file("fit.json", T("09:03:01"), b'{"tau": 2.4}')
    s.cmd(
        "python fit.py data.csv --params params.txt --out fit.json",
        T("09:04:00"),
        T("09:04:02"),
        hist_repeat=True,
    )
    s.file("fit.json", T("09:04:01"), b'{"tau": 2.4}')  # identical output
    # An hour later: a new phase with a failure.
    s.cmd(
        "python fit.py missing.csv --params params.txt --out bad.json",
        T("10:30:00"),
        T("10:30:01"),
        exit=1,
    )
    s.cmd("python plot.py fit.json --out fig.png", T("10:31:00"), T("10:31:03"))
    s.file("fig.png", T("10:31:02"), png_bytes("blue"))
    s.load()
    return s


def test_phases_and_collapsed_reruns(day):
    ctx = gather(day.conn, day.objects, date(2026, 10, 28), root=Path(ROOT))
    assert ctx.run_count == 7
    assert [len(p.runs) for p in ctx.phases] == [4, 2]  # 5 runs, two collapsed into one line
    rerun = ctx.phases[0].runs[3]
    assert len(rerun.ids) == 2 and "identical rerun" not in rerun.flags  # flag of the 2nd run
    assert ctx.phases[1].runs[0].exit == 1


def test_reruns_with_different_outputs_are_not_collapsed(day):
    ctx = gather(day.conn, day.objects, date(2026, 10, 28), root=Path(ROOT))
    fits = [r for r in ctx.phases[0].runs if r.cmd.startswith("python fit.py data.csv")]
    assert [len(r.ids) for r in fits] == [1, 2]


def test_drift_found_with_context(day):
    ctx = gather(day.conn, day.objects, date(2026, 10, 28), root=Path(ROOT))
    (d,) = ctx.drift
    assert d.path == "params.txt" and d.changes == [("cutoff", "1.0", "1.2")]
    runs = {r.cmd.split()[0] + str(i): r for i, r in enumerate(ctx.phases[0].runs)}
    assert d.changed_by == ctx.phases[0].runs[2].ids[0]  # the sed run
    assert d.used_before == ctx.phases[0].runs[0].ids[0]
    assert d.used_after == ctx.phases[0].runs[3].ids[0]
    assert runs


def test_latest_figure_version_only(day):
    ctx = gather(day.conn, day.objects, date(2026, 10, 28), root=Path(ROOT))
    assert len(ctx.figures) == 1
    fig, run_id = ctx.figures[0]
    assert fig.path == "fig.png" and run_id == ctx.phases[1].runs[1].ids[0]


def test_every_valid_id_is_in_the_prompt_and_nothing_else(day):
    import re

    ctx = gather(day.conn, day.objects, date(2026, 10, 28), root=Path(ROOT))
    text = to_prompt(ctx)
    shown = set(re.findall(r"\[([RAN]:[0-9A-Z]{26})\]", text))
    assert shown == ctx.valid_ids
    assert "cutoff: 1.0 → 1.2" in text
    assert "ran 2 times identically" in text
    assert "No notes were recorded today." in text


def test_error_tail_from_background_log(day):
    rid = day.conn.execute("SELECT id FROM runs WHERE exit = 1").fetchone()[0]
    (day.logs / f"{rid}.log").write_text(
        "\n".join(f"line {i}" for i in range(50)) + "\nFileNotFoundError: missing.csv\n"
    )
    ctx = gather(day.conn, day.objects, date(2026, 10, 28), root=Path(ROOT), logs_dir=day.logs)
    tail = ctx.phases[1].runs[0].error_tail
    assert tail.endswith("FileNotFoundError: missing.csv")
    assert tail.count("\n") == 19
    assert "| FileNotFoundError: missing.csv" in to_prompt(ctx)


def test_day_bounds_follow_local_timezone(tmp_path):
    s = Store(tmp_path)
    s.cmd("echo late", T("19:00:00", "2026-10-28"), T("19:00:01", "2026-10-28"))  # 00:30 IST
    s.load()
    assert gather(s.conn, s.objects, date(2026, 10, 28), tz=IST).run_count == 0
    assert gather(s.conn, s.objects, date(2026, 10, 29), tz=IST).run_count == 1
    assert day_bounds(date(2026, 10, 29), IST) == (
        "2026-10-28T18:30:00.000Z",
        "2026-10-29T18:30:00.000Z",
    )
    assert day_bounds(date(2026, 10, 29), UTC)[0] == "2026-10-29T00:00:00.000Z"


def test_prompt_shrinks_to_fit(day):
    ctx = gather(day.conn, day.objects, date(2026, 10, 28), root=Path(ROOT))
    full = to_prompt(ctx)
    small = to_prompt(ctx, max_chars=len(full) - 10)
    assert "used:" not in small or len(small) < len(full)
