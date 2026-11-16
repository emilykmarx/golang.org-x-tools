"""paramtrack's testbed for its dynamic phase: prombench, with Caddy's ingress controller in place of ingress-nginx, in
a kind cluster on a GCE VM. Each module in prombench (COMPONENTS: Prometheus as prometheus-meta, Caddy's ingress
controller) runs either instrumented, if the run gives it a concrete config (built with argmatch and contexttrack, run
with the concrete config, its events and argmatch's matches collected), or stock (upstream's image, the shipped
config). See README.md.

Also run on the VM by PrombenchVM: testbeds.py --out DIR [--config MODULE=FILE [--files MODULE=DIR]
[--stacklog MODULE=FILE --matchfile MODULE=FILE] ...]
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

import common
from common import CT, log_command, popen, run, say, source_version

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "testbeds", "prombench"))
import upstream  # noqa: E402

# The testbed's name, as testbed.json records it
NAME = "prombench-caddyICingress"
# An instrumented module's outputs, in <out_dir>/<module>/
OUTPUTS = ("events.jsonl", "matches.csv", "matches.calls.csv")

# argmatch build args for both runs, as in argmatch-labels/bootstrap-example/bootstrap.sh: search all of each
# receiver (no libraries or types ignored, no time or memory caps), and spread each function's 30 checks out. A
# module's module.py can override some (its ARGMATCH_ARGS: argmatch_args). OUT is each module's (Component.build).
ARGMATCH_ARGS = {
    "MATCH": "", "MATCHFILE": "", "FUNC": "", "STACKLOG": "", "RECVONLY": "1", "DEPTH": "10", "MAXELEMS": "64",
    "MAXCALLS": "30", "IGNORELIBS": "false", "IGNORETYPES": "", "MAXMEM": "0", "MAXTIME": "0",
    "OUT": "/argmatch-out/matches.csv", "STATS": "true",
}



def argmatch_args(mod):
    """A module's argmatch build args: ARGMATCH_ARGS, with its module.py's ARGMATCH_ARGS over them"""
    return {**ARGMATCH_ARGS, **getattr(mod, "ARGMATCH_ARGS", {})}


EXPERIMENT = f"{CT}/dump_ancestors/argmatch/experiment"


class Component:
    """A module in prombench (its paramtrack module name: NAME), as one deployment. Instrumented if given a config:
    built from DOCKERFILE under argmatch and contexttrack (the checkout $CT/SRC is the build context's), run as TAG
    with CONFTAMER_EVENTS and argmatch's output in /argmatch-out/<NAME>/ (the host's upstream.ARGMATCH_OUT/<NAME>), and
    with the config. Otherwise stock: STOCK_IMAGE, with the shipped config. Its files go to <out_dir>/<NAME>/."""

    NAME = DOCKERFILE = SRC = TAG = STOCK_IMAGE = NS = DEPLOY = CONTAINER = LABEL = None
    BUILD_ARGS = {}
    CTX = upstream.CTX

    def __init__(self, out_dir, config=None, files=None, stacklog=None, matchfile=None, argmatch_args=None):
        self.argmatch_args = {**ARGMATCH_ARGS, **(argmatch_args or {})}
        self.out = f"{out_dir}/{self.NAME}"
        self.config, self.files, self.stacklog, self.matchfile = config, files, stacklog, matchfile
        self.instrumented = config is not None
        self.host_out = f"{upstream.ARGMATCH_OUT}/{self.NAME}"  # mounted at /argmatch-out in the container
        self.pod_out = f"/argmatch-out/{self.NAME}"
        os.makedirs(self.out, exist_ok=True)

    def what(self):
        return f"{self.NAME} ({'instrumented' if self.instrumented else 'stock'})"

    def k(self, *args, timeout=120, log=None):
        return run(["kubectl", "--context", self.CTX, "-n", self.NS, *args], timeout=timeout, log=log)

    def exec(self, *cmd, stdin=None, timeout=60):
        full = ["kubectl", "--context", self.CTX, "-n", self.NS, "exec", *(["-i"] if stdin else []), self.DEPLOY,
                "-c", self.CONTAINER, "--", *cmd]
        log_command(full + (["<", stdin.name] if stdin else []))
        return subprocess.run(full, stdin=stdin, capture_output=True, text=stdin is None, timeout=timeout)

    # -- build

    def build(self):
        upstream.ensure_builder(self.out)  # the image the Dockerfiles build FROM
        # What the image is built from: the module checkout on the machine that builds it (the VM's, which may differ
        # from the checkout the graph was built from: settings.json's module_source)
        src = source_version(f"{CT}/{self.SRC}")
        with open(f"{self.out}/build_source.json", "w") as f:
            json.dump({"checkout": f"{CT}/{self.SRC}", **(src or {})}, f, indent=2)
        say(f"{self.NAME}: building from {CT}/{self.SRC} at {(src or {}).get('describe')}")
        if self.stacklog:
            overrides = {"STACKLOG": self.in_build_context(self.stacklog, "paramtrack-stacklog.txt"),
                         "MATCHFILE": self.in_build_context(self.matchfile, "paramtrack-matches.txt")}
        else:
            # argmatch needs -func or -stacklog: name a function that doesn't exist
            overrides = {"FUNC": "main.paramtrackNone", "MATCH": "paramtrack-none"}
        a = {**self.argmatch_args, "OUT": f"{self.pod_out}/matches.csv", **overrides}
        cmd = ["docker", "build", "--progress=plain", "-f", self.DOCKERFILE]
        for k, v in sorted(self.BUILD_ARGS.items()):
            cmd += ["--build-arg", f"{k}={v}"]
        for k, v in sorted(a.items()):
            cmd += ["--build-arg", f"ARGMATCH_{k}={v}"]
        say(f"building {self.TAG} (about 5 minutes if what argmatch instruments changed)")
        run(cmd + ["-t", self.TAG, "."], cwd=CT, log=f"{self.out}/build.log", timeout=2400)

    def in_build_context(self, path, name):
        """Copies a file into the module's checkout, which the Dockerfile copies to /src; returns its path there.
        (The build reads the stack log and match file at build time.)"""
        shutil.copy(path, f"{CT}/{self.SRC}/{name}")
        return f"/src/{name}"

    # -- restart

    def stop(self):
        """Scales the deployment to 0 and waits for its pod to be gone (no more writes to its outputs)"""
        self.k("scale", self.DEPLOY, "--replicas=0")
        cmd = ["kubectl", "--context", self.CTX, "-n", self.NS, "wait", "--for=delete", "pod", "-l", self.LABEL,
               "--timeout=600s"]
        log_command(cmd)
        subprocess.run(cmd, capture_output=True, timeout=660)

    def configure(self):
        """While stopped: the image, CONFTAMER_EVENTS (instrumented) or none (stock), and no earlier outputs"""
        image = self.TAG if self.instrumented else self.STOCK_IMAGE
        self.k("set", "image", self.DEPLOY, f"{self.CONTAINER}={image}")
        if self.instrumented:
            self.k("set", "env", self.DEPLOY, "-c", self.CONTAINER, f"CONFTAMER_EVENTS={self.pod_out}/events.jsonl")
            # Writable by any user: Caddy's ingress controller runs as root but drops every capability
            run(["sudo", "mkdir", "-p", self.host_out], timeout=60)
            run(["sudo", "chmod", "777", self.host_out], timeout=60)
            run(["sudo", "rm", "-f"] + [f"{self.host_out}/{f}" for f in OUTPUTS], timeout=60)
        else:
            self.k("set", "env", self.DEPLOY, "-c", self.CONTAINER, "CONFTAMER_EVENTS-")

    def start(self):
        self.k("scale", self.DEPLOY, "--replicas=1")

    def ready(self):
        """Whether it's ready to take the workload (polled after start)"""
        raise NotImplementedError

    def apply_config(self):
        """Once ready: anything left to apply the config (Caddy's admin /load)"""

    # -- workload

    def check_events(self):
        events = f"{self.host_out}/events.jsonl"
        if not os.path.exists(events) or os.path.getsize(events) == 0:
            raise RuntimeError(f"{self.NAME}: no events in {events} (is CONFTAMER_EVENTS set?)")

    def reload(self):
        """Starts reloading the config (instrumented only), so the code that runs on a config change runs"""

    def finish_reload(self):
        """Waits for the reload to finish, if it's in the background"""

    # -- capture

    def capture(self):
        for f in OUTPUTS:
            if os.path.exists(f"{self.host_out}/{f}"):
                run(["sudo", "cp", f"{self.host_out}/{f}", f"{self.out}/{f}"], timeout=120)
                run(["sudo", "chown", f"{os.getuid()}:{os.getgid()}", f"{self.out}/{f}"], timeout=60)
            else:
                open(f"{self.out}/{f}", "w").close()

    def check_capture(self):
        """At the capture: fail if the module isn't running the concrete config anymore"""

    def logs(self):
        self.k("logs", "--timestamps", self.DEPLOY, "-c", self.CONTAINER, log=f"{self.out}/pod.log", timeout=300)

    def summarize(self):
        if os.path.getsize(f"{self.out}/events.jsonl") == 0:
            raise RuntimeError(f"no events in {self.out}/events.jsonl; see {self.out}/pod.log")
        n_events = sum(1 for _ in open(f"{self.out}/events.jsonl"))
        n_calls = max(0, sum(1 for _ in open(f"{self.out}/matches.calls.csv")) - 1)
        say(f"{self.NAME}: {n_events} events; {n_calls} lines of argmatch calls")


