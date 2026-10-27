"""Compute the evaluation metrics for a synthlab project against its answer key.

Capture: run recall/precision, exit codes, seeded faults, file versions, ignore rules.
Provenance: edge precision/recall (overall, per role, per method). Drift, note
placement, entry faithfulness and reproduce success are added as those features land.
"""

import json
import sqlite3
from pathlib import Path


def _align(expected: list[dict], runs: list[sqlite3.Row]) -> list[tuple[dict, sqlite3.Row]]:
    """Match expected steps to recorded runs by command text.

    In order where possible; a step whose run was recorded out of order (a background
    job's monitor can start after the next command) falls back to any unmatched run.
    """
    pairs, used, j = [], set(), 0
    for step in expected:
        order = list(range(j, len(runs))) + list(range(0, j))
        for k in order:
            if k not in used and runs[k]["cmd"] == step["cmd"]:
                pairs.append((step, runs[k]))
                used.add(k)
                if k >= j:
                    j = k + 1
                break
    return pairs


def compute(project: Path) -> dict:
    """Metrics for the synthlab project in ``project`` (answer_key.json + .cairn)."""
    key = json.loads((project / "answer_key.json").read_text())
    conn = sqlite3.connect(project / ".cairn" / "cairn.db")
    conn.row_factory = sqlite3.Row
    runs = conn.execute(
        "SELECT r.*, e.payload FROM runs r JOIN events e ON e.id = r.id ORDER BY r.ts_start, r.id"
    ).fetchall()
    expected = [s for s in key["steps"] if s["record"]]
    pairs = _align(expected, runs)
    matched_ids = {r["id"] for _, r in pairs}

    exit_ok = sum(1 for s, r in pairs if r["exit"] == s["exit"])
    cwd_ok = sum(1 for s, r in pairs if Path(r["cwd"]).resolve() == (project / s["cwd"]).resolve())
    flags_expected = flags_found = 0
    for s, r in pairs:
        payload = json.loads(r["payload"])
        for flag in s["flags"]:
            flags_expected += 1
            flags_found += bool(payload.get(flag))

    dump = "\n".join(row[0] for row in conn.execute("SELECT payload FROM events")) + "\n".join(
        str(tuple(r)) for r in runs
    )
    leaks = [m for m in key["must_not_appear"] if m in dump]

    root = str(project.resolve())
    versions = {
        row["path"]: row["n"]
        for row in conn.execute(
            "SELECT a.path, COUNT(v.id) AS n FROM artifacts a"
            " LEFT JOIN artifact_versions v ON v.artifact_id = a.id GROUP BY a.id"
        )
    }
    rel = {p.removeprefix(root + "/"): n for p, n in versions.items()}
    generated = sorted({g for s in key["steps"] for g in s["generated"]})
    files_found = [g for g in generated if g in rel]
    ignored_leaks = [p for p in key["ignored_paths"] if p in rel]
    stored = conn.execute(
        "SELECT SUM(stored), COUNT(*) FROM artifact_versions WHERE fp = 'full'"
    ).fetchone()

    edge_m = _edge_metrics(conn, pairs, root)
    entry_m = _entry_metrics(conn, key, pairs)

    m = {
        "runs_expected": len(expected),
        "runs_recorded": len(runs),
        "run_recall": round(len(pairs) / len(expected), 3) if expected else 1.0,
        "run_precision": round(len(matched_ids) / len(runs), 3) if runs else 1.0,
        "exit_codes_correct": f"{exit_ok}/{len(pairs)}",
        "cwd_correct": f"{cwd_ok}/{len(pairs)}",
        "fault_flags": f"{flags_found}/{flags_expected}",
        "leaks": leaks,
        "generated_files_captured": f"{len(files_found)}/{len(generated)}",
        "ignored_paths_leaked": ignored_leaks,
        "params_versions": rel.get("params.txt", 0),
        "fit_json_versions": rel.get("results/fit.json", 0),
        "small_versions_stored": f"{stored[0] or 0}/{stored[1]}",
        "missing_runs": [s["cmd"] for s in expected if s not in [p for p, _ in pairs]],
        **edge_m,
        **entry_m,
    }
    m["ok"] = (
        m["run_recall"] == 1.0
        and m["run_precision"] == 1.0
        and exit_ok == len(pairs)
        and cwd_ok == len(pairs)
        and flags_found == flags_expected
        and not leaks
        and len(files_found) == len(generated)
        and not ignored_leaks
    )
    return m


