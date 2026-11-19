"""Paths and helpers shared by paramtrack's modules."""

import os
import shlex
import subprocess
import time

CT = "/home/emily/projects/config_tracing"
GOPLS = f"{CT}/golang.org-x-tools/gopls"
QUERYGRAPH = f"{GOPLS}/internal/cmd/conftamer/querygraph"
ARGMATCH_LABELS = f"{CT}/dump_ancestors/argmatch-labels"


def source_version(path):
    """The git version of a source checkout: {"describe": its nearest tag and how far past it (git describe --tags
    --always --dirty, e.g. v3.2.1-18-g52c2a0417-dirty), "commit": its full commit}; None if it isn't a git checkout"""
    def git(*args):
        p = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=60)
        return p.stdout.strip() if p.returncode == 0 else None
    commit = git("rev-parse", "HEAD")
    return {"describe": git("describe", "--tags", "--always", "--dirty"), "commit": commit} if commit else None


# The checkouts the workflow's tools come from (paramtrack's README, "Programs" and "Skills"), the same paths on the
# VM: paramtrack, gopls conftamer, configkeys, stats.py and the skills; argmatch and argmatch-labels (join.py,
# events_to_stacklog.py); contexttrack's patches. Each instrumented image copies parts of all three (the Dockerfiles)
CHECKOUTS = {"golang.org-x-tools": f"{CT}/golang.org-x-tools", "dump_ancestors": f"{CT}/dump_ancestors",
             "conftamer": f"{CT}/conftamer"}


# What a step or skill run ran from, as each records it (write_versions): on this machine, and on the VM, where a
# testbed step builds the instrumented images
LOCAL_VERSIONS = "local_build_versions.json"
VM_VERSIONS = "VM_build_versions.json"


def versions(modules=None):
    """{name: {"path", "describe", "commit"}} for CHECKOUTS and each module's checkout ({module name: path}, as
    "module <name>"); describe ends in -dirty if the checkout had uncommitted changes"""
    paths = {**CHECKOUTS, **{f"module {m}": p for m, p in (modules or {}).items()}}
    return {name: {"path": path, **(source_version(path) or {"commit": None})} for name, path in paths.items()}


def write_versions(path, modules=None):
    """Writes versions(modules) to path (LOCAL_VERSIONS or VM_VERSIONS), as JSON, with the time and machine"""
    import json
    import socket
    with open(path, "w") as f:
        json.dump({"time": time.strftime("%Y-%m-%d %H:%M:%S %z"), "host": socket.gethostname(),
                   "checkouts": versions(modules)}, f, indent=2)


def say(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


# File each command run is appended to (log_command), if set: paramtrack sets it to each step's commands.log
COMMANDS_LOG = None


def log_command(cmd, cwd=None):
    """Appends cmd, shell-quoted, with the time and its working directory, to COMMANDS_LOG"""
    if COMMANDS_LOG:
        with open(COMMANDS_LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} (in {cwd or os.getcwd()}) {shlex.join(cmd)}\n")


def popen(cmd, **kwargs):
    """subprocess.Popen, logging the command"""
    log_command(cmd, kwargs.get("cwd"))
    return subprocess.Popen(cmd, **kwargs)


def run(cmd, cwd=None, log=None, timeout=600, env=None):
    """Runs cmd, writing its output to log (if given), and raises with the log's tail if it fails or times out."""
    log_command(cmd, cwd)
    out = open(log, "w") if log else subprocess.PIPE
    try:
        p = subprocess.run(cmd, cwd=cwd, stdout=out, stderr=subprocess.STDOUT, timeout=timeout, text=True,
                           env={**os.environ, **(env or {})})
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"timed out after {timeout}s: {' '.join(cmd)}" + (f"; see {log}" if log else ""))
    finally:
        if log:
            out.close()
    if p.returncode != 0:
        tail = "".join(open(log).readlines()[-20:]) if log else p.stdout[-3000:]
        raise RuntimeError(f"exit {p.returncode}: {' '.join(cmd)}\n{tail}" + (f"(see {log})" if log else ""))
    return p.stdout if not log else None
