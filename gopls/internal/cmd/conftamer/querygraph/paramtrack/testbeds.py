"""Testbeds for paramtrack's dynamic phase: each builds the module with argmatch and contexttrack, runs it with the
concrete config under a workload, and collects contexttrack's events and argmatch's matches. See README.md.

Also run on the VM by PrombenchVM: testbeds.py prombench-nginxingress-prom|prombench-caddyICingress-caddyIC --out DIR --config FILE
[--stacklog FILE --matchfile FILE]
"""

import argparse
import base64
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import common
from common import CT, log_command, popen, run, say, source_version

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "testbeds", "prombench"))
import upstream  # noqa: E402

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


class ArgmatchImage(Testbed):
    """Builds the module under argmatch and contexttrack from DOCKERFILE, with $CT as the build context. The module's
    checkout is $CT/SRC."""

    DOCKERFILE = None
    SRC = None
    BUILD_ARGS = {}

    def build(self, tag, out_dir, stacklog, matchfile):
        upstream.ensure_builder(out_dir)  # the image the Dockerfile builds FROM
        # What the image is built from: the module checkout on the machine that builds it (the VM's, for the prombench
        # testbeds, which may differ from the checkout the graph was built from: settings.json's module_source)
        src = source_version(f"{CT}/{self.SRC}")
        with open(f"{out_dir}/build_source.json", "w") as f:
            json.dump({"checkout": f"{CT}/{self.SRC}", **(src or {})}, f, indent=2)
        say(f"building from {CT}/{self.SRC} at {(src or {}).get('describe')}")
        if stacklog:
            overrides = {"STACKLOG": self.in_build_context(stacklog, "paramtrack-stacklog.txt"),
                         "MATCHFILE": self.in_build_context(matchfile, "paramtrack-matches.txt")}
        else:
            # argmatch needs -func or -stacklog: name a function that doesn't exist
            overrides = {"FUNC": "main.paramtrackNone", "MATCH": "paramtrack-none"}
        a = {**self.argmatch_args, **overrides}
        cmd = ["docker", "build", "--progress=plain", "-f", self.DOCKERFILE]
        for k, v in sorted(self.BUILD_ARGS.items()):
            cmd += ["--build-arg", f"{k}={v}"]
        for k, v in sorted(a.items()):
            cmd += ["--build-arg", f"ARGMATCH_{k}={v}"]
        say(f"building {tag} (about 5 minutes if what argmatch instruments changed)")
        run(cmd + ["-t", tag, "."], cwd=CT, log=f"{out_dir}/build.log", timeout=2400)

    def in_build_context(self, path, name):
        """Copies a file into the module's checkout, which the Dockerfile copies to /src; returns its path there.
        (The build reads the stack log and match file at build time.)"""
        shutil.copy(path, f"{CT}/{self.SRC}/{name}")
        return f"/src/{name}"

    def summarize(self, out_dir, what, log):
        if os.path.getsize(f"{out_dir}/events.jsonl") == 0:
            raise RuntimeError(f"no events in {out_dir}/events.jsonl; see {log}")
        n_events = sum(1 for _ in open(f"{out_dir}/events.jsonl"))
        n_calls = max(0, sum(1 for _ in open(f"{out_dir}/matches.calls.csv")) - 1)
        say(f"{what}: {n_events} events; {n_calls} lines of argmatch calls")


class PrometheusImage(ArgmatchImage):
    """Prometheus, from prometheus/prombench-argmatch.Dockerfile"""

    VERSION = "v3.2.1"
    DOCKERFILE = f"{CT}/prometheus/prombench-argmatch.Dockerfile"
    SRC = "prometheus"
    BUILD_ARGS = {"VERSION": VERSION}


