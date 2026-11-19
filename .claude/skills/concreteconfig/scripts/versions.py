#!/usr/bin/env python3
"""Records what a skill run ran from: writes <out dir>/local_build_versions.json (paramtrack's common.LOCAL_VERSIONS)
with the git version of the workflow's tool checkouts (common.CHECKOUTS: golang.org-x-tools, which holds the skills
and their scripts, paramtrack and stats.py; dump_ancestors; conftamer) and of the module's checkout (its module.py's
MODULE_DIR), as paramtrack records for each of its steps (common.write_versions).

  versions.py <out dir> [--module <module>] [--checkout <name>=<path> ...]
      now: each checkout's commit and `git describe --dirty`; --checkout for a module's checkouts when it has no
      module.py yet (e.g. a documentedkeys run), recorded as "module <name>"
  versions.py <out dir> [--module <module>] --at "<time>" [--note "<how the time was chosen>"]
      afterwards, for a run made before this script existed: each checkout's last commit at that time (git log
      --before), with "retroactive": true; whether the checkouts had uncommitted changes then is unknown
  versions.py --run <paramtrack out dir> [--vm <VM>]
      afterwards, for a paramtrack run made before paramtrack recorded versions (2026-11-18): writes what paramtrack
      now writes, each step's local_build_versions.json (the tools' and every instrumented module's local checkouts)
      and each testbed step's VM_build_versions.json (the same on --vm, default paramtrack-e2e, over ssh), as of the
      step's start (the first line of its commands.log); step directories that already have the file are skipped

Usable by any skill (the documentedkeys skill too).
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "../../../../gopls/internal/cmd/conftamer/querygraph/paramtrack"))
import common  # noqa: E402
import modules  # noqa: E402
import testbeds  # noqa: E402


def at(path, when):
    """A checkout's last commit (on its current branch) at time when"""
    def git(*args):
        p = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=60)
        return p.stdout.strip() if p.returncode == 0 else None
    commit = git("rev-list", "-1", f"--before={when}", "HEAD")
    if not commit:
        return {"commit": None}
    return {"describe": git("describe", "--tags", "--always", commit), "commit": commit}


def vm_at(vm, paths, when):
    """at() for each of paths on the VM, over ssh: {path: {"describe", "commit"}}"""
    script = "; ".join(
        f'c=$(git -C {p} rev-list -1 --before="{when}" HEAD 2>/dev/null); '
        f'd=$(git -C {p} describe --tags --always $c 2>/dev/null); echo "{p}|$c|$d"' for p in paths)
    out = subprocess.run(["gcloud", "compute", "ssh", "--zone", "us-central1-b", vm, "--project", "conftamer",
                          "--command", script], capture_output=True, text=True, timeout=300, check=True).stdout
    got = {}
    for line in out.splitlines():
        if line.count("|") == 2:
            p, c, d = line.split("|")
            got[p] = {"describe": d or None, "commit": c or None}
    return got


NOTE = ("written {today} from git history: each checkout's last commit before the step's start (the first line of "
        "its commands.log); uncommitted changes then unknown")


def record(path, when, host, checkouts, note):
    with open(path, "w") as f:
        json.dump({"time": when, "host": host, "retroactive": True, "note": note, "checkouts": checkouts}, f, indent=2)


def backfill_run(run, vm):
    """versions.py --run: each step directory of a paramtrack run (layout.py: <run>/testbed/<step>,
    <run>/<module>/<step>, <run>/<module>/<config source>/<step>)"""
    tb = next((f"{run}/testbed/{s}/testbed.json" for s in ("3-chains", "5-matches")
               if os.path.exists(f"{run}/testbed/{s}/testbed.json")), None)
    mods = json.load(open(tb))["modules"] if tb else sorted(
        d for d in os.listdir(run) if os.path.isdir(f"{run}/{d}") and d != "testbed")
    local = {f"module {m}": modules.load(m).MODULE_DIR for m in mods}
    remote = {f"module {m}": os.path.join(common.CT, testbeds.COMPONENTS[m].SRC) for m in mods}
    note = NOTE.format(today=time.strftime("%Y-%m-%d"))
    for root, dirs, _ in os.walk(run):
        for d in sorted(dirs):
            step = f"{root}/{d}"
            if not (d[:2] in {f"{i}-" for i in range(1, 9)}) or d.endswith(".partial"):
                continue
            log = f"{step}/commands.log"
            when = open(log).readline()[:19] if os.path.exists(log) else time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(step)))
            lp = f"{step}/{common.LOCAL_VERSIONS}"
            if not os.path.exists(lp):
                paths = {**common.CHECKOUTS, **local}
                record(lp, when, socket.gethostname(), {n: {"path": p, **at(p, when)} for n, p in paths.items()}, note)
                print(f"wrote {lp} (as of {when})")
            vp = f"{step}/{common.VM_VERSIONS}"
            if os.path.basename(root) == "testbed" and not os.path.exists(vp):
                paths = {**common.CHECKOUTS, **remote}
                got = vm_at(vm, list(paths.values()), when)
                record(vp, when, vm, {n: {"path": p, **got.get(p, {"commit": None})} for n, p in paths.items()},
                       note + "; read on the VM over ssh")
                print(f"wrote {vp} (as of {when})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", nargs="?")
    ap.add_argument("--run", help="a paramtrack out dir to backfill (see above)")
    ap.add_argument("--vm", default="paramtrack-e2e", help="with --run: the VM the testbed steps ran on")
    ap.add_argument("--module", help="the skill's module (modules/<module>/), whose checkout is recorded too")
    ap.add_argument("--checkout", action="append", default=[], metavar="NAME=PATH",
                    help="a module checkout to record too (for a module with no module.py yet)")
    ap.add_argument("--at", help="record the checkouts as they were at this time (git's date formats)")
    ap.add_argument("--note", help="with --at: how the time was chosen, or what else is known (recorded as given)")
    a = ap.parse_args()
    if a.run:
        return backfill_run(os.path.abspath(a.run), a.vm)
    if not a.out:
        ap.error("give <out dir> or --run")
    mods = {a.module: modules.load(a.module).MODULE_DIR} if a.module else {}
    for c in a.checkout:
        name, _, path = c.partition("=")
        if not path:
            ap.error(f"--checkout {c}: give NAME=PATH")
        mods[name] = os.path.abspath(path)
    os.makedirs(a.out, exist_ok=True)
    path = f"{a.out}/{common.LOCAL_VERSIONS}"
    if not a.at:
        common.write_versions(path, mods)
    else:
        paths = {**common.CHECKOUTS, **{f"module {m}": p for m, p in mods.items()}}
        checkouts = {name: {"path": p, **at(p, a.at)} for name, p in paths.items()}
        with open(path, "w") as f:
            json.dump({"time": a.at, "host": socket.gethostname(), "retroactive": True,
                       "note": f"written {time.strftime('%Y-%m-%d')} from git history: each checkout's last commit "
                               f"before the run's time; uncommitted changes then unknown"
                               + (f". {a.note}" if a.note else ""),
                       "checkouts": checkouts}, f, indent=2)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
