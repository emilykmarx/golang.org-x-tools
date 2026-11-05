"""Testbeds for paramtrack's dynamic phase: each builds the module with argmatch and contexttrack, runs it with the
concrete config under a workload, and collects contexttrack's events and argmatch's matches. See README.md.

Also run on the VM by PrombenchVM: testbeds.py prombench --out DIR --config FILE [--stacklog FILE --matchfile FILE]
"""

import argparse
import base64
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

from common import CT, run, say

OUTPUTS = ("events.jsonl", "matches.csv", "matches.calls.csv")


class Testbed:
    """One run of the module in a testbed. Writes, in out_dir:
    - events.jsonl: contexttrack's events
    - matches.csv, matches.calls.csv: argmatch's output (empty files if argmatch matched nothing)
    - logs of the build and the run
    """

    def __init__(self, wait=240, argmatch_args=None, vm=None):
        self.wait = wait
        self.argmatch_args = {**ARGMATCH_ARGS, **(argmatch_args or {})}
        self.vm = vm

    def run(self, out_dir, config, stacklog=None, matchfile=None, files=None):
        """Runs the module with `config`, and the files it refers to (`files`: a directory, or None) where the config
        expects them (each testbed says where). With stacklog, argmatch instruments its functions and searches for the
        strings in matchfile; without, it instruments nothing (contexttrack still records events)."""
        raise NotImplementedError


# argmatch build args for both runs, as in argmatch-labels/bootstrap-example/bootstrap.sh: search all of each
# receiver (no libraries or types ignored, no time or memory caps), and spread each function's 30 checks out
ARGMATCH_ARGS = {
    "MATCH": "", "MATCHFILE": "", "FUNC": "", "STACKLOG": "", "RECVONLY": "1", "DEPTH": "10", "MAXELEMS": "64",
    "MAXCALLS": "30", "IGNORELIBS": "false", "IGNORETYPES": "", "MAXMEM": "0", "MAXTIME": "0",
    "OUT": "/argmatch-out/matches.csv", "STATS": "true",
}


class PrometheusImage(Testbed):
    """Builds Prometheus under argmatch and contexttrack, from prometheus/prombench-argmatch.Dockerfile"""

    DOCKERFILE = "prometheus/prombench-argmatch.Dockerfile"
    VERSION = "v3.2.1"

    def build(self, tag, out_dir, stacklog, matchfile):
        if stacklog:
            overrides = {"STACKLOG": self.in_build_context(stacklog, "paramtrack-stacklog.txt"),
                         "MATCHFILE": self.in_build_context(matchfile, "paramtrack-matches.txt")}
        else:
            # argmatch needs -func or -stacklog: name a function that doesn't exist
            overrides = {"FUNC": "main.paramtrackNone", "MATCH": "paramtrack-none"}
        a = {**self.argmatch_args, **overrides}
        cmd = ["docker", "build", "--progress=plain", "-f", self.DOCKERFILE, "--build-arg", f"VERSION={self.VERSION}"]
        for k, v in sorted(a.items()):
            cmd += ["--build-arg", f"ARGMATCH_{k}={v}"]
        say(f"building {tag} (about 5 minutes if what argmatch instruments changed)")
        run(cmd + ["-t", tag, "."], cwd=CT, log=f"{out_dir}/build.log", timeout=2400)

    def in_build_context(self, path, name):
        """Copies a file into the Prometheus source directory, which the Dockerfile copies to /src; returns its path
        there. (The build reads the stack log and match file at build time.)"""
        shutil.copy(path, f"{CT}/prometheus/{name}")
        return f"/src/{name}"

    def summarize(self, out_dir, what, log):
        if os.path.getsize(f"{out_dir}/events.jsonl") == 0:
            raise RuntimeError(f"no events in {out_dir}/events.jsonl; see {log}")
        n_events = sum(1 for _ in open(f"{out_dir}/events.jsonl"))
        n_calls = max(0, sum(1 for _ in open(f"{out_dir}/matches.calls.csv")) - 1)
        say(f"{what}: {n_events} events; {n_calls} lines of argmatch calls")


