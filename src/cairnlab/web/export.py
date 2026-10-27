"""Static HTML export of the notebook: entries, weekly reports, and every cited item.

``cairn export`` writes a self-contained site (no JavaScript, no server) to
``.cairn/site/``: an index of entries and reports, each entry rendered with its
footnotes linked to pages for the runs, file versions and notes it cites, and for
each file version its content (image or text) and provenance tree. Real research
data stays inside ``.cairn/`` (git-ignored); share the site only if you mean to.
"""

import html
import json
import re
import shutil
import sqlite3
from datetime import datetime, tzinfo
from pathlib import Path

import markdown

from cairnlab.llm.figures import is_raster
from cairnlab.provenance.graph import node, trace
from cairnlab.provenance.objects import object_path

CITE_SHOW = re.compile(r"\(`cairn show ([RAN]):([0-9A-HJKMNP-TV-Z]{26})`\)")
ID_CODE = re.compile(r"`([RAN]):([0-9A-HJKMNP-TV-Z]{26})`")
TEXT_LIMIT = 20_000

CSS = """
:root { --bg:#fbfaf7; --fg:#1f2328; --muted:#6a737d; --line:#e4e1da; --accent:#2f6f5e;
        --code:#f2efe8; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#16181b; --fg:#e6e6e6; --muted:#9aa4ae; --line:#2c3036; --accent:#7cc3ad;
          --code:#202328; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font: 16px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 860px; margin: 0 auto; padding: 32px 16px 64px; }
a { color: var(--accent); }
h1 { font-size: 1.7rem; line-height: 1.25; }
h2 { font-size: 1.2rem; margin-top: 2rem; border-bottom: 1px solid var(--line); }
code, pre { background: var(--code); border-radius: 4px; font-size: .88em; }
code { padding: .1em .3em; } pre { padding: 12px; overflow-x: auto; }
.muted { color: var(--muted); font-size: .9rem; }
.footnote { font-size: .85rem; color: var(--muted); }
img { max-width: 100%; border: 1px solid var(--line); border-radius: 4px; }
table { border-collapse: collapse; width: 100%; }
td, th { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); }
ul.tree, ul.tree ul { list-style: none; padding-left: 1.2rem; border-left: 1px solid var(--line); }
nav { margin-bottom: 1.5rem; font-size: .9rem; }
"""


def _page(title: str, body: str, depth: int = 0) -> str:
    up = "../" * depth
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>"
        f'<nav><a href="{up}index.html">Cairn notebook</a></nav>{body}</main></body></html>'
    )


