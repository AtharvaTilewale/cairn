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
