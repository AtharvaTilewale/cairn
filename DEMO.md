# Demo plan (3 minutes)

The video is judged on impact, use of Meta's models, whether the product holds up
live, and technical depth. Every step below is a real command; nothing is mocked.
Record on the real dissertation store if it is ready, otherwise on the rehearsal
project (last section). Keep `[cairn]` visible in the prompt throughout.

## Before recording

- [ ] `cairn status` shows several days of runs, notes and at least one figure.
- [ ] Yesterday's entry exists (`cairn entry yesterday`) and `cairn export` has run;
      open `.cairn/site/index.html` in a browser tab.
- [ ] Pick one figure whose chain is 3–5 steps deep and replays in under a minute;
      check `cairn trace <figure>` and `cairn reproduce <figure> --yes` once off camera.
- [ ] Glimmer server reachable (`curl $CAIRN_GLIMMER_BASE_URL/models`) and one Glimmer
      entry already written, so `cairn status` shows a measured tokens/s.
- [ ] Terminal font large, dark theme, 100–110 columns; browser zoom 125%.
- [ ] `export COLUMNS=110`; no secrets in scrollback (`clear` first).

## Script

| time | show | say (roughly) |
|---|---|---|
| 0:00–0:15 | A figure from the dissertation | "Three weeks from now I won't remember which parameters made this plot, or why I changed them. Every computational researcher has this problem." |
| 0:15–0:40 | Terminal: `eval "$(cairn activate)"`, a real command, then the voice-note shortcut and its notification | "Cairn records what I run, opt-in per terminal, no dotfiles touched. When I decide something, I press a key and say why: Meta's voice model transcribes it and pins it to the run." |
| 0:40–1:20 | Browser: the exported entry. Scroll the phases, hover footnotes, open the figure result | "Overnight, Muse Spark writes the notebook entry. It reads the figures, not just the logs. Every sentence cites the run or file it rests on, and anything uncited is deleted. Then a second pass checks each claim against the record: 29 supported, 0 unsupported." |
| 1:20–1:45 | `cairn trace figs/<figure>.png` | "Where did this figure come from? The exact chain: which script, which version of the parameter file, which input data, back to raw files." |
| 1:45–2:25 | `cairn reproduce figs/<figure>.png`: the plan, approve with `y`, the replay, `identical` | "Can I trust it? The reproduce agent reads the trace and the scripts and proposes a plan. Nothing runs until I approve. It replays the recorded command in a scratch copy with the exact inputs, and the verdict comes from comparing bytes, not from the model." |
| 2:25–2:40 | `cairn ask "why did I change the cutoff?"` | "And I can just ask, with citations." |
| 2:40–2:52 | `cairn status` model-usage table (Glimmer tokens/s), `cairn report week` | "For unpublished work it runs privately on Muse Glimmer on our own GPU. And on Friday it writes my supervisor update." |
| 2:52–3:00 | README results table | "Cairn: the lab notebook that writes itself." |

## Fallbacks

- If `cairn reproduce` is slow live, show a saved run: the report in
  `.cairn/reproductions/` plus its terminal recording.
- If the network drops, everything recorded so far still works offline (`trace`,
  `show`, `status`, `export`); only entries, ask and reproduce need the model.

## Rehearsal project (synthetic)

A complete synthetic notebook with an entry, a weekly report and reproductions:

```bash
python -m eval.synthlab --readset --entry --reproduce --keep ~/cairn-demo
cd ~/cairn-demo/proj
export CAIRN_HOST=synthlab
cairn report week && cairn export
cairn trace figs/fit2.png
cairn reproduce results/summary.txt
cairn ask "When did I switch to the poly model, and what did the first poly fit give?"
```

The scripts run with whatever `python` is on PATH, so activate the environment that
has numpy and matplotlib first.