class PrometheusMeta(Component):
    """Prometheus as prombench's prometheus-meta, built from prometheus/prombench-argmatch.Dockerfile. The config is the
    prometheus-meta ConfigMap's prometheus.yaml, its files the ConfigMap's other keys (so they appear in CONFIG_DIR).
    Stock: shipped-config.yaml (upstream's ConfigMap at upstream.UPSTREAM_COMMIT). The reload: the experiment's
    reload_on_vm.sh, which reloads a changed config and then the concrete config again."""

    NAME = "prometheus"
    VERSION = "v3.2.1"
    DOCKERFILE = f"{CT}/prometheus/prombench-argmatch.Dockerfile"
    SRC = "prometheus"
    BUILD_ARGS = {"VERSION": VERSION}
    TAG = "prometheus-conftamer:v3.2.1"
    STOCK_IMAGE = "quay.io/prometheus/prometheus:v3-distroless"  # upstream's manifest's
    SHIPPED = f"{HERE}/testbeds/prombench/prometheus/shipped-config.yaml"
    NS, DEPLOY, CONTAINER, LABEL = "default", "deploy/prometheus-meta", "prometheus", "app=prometheus-meta"
    CONFIG_DIR = "/etc/prometheus/config"  # where prometheus-meta mounts its ConfigMap

    def configure(self):
        super().configure()
        config, files = (self.config, self.files) if self.instrumented else (self.SHIPPED, None)
        data, binary = {"prometheus.yaml": open(config).read()}, {}
        for name in sorted(os.listdir(files)) if files else []:
            content = open(f"{files}/{name}", "rb").read()
            try:
                data[name] = content.decode()
            except UnicodeDecodeError:
                binary[name] = base64.b64encode(content).decode()
        # Remove earlier runs' files (a merge patch sets the keys it names; null removes one)
        have = json.loads(self.k("get", "configmap", "prometheus-meta", "-o", "json"))
        patch = {"data": {**{k: None for k in have.get("data") or {}}, **data},
                 "binaryData": {**{k: None for k in have.get("binaryData") or {}}, **binary}}
        with tempfile.NamedTemporaryFile("w", suffix=".json") as f:  # (can be too long for a command line)
            json.dump(patch, f)
            f.flush()
            self.k("patch", "configmap", "prometheus-meta", "--type", "merge", "--patch-file", f.name)

    def ready(self):
        # Through the API server's service proxy (the stock image is distroless: no wget to exec)
        cmd = ["kubectl", "--context", self.CTX, "get", "--raw",
               "/api/v1/namespaces/default/services/prometheus-meta:prom-web/proxy/prometheus-meta/-/ready"]
        return subprocess.run(cmd, capture_output=True, timeout=60).returncode == 0

    def reload(self):
        self.reloader = popen(["bash", f"{EXPERIMENT}/reload_on_vm.sh", f"{self.out}/reload.log"])
        say(f"{self.NAME}: config reload started")

    def finish_reload(self):
        r = getattr(self, "reloader", None)
        if r is None:
            return
        # Let it finish, since it restores the config map; killing it restores it too (its EXIT trap)
        try:
            r.wait(timeout=300)
        except subprocess.TimeoutExpired:
            r.terminate()
            r.wait(timeout=60)
            say(f"{self.NAME}: config reloads not done after 300s; stopped them")
        if r.returncode not in (0, None):
            say(f"{self.NAME}: config reload failed; see {self.out}/reload.log")