class CaddyIngressImage(ArgmatchImage):
    """Caddy's ingress controller, from modules/caddy/argmatch.Dockerfile"""

    DOCKERFILE = f"{HERE}/modules/caddy/argmatch.Dockerfile"
    SRC = "caddyserver-ingress"


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
                log_command(["docker", "logs", name])
                subprocess.run(["docker", "logs", name], stdout=f, stderr=subprocess.STDOUT, timeout=60)
            log_command(["docker", "rm", "-f", name])
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
    """prombench's prometheus-meta, in the kind cluster on the machine this runs on (testbeds/prombench/upstream.py
    deploys it if the cluster doesn't exist). Ported from dump_ancestors/argmatch/experiment/run_on_vm.sh: builds
    prometheus-conftamer:v3.2.1 (the image the deployment runs), loads it into the main node, sets the prometheus-meta
    config map to the concrete config and CONFTAMER_EVENTS on the deployment, and restarts prometheus-meta. Once it's
    ready, a simulated person views Grafana's dashboards until the capture (the experiment's view_dashboards.py), and
    30 s later the experiment's reload_on_vm.sh reloads a changed config and then the concrete config again. Stops
    after `wait` seconds from the restart."""

    CTX = upstream.CTX
    VARIANT = "nginx"
    TAG = "prometheus-conftamer:v3.2.1"
    OUTDIR = upstream.ARGMATCH_OUT  # mounted at /argmatch-out in prometheus-meta
    CONFIG_DIR = "/etc/prometheus/config"  # where prometheus-meta mounts its ConfigMap
    EXPERIMENT = f"{CT}/dump_ancestors/argmatch/experiment"
    RELOAD_AFTER = 30

    def k(self, *args, timeout=120, log=None):
        return run(["kubectl", "--context", self.CTX, *args], timeout=timeout, log=log)

    def restart(self, config, files=None):
        """Restarts prometheus-meta with the config, the files it refers to (added to the ConfigMap, so they appear in
        CONFIG_DIR), CONFTAMER_EVENTS set and no earlier outputs. Scaling to 0 first means the old pod is gone (no more
        writes, TSDB unlocked) before the new one starts."""
        self.k("scale", "deploy/prometheus-meta", "--replicas=0")
        cmd = ["kubectl", "--context", self.CTX, "wait", "--for=delete", "pod", "-l", "app=prometheus-meta",
               "--timeout=600s"]
        log_command(cmd)
        subprocess.run(cmd, capture_output=True, timeout=660)
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
        deploy_or_load(self.VARIANT, self.TAG, out_dir)
        self.restart(config, files)
        start = time.time()
        helpers = []  # background processes, stopped at the capture
        ready = reload = None
        try:
            while (t := time.time() - start) < self.wait:
                if ready is None and self.exec_wget("/-/ready"):
                    ready = t
                    say(f"ready after {t:.0f}s")
                    pf = popen(["kubectl", "--context", self.CTX, "port-forward", "svc/grafana", "3000:80"],
                                          stdout=open(f"{out_dir}/port-forward.log", "w"), stderr=subprocess.STDOUT)
                    time.sleep(2)
                    viewer = popen(
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
                    reload = popen(["bash", f"{self.EXPERIMENT}/reload_on_vm.sh", f"{out_dir}/reload.log"])
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


def deploy_or_load(variant, tag, out_dir):
    """Loads the module's image into the prombench cluster, or deploys the cluster (with the image) if there's none or
    it's the other variant"""
    have = upstream.cluster_variant()
    if have == variant:
        upstream.load(tag)
    else:
        say(f"prombench cluster: {have or 'none'}; deploying the {variant} variant (about 5 minutes)")
        upstream.deploy(variant, tag, f"{out_dir}/deploy")  # (loads the image)


class PrombenchCaddy(CaddyIngressImage):
    """Caddy's ingress controller in prombench (upstream.py's caddy variant: it replaces ingress-nginx), in the kind
    cluster on the machine this runs on. The concrete config is Caddy's JSON config, which the controller otherwise
    generates from the cluster's Ingresses and its ConfigMap. Builds the controller (caddy-ingress-conftamer:latest,
    which the deployment runs), deploys the cluster or loads the image, and restarts the controller with
    CONFTAMER_EVENTS set. Once the controller has loaded its own config, loads the concrete config in its place
    through Caddy's admin API (POST /load, which validates and provisions it and rolls back on failure); the
    controller doesn't reload while its generated config is unchanged, and the run fails if the running config
    isn't the concrete config at the capture. The workload is Prombench's, through the ingress: the dashboard viewer
    reaches Grafana through the controller's service, the loadgen querier queries the benchmark Prometheus instances
    through it, and `RELOAD_AFTER` seconds after the config is loaded, it's loaded again (forced: Caddy skips an
    unchanged config). Stops `wait` seconds after the concrete config is loaded (not after the restart, as Prombench
    does: under argmatch, loading a large config can take minutes, which would otherwise shorten the workload)."""

    CTX = upstream.CTX
    VARIANT = "caddy"
    TAG = "caddy-ingress-conftamer:latest"
    NS = "caddy-system"
    DEPLOY = "deploy/caddy-caddy-ingress-controller"
    SERVICE = "svc/caddy-caddy-ingress-controller"
    LABEL = "app.kubernetes.io/name=caddy-ingress-controller"
    CONFIG_DIR = "/tmp/paramtrack-files"  # where the config's files go in the controller's container (emptyDir /tmp)
    CONFIG_IN_POD = "/tmp/paramtrack-config.json"
    OUTDIR = upstream.ARGMATCH_OUT
    EXPERIMENT = Prombench.EXPERIMENT
    RELOAD_AFTER = 30

    def k(self, *args, timeout=120, log=None):
        return run(["kubectl", "--context", self.CTX, "-n", self.NS, *args], timeout=timeout, log=log)

    def exec(self, *cmd, stdin=None, timeout=60):
        full = ["kubectl", "--context", self.CTX, "-n", self.NS, "exec", *(["-i"] if stdin else []), self.DEPLOY,
                "--", *cmd]
        log_command(full + (["<", stdin.name] if stdin else []))
        return subprocess.run(full, stdin=stdin, capture_output=True, text=stdin is None, timeout=timeout)

    def restart(self):
        """Restarts the controller with CONFTAMER_EVENTS set and no earlier outputs; returns once it's ready"""
        self.k("scale", self.DEPLOY, "--replicas=0")
        cmd = ["kubectl", "--context", self.CTX, "-n", self.NS, "wait", "--for=delete", "pod", "-l", self.LABEL,
               "--timeout=300s"]
        log_command(cmd)
        subprocess.run(cmd, capture_output=True, timeout=360)
        self.k("set", "env", self.DEPLOY, "CONFTAMER_EVENTS=/argmatch-out/events.jsonl")
        run(["sudo", "rm", "-f"] + [f"{self.OUTDIR}/{f}" for f in OUTPUTS], timeout=60)
        # The controller runs as root but drops every capability, so it can only write to a directory any user can
        run(["sudo", "chmod", "777", self.OUTDIR], timeout=60)
        self.k("scale", self.DEPLOY, "--replicas=1")
        self.k("rollout", "status", self.DEPLOY, "--timeout=600s", timeout=660)
        say("controller restarted")

    def copy_in(self, path, dest):
        """Copies a local file into the controller's container (kubectl cp: `kubectl exec -i ... cat > file` sometimes
        truncates stdin at 32 or 64 KiB), and checks its md5 there"""
        pod = self.k("get", "pod", "-l", self.LABEL, "-o", "jsonpath={.items[0].metadata.name}").strip()
        self.k("cp", path, f"{pod}:{dest}", timeout=300)
        want = hashlib.md5(open(path, "rb").read()).hexdigest()
        p = self.exec("md5sum", dest)
        if p.returncode != 0 or p.stdout.split()[:1] != [want]:
            raise RuntimeError(f"copying {path} into the controller failed: md5 {p.stdout.strip()} {p.stderr[-300:]}, "
                               f"want {want}")

    def install(self, config, files):
        """Copies the config and its files into the controller's container"""
        self.exec("mkdir", "-p", self.CONFIG_DIR)
        for n in sorted(os.listdir(files)) if files else []:
            self.copy_in(f"{files}/{n}", f"{self.CONFIG_DIR}/{n}")
        self.copy_in(config, self.CONFIG_IN_POD)

    @staticmethod
    def admin_address(cfg):
        """Where the admin API listens, per a Caddy config (default localhost:2019, which is 127.0.0.1's)"""
        listen = (cfg.get("admin") or {}).get("listen") or "127.0.0.1:2019"
        if listen.startswith("unix/"):
            raise RuntimeError(f"the config's admin API listens on a unix socket ({listen}), which the testbed can't "
                               "reach with wget")
        # (busybox wget tries localhost's ::1 first; Caddy's default listens on 127.0.0.1 only)
        return listen.removeprefix("tcp/").replace("localhost:", "127.0.0.1:")

    def running_config(self, admin):
        p = self.exec("wget", "-qO-", f"http://{admin}/config/")
        return json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else None

    def load_config(self, admin, force=False):
        """POSTs the config in the container to the admin API's /load; returns (ok, response)"""
        p = self.exec("wget", "-qO-", "-S", "--header", "Content-Type: application/json",
                      *(["--header", "Cache-Control: must-revalidate"] if force else []),
                      f"--post-file={self.CONFIG_IN_POD}", f"http://{admin}/load",
                      timeout=1200)  # (under argmatch, provisioning a large config can take minutes)
        return p.returncode == 0, (p.stdout + p.stderr).strip()

    def run(self, out_dir, config, stacklog=None, matchfile=None, files=None):
        cfg = json.load(open(config))
        self.build(self.TAG, out_dir, stacklog, matchfile)
        deploy_or_load(self.VARIANT, self.TAG, out_dir)
        self.restart()
        start = time.time()
        helpers = []  # background processes, stopped at the capture
        loaded = reloaded = None
        try:
            # The controller's own config first (else it could replace the concrete config when it loads it)
            while True:
                running = self.running_config("127.0.0.1:2019")
                if running and running.get("apps"):
                    break
                if time.time() - start > 300:
                    raise RuntimeError("the controller hasn't loaded its own config 300s after the restart")
                time.sleep(5)
            open(f"{out_dir}/generated-config.json", "w").write(json.dumps(running, indent=2) + "\n")
            self.install(config, files)
            ok, resp = self.load_config("127.0.0.1:2019")
            open(f"{out_dir}/load.log", "w").write(resp + "\n")
            if not ok:
                raise RuntimeError(f"the admin API rejected the concrete config (see {out_dir}/load.log): "
                                   f"{resp[-1500:]}")
            admin = self.admin_address(cfg)
            loaded = time.time() - start
            say(f"concrete config loaded {loaded:.0f}s after the restart")
            pf = popen(["kubectl", "--context", self.CTX, "-n", self.NS, "port-forward", self.SERVICE, "3000:80"],
                       stdout=open(f"{out_dir}/port-forward.log", "w"), stderr=subprocess.STDOUT)
            time.sleep(2)
            viewer = popen([sys.executable, f"{self.EXPERIMENT}/view_dashboards.py", "--url",
                            "http://localhost:3000/grafana", "--duration", str(self.wait)],
                           stdout=open(f"{out_dir}/viewer.log", "w"), stderr=subprocess.STDOUT)
            helpers += [pf, viewer]
            while (t := time.time() - start) < loaded + self.wait:
                if reloaded is None and t >= loaded + self.RELOAD_AFTER:
                    # Check early that contexttrack is recording
                    events = f"{self.OUTDIR}/events.jsonl"
                    if not os.path.exists(events) or os.path.getsize(events) == 0:
                        raise RuntimeError(f"no events {t:.0f}s after the restart (is CONFTAMER_EVENTS set?)")
                    ok, resp = self.load_config(admin, force=True)
                    open(f"{out_dir}/reload.log", "w").write(resp + "\n")
                    reloaded = t
                    say("config reloaded" if ok else f"config reload failed; see {out_dir}/reload.log")
                time.sleep(5)
            say(f"capturing after {time.time() - start:.0f}s ({time.time() - start - loaded:.0f}s after the load)")
            for f in OUTPUTS:
                if os.path.exists(f"{self.OUTDIR}/{f}"):
                    run(["sudo", "cp", f"{self.OUTDIR}/{f}", f"{out_dir}/{f}"], timeout=120)
                    run(["sudo", "chown", f"{os.getuid()}:{os.getgid()}", f"{out_dir}/{f}"], timeout=60)
                else:
                    open(f"{out_dir}/{f}", "w").close()
            running = self.running_config(admin)
            if running != cfg:
                if running is not None:
                    open(f"{out_dir}/running-config.json", "w").write(json.dumps(running, indent=2) + "\n")
                raise RuntimeError(f"the running config isn't the concrete config at the capture (see "
                                   f"{out_dir}/running-config.json): did the controller replace it?")
        finally:
            for p in helpers:
                p.terminate()
            self.k("logs", "--timestamps", self.DEPLOY, log=f"{out_dir}/pod.log", timeout=300)
        self.summarize(out_dir, "caddy ingress controller", f"{out_dir}/pod.log")


class PrombenchVM(Testbed):
    """Prombench on a GCE VM (--vm): copies this code and the run's inputs there, runs the Prombench testbed on it, and
    copies the outputs back. The VM needs what testbeds/prombench/vm_setup.sh installs and clones, at the same paths
    as here."""

    ZONE = "us-central1-b"
    PROJECT = "conftamer"
    REMOTE = "/tmp/paramtrack-testbed"
    TESTBED = "prombench-nginxingress-prom"  # the testbed it runs there (main's choices)
    DOCKERFILES = []  # Dockerfiles under HERE that the testbed builds

    POLL = 30  # seconds between checks on the run
    GIVE_UP = 1200  # seconds of failed ssh calls before giving up

    def ssh(self, command, timeout, stdin=None, stdout=None, log=True):
        """Runs command on the VM, retrying while ssh itself fails (exit 255, e.g. the network dropped) for up to
        GIVE_UP seconds. stdin and stdout are files (stdin is rewound for each try). log=False leaves it out of the
        commands log (for polls)."""
        cmd = ["gcloud", "compute", "ssh", "--zone", self.ZONE, self.vm, "--project", self.PROJECT, "--command",
               command, "--", "-o", "ServerAliveInterval=30", "-o", "ConnectTimeout=30"]
        if log:
            log_command(cmd)
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
        config_name = "config" + os.path.splitext(config)[1]
        inputs = {"testbeds.py": f"{here}/testbeds.py", "common.py": f"{here}/common.py", config_name: config,
                  "testbeds/prombench/upstream.py": f"{here}/testbeds/prombench/upstream.py"}
        for d in ("patches", "patches-caddy"):
            for p in sorted(os.listdir(f"{here}/testbeds/prombench/{d}")):
                inputs[f"testbeds/prombench/{d}/{p}"] = f"{here}/testbeds/prombench/{d}/{p}"
        for d in self.DOCKERFILES:
            inputs[d] = f"{here}/{d}"
        if stacklog:
            inputs |= {"stacklog.txt": stacklog, "matches.txt": matchfile}
        with tempfile.TemporaryDirectory() as tmp:
            for name, path in inputs.items():
                os.makedirs(os.path.dirname(f"{tmp}/{name}"), exist_ok=True)
                shutil.copy(path, f"{tmp}/{name}")
            if files:
                shutil.copytree(files, f"{tmp}/files")
            log_command(["tar", "-C", tmp, "-cf", "-", "."] + [f"# {n} = {p}" for n, p in sorted(inputs.items())]
                        + ([f"# files = {files}"] if files else []))
            tar = subprocess.run(["tar", "-C", tmp, "-cf", "-", "."], capture_output=True, check=True).stdout
        say(f"copying inputs to {self.vm}")
        self.ssh(f"rm -rf {self.REMOTE} && mkdir -p {self.REMOTE}/out && tar -C {self.REMOTE} -xf -", timeout=300,
                 stdin=_bytes_file(tar))
        args = ["python3", "testbeds.py", self.TESTBED, "--out", "out", "--config", config_name, "--wait",
                str(self.wait)]
        if stacklog:
            args += ["--stacklog", "stacklog.txt", "--matchfile", "matches.txt"]
        if files:
            args += ["--files", "files"]
        for k, v in self.argmatch_args.items():
            if ARGMATCH_ARGS.get(k) != v:
                args += ["--argmatch-arg", f"{k}={v}"]
        # Start the run detached on the VM, so that it survives ssh dropping; it writes out/testbed.log and, when it
        # exits, out/exit. Builds (up to 40 min each: the builder image if missing, then the module's) + deploying
        # (if there's no cluster, about 5 min) or loading + up to 10 min to be ready + up to 20 min to load the config
        # (PrombenchCaddy) + the wait + the capture.
        limit = 2400 + 2400 + 3600 + 600 + 1200 + self.wait + 900
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
                               f"echo; echo \"@@exit $(cat exit 2>/dev/null)\"", timeout=120, log=False)
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