class PrometheusDocker(PrometheusImage):
    """Prometheus in one local Docker container, with a workload ported from
    argmatch-labels/bootstrap-example/bootstrap.sh: from 20 s on, two API queries every 5 s; one config reload at
    30 s; stopped after `wait` seconds. Prometheus scrapes whatever the config says it can reach from the container.

    The config's Kubernetes service account files (token, CA, namespace) are stand-ins, so that it loads; Kubernetes
    service discovery then fails to connect, so its targets are never scraped."""

    SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
    PORT = 19095

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

    def run(self, out_dir, config, stacklog=None, matchfile=None, files=None):
        tag = "paramtrack-instrumented:latest" if stacklog else "paramtrack-uninstrumented:latest"
        self.build(tag, out_dir, stacklog, matchfile)

        sa, data = self.fixtures(out_dir)
        argmatch_out = f"{out_dir}/argmatch-out"
        os.makedirs(argmatch_out, exist_ok=True)
        for d in (argmatch_out, data):
            os.chmod(d, 0o777)  # the container runs as nobody
        name = f"paramtrack-{os.getpid()}"
        say(f"running {tag} for {self.wait}s")
        run(["docker", "run", "-d", "--name", name, "-p", f"{self.PORT}:9090",
             "-v", f"{argmatch_out}:/argmatch-out", "-v", f"{os.path.abspath(config)}:/etc/prometheus/prometheus.yml:ro",
             "-v", f"{sa}:{self.SA_DIR}:ro", "-v", f"{data}:/data",
             # The config's files, where prombench's ConfigMap puts them (see Prombench.restart)
             *(["-v", f"{os.path.abspath(files)}:{Prombench.CONFIG_DIR}:ro"] if files else []),
             "-e", "CONFTAMER_EVENTS=/argmatch-out/events.jsonl", tag,
             "--config.file=/etc/prometheus/prometheus.yml", "--storage.tsdb.path=/prometheus",
             "--web.enable-lifecycle"], timeout=60)
        try:
            self.workload(name, argmatch_out)
        finally:
            with open(f"{out_dir}/container.log", "w") as f:
                subprocess.run(["docker", "logs", name], stdout=f, stderr=subprocess.STDOUT, timeout=60)
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)

        for f in OUTPUTS:
            src = f"{argmatch_out}/{f}"
            if os.path.exists(src):
                shutil.move(src, f"{out_dir}/{f}")
            else:
                open(f"{out_dir}/{f}", "w").close()
        self.summarize(out_dir, tag, f"{out_dir}/container.log")

    def get(self, path, method="GET"):
        try:
            urllib.request.urlopen(urllib.request.Request(f"http://localhost:{self.PORT}{path}", method=method),
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


class Prombench(PrometheusImage):
    """prombench's prometheus-meta, in the kind cluster on the machine this runs on (deployed with
    prometheus-test-infra/prombench/deploy.sh). Ported from dump_ancestors/argmatch/experiment/run_on_vm.sh: builds
    prometheus-conftamer:v3.2.1 (the image the deployment runs), loads it into the main node, sets the prometheus-meta
    config map to the concrete config and CONFTAMER_EVENTS on the deployment, and restarts prometheus-meta. Once it's
    ready, a simulated person views Grafana's dashboards until the capture (the experiment's view_dashboards.py), and
    30 s later the experiment's reload_on_vm.sh reloads a changed config and then the concrete config again. Stops
    after `wait` seconds from the restart."""

    CTX = "kind-prombench"
    TAG = "prometheus-conftamer:v3.2.1"
    OUTDIR = f"{CT}/dump_ancestors/argmatch-out"  # mounted at /argmatch-out in prometheus-meta
    CONFIG_DIR = "/etc/prometheus/config"  # where prometheus-meta mounts its ConfigMap
    EXPERIMENT = f"{CT}/dump_ancestors/argmatch/experiment"
    RELOAD_AFTER = 30

    def k(self, *args, timeout=120, log=None):
        return run(["kubectl", "--context", self.CTX, *args], timeout=timeout, log=log)

    def load(self):
        say("loading the image into the main node")
        with tempfile.NamedTemporaryFile(suffix=".tar") as tar:
            run(["docker", "save", self.TAG, "-o", tar.name], timeout=600)
            nodes = self.k("get", "nodes", "-l", "cloud.google.com/gke-nodepool=main-node", "-o", "name").split()
            for n in nodes:
                with open(tar.name, "rb") as f:
                    subprocess.run(["docker", "exec", "-i", n.removeprefix("node/"), "ctr", "-n", "k8s.io", "images",
                                    "import", "-"], stdin=f, check=True, capture_output=True, timeout=600)

    def restart(self, config, files=None):
        """Restarts prometheus-meta with the config, the files it refers to (added to the ConfigMap, so they appear in
        CONFIG_DIR), CONFTAMER_EVENTS set and no earlier outputs. Scaling to 0 first means the old pod is gone (no more
        writes, TSDB unlocked) before the new one starts."""
        self.k("scale", "deploy/prometheus-meta", "--replicas=0")
        subprocess.run(["kubectl", "--context", self.CTX, "wait", "--for=delete", "pod", "-l", "app=prometheus-meta",
                        "--timeout=600s"], capture_output=True, timeout=660)
        data, binary = {"prometheus.yaml": open(config).read()}, {}
        for name in sorted(os.listdir(files)) if files else []:
            content = open(f"{files}/{name}", "rb").read()
            try:
                data[name] = content.decode()
            except UnicodeDecodeError:
                binary[name] = base64.b64encode(content).decode()
        with tempfile.NamedTemporaryFile("w", suffix=".json") as f:  # (can be too long for a command line)
            json.dump({"data": data, "binaryData": binary}, f)
            f.flush()
            self.k("patch", "configmap", "prometheus-meta", "--type", "merge", "--patch-file", f.name)
        self.k("set", "env", "deploy/prometheus-meta", "-c", "prometheus", "CONFTAMER_EVENTS=/argmatch-out/events.jsonl")
        run(["sudo", "rm", "-f"] + [f"{self.OUTDIR}/{f}" for f in OUTPUTS], timeout=60)
        self.k("scale", "deploy/prometheus-meta", "--replicas=1")
        say("prometheus-meta restarted")

    def exec_wget(self, path):
        p = subprocess.run(["kubectl", "--context", self.CTX, "exec", "deploy/prometheus-meta", "-c", "prometheus",
                            "--", "wget", "-qO-", f"localhost:9090/prometheus-meta{path}"], capture_output=True,
                           timeout=60)
        return p.returncode == 0

    def run(self, out_dir, config, stacklog=None, matchfile=None, files=None):
        self.build(self.TAG, out_dir, stacklog, matchfile)
        self.load()
        self.restart(config, files)
        start = time.time()
        helpers = []  # background processes, stopped at the capture
        ready = reload = None
        try:
            while (t := time.time() - start) < self.wait:
                if ready is None and self.exec_wget("/-/ready"):
                    ready = t
                    say(f"ready after {t:.0f}s")
                    pf = subprocess.Popen(["kubectl", "--context", self.CTX, "port-forward", "svc/grafana", "3000:80"],
                                          stdout=open(f"{out_dir}/port-forward.log", "w"), stderr=subprocess.STDOUT)
                    time.sleep(2)
                    viewer = subprocess.Popen(
                        [sys.executable, f"{self.EXPERIMENT}/view_dashboards.py", "--url",
                         "http://localhost:3000/grafana", "--duration", str(int(self.wait - t))],
                        stdout=open(f"{out_dir}/viewer.log", "w"), stderr=subprocess.STDOUT)
                    helpers += [pf, viewer]
                if ready is None and t > 600:
                    raise RuntimeError(f"prometheus-meta not ready after {t:.0f}s")
                if ready is not None and reload is None and t >= ready + self.RELOAD_AFTER:
                    # Check early that contexttrack is recording
                    events = f"{self.OUTDIR}/events.jsonl"
                    if not os.path.exists(events) or os.path.getsize(events) == 0:
                        raise RuntimeError(f"no events {t:.0f}s after the restart (is CONFTAMER_EVENTS set?)")
                    reload = subprocess.Popen(["bash", f"{self.EXPERIMENT}/reload_on_vm.sh", f"{out_dir}/reload.log"])
                    say("config reload started")
                time.sleep(5)
            say(f"capturing after {time.time() - start:.0f}s")
            for f in OUTPUTS:
                if os.path.exists(f"{self.OUTDIR}/{f}"):
                    run(["sudo", "cp", f"{self.OUTDIR}/{f}", f"{out_dir}/{f}"], timeout=120)
                    run(["sudo", "chown", f"{os.getuid()}:{os.getgid()}", f"{out_dir}/{f}"], timeout=60)
                else:
                    open(f"{out_dir}/{f}", "w").close()
        finally:
            for p in helpers:
                p.terminate()
            if reload is not None:
                # Let it finish, since it restores the config map; killing it restores it too (its EXIT trap)
                try:
                    reload.wait(timeout=300)
                except subprocess.TimeoutExpired:
                    reload.terminate()
                    reload.wait(timeout=60)
                    say("config reloads not done after 300s; stopped them")
                if reload.returncode not in (0, None):
                    say(f"config reload failed; see {out_dir}/reload.log")
            self.k("logs", "--timestamps", "deploy/prometheus-meta", "-c", "prometheus", log=f"{out_dir}/pod.log",
                   timeout=300)
        self.summarize(out_dir, "prometheus-meta", f"{out_dir}/pod.log")


class PrombenchVM(Testbed):
    """Prombench on a GCE VM (--vm): copies this code and the run's inputs there, runs the Prombench testbed on it, and
    copies the outputs back. The VM needs the checkouts that prometheus-test-infra/prombench/vm_setup.sh makes, at the
    same paths as here, and prombench deployed (deploy.sh)."""

    ZONE = "us-central1-b"
    PROJECT = "conftamer"
    REMOTE = "/tmp/paramtrack-testbed"

    POLL = 30  # seconds between checks on the run
    GIVE_UP = 1200  # seconds of failed ssh calls before giving up

    def ssh(self, command, timeout, stdin=None, stdout=None):
        """Runs command on the VM, retrying while ssh itself fails (exit 255, e.g. the network dropped) for up to
        GIVE_UP seconds. stdin and stdout are files (stdin is rewound for each try)."""
        cmd = ["gcloud", "compute", "ssh", "--zone", self.ZONE, self.vm, "--project", self.PROJECT, "--command",
               command, "--", "-o", "ServerAliveInterval=30", "-o", "ConnectTimeout=30"]
        start = time.time()
        while True:
            if stdin:
                stdin.seek(0)
            if stdout:
                stdout.seek(0)
                stdout.truncate()
            try:
                p = subprocess.run(cmd, stdin=stdin, stdout=stdout or subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=timeout)
                code, err = p.returncode, p.stderr.decode(errors="replace").strip()
            except subprocess.TimeoutExpired:
                code, err = 255, f"timed out after {timeout}s"
            if code == 0:
                return None if stdout else p.stdout.decode(errors="replace")
            if code != 255:
                raise RuntimeError(f"exit {code} on {self.vm}: {command}\n{err[-2000:]}")
            if time.time() - start > self.GIVE_UP:
                raise RuntimeError(f"ssh to {self.vm} failing for {self.GIVE_UP}s: {err[-500:]}")
            say(f"ssh to {self.vm} failed ({err.splitlines()[-1] if err else 'exit 255'}); retrying")
            time.sleep(self.POLL)

    def run(self, out_dir, config, stacklog=None, matchfile=None, files=None):
        if not self.vm:
            raise RuntimeError("the prombench testbed needs --vm")
        here = os.path.dirname(os.path.abspath(__file__))
        inputs = {"testbeds.py": f"{here}/testbeds.py", "common.py": f"{here}/common.py", "config.yaml": config}
        if stacklog:
            inputs |= {"stacklog.txt": stacklog, "matches.txt": matchfile}
        with tempfile.TemporaryDirectory() as tmp:
            for name, path in inputs.items():
                shutil.copy(path, f"{tmp}/{name}")
            if files:
                shutil.copytree(files, f"{tmp}/files")
            tar = subprocess.run(["tar", "-C", tmp, "-cf", "-", "."], capture_output=True, check=True).stdout
        say(f"copying inputs to {self.vm}")
        self.ssh(f"rm -rf {self.REMOTE} && mkdir -p {self.REMOTE}/out && tar -C {self.REMOTE} -xf -", timeout=300,
                 stdin=_bytes_file(tar))
        args = ["python3", "testbeds.py", "prombench", "--out", "out", "--config", "config.yaml", "--wait",
                str(self.wait)]
        if stacklog:
            args += ["--stacklog", "stacklog.txt", "--matchfile", "matches.txt"]
        if files:
            args += ["--files", "files"]
        for k, v in self.argmatch_args.items():
            if ARGMATCH_ARGS.get(k) != v:
                args += ["--argmatch-arg", f"{k}={v}"]
        # Start the run detached on the VM, so that it survives ssh dropping; it writes out/testbed.log and, when it
        # exits, out/exit. Build (up to 40 min) + load + up to 10 min to be ready + the wait + the capture.
        limit = 2400 + 600 + 600 + self.wait + 900
        inner = f"timeout {limit} {shlex.join(args)} > out/testbed.log 2>&1; echo $? > out/exit"
        # (If ssh drops after starting it, the retry mustn't start it again)
        self.ssh(f"cd {self.REMOTE} && {{ [ -e out/testbed.log ] || (setsid nohup bash -c {shlex.quote(inner)} "
                 f"> /dev/null 2>&1 < /dev/null &); }}", timeout=120)
        say(f"testbed started on {self.vm}; its log is {self.REMOTE}/out/testbed.log there")
        try:
            # Print the run's new log lines as they appear, until it exits
            n_lines, deadline = 0, time.time() + limit + 300
            while True:
                time.sleep(self.POLL)
                out = self.ssh(f"cd {self.REMOTE}/out && tail -n +{n_lines + 1} testbed.log; "
                               f"echo; echo \"@@exit $(cat exit 2>/dev/null)\"", timeout=120)
                body, _, status = out.rstrip("\n").rpartition("\n@@exit ")
                lines = body.split("\n")[:-1]  # the last is the blank line echoed before the status
                for line in lines:
                    print(f"  [{self.vm}] {line}", flush=True)
                n_lines += len(lines)
                if status.strip():
                    code = int(status)
                    break
                if time.time() > deadline:
                    raise RuntimeError(f"testbed on {self.vm} still running after {limit + 300}s")
        finally:
            # Compressed: argmatch's matches.csv repeats rows for every call, so it compresses about 75x (run 2's
            # 5 GB of outputs: 66 MB, in about 15 s in all, against 3.5 minutes uncompressed). argmatch's dedup.py
            # doesn't help: keeping the call column, which join.py needs, it removed 0.3% of rows, in 11 minutes.
            say(f"copying outputs from {self.vm}")
            with open(f"{out_dir}/.outputs.tar.zst", "wb") as f:
                self.ssh(f"set -o pipefail; tar -C {self.REMOTE}/out -cf - . | zstd -q -T0 -3", timeout=900, stdout=f)
            run(["tar", "-C", out_dir, "--zstd", "-xf", f"{out_dir}/.outputs.tar.zst"], timeout=300)
            os.remove(f"{out_dir}/.outputs.tar.zst")
        if code != 0:
            raise RuntimeError(f"testbed on {self.vm} exited {code}; see {out_dir}/testbed.log")


def _bytes_file(data):
    f = tempfile.TemporaryFile()
    f.write(data)
    f.seek(0)
    return f


TESTBEDS = {"docker": PrometheusDocker, "prombench": PrombenchVM}


def main():
    """Runs a testbed here; used by PrombenchVM on the VM"""
    ap = argparse.ArgumentParser()
    ap.add_argument("testbed", choices=["prombench"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--stacklog")
    ap.add_argument("--matchfile")
    ap.add_argument("--files")
    ap.add_argument("--wait", type=int, default=240)
    ap.add_argument("--argmatch-arg", action="append", default=[])
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    Prombench(wait=a.wait, argmatch_args=dict(kv.split("=", 1) for kv in a.argmatch_arg)).run(
        os.path.abspath(a.out), os.path.abspath(a.config), a.stacklog and os.path.abspath(a.stacklog),
        a.matchfile and os.path.abspath(a.matchfile), a.files and os.path.abspath(a.files))


if __name__ == "__main__":
    main()