class CaddyController(Component):
    """Caddy's ingress controller, built from modules/caddy/argmatch.Dockerfile. The config is Caddy's JSON config,
    which the controller otherwise generates from the cluster's Ingresses and its ConfigMap (stock: that generated
    config). Once the controller has loaded its own config (generated-config.json), the concrete config is loaded in
    its place through Caddy's admin API (POST /load, which validates and provisions it and rolls back on failure); the
    controller doesn't reload while its generated config is unchanged, and the run fails if the running config isn't
    the concrete config at the capture. Just before the load, every certificate Caddy has stored is made expired
    (expire_certificates), so the load renews the ones the config manages in every run. The reload: the concrete
    config loaded again (forced: Caddy skips an unchanged config)."""

    NAME = "caddy"
    DOCKERFILE = f"{HERE}/modules/caddy/argmatch.Dockerfile"
    SRC = "caddyserver-ingress"
    TAG = "caddy-ingress-conftamer:latest"
    STOCK_IMAGE = "caddy/ingress:v0.2.1"  # the checkout's Helm chart's
    NS, DEPLOY, CONTAINER = "caddy-system", "deploy/caddy-caddy-ingress-controller", "caddy-ingress-controller"
    LABEL = "app.kubernetes.io/name=caddy-ingress-controller"
    SERVICE = "svc/caddy-caddy-ingress-controller"
    CONFIG_DIR = "/tmp/paramtrack-files"  # where the config's files go in the controller's container (emptyDir /tmp)
    CONFIG_IN_POD = "/tmp/paramtrack-config.json"
    OWN_ADMIN = "127.0.0.1:2019"  # where the admin API listens under the controller's own config

    def copy_in(self, path, dest):
        """Copies a local file into the controller's container (kubectl cp: `kubectl exec -i ... cat > file` sometimes
        truncates stdin at 32 or 64 KiB), and checks its md5 there"""
        pod = self.k("get", "pod", "-l", self.LABEL, "-o", "jsonpath={.items[0].metadata.name}").strip()
        self.k("cp", "-c", self.CONTAINER, path, f"{pod}:{dest}", timeout=300)
        want = hashlib.md5(open(path, "rb").read()).hexdigest()
        p = self.exec("md5sum", dest)
        if p.returncode != 0 or p.stdout.split()[:1] != [want]:
            raise RuntimeError(f"copying {path} into the controller failed: md5 {p.stdout.strip()} {p.stderr[-300:]}, "
                               f"want {want}")

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

    def ready(self):
        """Once the controller's own config is loaded (else it could replace the concrete config when it loads it)"""
        running = self.running_config(self.OWN_ADMIN)
        if running and running.get("apps"):
            open(f"{self.out}/generated-config.json", "w").write(json.dumps(running, indent=2) + "\n")
            return True
        return False

    def expire_certificates(self, cfg):
        """Makes every certificate Caddy has stored expired, so that loading the config renews the ones it manages
        (otherwise whether a run renews them depends on when they were last issued: the internal CA's last 12 hours).
        Each stored leaf certificate is re-signed by the CA that issued it, with the same key, names and extensions and
        the same lifetime, ending an hour ago. Only certificates from CAs whose intermediate key Caddy stored (its
        internal issuer's CAs) can be re-signed; the rest are left alone. Writes expire-certificates.log."""
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        import datetime

        def storage_namespaces(v):
            if isinstance(v, dict):
                if v.get("module") == "secret_store":
                    yield next((x for k, x in v.items() if k.lower() == "namespace"), None) or self.NS
                for x in v.values():
                    yield from storage_namespaces(x)
            elif isinstance(v, list):
                for x in v:
                    yield from storage_namespaces(x)

        log = []
        for ns in sorted(set(storage_namespaces(cfg)) | {self.NS}):
            p = subprocess.run(["kubectl", "--context", self.CTX, "-n", ns, "get", "secrets", "-o", "json"],
                               capture_output=True, text=True, timeout=120)
            if p.returncode != 0:
                log.append(f"{ns}: can't list secrets: {p.stderr.strip()[-200:]}")
                continue
            stored = {s["metadata"]["name"]: base64.b64decode((s.get("data") or {}).get("value", ""))
                      for s in json.loads(p.stdout)["items"]}
            # The CAs whose intermediate certificate and key are stored, by the intermediate's subject
            cas = {}
            for name, value in stored.items():
                if name.startswith("caddy.ingress--pki.authorities.") and name.endswith(".intermediate.crt"):
                    key = stored.get(name[:-len(".crt")] + ".key")
                    if key:
                        crt = x509.load_pem_x509_certificate(value)
                        cas[crt.subject] = (crt, serialization.load_pem_private_key(key, None))
            for name, value in sorted(stored.items()):
                if not (name.startswith("caddy.ingress--certificates.") and name.endswith(".crt")):
                    continue
                leaf = x509.load_pem_x509_certificate(value)  # (the chain's first)
                if leaf.issuer not in cas:
                    log.append(f"{ns}/{name}: issuer {leaf.issuer.rfc4514_string()} isn't a stored CA; left alone")
                    continue
                ca, ca_key = cas[leaf.issuer]
                end = datetime.datetime.utcnow() - datetime.timedelta(hours=1)
                b = (x509.CertificateBuilder().subject_name(leaf.subject).issuer_name(leaf.issuer)
                     .public_key(leaf.public_key()).serial_number(x509.random_serial_number())
                     .not_valid_before(end - (leaf.not_valid_after - leaf.not_valid_before)).not_valid_after(end))
                for ext in leaf.extensions:
                    b = b.add_extension(ext.value, ext.critical)
                new = b.sign(ca_key, leaf.signature_hash_algorithm)
                pem = new.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM)
                with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
                    json.dump({"data": {"value": base64.b64encode(pem).decode()}}, f)
                    f.flush()
                    run(["kubectl", "--context", self.CTX, "-n", ns, "patch", "secret", name, "--type", "merge",
                         "--patch-file", f.name], timeout=60)
                log.append(f"{ns}/{name}: expired (was valid until {leaf.not_valid_after}Z; now {end:%Y-%m-%d %H:%M}Z)")
        open(f"{self.out}/expire-certificates.log", "w").write("\n".join(log) + "\n")
        n, alone = (sum(s in line for line in log) for s in (": expired (", "; left alone"))
        say(f"{self.NAME}: expired {n} stored certificates ({alone} left alone; see {self.out}/expire-certificates.log)")

    def apply_config(self):
        if not self.instrumented:
            return
        self.cfg = json.load(open(self.config))
        self.expire_certificates(self.cfg)
        self.exec("mkdir", "-p", self.CONFIG_DIR)
        for n in sorted(os.listdir(self.files)) if self.files else []:
            self.copy_in(f"{self.files}/{n}", f"{self.CONFIG_DIR}/{n}")
        self.copy_in(self.config, self.CONFIG_IN_POD)
        ok, resp = self.load_config(self.OWN_ADMIN)
        open(f"{self.out}/load.log", "w").write(resp + "\n")
        if not ok:
            raise RuntimeError(f"the admin API rejected the concrete config (see {self.out}/load.log): {resp[-1500:]}")
        self.admin = self.admin_address(self.cfg)
        say(f"{self.NAME}: concrete config loaded")

    def reload(self):
        ok, resp = self.load_config(self.admin, force=True)
        open(f"{self.out}/reload.log", "w").write(resp + "\n")
        say(f"{self.NAME}: config reloaded" if ok else f"{self.NAME}: config reload failed; see {self.out}/reload.log")

    def check_capture(self):
        running = self.running_config(self.admin)
        if running != self.cfg:
            if running is not None:
                open(f"{self.out}/running-config.json", "w").write(json.dumps(running, indent=2) + "\n")
            raise RuntimeError(f"the running config isn't the concrete config at the capture (see "
                               f"{self.out}/running-config.json): did the controller replace it?")


