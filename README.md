# Cairn — the lab notebook that writes itself

Computational researchers lose the *why* behind their runs. Which parameter file made
that figure? Which version of the script? Why was the cutoff changed on Tuesday?
Cairn answers from a record instead of memory:

- **It records what you ran**: every command in an opted-in terminal (exit code, timing,
  directory, conda env, git commit), every changed file (SHA-256, with old versions
  kept), and long jobs running in the background.
- **You add the why**: a typed or spoken note, pinned to the run it explains.
- **Meta's models write it up**: Muse Spark turns the day into a dated notebook entry
  where every claim cites the run, file or note it rests on, and it reads your figures.
- **It proves it**: `cairn trace fig.png` gives the exact chain that produced a file;
  `cairn reproduce fig.png` re-runs the cheap final steps in a scratch directory and
  checks that the output matches, byte for byte.

Built for the Meta Global AI Developer Hackathon (Indie track, "Build for Your World")
by an MSc computational biology student, for the kind of work in the author's own
dissertation: GROMACS molecular dynamics, bioinformatics pipelines, many small
analysis scripts across a desktop, a GPU workstation and a cluster.

## How Cairn uses Meta's models

| Model | What it does in Cairn |
|---|---|
| `muse-spark-1.3` (Meta Model API) | Writes the **daily entry** from the day's log and figures (vision), strict JSON schema, every claim cited · **splits the entry into checkable claims** for the faithfulness check · **readset**: reads each new script version and lists the files it opens · **reproduce agent**: tool calling, plans which recorded steps to re-run, executes after approval, verifies · **ask**: tool calling over the store, cited answers · **weekly report** for a supervisor |
| `muse-voice-transcribe-1.0` | **Voice notes**: push-to-talk from a keyboard shortcut, pinned to the last run |
| **Muse Glimmer** (open weights, self-hosted) | **Private mode**: the same entry and check pipeline on your own hardware via llama.cpp, so nothing leaves your machines |

The model never has the last word on facts. Entry claims must cite IDs that exist in
the record (anything else is deleted before you see it), the faithfulness check is
verified by code against the store, and reproduction verdicts come from comparing
files, not from the model.

## How it works

```
 bash hook (opt-in) ─┐                      ┌─> cairn entry   (Muse Spark / Glimmer)
 cairn watch ────────┼─> events/*.jsonl ─> ingest ─> SQLite: runs · file versions ·
 cairn run [--bg] ───┤   (append-only,        edges (used / generated) · notes
 cairn note ─────────┘    per machine)          │       └─> cairn ask · report week
 other machines ── cairn sync (rsync/SSH) ──────┘
                                    cairn trace <file> ──> cairn reproduce <file>
```

Provenance is W3C PROV in miniature: runs are activities, file versions are entities,
and each edge records how it was inferred (command-line paths, the time window, the
exact before/after diff of `cairn run`, files a background job held open, or readset)
with a confidence.

## Results

Measured on synthetic projects only; no real research data is used for evaluation.
`synthlab` is a scripted project with an answer key, used during development; the
**held-out** scenario was written afterwards and is never tuned on
([`eval/heldout/RESULTS.md`](eval/heldout/RESULTS.md) records every run, starting
from the first blind one).

| | synthlab | held-out |
|---|---|---|
| Commands captured (recall / precision) | 27/27 (1.0 / 1.0) | 22/22 (1.0 / 1.0) |
| Exit codes, working directories | all correct | all correct |
| Seeded faults flagged; secrets or hidden commands leaked | 4/4; none | 4/4; none |
| Notes pinned to the right run | 2/2 | 1/2 blind, 2/2 after the fix |
| Provenance edges, precision / recall (readset on) | 1.0 / 1.0 | 1.0 / 1.0 |
| Entry claims supported / unsupported / unverifiable | 29 / 0 / 4 | 25 / 0 / 2 |
| Uncited items dropped from the entry | 0 | 0 |
| Files reproduced by the agent | 4/4 | 3/4 |

Costs on the Meta Model API: a daily entry about $0.035 plus $0.043 for its
faithfulness check; a reproduction $0.02–0.04; a question $0.02; a weekly report
$0.015; readset about $0.002 per script version; voice $0.18 per audio hour. The shell
hook adds about 1.3 ms per recorded command.

## Privacy and safety

- **Opt-in, per terminal.** Nothing is recorded until you run `eval "$(cairn activate)"`;
  the prompt shows `[cairn]` while it is on. Cairn never edits `~/.bashrc` or any
  dotfile, which makes it safe on a shared account.
