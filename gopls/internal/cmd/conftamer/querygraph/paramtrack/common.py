"""Paths and helpers shared by paramtrack's modules."""

import os
import shlex
import subprocess
import time

CT = "/home/emily/projects/config_tracing"
GOPLS = f"{CT}/golang.org-x-tools/gopls"
QUERYGRAPH = f"{GOPLS}/internal/cmd/conftamer/querygraph"
ARGMATCH_LABELS = f"{CT}/dump_ancestors/argmatch-labels"


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
