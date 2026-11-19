#!/usr/bin/env python3
"""Check constraint 2 (SKILL.md) for candidate concrete configs: run paramtrack up to testbed run 1 (`--until 3`) with
the baseline's testbed settings, so every module the baseline instrumented is instrumented again, the skill's module
with the candidates and the others with their shipped configs; then compare each module's message labels with the
baseline's (count_labels.py). With --module, only that module's labels count: the other modules' are reported, but
their missing labels don't change the exit code (they're checked after the skill run, on a fresh cluster). Without
--module (configs for several modules), every module's count. Prints count_labels.py's report per module. A missing
label in the module's known timing-dependent labels (the varying-labels*.txt files in its testbed directory,
testbeds/prombench/<module>/, as `count_labels.py --varying` writes them) is marked "missing (known timing)" and
doesn't count. Exits 0 if no counted label is missing (known timing ones aside), 1 if one is, and 2 if it couldn't
check (the baseline doesn't fit, paramtrack failed, count_labels.py failed, or bad arguments).

Usage (from anywhere; takes as long as one testbed run with every module instrumented):
  uv run --with pandas --with pyyaml --with matplotlib --with tabulate --with numpy python check_labels.py \\
    --baseline <baseline run dir> --module <module> --config <source>=<candidate> [--config <source>=<candidate> ...] \\
    [--config-files <source>=<dir> ...] --out <dir> [--list]
  check_labels.py --baseline <dir> --config <module>:<source>=<config> ... --out <dir>
    (no --module: configs for several modules, e.g. the final check with every module on its concrete configs)
  check_labels.py --baseline <dir> [--module <module>] --compare <out dir> [<out dir> ...] [--list]
    (no run: compare the union of earlier checks' labels, per module, e.g. reruns of one candidate when a missing label
    may depend on timing)
The baseline run is paramtrack with every module's shipped configs, through step 3 (SKILL.md, "Inputs"; baseline.py).
Config sources without --config run their shipped configs. --config-files defaults to the files/ next to a --config, as
paramtrack's does. <out> is paramtrack's --out; paramtrack's log is <out>/paramtrack.log.
"""

import argparse
import glob
import os
import re
import subprocess
import sys

import baseline  # (next to this file)

QUERYGRAPH = "/home/emily/projects/config_tracing/golang.org-x-tools/gopls/internal/cmd/conftamer/querygraph"
HERE = os.path.dirname(os.path.abspath(__file__))


def fail(msg):
    """Couldn't check: exit 2, which a missing label (1) can't be confused with"""
    print(f"check_labels.py: {msg}", file=sys.stderr)
    sys.exit(2)


def qualified(values, module, flag):
    """--config / --config-files values as paramtrack's [<module>:]<root>=<path>, with absolute paths; the modules
    they name"""
    out, mods = [], set()
    for v in values:
        name, sep, path = v.partition("=")
        if not sep:
            fail(f"{flag} {v}: give <source>=<path> (with --module) or <module>:<source>=<path>")
        if ":" not in name:
            if not module:
                fail(f"{flag} {v}: without --module, name the module: <module>:<source>=<path>")
            name = f"{module}:{name}"
        mods.add(name.split(":", 1)[0])
        out.append(f"{name}={os.path.abspath(path)}")
    return out, mods


def known_timing(module):
    """The module's known timing-dependent labels: those in its testbed directory's varying-labels*.txt files (lines
    "  runs <runs>: <label>", count_labels.py --varying), and the files"""
    files = sorted(glob.glob(f"{QUERYGRAPH}/paramtrack/testbeds/prombench/{module}/varying-labels*.txt"))
    labels = set()
    for f in files:
        for line in open(f):
            if m := re.match(r"  runs [\d,]+: (.*)$", line.rstrip("\n")):
                labels.add(m.group(1))
    return labels, files


