"""synthlab: a scripted fake research project with a known answer key.

It builds a small numpy/matplotlib project in a temp directory, starts ``cairn watch``,
plays ~30 shell steps through the real bash hook (generate data, fit, plot, edit a
parameter file, re-run, notes, and seeded faults), stops the watcher and ingests.
``answer_key.json`` records what really happened: which steps must be recorded and
with what exit code, the files each step used and generated, the parameter changes,
which run each note explains, and what must never appear in the store.

Run it with ``python -m eval.synthlab`` (``--keep DIR`` keeps the project).
Synthetic data only; finishes in well under two minutes.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

GEN_PY = '''\
"""Generate noisy damped-decay observations."""
import argparse, csv, sys
import numpy as np

p = argparse.ArgumentParser()
p.add_argument("--n", type=int, default=200)
p.add_argument("--seed", type=int, default=0)
p.add_argument("--out", required=True)
a = p.parse_args()
rng = np.random.default_rng(a.seed)
x = np.linspace(0, 10, a.n)
y = 3.0 * np.exp(-x / 2.5) + rng.normal(0, 0.05, a.n)
with open(a.out, "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["x", "y"])
    w.writerows(zip(np.round(x, 6), np.round(y, 6)))
'''

FIT_PY = '''\
"""Fit the observations with the model named in the parameter file."""
import argparse, json, sys
from pathlib import Path
import numpy as np

p = argparse.ArgumentParser()
p.add_argument("data")
p.add_argument("--params", required=True)
p.add_argument("--out", required=True)
a = p.parse_args()
if not Path(a.data).is_file():
    sys.exit(f"fit.py: no such data file: {a.data}")
params = dict(
    (k.strip(), v.strip())
    for k, v in (line.split("=", 1) for line in open(a.params) if "=" in line)
)
x, y = np.loadtxt(a.data, delimiter=",", skiprows=1, unpack=True)
cut = float(params["cutoff"])
keep = x <= cut * 5
if params["model"] == "exp":
    slope, icpt = np.polyfit(x[keep], np.log(np.clip(y[keep], 1e-6, None)), 1)
    coef = {"amplitude": round(float(np.exp(icpt)), 4), "tau": round(float(-1 / slope), 4)}
else:
    coef = {f"c{i}": round(float(c), 4) for i, c in enumerate(np.polyfit(x[keep], y[keep], 3))}
json.dump({"model": params["model"], "cutoff": cut, "coef": coef}, open(a.out, "w"), indent=1)
'''

PLOT_PY = '''\
"""Plot observations and the fitted curve."""
import argparse, json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

p = argparse.ArgumentParser()
p.add_argument("data")
p.add_argument("fit")
p.add_argument("--out", required=True)
a = p.parse_args()
x, y = np.loadtxt(a.data, delimiter=",", skiprows=1, unpack=True)
fit = json.load(open(a.fit))
c = fit["coef"]
if fit["model"] == "exp":
    yf = c["amplitude"] * np.exp(-x / c["tau"])
else:
    yf = np.polyval([c[f"c{i}"] for i in range(len(c))], x)
fig, ax = plt.subplots(figsize=(5, 3), dpi=80)
ax.plot(x, y, ".", ms=2, label="observed")
ax.plot(x, yf, "-", label=f"{fit['model']} fit (cutoff {fit['cutoff']})")
ax.set_xlabel("time (ns)")
ax.set_ylabel("signal")
ax.legend()
fig.savefig(a.out, metadata={"Software": None})
'''

SIMULATE_PY = '''\
"""A stand-in for mdrun: holds its log open and appends a line per step."""
import argparse, time

p = argparse.ArgumentParser()
p.add_argument("--steps", type=int, default=10)
p.add_argument("--out", required=True)
a = p.parse_args()
with open(a.out, "w") as log:
    for step in range(a.steps):
        log.write(f"step {step} energy {-1000.0 - step * 0.5:.2f}\\n")
        log.flush()
        time.sleep(0.15)
'''

SUMMARIZE_PY = '''\
"""Compare the two fits for the report (paths are fixed in the code)."""
import json

first = json.load(open("results/fit.json"))
second = json.load(open("results/fit2.json"))
with open("results/summary.txt", "w") as fh:
    fh.write(f"fit:  {first['model']} cutoff={first['cutoff']} {first['coef']}\\n")
    fh.write(f"fit2: {second['model']} cutoff={second['cutoff']} {second['coef']}\\n")
'''

PARAMS = "model = exp\ncutoff = 1.0\n"


@dataclass
class Step:
    """One line typed at the prompt, and the truth about it."""

    line: str
    record: bool = True  # must appear as a run
    exit: int = 0
    cmd: str | None = None  # recorded text if not ``line`` (e.g. "[redacted]")
    cwd: str = "."
    used: list[str] = field(default_factory=list)
    generated: list[str] = field(default_factory=list)
    flags: dict[str, bool] = field(default_factory=dict)
    note: str | None = None  # text of a `cairn note` (explains the previous run)
    param: tuple[str, str, str] | None = None  # (key, old, new) in params.txt


FIT = "python fit.py data/obs.csv --params params.txt --out results/fit.json"
PLOT = "python plot.py data/obs.csv results/fit.json --out figs/fit.png"
FIT_USES = ["fit.py", "data/obs.csv", "params.txt"]

STEPS = [
    Step("mkdir -p data results figs"),
    Step(
        "python gen.py --n 200 --seed 1 --out data/obs.csv",
        used=["gen.py"],
        generated=["data/obs.csv"],
    ),
    Step("head -3 data/obs.csv", used=["data/obs.csv"]),
    Step(FIT, used=FIT_USES, generated=["results/fit.json"]),
    Step(PLOT, used=["plot.py", "data/obs.csv", "results/fit.json"], generated=["figs/fit.png"]),
    Step(
        'cairn note "first fit undershoots the tail; try a larger cutoff"',
        record=False,
        note="first fit undershoots the tail; try a larger cutoff",
    ),
    Step(
        "sed -i 's/^cutoff = 1.0$/cutoff = 1.2/' params.txt",
        used=["params.txt"],
        generated=["params.txt"],
        param=("cutoff", "1.0", "1.2"),
    ),
    Step(FIT, used=FIT_USES, generated=["results/fit.json"]),
    # Identical rerun: no new history entry under ignoredups, still a real run.
    Step(FIT, used=FIT_USES, generated=["results/fit.json"], flags={"hist_repeat": True}),
    Step(PLOT, used=["plot.py", "data/obs.csv", "results/fit.json"], generated=["figs/fit.png"]),
    # Hidden with a leading space: must never be recorded.
    Step(" echo hidden-step-marker", record=False),
    Step("export SYNTH_API_KEY=synthetic-not-a-real-key", cmd="[redacted]"),
    Step(
        "python fit.py data/missing.csv --params params.txt --out results/bad.json",
        exit=1,
        used=["fit.py", "params.txt"],
    ),
    Step("ls results", record=False),
    Step("cd results", record=False),
    Step("wc -l fit.json", cwd="results", used=["results/fit.json"]),
    Step("cd ..", record=False),
    Step(
        "python gen.py --n 500 --seed 2 --out data/obs2.csv",
        used=["gen.py"],
        generated=["data/obs2.csv"],
    ),
    Step(
        "sed -i 's/^model = exp$/model = poly/' params.txt",
        used=["params.txt"],
        generated=["params.txt"],
        param=("model", "exp", "poly"),
    ),
    Step(
        "python fit.py data/obs2.csv --params params.txt --out results/fit2.json",
        used=["fit.py", "data/obs2.csv", "params.txt"],
        generated=["results/fit2.json"],
    ),
    Step(
        "python plot.py data/obs2.csv results/fit2.json --out figs/fit2.png",
        used=["plot.py", "data/obs2.csv", "results/fit2.json"],
        generated=["figs/fit2.png"],
    ),
    Step("cp figs/fit2.png figs/final.png", used=["figs/fit2.png"], generated=["figs/final.png"]),
    Step("python -c \"open('scratch.tmp', 'w').write('x')\""),
    Step("mkdir -p __pycache__ && echo x > __pycache__/junk.pyc"),
    # A bare subshell never fires the parent's DEBUG trap: start time is approximate.
    Step(
        "(cd results && wc -l fit.json fit2.json)",
        used=["results/fit.json", "results/fit2.json"],
        flags={"ts_start_approx": True},
    ),
    # Wrapped runs record themselves with their exact outputs (the hook skips them).
    Step(
        "cairn run -- python plot.py data/obs2.csv results/fit2.json --out figs/wrapped.png",
        cmd="python plot.py data/obs2.csv results/fit2.json --out figs/wrapped.png",
        used=["plot.py", "data/obs2.csv", "results/fit2.json"],
        generated=["figs/wrapped.png"],
        flags={"wrapper": True},
    ),
    # A long job in the background, still writing its log while the next steps run.
    Step(
        "cairn run --bg -- python simulate.py --steps 14 --out sim.log",
        cmd="python simulate.py --steps 14 --out sim.log",
        used=["simulate.py"],
        generated=["sim.log"],
        flags={"wrapper": True},
    ),
    # Inputs named only inside the script: argpath sees summarize.py, readset the rest.
    Step(
        "python summarize.py",
        used=["summarize.py", "results/fit.json", "results/fit2.json"],
        generated=["results/summary.txt"],
        flags={},
    ),
    Step("sleep 1.2"),
    Step(
        'cairn note "poly model fits the tail better; using fit2 for the report"',
        record=False,
        note="poly model fits the tail better; using fit2 for the report",
    ),
    Step("false", exit=1),
    Step(FIT, used=FIT_USES, generated=["results/fit.json"]),
    Step("rm scratch.tmp"),
]

MUST_NOT_APPEAR = ["hidden-step-marker", "synthetic-not-a-real-key"]
# Files the reproduce agent should regenerate and verify (all deterministic, all cheap).
REPRODUCE_TARGETS = ["figs/fit2.png", "results/summary.txt", "sim.log", "results/fit.json"]
IGNORED_PATHS = ["scratch.tmp", "__pycache__/junk.pyc"]


def answer_key() -> dict:
    """The ground truth for STEPS, independent of what Cairn captured."""
    steps, notes, params = [], [], []
    last_recorded = None
    for i, s in enumerate(STEPS):
        d = asdict(s)
        d["i"] = i
        d["cmd"] = s.cmd or s.line
        steps.append(d)
        if s.note:
            notes.append({"step": i, "text": s.note, "explains_step": last_recorded})
        if s.param:
            key, old, new = s.param
            params.append({"step": i, "file": "params.txt", "key": key, "old": old, "new": new})
        if s.record:
            last_recorded = i
    return {
        "version": 0,
        "steps": steps,
        "notes": notes,
        "param_changes": params,
        "must_not_appear": MUST_NOT_APPEAR,
        "reproduce_targets": REPRODUCE_TARGETS,
        "ignored_paths": IGNORED_PATHS,
    }


def _env(home: Path, readset: bool = False) -> dict[str, str]:
    bin_dir = str(Path(sys.executable).parent)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(home),
        "HISTFILE": str(home / ".bash_history"),
        "HISTCONTROL": "ignoreboth",  # Ubuntu's default: exercises the rerun/hidden faults
        "LC_ALL": "C.UTF-8",
        "TERM": "dumb",
        "MPLCONFIGDIR": str(home / "mpl"),
        "CAIRN_USER": "synth",
        "CAIRN_HOST": "synthlab",
        "CAIRN_READSET": "1" if readset else "0",
    }
    if readset:
        # Network settings (proxies, CA bundles) must reach the ingest subprocess.
        for var, value in os.environ.items():
            if var.lower().endswith("_proxy") or var in (
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "REQUESTS_CA_BUNDLE",
            ):
                env[var] = value
        # Synthetic scripts only: this is the one place eval sends code to Muse Spark.
        from cairnlab.llm.client import load_env

        load_env()
        if not os.environ.get("MODEL_API_KEY"):
            raise SystemExit("--readset needs MODEL_API_KEY (export it or put it in .env)")
        env["MODEL_API_KEY"] = os.environ["MODEL_API_KEY"]
    return env


def build(project: Path) -> None:
    """Write the fake project's scripts and parameter file, and initialise Cairn."""
    project.mkdir(parents=True, exist_ok=True)
    (project / "gen.py").write_text(GEN_PY)
    (project / "fit.py").write_text(FIT_PY)
    (project / "plot.py").write_text(PLOT_PY)
    (project / "summarize.py").write_text(SUMMARIZE_PY)
    (project / "simulate.py").write_text(SIMULATE_PY)
    (project / "params.txt").write_text(PARAMS)