def _pr(pred: set, truth: set) -> tuple[float, float]:
    hit = len(pred & truth)
    precision = hit / len(pred) if pred else 1.0
    recall = hit / len(truth) if truth else 1.0
    return round(precision, 3), round(recall, 3)


def _edge_metrics(conn: sqlite3.Connection, pairs: list, root: str) -> dict:
    """Edge precision/recall over the runs matched to answer-key steps."""
    truth: set[tuple[int, str, str]] = set()
    pred: set[tuple[int, str, str]] = set()
    by_method: dict[str, set] = {}
    for step, run in pairs:
        truth |= {(step["i"], "used", u) for u in step["used"]}
        truth |= {(step["i"], "generated", g) for g in step["generated"]}
        rows = conn.execute(
            "SELECT e.role, a.path, e.method FROM edges e"
            " JOIN artifact_versions v ON v.id = e.version_id"
            " JOIN artifacts a ON a.id = v.artifact_id WHERE e.run_id = ?",
            (run["id"],),
        )
        for role, path, method in rows:
            edge = (step["i"], role, path.removeprefix(root + "/"))
            pred.add(edge)
            by_method.setdefault(method, set()).add(edge)
    precision, recall = _pr(pred, truth)
    out = {"edge_precision": precision, "edge_recall": recall}
    for role in ("used", "generated"):
        p, r = _pr({e for e in pred if e[1] == role}, {e for e in truth if e[1] == role})
        out[f"{role}_precision_recall"] = f"{p} / {r}"
    out["edges_by_method"] = {
        m: f"{len(es)} (precision {_pr(es, truth)[0]})" for m, es in sorted(by_method.items())
    }
    out["edge_false_positives"] = sorted(pred - truth)[:10]
    out["edge_false_negatives"] = sorted(truth - pred)[:10]
    return out


def _entry_metrics(conn: sqlite3.Connection, key: dict, pairs: list) -> dict:
    """Entry quality, when an entry was written: citations, faithfulness, coverage."""
    row = conn.execute("SELECT json, markdown FROM entries ORDER BY created DESC").fetchone()
    if row is None or not row[1] or not json.loads(row[0]).get("entry"):
        return {}
    payload, markdown = json.loads(row[0]), row[1]
    entry = payload["entry"]
    blob = json.dumps(entry)
    claims = sum(len(p["claims"]) for p in entry.get("phases", [])) + len(entry.get("drift", []))
    run_of = {s["i"]: r["id"] for s, r in pairs}
    failed = [s["i"] for s in key["steps"] if s["record"] and s["exit"] != 0 and s["i"] in run_of]
    cited_failed = [i for i in failed if f"R:{run_of[i]}" in blob]
    low = markdown.lower()
    params = [c for c in key["param_changes"] if c["key"] in low and c["new"] in low]
    figures = conn.execute(
        "SELECT COUNT(DISTINCT a.id) FROM edges e JOIN artifact_versions v"
        " ON v.id = e.version_id JOIN artifacts a ON a.id = v.artifact_id"
        " WHERE e.role = 'generated' AND a.kind = 'figure'"
    ).fetchone()[0]
    faith = payload.get("faithfulness") or {}
    return {
        "entry_claims": claims,
        "entry_dropped": len(payload.get("dropped", [])),
        "entry_faithfulness": faith.get("faithfulness"),
        "entry_claim_verdicts": {
            k: faith.get(k) for k in ("supported", "unsupported", "unverifiable")
        },
        "entry_param_changes_mentioned": f"{len(params)}/{len(key['param_changes'])}",
        "entry_failed_runs_cited": f"{len(cited_failed)}/{len(failed)}",
        "entry_figures_described": f"{len(entry.get('results', []))} items / {figures} figures",
        "entry_unsupported": [
            c["text"] for c in payload.get("claims", []) if c.get("verdict") == "unsupported"
        ][:5],
    }


def render(m: dict) -> str:
    """Plain-text metrics table."""
    width = max(map(len, m))
    return "\n".join(f"{k.ljust(width)}  {v}" for k, v in m.items())
