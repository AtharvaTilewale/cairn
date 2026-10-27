# Cairn — the lab notebook that writes itself

Cairn records what you ran in the terminal (commands, exit codes, working
directory, timing), lets you pin a note on *why*, and uses Meta's Muse Spark to
turn a day of raw events into a dated, provenance-linked notebook entry.

Built for the Meta Global AI Developer Hackathon (Indie track, "Build for Your World").

> Status: capture, snapshots, provenance, `cairn run`, notes (typed and voice), trace,
> multi-machine sync, daily entries, ask, the reproduce agent, the weekly report and
> private mode (Muse Glimmer) work; Glimmer has not yet been run on real weights.

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

## Notes: the *why*

```bash
cairn note "cutoff 1.0 truncates the tail; trying 1.2"   # pinned to your last command
cairn note --run R:01J... "this is the run I meant"
cairn note                                               # speak instead (push-to-talk)
```

A note attaches to the last command run in the same terminal (or the last one on the
machine within an hour). Voice notes record from the default microphone until you
press Enter or run `cairn note` again, then are transcribed by
`muse-voice-transcribe-1.0`; the audio stays in `.cairn/notes/`. They need
`pip install -e ".[voice]"` and PortAudio (`sudo apt install libportaudio2`).

To take voice notes from anywhere, add a GNOME custom shortcut (Settings → Keyboard →
Custom Shortcuts) whose command is:

```bash
env CAIRN_USER=alice CAIRN_HOME=/path/to/project/.cairn /path/to/.venv/bin/cairn note
```

Press it once to start recording and again to stop; a desktop notification shows
the transcript.

## Trace: where did this file come from?

```bash
cairn trace figs/fit2.png            # the chain of runs and inputs, back to raw files
cairn trace params.txt --at 2026-10-28T09:00:00Z   # an older version
cairn trace figs/fit2.png --json     # machine-readable
```

It ends with the exact command that made the file, the git commit and environment it
ran under, and whether each input on disk still matches what was used.

## Several machines

The HP is the hub; other machines (spokes) record into `~/.cairn-<user>` and are
pulled over SSH. Define them in `~/.config/cairn/hosts.toml`:

```toml
[workstation]
ssh = "labuser"              # any SSH target; ~/.ssh/config does the rest
home = "~/.cairn-alice"   # CAIRN_HOME on that machine
```

```bash
cairn sync workstation       # pull events, stored files and job logs, then ingest
cairn sync workstation --dry-run
```

Sync only reads from the spoke (no deletes, nothing written there). On a shared
account, keep everything under `~/.cairn-<user>`: install Cairn into a venv there
(`python3 -m venv ~/.cairn-alice/venv`), then in each terminal
`export CAIRN_USER=alice CAIRN_HOME=~/.cairn-alice CAIRN_HOST=workstation` and
`eval "$(~/.cairn-alice/venv/bin/cairn activate)"`. Nothing touches the account's
dotfiles. Run `cairn watch --root <project>` there to fingerprint files too.

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

## Reproduce: can this file be regenerated?

```bash
cairn reproduce figs/fit2.png        # shows a plan, asks, then re-runs and checks
cairn reproduce results/summary.txt --yes
```

Muse Spark reads the trace and the scripts, then proposes which recorded steps to
re-run (normally just the cheap final one). Nothing runs until you approve the plan.
Each step is replayed in a fresh scratch directory with the exact input versions
restored from `.cairn/objects`, and its outputs are compared with the recorded ones:
`identical`, `pixel_equal` (same image, different metadata), `data_equal`, `close`
(numbers within `--rel-tol`), or `different`. The verdict comes from those
comparisons, not from the model. Only recorded commands can be replayed, only runs
that took under 10 minutes and whose inputs were stored (no MD, no docking), and API
keys are removed from the environment.

**The scratch directory is not a security sandbox**: replayed commands run as you,
with your permissions. Paths into the project are redirected to the scratch copy so
your real files are not overwritten, but treat `cairn reproduce` like re-running the
command yourself.

