# Cairn — the lab notebook that writes itself

Cairn records what you ran in the terminal (commands, exit codes, working
directory, timing), lets you pin a note on *why*, and uses Meta's Muse Spark to
turn a day of raw events into a dated, provenance-linked notebook entry.

Built for the Meta Global AI Developer Hackathon (Indie track, "Build for Your World").

> Status: early. Shell and file capture, git/env snapshots and ingest work; provenance
> linking, notebook entries and the reproduce agent are in progress.

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
background process may run (it is what sync uses on an HPC login node).

## Evaluation

```bash
python -m eval.synthlab         # scripted fake project + answer key; prints capture metrics
```

Commands starting with a space are not recorded when `HISTCONTROL` includes
`ignorespace` (Ubuntu's default). Commands that look like they carry secrets
(`*KEY*=`, `*TOKEN*`, `*PASSWORD*`, `*SECRET*`) are stored as `[redacted]`.

## License

MIT