class Exporter:
    def __init__(
        self,
        conn: sqlite3.Connection,
        objects_dir: Path,
        out: Path,
        tz: tzinfo,
        root: Path | None = None,
    ):
        self.conn = conn
        self.objects = objects_dir
        self.out = out
        self.tz = tz
        self.root = root
        self.items: set[str] = set()

    def _rel(self, path: str) -> str:
        if self.root and path.startswith(str(self.root).rstrip("/") + "/"):
            return path[len(str(self.root).rstrip("/")) + 1 :]
        return path

    def _local(self, ts: str | None) -> str:
        if not ts:
            return "-"
        return datetime.fromisoformat(ts).astimezone(self.tz).strftime("%Y-%m-%d %H:%M:%S")

    def _link(self, ident: str, label: str | None = None, depth: int = 1) -> str:
        self.items.add(ident)
        name = ident.replace(":", "_")
        return f'<a href="{"../" * depth}items/{name}.html">{html.escape(label or ident)}</a>'

    def _md(self, text: str) -> str:
        # Footnotes' "(`cairn show X`)" become links to the item pages.
        def show(m):
            ident = f"{m.group(1)}:{m.group(2)}"
            self.items.add(ident)
            return f"([details](../items/{ident.replace(':', '_')}.html))"

        text = CITE_SHOW.sub(show, text)
        return markdown.markdown(text, extensions=["footnotes", "tables", "fenced_code"])

    def export(self) -> Path:
        if self.out.exists():
            shutil.rmtree(self.out)
        (self.out / "entries").mkdir(parents=True)
        (self.out / "items").mkdir()
        (self.out / "files").mkdir()
        rows = self.conn.execute(
            "SELECT day, model, json, markdown FROM entries WHERE model != 'none' ORDER BY day DESC"
        ).fetchall()
        index = ["<h1>Cairn notebook</h1>"]
        if rows:
            index.append("<table><tr><th>day</th><th>entry</th><th>claims checked</th></tr>")
        for day, model, payload, md in rows:
            title = md.splitlines()[0].lstrip("# ").strip() if md else day
            faith = (json.loads(payload).get("faithfulness") or {}) if payload else {}
            score = (
                f"{faith['supported']} supported, {faith['unsupported']} unsupported"
                if faith.get("total")
                else "not checked"
            )
            (self.out / "entries" / f"{day}.html").write_text(
                _page(
                    f"{day} · {title}",
                    self._md(md) + f'<p class="muted">Written by {html.escape(model)}.</p>',
                    depth=1,
                )
            )
            index.append(
                f'<tr><td>{day}</td><td><a href="entries/{day}.html">'
                f"{html.escape(title)}</a></td><td>{score}</td></tr>"
            )
        if rows:
            index.append("</table>")
        else:
            index.append("<p>No entries yet. Write one with <code>cairn entry today</code>.</p>")
        reports_dir = self.objects.parent / "reports"
        reports = sorted(reports_dir.glob("*.txt"), reverse=True) if reports_dir.is_dir() else []
        if reports:
            index.append("<h2>Weekly reports</h2><ul>")
            for r in reports:
                name = r.stem
                (self.out / "entries" / f"report-{name}.html").write_text(
                    _page(f"Weekly report {name}", f"<pre>{html.escape(r.read_text())}</pre>", 1)
                )
                index.append(f'<li><a href="entries/report-{name}.html">{name}</a></li>')
            index.append("</ul>")
        (self.out / "index.html").write_text(_page("Cairn notebook", "".join(index)))
        done: set[str] = set()
        while self.items - done:  # item pages can cite further items
            for ident in sorted(self.items - done):
                done.add(ident)
                self._item(ident)
        return self.out / "index.html"

    def _item(self, ident: str) -> None:
        kind, key = ident.split(":", 1)
        body = {"R": self._run, "A": self._version, "N": self._note}[kind](key)
        if body is None:
            return
        (self.out / "items" / f"{kind}_{key}.html").write_text(_page(ident, body, depth=1))

    def _run(self, key: str) -> str | None:
        r = self.conn.execute("SELECT * FROM runs WHERE id = ?", (key,)).fetchone()
        if r is None:
            return None
        status = f"exit {r['exit']}" if r["exit"] is not None else r["status"]
        parts = [
            f"<h1>Run</h1><pre>$ {html.escape(r['cmd'] or '')}</pre>",
            f'<p class="muted">R:{key} · {self._local(r["ts_start"])} → '
            f"{self._local(r['ts_end'])} · {status} · {html.escape(r['host'] or '')}"
            f" · cwd <code>{html.escape(self._rel(r['cwd'] or ''))}</code></p>",
        ]
        if r["git_commit"]:
            dirty = " (uncommitted changes)" if r["git_dirty"] else ""
            parts.append(
                f"<p>git {html.escape(r['git_branch'] or '')}@"
                f"<code>{r['git_commit'][:12]}</code>{dirty}</p>"
            )
        if r["env_name"]:
            parts.append(f"<p>environment <code>{html.escape(r['env_name'])}</code></p>")
        for role in ("used", "generated"):
            files = self.conn.execute(
                "SELECT a.path, v.id, e.method, e.confidence FROM edges e"
                " JOIN artifact_versions v ON v.id = e.version_id JOIN artifacts a"
                " ON a.id = v.artifact_id WHERE e.run_id = ? AND e.role = ? ORDER BY a.path",
                (key, role),
            ).fetchall()
            if files:
                parts.append(
                    f"<h2>{role}</h2><ul>"
                    + "".join(
                        f"<li>{self._link('A:' + v, self._rel(p))} "
                        f'<span class="muted">{m} {c:.1f}</span></li>'
                        for p, v, m, c in files
                    )
                    + "</ul>"
                )
        notes = self.conn.execute("SELECT id, text FROM notes WHERE run_id = ?", (key,)).fetchall()
        if notes:
            parts.append(
                "<h2>notes</h2><ul>"
                + "".join(
                    f"<li>{html.escape(t)} {self._link('N:' + i, 'note')}</li>" for i, t in notes
                )
                + "</ul>"
            )
        return "".join(parts)

    def _version(self, key: str) -> str | None:
        n = node(self.conn, key)
        if n is None:
            return None
        parts = [
            f"<h1>{html.escape(self._rel(n.path))}</h1>",
            f'<p class="muted">A:{key} · sha256 <code>{n.sha[:16]}</code> · first seen '
            f"{self._local(n.ts)}</p>",
        ]
        obj = object_path(self.objects, n.sha)
        if obj.is_file():
            if is_raster(n.path):
                ext = Path(n.path).suffix.lower()
                shutil.copyfile(obj, self.out / "files" / f"{n.sha}{ext}")
                parts.append(f'<img src="../files/{n.sha}{ext}" alt="{html.escape(n.path)}">')
            else:
                data = obj.read_bytes()[:TEXT_LIMIT]
                if b"\x00" not in data:
                    parts.append(
                        f"<pre>{html.escape(data.decode('utf-8', errors='replace'))}</pre>"
                    )
        t = trace(self.conn, key)
        parts.append("<h2>provenance</h2>" + self._tree(t, key, set()))
        return "".join(parts)

    def _tree(self, t, vid: str, seen: set[str]) -> str:
        label = self._link("A:" + vid, self._rel(t.nodes[vid].path)) if vid in t.nodes else vid
        step = t.producer_of(vid)
        if step is None:
            return f'<ul class="tree"><li>{label} <span class="muted">raw input</span></li></ul>'
        run = self._link("R:" + step.run["id"], step.run["cmd"] or "run")
        inner = ""
        if step.run["id"] not in seen:
            seen.add(step.run["id"])
            inner = "".join(self._tree(t, i, seen) for i in step.inputs if i in t.nodes)
        return (
            f'<ul class="tree"><li>{label}<ul class="tree"><li><code>{run}</code>'
            f"{inner}</li></ul></li></ul>"
        )

    def _note(self, key: str) -> str | None:
        r = self.conn.execute("SELECT * FROM notes WHERE id = ?", (key,)).fetchone()
        if r is None:
            return None
        about = f"<p>About {self._link('R:' + r['run_id'], 'this run')}.</p>" if r["run_id"] else ""
        return (
            f"<h1>Note</h1><blockquote>{html.escape(r['text'] or '')}</blockquote>"
            f'<p class="muted">{r["source"]} · {self._local(r["ts"])}</p>{about}'
        )
