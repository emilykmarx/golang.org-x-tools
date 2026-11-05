#!/usr/bin/env python3
"""Measure a candidate concrete config with the statistics it controls: runs configkeys on it (as paramtrack's step
3 does), then `stats.py --config-only --ignore` (configkeys/stats/STATISTICS.md, "Statistics the concrete config
controls"), and prints the numbers the skill optimizes. The full tables are in <out>/config_stats.md, and each
value's ambiguity, literal and signal columns in <out>/config_values.csv.

Usage (from anywhere):
  uv run --with pandas --with matplotlib --with tabulate python score.py --config <candidate.yaml> \\
    --graph <unmarshaler_subgraph.text> --ignore <module's ignore.txt> --out <dir> \\
    [--module github.com/prometheus/prometheus] [--module-dir <checkout>] [--root /config.Config] [--docs <keys>]
The graph is paramtrack's step 1 output (1-unmarshaler-graph/unmarshaler_subgraph.text); the defaults are
Prometheus'.
"""

import argparse
import os
import re
import subprocess
import sys

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
    ap.add_argument("--graph", required=True)
    ap.add_argument("--ignore", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--module", default="github.com/prometheus/prometheus")
    ap.add_argument("--module-dir", default=f"{CT}/prometheus")
    ap.add_argument("--root", default="/config.Config")
    ap.add_argument("--docs", default=f"{CT}/prom-test-infra/prombench/prometheus_config_keys.txt")
    a = ap.parse_args()
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)

    bin = f"{out}/configkeys"
    run(["go", "build", "-o", bin, "."], f"{out}/configkeys.build.log", cwd=f"{QUERYGRAPH}/configkeys")
    cmd = [bin, "--config", os.path.abspath(a.config), "--txt-dir", out, "--strings-only", "--unmarshaler-subgraph",
           os.path.abspath(a.graph), "--module-prefix", a.module, "--output", f"{out}/configkeys.csv"]
    if a.root:
        cmd += ["--root", a.root]
    run(cmd, f"{out}/configkeys.log")
    run([sys.executable, f"{QUERYGRAPH}/configkeys/stats/stats.py", "--config-only", "--ignore",
         os.path.abspath(a.ignore), "--configkeys", f"{out}/configkeys.csv", "--param-values",
         f"{out}/param_values.txt", "--graph", os.path.abspath(a.graph), "--docs", a.docs, "--prometheus",
         a.module_dir, "--out", out], f"{out}/stats.log", timeout=900)

    md = open(f"{out}/config_stats.md").read()
    print(section(md, r"^Summary:", r"^- |^## "))  # Q1's coverage tables, with and without service discovery
    print(section(md, r"^- In sections with a concrete key", r"^## "))  # how much of each touched section is set
    print(section(md, r"^Ambiguity:", r"^How similar"))
    print("\n".join(line[:300] for line in md.splitlines() if line.startswith("- Searched values")))
    print(section(md, r"^What a match on a value tells us", r"^- "))
    print(f"\nfull tables: {out}/config_stats.md; per value: {out}/config_values.csv")


if __name__ == "__main__":
    main()
