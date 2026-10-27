"""The weekly supervisor update (CLAUDE.md 9.3): from the week's entries, not raw events.

Muse Spark turns the entries into four short lists (what moved, what is blocked,
decisions and why, next week); Cairn renders them as plain email text and enforces
the 300-word limit by dropping the least important (last) items.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from importlib import resources
from pathlib import Path

from cairnlab.llm.schemas import REPORT

MAX_WORDS = 300
SECTIONS = [
    ("moved", "What moved"),
    ("blocked", "Blocked or open"),
    ("decisions", "Decisions"),
    ("next_week", "Next week"),
]


@dataclass
class WeeklyReport:
    start: date
    end: date
    text: str
    days_with_entries: list[str]
    days_without_entries: list[str] = field(default_factory=list)
    path: Path | None = None


def week_bounds(today: date, start: date | None = None) -> tuple[date, date]:
    """The seven days starting at ``start``, or ending today."""
    if start is not None:
        return start, start + timedelta(days=6)
    return today - timedelta(days=6), today


def _prompt() -> str:
    return resources.files("cairnlab.llm").joinpath("prompts/report.md").read_text()


def _words(text: str) -> int:
    return len(text.split())


def render(report: dict, start: date, end: date, missing: list[str]) -> str:
    """Plain email text; trimmed to MAX_WORDS by dropping the last items of long lists."""
    report = {k: list(report.get(k, [])) for k, _ in SECTIONS} | {
        "subject": report.get("subject", "")
    }

    def build() -> str:
        lines = [
            f"Subject: {report['subject'] or 'Weekly update'}",
            f"Week {start.isoformat()} to {end.isoformat()}",
            "",
        ]
        for key, heading in SECTIONS:
            if report[key]:
                lines.append(f"{heading}:")
                lines += [f"- {item.strip()}" for item in report[key]]
                lines.append("")
        if missing:
            lines.append("No notebook entry was written for " + ", ".join(missing) + ".")
        return "\n".join(lines).rstrip() + "\n"

    text = build()
    while _words(text) > MAX_WORDS:
        longest = max((k for k, _ in SECTIONS), key=lambda k: len(report[k]))
        if len(report[longest]) <= 1:
            break
        report[longest].pop()
        text = build()
    return text


def write_report(
    conn: sqlite3.Connection,
    home: Path,
    client,
    today: date,
    start: date | None = None,
    tz: tzinfo | None = None,
) -> WeeklyReport:
    """Write the update for the week; days with runs but no entry are named in it."""
    first, last = week_bounds(today, start)
    days = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
    entries = conn.execute(
        "SELECT day, markdown FROM entries WHERE day >= ? AND day <= ? AND model != 'none'"
        " ORDER BY day",
        (days[0], days[-1]),
    ).fetchall()
    have = [d for d, _ in entries]
    # Local calendar days, as entries use (runs are stored in UTC).
    since = datetime.combine(first, time(0), tzinfo=tz) if tz else None
    active = set()
    for (ts,) in conn.execute("SELECT ts_start FROM runs WHERE ts_start IS NOT NULL"):
        when = datetime.fromisoformat(ts)
        local = when.astimezone(tz).date() if tz else when.date()
        if (since is None or when >= since) and first <= local <= last:
            active.add(local.isoformat())
    missing = [d for d in days if d in active and d not in have]
    if not entries:
        text = (
            f"Subject: Weekly update\nWeek {first} to {last}\n\n"
            "No notebook entries were written this week"
            + (
                f" (runs were recorded on {', '.join(missing)}; write them with cairn entry <day>)"
                if missing
                else ""
            )
            + ".\n"
        )
        return WeeklyReport(first, last, text, [], missing)
    body = "\n\n".join(f"=== Entry for {d} ===\n{md}" for d, md in entries)
    answer = client.structured(
        purpose="report",
        messages=[{"role": "system", "content": _prompt()}, {"role": "user", "content": body}],
        schema_name="WeeklyReport",
        schema=REPORT,
        reasoning_effort="medium",
    )
    text = render(answer, first, last, missing)
    out = home / "reports" / f"{first.isoformat()}_{last.isoformat()}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return WeeklyReport(first, last, text, have, missing, out)
