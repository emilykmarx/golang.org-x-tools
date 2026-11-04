"""Testbeds for paramtrack's dynamic phase: each builds the module with argmatch and contexttrack, runs it with the
concrete config under a workload, and collects contexttrack's events and argmatch's matches. See README.md."""

import os
import shutil
import subprocess
import time
import urllib.request

from common import CT, run, say


class Testbed:
    """One run of the module in a testbed. Writes, in out_dir:
    - events.jsonl: contexttrack's events
    - matches.csv, matches.calls.csv: argmatch's output (empty files if argmatch matched nothing)
    - logs of the build and the run
    """

    def run(self, out_dir, config, stacklog=None, matchfile=None):
        """Runs the module with `config`. With stacklog, argmatch instruments its functions and searches for the
        strings in matchfile; without, it instruments nothing (contexttrack still records events)."""
        raise NotImplementedError


# argmatch build args for both runs, as in argmatch-labels/bootstrap-example/bootstrap.sh: search all of each
# receiver (no libraries or types ignored, no time or memory caps), and spread each function's 30 checks out
ARGMATCH_ARGS = {
    "MATCH": "", "MATCHFILE": "", "FUNC": "", "STACKLOG": "", "RECVONLY": "1", "DEPTH": "10", "MAXELEMS": "64",
    "MAXCALLS": "30", "IGNORELIBS": "false", "IGNORETYPES": "", "MAXMEM": "0", "MAXTIME": "0",
    "OUT": "/argmatch-out/matches.csv", "STATS": "true",
}