## Ask

```bash
cairn ask "When did I switch to the poly model, and what did the first poly fit give?"
cairn ask "Which runs failed yesterday and why?"
```

Muse Spark searches your runs, files, notes and entries with tools and answers with
citations; any cited ID that is not in the store is removed.

## Private mode: Muse Glimmer on your own hardware

Everything that calls Muse Spark can instead use **Muse Glimmer**, Meta's open-weight
model, on a machine you control, so nothing leaves it:

```bash
cairn entry yesterday --backend glimmer
cairn entry --queue --backend glimmer     # every recent day with runs but no entry
```

Glimmer runs behind any OpenAI-compatible server (llama.cpp, Ollama, vLLM, Modal).
Cairn follows the model card: temperature 1.0 / top-p 0.95 / top-k 64, and
`reasoning_strength` for effort. On the workstation (shared account: everything stays
inside `~/.cairn-alice`, including the Hugging Face cache):

```bash
cd ~/.cairn-alice
git clone https://github.com/ggml-org/llama.cpp && cd llama.cpp   # needs build >= b10353
cmake -B build -DBUILD_SHARED_LIBS=OFF -DGGML_CUDA=ON
cmake --build build --config Release -j --target llama-server
export HF_HOME=~/.cairn-alice/hf
~/.cairn-alice/venv/bin/pip install huggingface_hub
~/.cairn-alice/venv/bin/hf download meta-models/Muse-Glimmer-30B-GGUF \
    --local-dir ~/.cairn-alice/models/glimmer \
    --include "Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf" \
    --include "mmproj-Muse-Glimmer-30B-Q4_K_M.gguf"          # ~18 GB
./build/bin/llama-server \
    -m ~/.cairn-alice/models/glimmer/Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf \
    --mmproj ~/.cairn-alice/models/glimmer/mmproj-Muse-Glimmer-30B-Q4_K_M.gguf \
    -a muse-glimmer -ngl 12 -t 16 -c 131072 -np 1 \
    --host 127.0.0.1 --port 8080 --jinja --temp 1.0 --top-p 0.95 --top-k 64
```

The 8 GB GPU has 8 GB, so `-ngl` offloads only some layers (raise it until
`nvidia-smi` shows ~7 GB used); the rest runs from RAM on 16 threads. Keep `-np 1`:
llama-server splits `-c` across slots, and a starved slot returns nothing. From the
hub, reach it through SSH and point Cairn at it:

```bash
ssh -N -L 8080:127.0.0.1:8080 labuser &
export CAIRN_GLIMMER_BASE_URL=http://127.0.0.1:8080/v1
cairn entry yesterday --backend glimmer --no-check
cairn status                 # "Model usage" shows Glimmer's measured tokens/s
```

## Weekly report

```bash
cairn report week                    # the last seven days, from the daily entries
cairn report week --start 2026-10-26
```

A plain-text update for your supervisor, under 300 words: what moved, what is
blocked, decisions and why, next week. Saved to `.cairn/reports/`.

## Evaluation

```bash
python -m eval.synthlab              # scripted fake project + answer key; prints metrics
python -m eval.synthlab --readset    # also lets linking call Muse Spark (about $0.01)
python -m eval.synthlab --readset --entry   # and writes + checks the entry (about $0.09)
python -m eval.synthlab --readset --reproduce   # and reproduces 4 outputs (about $0.15)
python -m eval.synthlab --scenario heldout      # the held-out scenario (never tuned on)
```

Commands starting with a space are not recorded when `HISTCONTROL` includes
`ignorespace` (Ubuntu's default). Commands that look like they carry secrets
(`*KEY*=`, `*TOKEN*`, `*PASSWORD*`, `*SECRET*`) are stored as `[redacted]`.

Results so far (synthetic data only): see `eval/heldout/RESULTS.md` for the held-out
scenario, recorded from its first blind run.

## License

MIT
