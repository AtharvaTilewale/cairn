"""Citation validation and the faithfulness check (CLAUDE.md 9.2, hard rule 5).

``validate_entry`` runs on every generated entry before it is rendered: a claim or
narrative sentence survives only if it cites at least one ID and every ID it cites
was in the day's context. Everything dropped is reported.

``check_faithfulness`` asks Muse Spark to split the rendered entry into atomic claims
with machine-checkable assertions, then verifies each assertion against the store in
plain code: exit codes, command fragments, file names and parameter values in the
cited file versions. Each claim is ``supported``, ``unsupported`` or ``unverifiable``.
"""

import copy
import re
import sqlite3
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from cairnlab.llm.schemas import CLAIMS
from cairnlab.provenance.drift import parse
from cairnlab.provenance.objects import object_path

ID = r"[RAN]:[0-9A-HJKMNP-TV-Z]{26}"
CITE = re.compile(rf"\[({ID})\]")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[`\"'])")


def _sentences(text: str) -> list[str]:
    """Split prose into sentences, keeping trailing citations with their sentence."""
    pieces = [p for p in _SENTENCE_END.split(text.strip()) if p.strip()]
    out: list[str] = []
    for p in pieces:
        lead = re.match(rf"^((?:\s*\[{ID}\])+)\s*", p)
        if lead and out:  # "... 2.4 ns. [R:..] Next sentence" -> citation belongs before
            out[-1] += " " + lead.group(1).strip()
            p = p[lead.end() :]
        if p.strip():
            out.append(p.strip())
    return out


@dataclass
class Dropped:
    where: str
    text: str
    reason: str


def _check_ids(ids: list[str], valid: set[str]) -> str | None:
    """Why a citation list is unacceptable, or None if it is fine."""
    if not ids:
        return "cites nothing"
    bad = [i for i in ids if i not in valid]
    if bad:
        return f"cites IDs not in the log: {', '.join(bad)}"
    return None


def _clean_prose(text: str, valid: set[str], where: str, dropped: list[Dropped]) -> str:
    """Keep only sentences that cite valid IDs."""
    kept = []
    for i, s in enumerate(_sentences(text)):
        reason = _check_ids(CITE.findall(s), valid)
        if reason:
            dropped.append(Dropped(f"{where}.sentence[{i}]", s, reason))
        else:
            kept.append(s)
    return " ".join(kept)


def _strip_bad_citations(text: str, valid: set[str]) -> str:
    return CITE.sub(lambda m: m.group(0) if m.group(1) in valid else "", text).strip()


def validate_entry(raw: dict, valid: set[str]) -> tuple[dict, list[Dropped]]:
    """Drop every claim, sentence, result and decision without valid evidence."""
    entry = copy.deepcopy(raw)
    dropped: list[Dropped] = []
    for p_i, phase in enumerate(entry.get("phases", [])):
        where = f"phases[{p_i}]"
        phase["narrative"] = _clean_prose(
            phase.get("narrative", ""), valid, f"{where}.narrative", dropped
        )
        kept = []
        for c_i, claim in enumerate(phase.get("claims", [])):
            reason = _check_ids(claim.get("evidence", []), valid)
            if reason:
                dropped.append(Dropped(f"{where}.claims[{c_i}]", claim.get("text", ""), reason))
            else:
                kept.append(claim)
        phase["claims"] = kept
    entry["phases"] = [p for p in entry.get("phases", []) if p["claims"] or p["narrative"]]

    for key, id_field in (("results", "artifact"), ("decisions", "note")):
        kept = []
        for i, item in enumerate(entry.get(key, [])):
            ident = item.get(id_field, "")
            prefix = "A:" if key == "results" else "N:"
            if ident in valid and ident.startswith(prefix):
                kept.append(item)
            else:
                dropped.append(
                    Dropped(
                        f"{key}[{i}]",
                        item.get("text") or item.get("description", ""),
                        f"{id_field} {ident!r} is not a {prefix} ID in the log",
                    )
                )
        entry[key] = kept

    kept = []
    for i, d in enumerate(entry.get("drift", [])):
        reason = _check_ids(d.get("evidence", []), valid)
        if reason:
            dropped.append(Dropped(f"drift[{i}]", d.get("text", ""), reason))
        else:
            kept.append(d)
    entry["drift"] = kept

    goal = entry.get("goal", "")
    cited = CITE.findall(goal)
    if cited and _check_ids(cited, valid):
        dropped.append(Dropped("goal", goal, _check_ids(cited, valid) or ""))
        entry["goal"] = "Not recorded."
    for key in ("open_questions", "next_steps"):
        entry[key] = [_strip_bad_citations(t, valid) for t in entry.get(key, [])]
    return entry, dropped


# --- faithfulness ---------------------------------------------------------------------


@dataclass
class ClaimVerdict:
    text: str
    evidence: list[str]
    verdict: str  # supported | unsupported | unverifiable
    checks: list[dict] = field(default_factory=list)  # each with "ok": True/False/None


_VERSION_SHA = (
    "SELECT v.sha256 FROM artifact_versions v JOIN artifacts a ON a.id = v.artifact_id"
    " WHERE v.id = ? AND (a.path = ? OR a.path LIKE ?)"
)
_RUN_SHA = (
    "SELECT v.sha256 FROM edges e JOIN artifact_versions v ON v.id = e.version_id"
    " JOIN artifacts a ON a.id = v.artifact_id WHERE e.run_id = ? AND (a.path = ? OR a.path LIKE ?)"
)


def _param_matches(params: dict[str, str], key: str, value: str) -> bool:
    """``key`` (exact, or the unique nested key ending in ``.key``) has ``value``."""
    if key not in params:
        nested = [k for k in params if k.endswith("." + key)]
        if len(nested) != 1:
            return False
        key = nested[0]
    return _num_equal(params[key], value)


def _num_equal(a: str, b: str) -> bool:
    try:
        return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)))
    except ValueError:
        return a.strip().strip("'\"").lower() == b.strip().strip("'\"").lower()


class Verifier:
    """Checks claim assertions against the store."""

    def __init__(self, conn: sqlite3.Connection, objects_dir: Path):
        self.conn = conn
        self.objects_dir = objects_dir

    def _run(self, ident: str):
        rid = ident.removeprefix("R:")
        return self.conn.execute("SELECT * FROM runs WHERE id = ?", (rid,)).fetchone()

    def _runs_for(self, check: dict, evidence: list[str]):
        ids = [check["run"]] if check.get("run") else [e for e in evidence if e.startswith("R:")]
        return [r for r in (self._run(i) for i in ids) if r is not None]

    def check(self, check: dict, evidence: list[str]) -> bool | None:
        """True/False for a checkable assertion; None when it cannot be checked."""
        kind = check.get("kind")
        value = (check.get("value") or "").strip()
        if kind == "exit_code":
            runs = self._runs_for(check, evidence)
            if not runs or not value.lstrip("-").isdigit():
                return None
            return any(r["exit"] == int(value) for r in runs)
        if kind == "command_contains":
            runs = self._runs_for(check, evidence)
            if not runs or not value:
                return None
            norm = " ".join(value.split())
            return any(norm in " ".join((r["cmd"] or "").split()) for r in runs)
        if kind == "file_named":
            path = (check.get("path") or "").strip().lstrip("./")
            if not path:
                return None
            row = self.conn.execute(
                "SELECT 1 FROM artifacts WHERE path = ? OR path LIKE ? LIMIT 1",
                (path, "%/" + path),
            ).fetchone()
            return row is not None
        if kind == "param_value":
            return self._param(check, evidence)
        if kind == "content_contains":
            texts = self._texts(check, evidence)
            if not texts or not value:
                return None
            norm = " ".join(value.split())
            return any(norm in " ".join(t.split()) for t in texts)
        return None

    def _texts(self, check: dict, evidence: list[str]) -> list[str]:
        """Content of the cited versions of ``check["path"]`` (else those its runs touched)."""
        path = (check.get("path") or "").strip().lstrip("./")
        if not path:
            return []
        like = "%/" + path
        shas = []
        for ident in evidence:
            if ident.startswith("A:"):
                shas += [r[0] for r in self.conn.execute(_VERSION_SHA, (ident[2:], path, like))]
        if not shas:
            for run in self._runs_for(check, evidence):
                shas += [r[0] for r in self.conn.execute(_RUN_SHA, (run["id"], path, like))]
        texts = []
        for sha in shas:
            obj = object_path(self.objects_dir, sha)
            if obj.is_file():
                texts.append(obj.read_bytes().decode("utf-8", errors="replace"))
        return texts

    def _param(self, check: dict, evidence: list[str]) -> bool | None:
        path = (check.get("path") or "").strip().lstrip("./")
        key, value = (check.get("key") or "").strip(), (check.get("value") or "").strip()
        if not path or not key or not value:
            return None
        texts = self._texts(check, evidence)
        if not texts:
            return None
        return any(_param_matches(parse(path, t), key, value) for t in texts)

    def verdict(self, claim: dict) -> ClaimVerdict:
        checks = []
        for c in claim.get("checks", []):
            checks.append({**c, "ok": self.check(c, claim.get("evidence", []))})
        results = [c["ok"] for c in checks if c["ok"] is not None]
        if any(r is False for r in results):
            v = "unsupported"
        elif results:
            v = "supported"
        else:
            v = "unverifiable"
        return ClaimVerdict(claim.get("text", ""), claim.get("evidence", []), v, checks)


def _prompt() -> str:
    return resources.files("cairnlab.llm").joinpath("prompts/claims.md").read_text()


def check_faithfulness(
    client, conn: sqlite3.Connection, objects_dir: Path, entry_markdown: str, log_text: str
) -> list[ClaimVerdict]:
    """Split an entry into atomic claims (Muse Spark) and verify each against the store."""
    answer = client.structured(
        purpose="claims",
        messages=[
            {"role": "system", "content": _prompt()},
            {"role": "user", "content": f"Log:\n{log_text}\n\nEntry:\n{entry_markdown}"},
        ],
        schema_name="Claims",
        schema=CLAIMS,
        reasoning_effort="low",
    )
    verifier = Verifier(conn, objects_dir)
    return [verifier.verdict(c) for c in answer.get("claims", [])]


def summarize(verdicts: list[ClaimVerdict]) -> dict:
    """Counts and the faithfulness score: supported / (supported + unsupported)."""
    n = {
        v: sum(1 for c in verdicts if c.verdict == v)
        for v in ("supported", "unsupported", "unverifiable")
    }
    checked = n["supported"] + n["unsupported"]
    return {
        **n,
        "total": len(verdicts),
        "faithfulness": round(n["supported"] / checked, 3) if checked else None,
    }