class PrometheusDocker(Testbed):
    """Prometheus in one local Docker container (prometheus/prombench-argmatch.Dockerfile), with a workload ported
    from argmatch-labels/bootstrap-example/bootstrap.sh: from 20 s on, two API queries every 5 s; one config reload at
    30 s; stopped after `wait` seconds. Prometheus scrapes whatever the config says it can reach from the container.

    The config's Kubernetes service account files (token, CA, namespace) are stand-ins, so that it loads; Kubernetes
    service discovery then fails to connect, so its targets are never scraped."""

    SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
    DOCKERFILE = "prometheus/prombench-argmatch.Dockerfile"
    VERSION = "v3.2.1"

    def __init__(self, wait=240, port=19095, argmatch_args=None):
        self.wait = wait
        self.port = port
        self.argmatch_args = {**ARGMATCH_ARGS, **(argmatch_args or {})}

    def build(self, tag, overrides, log):
        a = {**self.argmatch_args, **overrides}
        cmd = ["docker", "build", "--progress=plain", "-f", self.DOCKERFILE, "--build-arg", f"VERSION={self.VERSION}"]
        for k, v in sorted(a.items()):
            cmd += ["--build-arg", f"ARGMATCH_{k}={v}"]
        say(f"building {tag} (about 5 minutes if what argmatch instruments changed)")
        run(cmd + ["-t", tag, "."], cwd=CT, log=log, timeout=2400)

    def in_build_context(self, path, name):
        """Copies a file into the Prometheus source directory, which the Dockerfile copies to /src; returns its path
        there. (The build reads the stack log and match file at build time.)"""
        shutil.copy(path, f"{CT}/prometheus/{name}")
        return f"/src/{name}"

    def fixtures(self, out_dir):
        sa = f"{out_dir}/fixtures/serviceaccount"
        os.makedirs(sa, exist_ok=True)
        run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{sa}/key.pem", "-out",
             f"{sa}/ca.crt", "-days", "3650", "-subj", "/CN=paramtrack"], log=f"{out_dir}/fixtures/openssl.log",
            timeout=60)
        open(f"{sa}/token", "w").write("paramtrack-token\n")
        open(f"{sa}/namespace", "w").write("default\n")
        os.makedirs(f"{out_dir}/fixtures/data", exist_ok=True)
        return sa, f"{out_dir}/fixtures/data"

    def run(self, out_dir, config, stacklog=None, matchfile=None):
        if stacklog:
            tag = "paramtrack-instrumented:latest"
            overrides = {"STACKLOG": self.in_build_context(stacklog, "paramtrack-stacklog.txt"),
                         "MATCHFILE": self.in_build_context(matchfile, "paramtrack-matches.txt")}
        else:
            # argmatch needs -func or -stacklog: name a function that doesn't exist
            tag = "paramtrack-uninstrumented:latest"
            overrides = {"FUNC": "main.paramtrackNone", "MATCH": "paramtrack-none"}
        self.build(tag, overrides, f"{out_dir}/build.log")

        sa, data = self.fixtures(out_dir)
        argmatch_out = f"{out_dir}/argmatch-out"
        os.makedirs(argmatch_out, exist_ok=True)
        for d in (argmatch_out, data):
            os.chmod(d, 0o777)  # the container runs as nobody
        name = f"paramtrack-{os.getpid()}"
        say(f"running {tag} for {self.wait}s")
        run(["docker", "run", "-d", "--name", name, "-p", f"{self.port}:9090",
             "-v", f"{argmatch_out}:/argmatch-out", "-v", f"{os.path.abspath(config)}:/etc/prometheus/prometheus.yml:ro",
             "-v", f"{sa}:{self.SA_DIR}:ro", "-v", f"{data}:/data",
             "-e", "CONFTAMER_EVENTS=/argmatch-out/events.jsonl", tag,
             "--config.file=/etc/prometheus/prometheus.yml", "--storage.tsdb.path=/prometheus",
             "--web.enable-lifecycle"], timeout=60)
        try:
            self.workload(name, argmatch_out)
        finally:
            with open(f"{out_dir}/container.log", "w") as f:
                subprocess.run(["docker", "logs", name], stdout=f, stderr=subprocess.STDOUT, timeout=60)
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)

        for f in ("events.jsonl", "matches.csv", "matches.calls.csv"):
            src = f"{argmatch_out}/{f}"
            if os.path.exists(src):
                shutil.move(src, f"{out_dir}/{f}")
            else:
                open(f"{out_dir}/{f}", "w").close()
        if os.path.getsize(f"{out_dir}/events.jsonl") == 0:
            raise RuntimeError(f"no events in {out_dir}/events.jsonl; see {out_dir}/container.log")
        n_events = sum(1 for _ in open(f"{out_dir}/events.jsonl"))
        n_calls = max(0, sum(1 for _ in open(f"{out_dir}/matches.calls.csv")) - 1)
        say(f"{tag}: {n_events} events; {n_calls} lines of argmatch calls")

    def get(self, path, method="GET"):
        try:
            urllib.request.urlopen(urllib.request.Request(f"http://localhost:{self.port}{path}", method=method),
                                   timeout=10).read()
            return True
        except OSError:
            return False

    def workload(self, name, argmatch_out):
        start = time.time()
        ready = reloaded = False
        while (t := time.time() - start) < self.wait:
            state = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name], capture_output=True,
                                   text=True, timeout=60).stdout.strip()
            if state != "true":
                raise RuntimeError(f"container {name} stopped after {t:.0f}s")
            if not ready and self.get("/-/ready"):
                ready = True
                say(f"ready after {t:.0f}s")
            if not ready and t > 120:
                raise RuntimeError(f"container {name} not ready after {t:.0f}s")
            if t >= 20:
                # Check early that contexttrack is recording
                events = f"{argmatch_out}/events.jsonl"
                if not os.path.exists(events) or os.path.getsize(events) == 0:
                    raise RuntimeError(f"no events after {t:.0f}s (is CONFTAMER_EVENTS set and the image patched?)")
                now = int(time.time())
                self.get("/api/v1/query?query=up")
                self.get(f"/api/v1/query_range?query=rate(prometheus_http_requests_total[1m])&start={now - 600}"
                         f"&end={now}&step=15")
            if t >= 30 and not reloaded:
                reloaded = True
                if not self.get("/-/reload", method="POST"):
                    say("config reload failed")
            time.sleep(5)


TESTBEDS = {"docker": PrometheusDocker}
