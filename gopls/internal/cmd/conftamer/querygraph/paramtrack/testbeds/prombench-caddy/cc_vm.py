#!/usr/bin/env python3
"""Runs on the prombench VM: apply and check concrete configs (Caddy JSON) on Caddy's ingress controller (TESTBED.md
next to this file). Loads configs as paramtrack's PrombenchCaddy testbed does: through Caddy's admin API, POST /load,
which validates and provisions the config and keeps the running one if that fails.

  cc_vm.py files <dir>              copy each file in dir into the controller's container, at /tmp/paramtrack-files/<name>
  cc_vm.py apply <config>           load config (forced, even if unchanged); exit 1 unless Caddy accepted it and runs it
  cc_vm.py restart <config> [dir]   restart the controller, wait until it loaded its own config, then copy dir's files
                                    (they don't survive a restart) and load config
  cc_vm.py shipped                  restart the controller and print the config it generates (the shipped config)
  cc_vm.py running                  print the running config
  cc_vm.py health                   the routes through Caddy, its metrics, and pods with restarts or not running
"""
import hashlib
import json
import os
import subprocess
import sys
import time

CTX = "kind-prombench"
NS = "caddy-system"
DEPLOY = "deploy/caddy-caddy-ingress-controller"
LABEL = "app.kubernetes.io/name=caddy-ingress-controller"
FILES = "/tmp/paramtrack-files"
CONFIG_IN_POD = "/tmp/paramtrack-config.json"
PR = "16075"


def k(*args, timeout=120, check=True, stdin=None, ns=True):
    p = subprocess.run(["kubectl", "--context", CTX, *(["-n", NS] if ns else []), *args], capture_output=True,
                       timeout=timeout, input=stdin)
    if check and p.returncode != 0:
        sys.exit(f"kubectl {' '.join(args)[:200]}: exit {p.returncode}\n{p.stderr.decode()[-3000:]}")
    return p


def pod_exec(*cmd, timeout=60, check=True, stdin=None):
    return k("exec", *(["-i"] if stdin is not None else []), DEPLOY, "--", *cmd, timeout=timeout, check=check,
             stdin=stdin)


def admin(cfg):
    listen = (cfg.get("admin") or {}).get("listen") or "localhost:2019"
    # (busybox wget tries localhost's ::1 first; Caddy's default listens on 127.0.0.1 only)
    return listen.removeprefix("tcp/").replace("localhost:", "127.0.0.1:")


def running(addr="127.0.0.1:2019"):
    p = pod_exec("wget", "-qO-", f"http://{addr}/config/", check=False)
    out = p.stdout.decode().strip()
    return json.loads(out) if p.returncode == 0 and out else None


def copy_in(path, dest):
    """Copies a local file into the controller's container (kubectl cp: `kubectl exec -i ... cat > file` sometimes
    truncates stdin at 32 or 64 KiB), and checks its md5 there"""
    pod = k("get", "pod", "-l", LABEL, "-o", "jsonpath={.items[0].metadata.name}").stdout.decode().strip()
    k("cp", path, f"{pod}:{dest}", timeout=300)
    want = hashlib.md5(open(path, "rb").read()).hexdigest()
    got = pod_exec("md5sum", dest, check=False).stdout.decode().split()[:1]
    if got != [want]:
        sys.exit(f"copying {path} into the controller failed: md5 {got}, want {want}")


def copy_files(d):
    pod_exec("mkdir", "-p", FILES)
    for n in sorted(os.listdir(d)):
        copy_in(os.path.join(d, n), f"{FILES}/{n}")
    print(f"copied {len(os.listdir(d))} files to {FILES}")


def load(path, addr="127.0.0.1:2019"):
    """Loads the config at path; exits 1 unless Caddy accepted it and runs it"""
    cfg = json.load(open(path))
    copy_in(path, CONFIG_IN_POD)
    p = pod_exec("wget", "-qO-", "-S", "--header", "Content-Type: application/json", "--header",
                 "Cache-Control: must-revalidate", f"--post-file={CONFIG_IN_POD}", f"http://{addr}/load",
                 check=False, timeout=300)
    print(f"load: exit {p.returncode}\n{(p.stdout + p.stderr).decode().strip()[-3000:]}")
    if p.returncode != 0:
        sys.exit(1)
    if running(admin(cfg)) != cfg:
        sys.exit("Caddy accepted the config, but the running config differs from it")
    print("running the config")


def restart():
    k("scale", DEPLOY, "--replicas=0")
    k("wait", "--for=delete", "pod", "-l", LABEL, "--timeout=300s", timeout=360, check=False)
    k("scale", DEPLOY, "--replicas=1")
    k("rollout", "status", DEPLOY, "--timeout=600s", timeout=660)
    start = time.time()
    while time.time() - start < 300:
        cfg = running()
        if cfg and cfg.get("apps"):
            print(f"controller loaded its own config after {time.time() - start:.0f}s", file=sys.stderr)
            return cfg
        time.sleep(5)
    sys.exit("the controller hasn't loaded its own config after 300s")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "files":
        copy_files(args[0])
    elif cmd == "apply":
        load(args[0], admin(running() or {}))
    elif cmd == "restart":
        restart()
        if len(args) > 1:
            copy_files(args[1])
        load(args[0])
    elif cmd == "shipped":
        print(json.dumps(restart(), indent=2))
    elif cmd == "running":
        print(json.dumps(running(), indent=2))
    elif cmd == "health":
        # The ingress routes, through Caddy (port 80 in its container)
        for path in ["/grafana/api/health", "/prometheus-meta/-/ready", f"/{PR}/prometheus-pr/-/ready",
                     f"/{PR}/prometheus-release/-/ready"]:
            p = pod_exec("wget", "-qO-", "-S", f"http://localhost:80{path}", check=False, timeout=60)
            status = [line.strip() for line in p.stderr.decode().splitlines() if line.strip().startswith("HTTP/")]
            print("route", path, status[-1] if status else f"exit {p.returncode}")
        metrics = pod_exec("wget", "-qO-", "localhost:9765/metrics", check=False).stdout.decode()
        for line in metrics.splitlines():
            if line.startswith(("caddy_http_requests_total", "caddy_reverse_proxy_upstreams_healthy",
                                "caddy_config_last_reload_successful", "caddy_admin_http_requests_total")):
                print(line)
        pods = json.loads(k("get", "pods", "-A", "-o", "json", ns=False).stdout)
        for p in pods["items"]:
            for c in p["status"].get("containerStatuses", []):
                if c["restartCount"] or not (c["ready"] or p["status"]["phase"] == "Succeeded"):
                    print("pod", p["metadata"]["namespace"], p["metadata"]["name"], c["name"], "restarts",
                          c["restartCount"], "ready", c["ready"], p["status"]["phase"])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
