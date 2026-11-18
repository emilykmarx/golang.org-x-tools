#!/usr/bin/env python3
"""Runs on the prombench VM: apply and check concrete configs on Alertmanager (the skill's TESTBED.md, next to this).

  cc_vm.py files <dir>          add each file in dir to the alertmanager-config ConfigMap (key = file name), wait for
                                the pod to have them (they appear in /etc/alertmanager/)
  cc_vm.py check <config>       put config in the ConfigMap as candidate.yml, wait for the pod, amtool check-config,
                                remove candidate.yml again
  cc_vm.py apply <config>       set alertmanager.yml, wait for the pod, POST /-/reload; exit 1 unless the reload succeeded
  cc_vm.py web <web config>     set web.yml (read on every request: no reload), wait for the pod, GET /-/ready
  cc_vm.py restart <config> [<web config>]
                                scale to 0, set alertmanager.yml (and web.yml, with --web.config.file; without one,
                                no web config), scale to 1, wait for /-/ready
  cc_vm.py health               the health numbers (reload status, alerts, notifications, pods with restarts or not
                                running)
  cc_vm.py rmkey <key>          remove a key from the ConfigMap
"""
import hashlib
import json
import os
import subprocess
import sys
import time

CTX = "kind-prombench"
NS, DEPLOY, CONTAINER, LABEL = "default", "deploy/alertmanager", "alertmanager", "app=alertmanager"
CM = "alertmanager-config"
DIR = "/etc/alertmanager"
BASE = "localhost:9093/alertmanager"  # (--web.external-url's path prefixes its routes)


def k(*args, timeout=120, check=True, stdin=None):
    p = subprocess.run(["kubectl", "--context", CTX, "-n", NS, *args], capture_output=True, text=True,
                       timeout=timeout, input=stdin)
    if check and p.returncode != 0:
        sys.exit(f"kubectl {' '.join(args)[:200]}: exit {p.returncode}\n{p.stderr[-3000:]}")
    return p


def pod_exec(*cmd, timeout=60, check=True):
    return k("exec", DEPLOY, "-c", CONTAINER, "--", *cmd, timeout=timeout, check=check)


def patch(data):
    k("patch", "configmap", CM, "--type", "merge", "--patch-file", "/dev/stdin", stdin=json.dumps({"data": data}))


def sync(want, limit=240):
    """Wait until the pod's copies of the ConfigMap keys in want ({key: content}) have that content"""
    start = time.time()
    while True:
        k("annotate", "pod", "-l", LABEL, f"ccfg-sync={time.time_ns()}", "--overwrite")
        sums = pod_exec("md5sum", *[f"{DIR}/{name}" for name in want], check=False).stdout
        have = {line.split()[1].rsplit("/", 1)[1]: line.split()[0] for line in sums.splitlines() if line.strip()}
        if all(have.get(n) == hashlib.md5(c.encode()).hexdigest() for n, c in want.items()):
            print(f"pod has the files after {time.time() - start:.0f}s", flush=True)
            return
        if time.time() - start > limit:
            sys.exit(f"pod's files not updated after {limit}s")
        time.sleep(3)


def get(path, check=False):
    return pod_exec("wget", "-qO-", f"{BASE}{path}", check=check)


def metric(name):
    return [line for line in get("/metrics", check=True).stdout.splitlines() if line.startswith(name)]


def set_flag(value):
    """Sets the container's --web.config.file argument, or removes it (value None)"""
    spec = json.loads(k("get", DEPLOY, "-o", "json").stdout)["spec"]["template"]["spec"]
    i = next(i for i, c in enumerate(spec["containers"]) if c["name"] == CONTAINER)
    args = [a for a in spec["containers"][i].get("args") or [] if not a.startswith("--web.config.file=")]
    args += [f"--web.config.file={value}"] if value else []
    k("patch", DEPLOY, "--type", "json", "-p",
      json.dumps([{"op": "add", "path": f"/spec/template/spec/containers/{i}/args", "value": args}]))


def wait_ready(limit=600):
    start = time.time()
    while time.time() - start < limit:
        if get("/-/ready").returncode == 0:
            print(f"ready after {time.time() - start:.0f}s")
            return
        time.sleep(5)
    sys.exit(f"not ready after {limit}s")


def main():
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "files":
        data = {n: open(os.path.join(args[0], n)).read() for n in sorted(os.listdir(args[0]))}
        patch(data)
        sync(data)
    elif cmd == "rmkey":
        k("patch", "configmap", CM, "--type", "json", "-p", json.dumps([{"op": "remove", "path": f"/data/{args[0]}"}]))
    elif cmd == "check":
        data = {"candidate.yml": open(args[0]).read()}
        patch(data)
        sync(data)
        p = pod_exec("amtool", "check-config", f"{DIR}/candidate.yml", check=False, timeout=120)
        print(p.stdout + p.stderr)
        # (a ConfigMap holds at most 1 MiB: a large candidate left there can stop the next apply)
        k("patch", "configmap", CM, "--type", "json", "-p", json.dumps([{"op": "remove", "path": "/data/candidate.yml"}]))
        sys.exit(p.returncode)
    elif cmd == "apply":
        data = {"alertmanager.yml": open(args[0]).read()}
        patch(data)
        sync(data)
        p = pod_exec("wget", "-qO-", "--post-data=", f"{BASE}/-/reload", check=False, timeout=120)
        print(f"reload: exit {p.returncode} {p.stdout.strip()} {p.stderr.strip()}")
        ok = metric("alertmanager_config_last_reload_successful ")
        print(ok)
        sys.exit(0 if p.returncode == 0 and ok and ok[0].split()[1] == "1" else 1)
    elif cmd == "web":
        data = {"web.yml": open(args[0]).read()}
        patch(data)
        sync(data)
        p = get("/-/ready")
        print(f"/-/ready: exit {p.returncode} {p.stdout.strip()} {p.stderr.strip()}")
        sys.exit(p.returncode)
    elif cmd == "restart":
        k("scale", DEPLOY, "--replicas=0")
        k("wait", "--for=delete", "pod", "-l", LABEL, "--timeout=600s", timeout=660, check=False)
        data = {"alertmanager.yml": open(args[0]).read()}
        if len(args) > 1:
            data["web.yml"] = open(args[1]).read()
        patch(data)
        set_flag(f"{DIR}/web.yml" if len(args) > 1 else None)
        k("scale", DEPLOY, "--replicas=1")
        wait_ready()
        print(metric("alertmanager_config_last_reload_successful "))
    elif cmd == "health":
        for name in ["alertmanager_config_last_reload_successful ", "alertmanager_alerts{",
                     "alertmanager_alerts_received_total", "alertmanager_notifications_total",
                     "alertmanager_notifications_failed_total", "alertmanager_notification_requests_total",
                     "alertmanager_receivers "]:
            print("\n".join(metric(name)))
        pods = json.loads(subprocess.run(["kubectl", "--context", CTX, "get", "pods", "-A", "-o", "json"],
                                         capture_output=True, text=True, timeout=120, check=True).stdout)
        for p in pods["items"]:
            for c in p["status"].get("containerStatuses", []):
                if c["restartCount"] or not (c["ready"] or p["status"]["phase"] == "Succeeded"):
                    print("pod", p["metadata"]["namespace"], p["metadata"]["name"], c["name"], "restarts",
                          c["restartCount"], "ready", c["ready"], p["status"]["phase"])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