- **Secrets.** Commands that look like they carry credentials (`*KEY*=`, `*TOKEN*`,
  `*PASSWORD*`, `*SECRET*`) are stored as `[redacted]`; with `HISTCONTROL` including
  `ignorespace` (Ubuntu's default), a command typed with a leading space is not
  recorded at all. API keys come from the environment or `.env` and
  are never logged.
- **What leaves your machine.** Entries send the day's commands, small text outputs
  and figures to the Meta Model API; readset sends script source (turn it off with
  `readset = false` under `[link]` in `.cairn/config.toml`). Private mode sends
  nothing outside your own hardware. The contributor tier, which lets Meta train on
  inputs, is refused by the client.
- **Reproduce is not a security sandbox.** It replays only recorded commands, only
  after you approve the plan, only steps that originally took under 10 minutes, in a
  scratch directory with API keys removed and paths redirected away from your real
  files; but the commands still run as you.

## Install

Python 3.11+ on Linux with bash (5.x; written for 4.2+).

```bash
git clone https://github.com/AtharvaTilewale/cairn && cd cairn
pip install -e ".[dev]"          # add ,voice for voice notes (needs libportaudio2)
cp .env.example .env             # put your MODEL_API_KEY in .env
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
ssh = "workstation"         # an SSH target or ~/.ssh/config Host alias
home = "~/.cairn-alice"   # CAIRN_HOME on that machine
```

```bash
cairn sync workstation       # pull events, stored files and job logs, then ingest
cairn sync workstation --dry-run
```

Sync only reads from the spoke (no deletes, nothing written there). On a shared
account, keep everything under `~/.cairn-<user>`. If the system Python is older than
3.11, Cairn can live in a private conda prefix with its own package cache (the
account's own conda envs are untouched):

```bash
git clone https://github.com/AtharvaTilewale/cairn ~/.cairn-alice/src/cairn
export CONDA_PKGS_DIRS=~/.cairn-alice/pkgs
conda create -y -p ~/.cairn-alice/env python=3.12 pip
~/.cairn-alice/env/bin/pip install -e ~/.cairn-alice/src/cairn
```

Then, in each terminal there:

```bash
export CAIRN_USER=alice CAIRN_HOME=~/.cairn-alice CAIRN_HOST=workstation
export PATH=~/.cairn-alice/env/bin:$PATH
eval "$(cairn activate)"
cairn watch --root ~/project     # in tmux: fingerprints that project's files
```

Nothing touches the account's dotfiles. A spoke store has no database; the hub
ingests what sync pulls. Files from a spoke are traced on the hub with `--host`:

```bash
cairn trace --host workstation md/rmsd.xvg     # a unique path suffix is enough
cairn reproduce --host workstation results/summary.txt
```

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
`reasoning_strength` for effort. For Glimmer, Cairn puts the JSON schema in the
prompt and makes the reply fit it in code, instead of sending a grammar: llama.cpp's
schema sampler stalled on a real entry prompt in our tests.

These are the commands that ran on the workstation (shared account: everything stays
inside `~/.cairn-alice`; if the system cmake is too old, a newer one comes from pip):

```bash
cd ~/.cairn-alice
~/.cairn-alice/env/bin/pip install cmake ninja
export PATH=~/.cairn-alice/env/bin:/usr/local/cuda/bin:$PATH
git clone --depth 1 https://github.com/ggml-org/llama.cpp && cd llama.cpp  # >= b10353
cmake -B build -G Ninja -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=86 \
    -DLLAMA_CURL=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build build -j 24 --target llama-server
cd ~/.cairn-alice/models && curl -L -C - -O \
  https://huggingface.co/meta-models/Muse-Glimmer-30B-GGUF/resolve/main/Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf
cd ~/.cairn-alice && nohup llama.cpp/build/bin/llama-server \
    -m models/Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf \
    -a muse-glimmer -ngl 20 -t 16 -c 32768 -np 1 \
    --host 127.0.0.1 --port 8080 --jinja --temp 1.0 --top-p 0.95 --top-k 64 \
    > glimmer.log 2>&1 & echo $! > glimmer.pid
```

With an 8 GB GPU, `-ngl 20` puts 20 layers on it (~7 GB) and the rest runs
from RAM on 16 threads. Measured: prompt processing ~270 tokens/s, generation
**2.2–2.3 tokens/s** (1.9 with `-ngl 12`), so Glimmer entries are an overnight job
(`--queue`). Keep `-np 1`: llama-server splits `-c` across slots, and a starved slot
returns nothing. Without the 1.4 GB `mmproj` file the server rejects images and Cairn
writes the entry from text (add `--mmproj` for figures). Stop it with
`kill $(cat ~/.cairn-alice/glimmer.pid)` so the GPU is free for other work.

From the hub, reach it through SSH and point Cairn at it:

```bash
ssh -f -N -L 8080:127.0.0.1:8080 workstation
cairn entry yesterday --backend glimmer --no-check   # default URL http://127.0.0.1:8080/v1
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

## Limitations

- Linux and bash only. zsh, fish and Windows are not supported.
- Re-runs use today's interpreter and packages, not the recorded environment; a
  changed package shows up as a `different` comparison rather than an explicit
  environment error.
- Git commit and branch are reconstructed from the reflog for runs on the machine
  that ingests them; runs synced from other machines carry no git snapshot yet.
- SVG and PDF figures are not shown to the model (only raster formats).
- The realtime voice WebSocket is not used (its handshake is undocumented); voice
  notes go through the file endpoint, which suits push-to-talk.
- HPC (SGE) job capture is designed but not built.

## License

MIT