# The modules prombench runs, by paramtrack module name (modules/<name>/)
COMPONENTS = {c.NAME: c for c in (PrometheusMeta, CaddyController)}


class Prombench:
    """One run of prombench, in the kind cluster on the machine this runs on (upstream.py deploys it if there's none,
    or it's another variant). Builds the instrumented modules' images and loads them; restarts every module, the
    instrumented ones with their concrete configs and the rest stock; once all are ready (and Caddy's concrete config
    is loaded), runs the workload for `wait` seconds: a simulated person views Grafana's dashboards through Caddy's
    ingress (the experiment's view_dashboards.py), and the loadgen querier queries the benchmark Prometheus instances
    through it; `RELOAD_AFTER` seconds in, each instrumented module reloads its config. Then captures each
    instrumented module's outputs in <out_dir>/<module>/ (OUTPUTS, build_source.json, logs); every module's pod log
    is in <out_dir>/<module>/pod.log. (`wait` counts from when every config is loaded, not from the restart: under
    argmatch, loading a large config can take minutes, which would otherwise shorten the workload.)"""

    RELOAD_AFTER = 30
    READY_WITHIN = 600

    def __init__(self, wait=240):
        self.wait = wait

    def run(self, out_dir, modules):
        """modules: module name => {config, files, stacklog, matchfile, argmatch_args} for each instrumented module
        (the others run stock). With a stacklog, argmatch instruments its functions and searches for the strings in
        matchfile; without, it instruments nothing (contexttrack still records events). argmatch_args: the module's
        argmatch build args that differ from ARGMATCH_ARGS."""
        unknown = set(modules) - set(COMPONENTS)
        if unknown:
            raise RuntimeError(f"prombench doesn't run {', '.join(sorted(unknown))} (COMPONENTS: "
                               f"{', '.join(COMPONENTS)})")
        comps = [cls(out_dir, **modules.get(name, {})) for name, cls in COMPONENTS.items()]
        instrumented = [c for c in comps if c.instrumented]
        say("modules: " + ", ".join(c.what() for c in comps))
        for c in instrumented:
            c.build()
        have = upstream.cluster_variant()
        if have != "caddy":
            say(f"prombench cluster: {have or 'none'}; deploying the caddy variant (about 5 minutes)")
            upstream.deploy("caddy", f"{out_dir}/deploy")
        for c in instrumented:
            upstream.load(c.TAG)
        for c in comps:
            c.stop()
        for c in comps:
            c.configure()
            c.start()
        say("modules restarted")
        start = time.time()
        helpers = []  # background processes, stopped at the capture
        try:
            waiting = list(comps)
            while waiting:
                waiting = [c for c in waiting if not c.ready()]
                if waiting and time.time() - start > self.READY_WITHIN:
                    raise RuntimeError(f"not ready {self.READY_WITHIN}s after the restart: "
                                       f"{', '.join(c.NAME for c in waiting)} (see their pod logs)")
                if waiting:
                    time.sleep(5)
            say(f"ready after {time.time() - start:.0f}s")
            for c in comps:
                c.apply_config()
            loaded = time.time()
            say(f"configs loaded {loaded - start:.0f}s after the restart")
            pf = popen(["kubectl", "--context", upstream.CTX, "-n", CaddyController.NS, "port-forward",
                        CaddyController.SERVICE, "3000:80"],
                       stdout=open(f"{out_dir}/port-forward.log", "w"), stderr=subprocess.STDOUT)
            time.sleep(2)
            viewer = popen([sys.executable, f"{EXPERIMENT}/view_dashboards.py", "--url",
                            "http://localhost:3000/grafana", "--duration", str(self.wait)],
                           stdout=open(f"{out_dir}/viewer.log", "w"), stderr=subprocess.STDOUT)
            helpers += [pf, viewer]
            reloaded = False
            while (t := time.time() - loaded) < self.wait:
                if not reloaded and t >= self.RELOAD_AFTER:
                    for c in instrumented:
                        c.check_events()  # (early: is contexttrack recording?)
                    for c in instrumented:
                        c.reload()
                    reloaded = True
                time.sleep(5)
            say(f"capturing after {time.time() - loaded:.0f}s of workload")
            for c in instrumented:
                c.capture()
            for c in instrumented:
                c.check_capture()
        finally:
            for p in helpers:
                p.terminate()
            for c in instrumented:
                c.finish_reload()
            for c in comps:
                try:
                    c.logs()
                except RuntimeError as e:
                    say(f"{c.NAME}: no pod log: {e}")
        for c in instrumented:
            c.summarize()


