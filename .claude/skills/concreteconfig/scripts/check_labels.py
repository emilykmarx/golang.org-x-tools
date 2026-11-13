#!/usr/bin/env python3
"""Check constraint 2 (SKILL.md) for a candidate concrete config: run paramtrack up to testbed run 1 (`--until 3`) with
it, reusing the baseline run's graph as step 1, then compare its message labels with the baseline run's
(count_labels.py). Prints count_labels.py's report. Exits 0 if no label is missing, 1 if one is, and 2 if it couldn't
check (the baseline run lacks a file it needs, paramtrack failed, count_labels.py failed, or bad arguments).

Usage (from anywhere; takes as long as one testbed run, about 6 minutes on prombench):
  uv run --with pandas --with matplotlib --with tabulate python check_labels.py --config <candidate.yaml> \\
    --baseline <baseline run dir> --out <dir> [--config-files <dir>] [--list]
The baseline run is paramtrack with the shipped config, through step 3 (SKILL.md, "Inputs"). The candidate's run uses
the baseline run's graph and settings, which paramtrack records: static (module, root, format, docs, ignore file:
2-full-keys/settings.json) and testbed (testbed, --wait, VM, argmatch's build args: 3-chains/testbed.json); a shorter
run, for example, can miss labels for that reason alone. --config-files defaults to the files/ next to --config, as
paramtrack's does. <out> is paramtrack's --out; paramtrack's log is <out>/paramtrack.log.
"""

import argparse
import os
import subprocess
import sys

import baseline  # (next to this file)

QUERYGRAPH = "/home/emily/projects/config_tracing/golang.org-x-tools/gopls/internal/cmd/conftamer/querygraph"
HERE = os.path.dirname(os.path.abspath(__file__))


def fail(msg):
    """Couldn't check: exit 2, which a missing label (1) can't be confused with"""
    print(f"check_labels.py: {msg}", file=sys.stderr)
    sys.exit(2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--baseline", required=True, help="the baseline run's paramtrack --out directory")
    ap.add_argument("--out", required=True)
    ap.add_argument("--config-files")
    ap.add_argument("--list", action="store_true", help="count_labels.py --list: print every label, both runs'")
    a = ap.parse_args()
    out, base = os.path.abspath(a.out), os.path.abspath(a.baseline)
    base_events = f"{base}/3-chains/events.jsonl"
    if not os.path.exists(base_events):
        fail(f"no {base_events}: the baseline run must be paramtrack through step 3 (3-chains)")
    # The baseline run's graph (reused, so paramtrack doesn't rebuild it), and its static and testbed settings, which
    # the candidate's run must match
    try:
        baseline.graph(base)  # (checks the baseline's graph is usable)
        args = baseline.static_args(baseline.settings(base)) + baseline.testbed_args(base)
    except baseline.MissingInput as e:
        fail(str(e))
    baseline.copy_graph(base, out)
    cmd = [sys.executable, f"{QUERYGRAPH}/paramtrack/paramtrack.py", "--out", out, "--config",
           os.path.abspath(a.config), "--until", "3", "--redo", "2"] + args
    if a.config_files:
        cmd += ["--config-files", os.path.abspath(a.config_files)]
    with open(f"{out}/paramtrack.log", "w") as log:
        p = subprocess.run(cmd, cwd=f"{QUERYGRAPH}/paramtrack", stdout=log, stderr=subprocess.STDOUT)
    if p.returncode != 0:
        fail(f"paramtrack exited {p.returncode}; see {out}/paramtrack.log")
    code = subprocess.run([sys.executable, f"{HERE}/count_labels.py", f"{out}/3-chains/events.jsonl",
                           "--baseline", base_events] + (["--list"] if a.list else [])).returncode
    sys.exit(code if code in (0, 1) else 2)


if __name__ == "__main__":
    main()
