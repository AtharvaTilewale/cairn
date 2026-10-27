# Cairn — the lab notebook that writes itself

Cairn records what you ran in the terminal (commands, exit codes, working
directory, timing), lets you pin a note on *why*, and uses Meta's Muse Spark to
turn a day of raw events into a dated, provenance-linked notebook entry.

Built for the Meta Global AI Developer Hackathon (Indie track, "Build for Your World").

> Status: capture, snapshots, provenance linking, `cairn run` and daily entries work;
> trace, notes, the weekly report and the reproduce agent are in progress.

## Install

```bash
pip install -e ".[dev]"
```

## Quick start

```bash
cd my-project
cairn init                      # creates .cairn/ here; touches no dotfiles
export CAIRN_USER=$USER
eval "$(cairn activate)"        # capture is opt-in, per terminal; prompt shows [cairn]
cairn watch                     # in a spare terminal: fingerprints files as they change
# ... work as usual, one command per prompt ...
cairn ingest && cairn status
cairn deactivate                # restores your PROMPT_COMMAND, DEBUG trap and prompt
```

What gets recorded:

- **Commands:** text, exit code, start/end time, working directory, conda env or venv,
  plus the git commit and branch at the time (from the reflog) when you ingest.
- **Files:** SHA-256 of every changed file under the project; files up to 50 MB are
  also copied to `.cairn/objects/` so old versions can be restored. Files over 50 MB
  (trajectories) get a fast fingerprint and are never copied. `.cairnignore` uses
  gitignore syntax.
- Identical reruns are recorded even with `HISTCONTROL=ignoredups`.

`cairn scan [--since TIME]` is the one-shot version of `watch` for machines where no
background process may run.

For exact provenance, wrap a command:

```bash
cairn run -- python fit.py data/obs.csv --out results/fit.json
cairn run --bg -- gmx mdrun -deffnm md      # detached; survives closing the terminal
```

`cairn run` snapshots the project before and after, so the files it wrote are known
exactly. `--bg` also tracks which files the job holds open, so a long simulation's
outputs are never confused with what you do in the same directory meanwhile.

## Provenance

`cairn ingest` links every run to the artefact versions it used and generated:
paths named on the command line, files changed while the run was active, the exact
outputs of `cairn run`, and (**readset**) the files a script names in its own code,
which Muse Spark reads once per script version. Readset sends the script's source to
the Meta Model API; set `readset = false` under `[link]` in `.cairn/config.toml` (or
`CAIRN_READSET=0`) to keep scripts local. Model calls and their cost are logged to
`.cairn/usage.jsonl`.

## Notebook entries

```bash
cairn entry today            # or yesterday, or 2026-10-28
cairn entry today --dry-run  # print exactly what would be sent; call nothing
cairn show R:01J...          # any run, file version (A:) or note (N:) an entry cites
```

Muse Spark writes the day's entry from the recorded log and the day's figures (it
reads the plots). Every claim must cite the run, file version or note it rests on;
anything uncited, or citing an ID that is not in the log, is dropped before the entry
is shown. A second pass splits the entry into atomic claims and checks each against
the store (exit codes, commands, file names, parameter values, file contents), so each
entry comes with a faithfulness score. Entries are saved to `.cairn/entries/`.

## Evaluation

```bash
python -m eval.synthlab              # scripted fake project + answer key; prints metrics
python -m eval.synthlab --readset    # also lets linking call Muse Spark (about $0.01)
python -m eval.synthlab --readset --entry   # and writes + checks the entry (about $0.09)
```

Commands starting with a space are not recorded when `HISTCONTROL` includes
`ignorespace` (Ubuntu's default). Commands that look like they carry secrets
(`*KEY*=`, `*TOKEN*`, `*PASSWORD*`, `*SECRET*`) are stored as `[redacted]`.

## License

MIT
