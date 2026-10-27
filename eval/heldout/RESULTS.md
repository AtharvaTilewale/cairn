# Held-out results

The held-out scenario (`scenario.py`, a small sequencing pipeline) was written on
2026-10-27 after Cairn's prompts and heuristics had been tuned on synthlab. Its
results are recorded here as they come, including the first, blind run.

## Run 1 (blind), 2026-10-27, offline (no model calls)

`python -m eval.synthlab --scenario heldout`

| metric | value |
|---|---|
| runs recorded (recall / precision) | 22/22 (1.0 / 1.0) |
| exit codes, working directories | 22/22, 22/22 |
| seeded faults flagged (rerun, bare subshell, 2 wrapped runs) | 4/4 |
| leaks (hidden command, token value) | none |
| generated files captured / ignored paths leaked | 8/8 / none |
| notes captured / placed on the right run | 2/2 / **1/2** |
| edge precision / recall | 1.0 / 0.958 |

Misses: the two inputs `make_report.py` names only in its code (`out/filtered.tsv`,
`config.yaml`); readset was off in this run, so this is the expected gap.

**Bug found:** the second note was pinned to the background aligner instead of
`make_report.py`. The `--bg` monitor recorded its start 19 ms after `make_report.py`
started, and notes were pinned to the latest *started* run. Fixed in the next commit
(a note now explains the last run that *finished* before it). Later runs are no
longer blind for note placement; every other metric still is, as nothing else was
changed in response to this run.

## Run 2, 2026-10-27, with models (readset, entry, reproduce)

`python -m eval.synthlab --scenario heldout --readset --entry --reproduce`, after the
note fix above. Blind for every prompt (entry, claims, readset, reproduce).

| metric | value |
|---|---|
| runs, exit codes, cwds, fault flags, leaks | 22/22, 22/22, 22/22, 4/4, none |
| notes placed | 2/2 |
| edge precision / recall (readset found both code-only inputs) | 1.0 / 1.0 |
| entry: claims supported / unsupported / unverifiable, dropped | 25 / 0 / 2, 0 |
| entry: parameter changes reported, failed runs cited | 2/2, 2/2 |
| reproduce: targets verified | **3/4** |
| model cost, wall time | $0.20, 151 s |

The miss: for `out/report.md` the agent spent its 12 planning calls reading files
and never proposed a plan (verdict inconclusive). Re-running it alone the agent
planned the single `make_report.py` step and reproduced `report.md` byte-identically,
so this is run-to-run variance in a long plan. Two loop fixes followed (not prompt
changes): the last planning turn now offers only `propose_plan`, and `propose_plan`
accepts a malformed `steps` argument instead of losing the plan. Also found: replay
scratch directories fell back to the project directory when `TMPDIR` was unset; they
now live in `.cairn/scratch/` (never recorded) and are deleted afterwards.
