# Cairn — the lab notebook that writes itself

Cairn records what you ran in the terminal (commands, exit codes, working
directory, timing), lets you pin a note on *why*, and uses Meta's Muse Spark to
turn a day of raw events into a dated, provenance-linked notebook entry.

Built for the Meta Global AI Developer Hackathon (Indie track, "Build for Your World").

> Status: early. Shell capture and ingest work; everything else is in progress.

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
# ... work as usual ...
cairn ingest && cairn status
cairn deactivate                # restores your PROMPT_COMMAND, DEBUG trap and prompt
```

Commands starting with a space are not recorded when `HISTCONTROL` includes
`ignorespace` (Ubuntu's default). Commands that look like they carry secrets
(`*KEY*=`, `*TOKEN*`, `*PASSWORD*`, `*SECRET*`) are stored as `[redacted]`.

## License

MIT