class PrombenchVM:
    """Prombench on a GCE VM (`vm`): copies this code and the run's inputs there, runs Prombench on it (main), and
    copies the outputs back. The VM needs what testbeds/prombench/vm_setup.sh installs and clones, at the same paths
    as here."""

    ZONE = "us-central1-b"
    PROJECT = "conftamer"
    REMOTE = "/tmp/paramtrack-testbed"
    # Files under HERE that the testbed reads, besides its code and patches
    FILES = ["modules/caddy/argmatch.Dockerfile", "testbeds/prombench/prometheus/shipped-config.yaml"]

    POLL = 30  # seconds between checks on the run
    GIVE_UP = 1200  # seconds of failed ssh calls before giving up

    def __init__(self, wait=240, vm=None):
        self.wait = wait
        self.vm = vm

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

    def run(self, out_dir, modules):
        """As Prombench.run; out_dir gets the VM run's out/ (testbed.log, vm-commands.log, <module>/...)"""
        if not self.vm:
            raise RuntimeError("the prombench testbed needs --vm")
        inputs = {"testbeds.py": f"{HERE}/testbeds.py", "common.py": f"{HERE}/common.py",
                  "testbeds/prombench/upstream.py": f"{HERE}/testbeds/prombench/upstream.py"}
        for d in ("patches", "patches-caddy"):
            for p in sorted(os.listdir(f"{HERE}/testbeds/prombench/{d}")):
                inputs[f"testbeds/prombench/{d}/{p}"] = f"{HERE}/testbeds/prombench/{d}/{p}"
        for d in self.FILES:
            inputs[d] = f"{HERE}/{d}"
        args = ["python3", "testbeds.py", "--out", "out", "--wait", str(self.wait)]
        dirs = {}
        for name, m in sorted(modules.items()):
            for k, v in (m.get("argmatch_args") or {}).items():
                if ARGMATCH_ARGS.get(k) != v:
                    args += ["--argmatch-arg", f"{name}:{k}={v}"]
            config = f"inputs/{name}/config{os.path.splitext(m['config'])[1]}"
            inputs[config] = m["config"]
            args += ["--config", f"{name}={config}"]
            if m.get("stacklog"):
                inputs |= {f"inputs/{name}/stacklog.txt": m["stacklog"], f"inputs/{name}/matches.txt": m["matchfile"]}
                args += ["--stacklog", f"{name}=inputs/{name}/stacklog.txt",
                         "--matchfile", f"{name}=inputs/{name}/matches.txt"]
            if m.get("files"):
                dirs[f"inputs/{name}/files"] = m["files"]
                args += ["--files", f"{name}=inputs/{name}/files"]
        with tempfile.TemporaryDirectory() as tmp:
            for name, path in inputs.items():
                os.makedirs(os.path.dirname(f"{tmp}/{name}"), exist_ok=True)
                shutil.copy(path, f"{tmp}/{name}")
            for name, path in dirs.items():
                shutil.copytree(path, f"{tmp}/{name}")
            log_command(["tar", "-C", tmp, "-cf", "-", "."] +
                        [f"# {n} = {p}" for n, p in sorted({**inputs, **dirs}.items())])
            tar = subprocess.run(["tar", "-C", tmp, "-cf", "-", "."], capture_output=True, check=True).stdout
        say(f"copying inputs to {self.vm}")
        self.ssh(f"rm -rf {self.REMOTE} && mkdir -p {self.REMOTE}/out && tar -C {self.REMOTE} -xf -", timeout=300,
                 stdin=_bytes_file(tar))
        # Start the run detached on the VM, so that it survives ssh dropping; it writes out/testbed.log and, when it
        # exits, out/exit. The builder image if missing (up to 40 min) + each instrumented module's build (up to 40
        # min) + deploying (if there's no cluster, about 5 min) or loading + up to 10 min to be ready + up to 20 min to
        # load Caddy's config + the wait + the capture.
        limit = 2400 + 2400 * max(1, len(modules)) + 3600 + 600 + 1200 + self.wait + 900
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


