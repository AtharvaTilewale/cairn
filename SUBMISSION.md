# Submission text

Paste-ready copy for the hackathon form. Fill the `<…>` placeholders first; every
number below comes from `eval/heldout/RESULTS.md` or the README results table.

## Name and tagline

**Cairn** — the lab notebook that writes itself.

## One-line description

Cairn records what a computational researcher runs, lets them say why with a
keystroke, and uses Muse Spark to write a cited, fact-checked lab notebook entry and
to prove any figure can be regenerated.

## Description (≈200 words)

Computational researchers lose the *why* behind their runs: which parameter file made
a figure, which script version, why a cutoff changed last Tuesday. Cairn answers from
a record instead of memory.

An opt-in shell hook and a file watcher record every command, exit code, git commit,
environment and file version (with SHA-256 hashes and stored copies), including
long GROMACS jobs running in the background. A spoken or typed note, transcribed by
`muse-voice-transcribe-1.0`, is pinned to the run it explains.

Each day, `muse-spark-1.3` turns the raw log and the day's figures into a notebook
entry in which every sentence cites the run, file or note it rests on; uncited claims
are deleted before the entry is shown, and a second pass checks each claim against
the record in code. `cairn trace` returns the exact chain that produced any file,
and a Muse Spark reproduce agent plans a re-run, waits for approval, replays the
recorded commands in a scratch copy and verifies the output byte for byte. For
unpublished work, the same pipeline runs privately on Muse Glimmer on the author's
own GPU.

## How we used Meta's models

- **muse-spark-1.3**: daily entries from logs and figures (vision, strict JSON
  schema); splitting each entry into checkable claims; readset (reads each script
  version to find the files it opens); the reproduce agent (tool calling: plan,
  approval, replay, verify); ask (tool calling over the record, cited answers); the
  weekly supervisor report.
- **muse-voice-transcribe-1.0**: push-to-talk voice notes from a keyboard shortcut.
- **Muse Glimmer (open weights)**: private mode on our own hardware. Muse Glimmer
  30B (Q4_K_M GGUF) under llama.cpp on a lab workstation's 8 GB GPU, 20
  layers on the GPU and the rest in RAM: 2.2 tokens/s generation, 270 tokens/s prompt
  processing. It wrote a complete cited notebook entry end to end (6.8K tokens in,
  6.6K out, 50 minutes), so private entries run overnight with `cairn entry --queue`.

The models never have the last word on facts: citations are validated against the
record, faithfulness is verified in code, and reproduction verdicts come from
comparing files.

## Technical depth

- Provenance graph (W3C PROV in miniature) with confidence-scored edges from five
  sources: command-line paths, run time windows, exact before/after diffs, files a
  background job holds open (via /proc), and model-read scripts.
- Content-addressed object store; fast fingerprints for 50 GB trajectories in
  milliseconds; git state reconstructed from the reflog at run time.
- An agent that can only replay recorded commands, only after approval, refuses jobs
  over 10 minutes, and never decides its own verdict.
- Evaluation harness with an answer key, plus a held-out scenario never tuned on,
  recorded from its first blind run: capture 22/22 runs, provenance edges precision
  and recall 1.0, entry claims 25 supported / 0 unsupported, 3 of 4 files reproduced
  byte-identically.
- 358 tests; a ~1.3 ms per-command shell hook that works under `set -u`, restores the
  user's prompt hooks exactly and never edits dotfiles.

## Theme: Build for Your World

Built by an MSc computational biology student for the work in their own
dissertation (molecular dynamics and bioinformatics across a desktop, a shared GPU
workstation and a cluster), and designed so a supervisor's shared account is never
modified.

## Bonus prizes

- **Best Agentic Use-Case**: the reproduce agent (plan → human approval → replay in
  scratch → deterministic verification; it found inputs a script names only in its
  code and restored the versions that existed at run time).
- **Best Build on Meta Open Weight Models**: private mode on Muse Glimmer,
  self-hosted with llama.cpp on a shared lab workstation (8 GB GPU, partial offload,
  2.2 tokens/s), verified end to end: a full cited entry with nothing leaving our
  machines. Two llama.cpp behaviours shaped the client: the JSON-schema grammar stalled
  on a real entry prompt (so the schema goes in the prompt and the reply is made to fit
  in code), and a server without a vision projector rejects images (so the entry
  falls back to text at once).

## Links

- Video: `<link>`
- Repository: https://github.com/AtharvaTilewale/cairn

## X post draft (Most Viral)

> I kept forgetting why I ran things in my MSc, so I built Cairn: a lab notebook
> that writes itself. It records what I run, I say why with one key, and Meta's Muse
> Spark writes a cited, fact-checked entry and proves figures regenerate.
> `<video link>` #MetaHackathon