class PrombenchCaddyVM(PrombenchVM):
    """PrombenchCaddy on the GCE VM (--vm), as PrombenchVM. The VM also needs the caddyserver-ingress checkout."""

    TESTBED = "prombench-caddyICingress-caddyIC"
    DOCKERFILES = ["modules/caddy/argmatch.Dockerfile"]


# The prombench testbeds are named <environment>-<ingress controller>ingress-<module> (testbeds/prombench/README.md)
TESTBEDS = {"docker": PrometheusDocker, "prombench-nginxingress-prom": PrombenchVM, "prombench-caddyICingress-caddyIC": PrombenchCaddyVM}
ON_VM = {"prombench-nginxingress-prom": Prombench, "prombench-caddyICingress-caddyIC": PrombenchCaddy}
# Earlier names (2026-11-13), as runs before then recorded them in testbed.json
RENAMED = {"prombench": "prombench-nginxingress-prom", "prombench-caddy": "prombench-caddyICingress-caddyIC"}


def canonical(name):
    """A testbed's current name, for a name a run may have recorded"""
    return RENAMED.get(name, name)


def main():
    """Runs a testbed here; used by PrombenchVM on the VM"""
    ap = argparse.ArgumentParser()
    ap.add_argument("testbed", choices=sorted(ON_VM))
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--stacklog")
    ap.add_argument("--matchfile")
    ap.add_argument("--files")
    ap.add_argument("--wait", type=int, default=240)
    ap.add_argument("--argmatch-arg", action="append", default=[])
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    # (PrombenchVM copies out/ back into the step's directory, next to its commands.log)
    common.COMMANDS_LOG = os.path.abspath(f"{a.out}/vm-commands.log")
    log_command(sys.argv)
    ON_VM[a.testbed](wait=a.wait, argmatch_args=dict(kv.split("=", 1) for kv in a.argmatch_arg)).run(
        os.path.abspath(a.out), os.path.abspath(a.config), a.stacklog and os.path.abspath(a.stacklog),
        a.matchfile and os.path.abspath(a.matchfile), a.files and os.path.abspath(a.files))


if __name__ == "__main__":
    main()