# Earlier testbeds, as runs recorded them in testbed.json: each ran one module instrumented, the other stock (the same
# as this testbed with that module). The docker testbed and prombench-nginxingress-prom (earlier prombench), whose
# Prometheus ran behind ingress-nginx, were removed 2026-11-15.
RENAMED = {"prombench-caddy": ["caddy"], "prombench-caddyICingress-caddyIC": ["caddy"]}


def recorded(settings):
    """A testbed.json's settings in today's terms: an earlier testbed's name becomes NAME and its module; argmatch's
    args, recorded for all modules before 2026-11-15, become each module's"""
    if settings.get("testbed") in RENAMED:
        settings = {**settings, "modules": RENAMED[settings["testbed"]], "testbed": NAME}
    a = settings.get("argmatch_args")
    if a and all(isinstance(v, str) for v in a.values()):
        settings = {**settings, "argmatch_args": {m: a for m in settings.get("modules", [])}}
    return settings


def main():
    """Runs Prombench here; used by PrombenchVM on the VM"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    for flag in ("--config", "--files", "--stacklog", "--matchfile"):
        ap.add_argument(flag, action="append", default=[], metavar="MODULE=PATH")
    ap.add_argument("--wait", type=int, default=240)
    ap.add_argument("--argmatch-arg", action="append", default=[], metavar="MODULE:KEY=VALUE")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    # (PrombenchVM copies out/ back into the step's directory, next to its commands.log)
    common.COMMANDS_LOG = os.path.abspath(f"{a.out}/vm-commands.log")
    log_command(sys.argv)
    modules = {}
    for key, values in (("config", a.config), ("files", a.files), ("stacklog", a.stacklog),
                        ("matchfile", a.matchfile)):
        for v in values:
            name, _, path = v.partition("=")
            modules.setdefault(name, {})[key] = os.path.abspath(path)
    for v in a.argmatch_arg:
        name, _, kv = v.partition(":")
        k, _, val = kv.partition("=")
        modules.setdefault(name, {}).setdefault("argmatch_args", {})[k] = val
    if any("config" not in m for m in modules.values()):
        sys.exit("every module named needs a --config")
    Prombench(wait=a.wait).run(os.path.abspath(a.out), modules)


if __name__ == "__main__":
    main()
