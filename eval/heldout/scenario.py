"""Held-out scenario: a small sequencing pipeline, written after the prompts were tuned.

Never tune prompts, thresholds or heuristics on this file's results; it exists to show
how Cairn does on work it was not developed against. Synthetic data only.

Pipeline: simulate reads -> count k-mers (k from config.yaml) -> filter by min_count
-> histogram; a report script that names its inputs only in its code; a background
"aligner" that holds its log open; parameter edits, reruns, a hidden command, a token
export and a failure.
"""

from eval.synthlab import Scenario, Step

SIMULATE_READS = '''\
"""Write random DNA reads as FASTA."""
import argparse, random

p = argparse.ArgumentParser()
p.add_argument("--n", type=int, required=True)
p.add_argument("--length", type=int, required=True)
p.add_argument("--seed", type=int, default=0)
p.add_argument("--out", required=True)
a = p.parse_args()
rng = random.Random(a.seed)
with open(a.out, "w") as fh:
    for i in range(a.n):
        fh.write(f">read{i}\\n{''.join(rng.choice('ACGT') for _ in range(a.length))}\\n")
'''

CONFIG_READER = """
def read_config(path):
    cfg = {}
    for line in open(path):
        if ":" in line and not line.lstrip().startswith("#"):
            key, value = line.split(":", 1)
            cfg[key.strip()] = value.strip()
    return cfg
"""

KMER_COUNT = (
    '''\
"""Count k-mers in FASTA reads; k comes from the config file."""
import argparse
from collections import Counter
'''
    + CONFIG_READER
    + """
p = argparse.ArgumentParser()
p.add_argument("reads")
p.add_argument("--config", required=True)
p.add_argument("--out", required=True)
a = p.parse_args()
k = int(read_config(a.config)["k"])
counts = Counter()
for line in open(a.reads):
    if not line.startswith(">"):
        seq = line.strip()
        counts.update(seq[i:i + k] for i in range(len(seq) - k + 1))
with open(a.out, "w") as fh:
    fh.write("kmer\\tcount\\n")
    for kmer, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        fh.write(f"{kmer}\\t{n}\\n")
"""
)

FILTER_KMERS = (
    '''\
"""Keep k-mers seen at least min_count times (from the config file)."""
import argparse, sys
from pathlib import Path
'''
    + CONFIG_READER
    + """
p = argparse.ArgumentParser()
p.add_argument("counts")
p.add_argument("--config", required=True)
p.add_argument("--out", required=True)
a = p.parse_args()
if not Path(a.counts).is_file():
    sys.exit(f"filter_kmers.py: {a.counts}: no such file")
min_count = int(read_config(a.config)["min_count"])
rows = [line.rstrip("\\n").split("\\t") for line in open(a.counts)][1:]
kept = [(kmer, n) for kmer, n in rows if int(n) >= min_count]
with open(a.out, "w") as fh:
    fh.write("kmer\\tcount\\n")
    fh.writelines(f"{kmer}\\t{n}\\n" for kmer, n in kept)
print(f"kept {len(kept)} of {len(rows)} k-mers")
"""
)

HIST = '''\
"""Histogram of k-mer counts."""
import argparse
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

p = argparse.ArgumentParser()
p.add_argument("counts")
p.add_argument("--out", required=True)
a = p.parse_args()
values = [int(line.split("\\t")[1]) for line in list(open(a.counts))[1:]]
fig, ax = plt.subplots(figsize=(5, 3), dpi=80)
ax.hist(values, bins=range(1, max(values) + 2), color="tab:green")
ax.set_xlabel("times a k-mer was seen")
ax.set_ylabel("number of k-mers")
ax.set_yscale("log")
fig.savefig(a.out, metadata={"Software": None})
'''

ALIGN_SIM = '''\
"""A stand-in for a slow aligner: holds its log open and appends progress lines."""
import argparse, time

p = argparse.ArgumentParser()
p.add_argument("--steps", type=int, default=10)
p.add_argument("--out", required=True)
a = p.parse_args()
with open(a.out, "w") as log:
    for i in range(a.steps):
        log.write(f"batch {i}: aligned {1000 * (i + 1)} reads, mapq30 {0.91 - i * 0.002:.3f}\\n")
        log.flush()
        time.sleep(0.15)
'''

MAKE_REPORT = (
    '''\
"""Summarise the filtered k-mers (input paths are fixed in the code)."""
'''
    + CONFIG_READER
    + """
cfg = read_config("config.yaml")
rows = [line.rstrip("\\n").split("\\t") for line in open("out/filtered.tsv")][1:]
with open("out/report.md", "w") as fh:
    fh.write(f"# k-mer report\\n\\nk = {cfg['k']}, min_count = {cfg['min_count']}\\n")
    fh.write(f"k-mers kept: {len(rows)}\\n\\nTop 3:\\n")
    for kmer, n in rows[:3]:
        fh.write(f"- {kmer}: {n}\\n")
"""
)