def compare(base, outs, tb, show_list, counted):
    """count_labels.py per module of the baseline: the union of outs' events against the baseline's; the exit code,
    from the modules counted (the others are reported only)"""
    worst, short = 0, []
    for m in sorted(tb["modules"], key=lambda m: m not in counted):
        evs = [f"{o}/{m}/3-chains/events.jsonl" for o in outs]
        if missing := [e for e in evs if not os.path.exists(e)]:
            fail(f"no {', '.join(missing)}: was that run through step 3 with the baseline's modules?")
        print(f"\n======== {m}" + ("" if m in counted else " (reported only: doesn't count)"), flush=True)
        p = subprocess.run([sys.executable, f"{HERE}/count_labels.py", *evs, "--baseline",
                            baseline.events(base, m)] + (["--list"] if show_list else []), capture_output=True, text=True)
        code = p.returncode
        if code not in (0, 1):
            sys.stderr.write(p.stderr)
            fail(f"count_labels.py exited {code} for {m}")
        # Missing labels that are known to depend on timing don't count
        timing, files = known_timing(m)
        unknown = 0
        for line in p.stdout.splitlines():
            if line.startswith("  missing: "):
                if line[len("  missing: "):] in timing:
                    line = "  missing (known timing): " + line[len("  missing: "):]
                else:
                    unknown += 1
            print(line)
        if code == 1 and not unknown:
            print(f"all missing labels are known timing-dependent ones ({', '.join(os.path.relpath(f, QUERYGRAPH) for f in files)})")
            code = 0
        sys.stdout.flush()
        if m in counted:
            worst = max(worst, code)
        short += [m] if code == 1 else []
    others = [m for m in short if m not in counted]
    print(f"\nlabels missing (above) in: {', '.join(m for m in short if m in counted) or 'none'} (counted: "
          f"{', '.join(counted)})" + (f"; also in {', '.join(others)} (reported only)" if others else ""))
    return worst


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", required=True, help="the baseline run's paramtrack --out directory")
    ap.add_argument("--module", help="the skill's module (its modules/ directory name)")
    ap.add_argument("--config", action="append", default=[], metavar="[MODULE:]CONFIG_SOURCE=PATH")
    ap.add_argument("--config-files", action="append", default=[], metavar="[MODULE:]CONFIG_SOURCE=DIR")
    ap.add_argument("--out")
    ap.add_argument("--compare", nargs="+", metavar="OUT", help="earlier checks' --out directories: compare only")
    ap.add_argument("--list", action="store_true", help="count_labels.py --list: print every label, both runs'")
    a = ap.parse_args()
    base = os.path.abspath(a.baseline)
    try:
        tb = baseline.testbed(base)
        module = baseline.module(base, a.module)[1] if a.module else None  # (its modules/ directory name)
    except baseline.MissingInput as e:
        fail(str(e))
    # With --module, only its labels count; else (configs for several modules) every module's
    counted = [module] if module else list(tb["modules"])
    if a.compare:
        sys.exit(compare(base, [os.path.abspath(o) for o in a.compare], tb, a.list, counted))
    if not a.config or not a.out:
        fail("give --config (one or more) and --out, or --compare")
    out = os.path.abspath(a.out)
    configs, mods = qualified(a.config, module, "--config")
    files, _ = qualified(a.config_files, module, "--config-files")
    try:
        for m in mods:
            baseline.module(base, m)
    except baseline.MissingInput as e:
        fail(str(e))
    # The configured modules' graphs, kept in the baseline by score.py (else paramtrack builds them, and they're kept)
    for m in mods:
        baseline.copy_graph(base, m, out)
    cmd = [sys.executable, f"{QUERYGRAPH}/paramtrack/paramtrack.py", "--out", out, "--until", "3", "--redo", "2"]
    cmd += [x for c in configs for x in ("--config", c)] + [x for f in files for x in ("--config-files", f)]
    cmd += baseline.testbed_args(base)
    os.makedirs(out, exist_ok=True)
    with open(f"{out}/paramtrack.log", "w") as log:
        p = subprocess.run(cmd, cwd=f"{QUERYGRAPH}/paramtrack", stdout=log, stderr=subprocess.STDOUT)
    if p.returncode != 0:
        fail(f"paramtrack exited {p.returncode}; see {out}/paramtrack.log")
    for m in mods:
        baseline.keep_graph(base, m, out)
    sys.exit(compare(base, [out], tb, a.list, counted))


if __name__ == "__main__":
    main()
