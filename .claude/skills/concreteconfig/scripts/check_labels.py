#!/usr/bin/env python3
"""Check constraint 2 (SKILL.md) for a candidate concrete config: run paramtrack up to testbed run 1 (`--until 3`) with
it, reusing the baseline run's graph as step 1, then compare its message labels with the baseline run's
(count_labels.py). Prints count_labels.py's report, and exits 1 if a label is missing (or paramtrack fails).

Usage (from anywhere; takes as long as one testbed run, about 6 minutes on prombench):
  uv run --with pandas --with matplotlib --with tabulate python check_labels.py --config <candidate.yaml> \\
    --baseline <baseline run dir> --out <dir> --testbed prombench [--config-files <dir>] [--wait 240] [--list]
The baseline run is paramtrack with the shipped config and the same --testbed and --wait, through step 3 (TESTBED.md,
"Message labels"). --config-files defaults to the files/ next to --config, as paramtrack's does. <out> is
paramtrack's --out; paramtrack's log is <out>/paramtrack.log.
"""

import argparse
import os
import shutil
import subprocess
import sys

QUERYGRAPH = "/home/emily/projects/config_tracing/golang.org-x-tools/gopls/internal/cmd/conftamer/querygraph"
HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--baseline", required=True, help="the baseline run's paramtrack --out directory")
    ap.add_argument("--out", required=True)
    ap.add_argument("--testbed", required=True)
    ap.add_argument("--config-files")
    ap.add_argument("--list", action="store_true", help="count_labels.py --list: print every label, both runs'")
    ap.add_argument("--wait", default="240")
    a = ap.parse_args()
    out, baseline = os.path.abspath(a.out), os.path.abspath(a.baseline)
    base_events = f"{baseline}/3-chains/events.jsonl"
    if not os.path.exists(base_events):
        sys.exit(f"no {base_events}: the baseline run must be paramtrack through step 3 (3-chains)")

    # Reuse the baseline's graph, so paramtrack doesn't rebuild it (step 1 is the same for every config)
    os.makedirs(f"{out}/1-unmarshaler-graph", exist_ok=True)
    shutil.copy(f"{baseline}/1-unmarshaler-graph/unmarshaler_subgraph.text", f"{out}/1-unmarshaler-graph/")
    cmd = [sys.executable, f"{QUERYGRAPH}/paramtrack/paramtrack.py", "--out", out, "--config",
           os.path.abspath(a.config), "--testbed", a.testbed, "--wait", a.wait, "--until", "3", "--redo", "2"]
    if a.config_files:
        cmd += ["--config-files", os.path.abspath(a.config_files)]
    with open(f"{out}/paramtrack.log", "w") as log:
        p = subprocess.run(cmd, cwd=f"{QUERYGRAPH}/paramtrack", stdout=log, stderr=subprocess.STDOUT)
    if p.returncode != 0:
        sys.exit(f"paramtrack exited {p.returncode}; see {out}/paramtrack.log")
    sys.exit(subprocess.run([sys.executable, f"{HERE}/count_labels.py", f"{out}/3-chains/events.jsonl",
                             "--baseline", base_events] + (["--list"] if a.list else [])).returncode)


if __name__ == "__main__":
    main()