CONFIG = "k: 5\nmin_count: 3\n"

COUNT = "python kmer_count.py out/reads.fa --config config.yaml --out out/counts.tsv"
COUNT_USES = ["kmer_count.py", "out/reads.fa", "config.yaml"]
FILTER = "python filter_kmers.py out/counts.tsv --config config.yaml --out out/filtered.tsv"
FILTER_USES = ["filter_kmers.py", "out/counts.tsv", "config.yaml"]

STEPS = [
    Step("mkdir -p out"),
    Step(
        "python simulate_reads.py --n 300 --length 60 --seed 7 --out out/reads.fa",
        used=["simulate_reads.py"],
        generated=["out/reads.fa"],
    ),
    Step("head -2 out/reads.fa", used=["out/reads.fa"]),
    Step(COUNT, used=COUNT_USES, generated=["out/counts.tsv"]),
    Step(FILTER, used=FILTER_USES, generated=["out/filtered.tsv"]),
    Step(
        "python hist.py out/counts.tsv --out out/hist.png",
        used=["hist.py", "out/counts.tsv"],
        generated=["out/hist.png"],
    ),
    Step(
        'cairn note "k=5 gives too many singletons; trying k=7"',
        record=False,
        note="k=5 gives too many singletons; trying k=7",
    ),
    Step(
        "sed -i 's/^k: 5$/k: 7/' config.yaml",
        used=["config.yaml"],
        generated=["config.yaml"],
        param=("k", "5", "7"),
    ),
    Step(COUNT, used=COUNT_USES, generated=["out/counts.tsv"]),
    Step(COUNT, used=COUNT_USES, generated=["out/counts.tsv"], flags={"hist_repeat": True}),
    Step(" echo do-not-record-heldout", record=False),
    Step("export SEQ_SERVICE_TOKEN=heldout-fake-token", cmd="[redacted]"),
    Step(
        "python filter_kmers.py out/missing.tsv --config config.yaml --out out/bad.tsv",
        exit=1,
        used=["filter_kmers.py", "config.yaml"],
    ),
    Step("ls out", record=False),
    Step("cd out", record=False),
    Step("wc -l counts.tsv", cwd="out", used=["out/counts.tsv"]),
    Step("cd ..", record=False),
    Step(FILTER, used=FILTER_USES, generated=["out/filtered.tsv"]),
    Step(
        "cairn run -- python hist.py out/counts.tsv --out out/hist_k7.png",
        cmd="python hist.py out/counts.tsv --out out/hist_k7.png",
        used=["hist.py", "out/counts.tsv"],
        generated=["out/hist_k7.png"],
        flags={"wrapper": True},
    ),
    Step(
        "cairn run --bg -- python align_sim.py --steps 12 --out out/align.log",
        cmd="python align_sim.py --steps 12 --out out/align.log",
        used=["align_sim.py"],
        generated=["out/align.log"],
        flags={"wrapper": True},
    ),
    Step(
        "python make_report.py",
        used=["make_report.py", "out/filtered.tsv", "config.yaml"],
        generated=["out/report.md"],
    ),
    Step(
        'cairn note "keeping k=7; min_count 3 still looks right"',
        record=False,
        note="keeping k=7; min_count 3 still looks right",
    ),
    Step(
        "sed -i 's/^min_count: 3$/min_count: 2/' config.yaml",
        used=["config.yaml"],
        generated=["config.yaml"],
        param=("min_count", "3", "2"),
    ),
    Step(FILTER, used=FILTER_USES, generated=["out/filtered.tsv"]),
    Step("python -c \"open('tmp.swp', 'w').write('x')\""),
    Step(
        "(cd out && wc -l filtered.tsv report.md)",
        used=["out/filtered.tsv", "out/report.md"],
        flags={"ts_start_approx": True},
    ),
    Step("false", exit=1),
    Step("rm tmp.swp"),
]

HELDOUT = Scenario(
    name="heldout",
    files={
        "simulate_reads.py": SIMULATE_READS,
        "kmer_count.py": KMER_COUNT,
        "filter_kmers.py": FILTER_KMERS,
        "hist.py": HIST,
        "align_sim.py": ALIGN_SIM,
        "make_report.py": MAKE_REPORT,
        "config.yaml": CONFIG,
    },
    steps=STEPS,
    param_file="config.yaml",
    must_not_appear=["do-not-record-heldout", "heldout-fake-token"],
    ignored_paths=["tmp.swp"],
    reproduce_targets=["out/hist_k7.png", "out/report.md", "out/align.log", "out/filtered.tsv"],
)
