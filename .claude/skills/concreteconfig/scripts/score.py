#!/usr/bin/env python3
"""Measure candidate concrete configs for a module with the statistics they control: runs paramtrack's steps 2 and 4 on
them (`--only`: their copies, and configkeys on them), one per config source the candidate sets, then `stats.py
--config-only --run` on each config source (configkeys/stats/STATISTICS.md, "Statistics the concrete config controls"),
and prints the numbers the skill optimizes, per config source. The config sources are scored together: a value set in
two of the module's config sources is ambiguous in both (stats.py counts the run's other config sources' concrete keys).
Each source's full tables are in <out>/<source>/config_stats.md, each value's ambiguity, literal and signal columns in
<out>/<source>/config_values.csv, and each in-scope full key's columns (in_config, searchable_kind, transformed, target,
searched, kind, section, ...) in <out>/<source>/config_full_keys.csv: its rows with in_config false and target true are
the target keys not set yet.

Usage (from anywhere):
  uv run --with pandas --with pyyaml --with matplotlib --with tabulate --with numpy python score.py \\
    --baseline <baseline run dir> --module <module> --config <source>=<candidate> [--config <source>=<candidate> ...] \\
    --out <dir>
The module's step 1 (its graph) is built by the first call and kept in the baseline (baseline.graph_cache: about 3
minutes for Grafana, seconds for the others); later calls reuse it. <out> is also paramtrack's --out for these steps
(their directories, and bin/). Exits 2 if the baseline doesn't fit (baseline.py) or a step fails.
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
        print(f"exit {p.returncode}: {' '.join(cmd)}\n" + "".join(open(log).readlines()[-20:]), file=sys.stderr)
        sys.exit(2)


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
    ap.add_argument("--baseline", required=True, help="the baseline run's paramtrack --out directory")
    ap.add_argument("--module", required=True, help="the skill's module (its modules/ directory name)")
    ap.add_argument("--config", required=True, action="append", metavar="CONFIG_SOURCE=PATH",
                    help="a candidate config for a config source of the module (module.py's CONFIG_SOURCES); once per "
                         "config source it sets")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out, base = os.path.abspath(a.out), os.path.abspath(a.baseline)
    try:
        mod, name = baseline.module(base, a.module)
        configs = baseline.parse_configs(a.config, mod)
    except baseline.MissingInput as e:
        print(f"score.py: {e}", file=sys.stderr)
        sys.exit(2)
    os.makedirs(out, exist_ok=True)
    # A reused <out> may have roots an earlier call set and this one doesn't: stats.py would count their values
    for root in mod.CONFIG_SOURCES:
        if root not in configs and os.path.exists(f"{out}/{name}/{root}"):
            shutil.rmtree(f"{out}/{name}/{root}")
    steps = ["full-keys", "configkeys"]
    if not baseline.copy_graph(base, name, out):
        steps.insert(0, "unmarshaler-graph")
    cmd = [sys.executable, f"{QUERYGRAPH}/paramtrack/paramtrack.py", "--out", out, "--module", name]
    cmd += [x for root, path in sorted(configs.items()) for x in ("--config", f"{root}={path}")]
    cmd += [x for s in steps for x in ("--only", s)]
    run(cmd, f"{out}/paramtrack.log", cwd=f"{QUERYGRAPH}/paramtrack", timeout=1800)
    baseline.keep_graph(base, name, out)
    for root in sorted(configs):
        # stats.py takes the rest from the root's run in <out>: step 2's settings, step 4's configkeys (and, for
        # ambiguity, the other roots' step 4)
        rout = f"{out}/{root}"
        os.makedirs(rout, exist_ok=True)
        run([sys.executable, f"{QUERYGRAPH}/configkeys/stats/stats.py", "--config-only", "--run", f"{out}/{name}/{root}",
             "--out", rout], f"{rout}/stats.log", timeout=900)
        print(f"\n======== {name} config source {root}: {configs[root]}")
        md = open(f"{rout}/config_stats.md").read()
        print(section(md, r"^Summary", r"^- |^## "))  # Q1's coverage table (in scope)
        print(section(md, r"^- In sections with a concrete key", r"^## "))  # how much of each touched section is set
        print(section(md, r"^Ambiguity:", r"^How similar"))
        print("\n".join(line[:300] for line in md.splitlines() if line.startswith("- Searched values")))
        print(section(md, r"^What a match on a value tells us", r"^- "))
        rows = list(csv.DictReader(open(f"{rout}/config_full_keys.csv")))
        unset = sorted({r["key"] for r in rows if r["in_config"] == "False" and r["target"] == "True"})
        print(f"\ntarget keys not set (in scope, searchable kind, not stored transformed): {len(unset)}")
        print(f"full tables: {rout}/config_stats.md; per value: {rout}/config_values.csv; per full key: "
              f"{rout}/config_full_keys.csv")
    unconfigured = [r for r in baseline.roots(mod) if r not in configs]
    if unconfigured:
        print(f"\nconfig sources of {name} not scored (no --config): {', '.join(unconfigured)}")

if __name__ == "__main__":
    main()