def play(
    project: Path,
    home: Path,
    step_delay: float = 0.6,
    readset: bool = False,
    entry: bool = False,
    reproduce: bool = False,
) -> dict:
    """Run every step through the real hook with ``cairn watch`` running; then ingest.

    With ``readset``, ingest asks Muse Spark which files each script reads and writes.
    With ``entry``, Muse Spark then writes and checks the day's notebook entry.
    """
    env = _env(home, readset or entry or reproduce)
    if (entry or reproduce) and not readset:
        env["CAIRN_READSET"] = "0"
    home.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cairn", "init"], cwd=project, env=env, check=True, capture_output=True)
    watcher = subprocess.Popen(
        ["cairn", "watch"], cwd=project, env=env, stderr=subprocess.PIPE, text=True
    )
    try:
        assert watcher.stderr is not None
        for line in watcher.stderr:
            if line.startswith("Watching"):
                break
        # Lines are typed one at a time with a pause, like a person; the pause is not a
        # command, so it never shows up as a run or breaks a consecutive rerun.
        shell = subprocess.Popen(
            ["bash", "--noprofile", "--norc", "--noediting", "-i"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            cwd=project,
            env=env,
            text=True,
        )
        assert shell.stdin is not None
        for line in ['eval "$(cairn activate)"', *(s.line for s in STEPS), "exit"]:
            shell.stdin.write(line + "\n")
            shell.stdin.flush()
            time.sleep(step_delay)
        shell.stdin.close()
        stderr = shell.stderr.read() if shell.stderr else ""
        shell.wait(timeout=120)
        time.sleep(2.5)  # let the background job finish and the watcher settle
    finally:
        watcher.send_signal(signal.SIGTERM)
        watcher.wait(timeout=30)
    ingest = subprocess.run(
        ["cairn", "ingest"], cwd=project, env=env, capture_output=True, text=True, check=True
    )
    key = answer_key()
    (project / "answer_key.json").write_text(json.dumps(key, indent=1))
    info = {"shell_stderr": stderr, "ingest": ingest.stdout.strip()}
    if entry:
        res = subprocess.run(
            ["cairn", "entry", "today"],
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        info["entry"] = (res.stdout + res.stderr).strip()
    if reproduce:
        verdicts = {}
        for target in REPRODUCE_TARGETS:
            res = subprocess.run(
                ["cairn", "reproduce", target, "--yes"],
                cwd=project,
                env=env,
                capture_output=True,
                text=True,
                timeout=900,
            )
            verdicts[target] = {0: "reproduced", 1: "not_reproduced"}.get(
                res.returncode, "refused/inconclusive"
            )
        (project / "reproduce_results.json").write_text(json.dumps(verdicts, indent=1))
        info["reproduce"] = verdicts
    return info


def _cost(project: Path) -> float:
    usage = project / ".cairn" / "usage.jsonl"
    if not usage.is_file():
        return 0.0
    return round(sum(json.loads(x)["cost_usd"] for x in usage.read_text().splitlines()), 4)


def main(argv: list[str] | None = None) -> int:
    from eval.metrics import compute, render

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--keep", type=Path, help="Build the project here and keep it.")
    ap.add_argument(
        "--readset",
        action="store_true",
        help="Let linking call Muse Spark on the synthetic scripts (a few cents).",
    )
    ap.add_argument(
        "--entry",
        action="store_true",
        help="Also write and check the day's entry with Muse Spark (a few cents).",
    )
    ap.add_argument(
        "--reproduce",
        action="store_true",
        help="Also ask the reproduce agent to regenerate and verify key outputs (~$0.15).",
    )
    args = ap.parse_args(argv)
    t0 = time.perf_counter()
    base = args.keep or Path(tempfile.mkdtemp(prefix="synthlab-"))
    if args.keep and base.exists():
        shutil.rmtree(base)
    project, home = base / "proj", base / "home"
    build(project)
    info = play(project, home, readset=args.readset, entry=args.entry, reproduce=args.reproduce)
    metrics = compute(project)
    metrics["readset"] = args.readset
    if args.readset or args.entry:
        metrics["model_cost_usd"] = _cost(project)
    if args.entry:
        print(info.get("entry", ""))
    metrics["seconds"] = round(time.perf_counter() - t0, 1)
    print(info["ingest"])
    print(render(metrics))
    if not args.keep:
        shutil.rmtree(base, ignore_errors=True)
    return 0 if metrics["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
