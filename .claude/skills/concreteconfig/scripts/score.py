#!/usr/bin/env python3
"""Measure a candidate concrete config with the statistics it controls: runs paramtrack's steps 2 and 4 on it (`--only`:
its copy, and configkeys on it), with the baseline run's graph as step 1 and its settings (module, root, format,
docs, ignore file, transformed-keys file: its 2-full-keys/settings.json), then `stats.py --config-only --run`
(configkeys/stats/STATISTICS.md, "Statistics the concrete config controls"), and prints the numbers the skill
optimizes. The full tables are in <out>/config_stats.md, each value's ambiguity, literal and signal columns in
<out>/config_values.csv, and each in-scope full key's columns (in_config, searchable_kind, transformed, target,
searched, kind, section, ...) in <out>/config_full_keys.csv: its rows with in_config false and target true are the
target keys not set yet.

Usage (from anywhere):
  uv run --with pandas --with matplotlib --with tabulate python score.py --config <candidate.yaml> \\
    --baseline <baseline run dir> --out <dir>
<out> is also paramtrack's --out for those steps (their directories, and bin/). Exits 2 if the baseline run lacks a
file it needs (e.g. it predates paramtrack recording its settings).
"""

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys

import baseline  # (next to this file)

CT = "/home/emily/projects/config_tracing"
QUERYGRAPH = f"{CT}/golang.org-x-tools/gopls/internal/cmd/conftamer/querygraph"


def run(cmd, log, cwd=None, timeout=600):
    with open(log, "w") as f:
        p = subprocess.run(cmd, cwd=cwd, stdout=f, stderr=subprocess.STDOUT, timeout=timeout)
    if p.returncode != 0:
        sys.exit(f"exit {p.returncode}: {' '.join(cmd)}\n" + "".join(open(log).readlines()[-20:]))


def section(md, heading_re, until_re=r"^## "):
    """Lines of md from the line matching heading_re up to the next line matching until_re"""
    out, on = [], False
    for line in md.splitlines():
        if on and re.match(until_re, line):
            break
        if re.match(heading_re, line):
            on = True
        if on:
            out.append(line)
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--baseline", required=True, help="the baseline run's paramtrack --out directory")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out, base = os.path.abspath(a.out), os.path.abspath(a.baseline)
    try:
        base_graph, st = baseline.graph(base), baseline.settings(base)
    except baseline.MissingInput as e:
        print(f"score.py: {e}", file=sys.stderr)
        sys.exit(2)
    os.makedirs(f"{out}/1-unmarshaler-graph", exist_ok=True)
    graph = f"{out}/1-unmarshaler-graph/unmarshaler_subgraph.text"
    shutil.copy(base_graph, graph)
    run([sys.executable, f"{QUERYGRAPH}/paramtrack/paramtrack.py", "--out", out, "--config", os.path.abspath(a.config),
         "--only", "full-keys", "--only", "configkeys"] + baseline.static_args(st),
        f"{out}/paramtrack.log", cwd=f"{QUERYGRAPH}/paramtrack", timeout=900)
    # stats.py takes the rest from the run in <out>: step 2's settings (the baseline's), step 4's configkeys
    run([sys.executable, f"{QUERYGRAPH}/configkeys/stats/stats.py", "--config-only", "--run", out, "--out", out],
        f"{out}/stats.log", timeout=900)

    md = open(f"{out}/config_stats.md").read()
    print(section(md, r"^Summary", r"^- |^## "))  # Q1's coverage table (in scope)
    print(section(md, r"^- In sections with a concrete key", r"^## "))  # how much of each touched section is set
    print(section(md, r"^Ambiguity:", r"^How similar"))
    print("\n".join(line[:300] for line in md.splitlines() if line.startswith("- Searched values")))
    print(section(md, r"^What a match on a value tells us", r"^- "))
    rows = list(csv.DictReader(open(f"{out}/config_full_keys.csv")))
    unset = sorted({r["key"] for r in rows if r["in_config"] == "False" and r["target"] == "True"})
    print(f"\ntarget keys not set (in scope, searchable kind, not stored transformed): {len(unset)}")
    print(f"full tables: {out}/config_stats.md; per value: {out}/config_values.csv; per full key: "
          f"{out}/config_full_keys.csv")


if __name__ == "__main__":
    main()
