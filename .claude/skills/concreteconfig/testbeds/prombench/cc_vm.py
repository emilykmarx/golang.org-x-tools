#!/usr/bin/env python3
"""Runs on the prombench VM: apply and check concrete configs on prometheus-meta (the skill's TESTBED.md).

  cc_vm.py files <dir>          add each file in dir to the prometheus-meta ConfigMap (key = file name), wait for the pod
  cc_vm.py check <config>       put config in the ConfigMap as candidate.yaml, wait for the pod, promtool check config
  cc_vm.py apply <config>       set prometheus.yaml, wait for the pod, reload; exit 1 unless the reload succeeded
  cc_vm.py restart <config>     scale to 0, set prometheus.yaml, scale to 1, wait for /-/ready
  cc_vm.py health               the health numbers (metrics, up, pods with restarts or not running)
  cc_vm.py rmkey <key>          remove a key from the ConfigMap
"""
import hashlib
import json
import os
import subprocess
import sys
import time

CTX = "kind-prombench"
DIR = "/etc/prometheus/config"


def k(*args, timeout=120, check=True, stdin=None):
    p = subprocess.run(["kubectl", "--context", CTX, *args], capture_output=True, text=True, timeout=timeout, input=stdin)
    if check and p.returncode != 0:
        sys.exit(f"kubectl {' '.join(args)[:200]}: exit {p.returncode}\n{p.stderr[-3000:]}")
    return p


def pod_exec(*cmd, timeout=60, check=True):
    return k("exec", "deploy/prometheus-meta", "-c", "prometheus", "--", *cmd, timeout=timeout, check=check)


def patch(data):
    k("patch", "configmap", "prometheus-meta", "--type", "merge", "--patch-file", "/dev/stdin",
      stdin=json.dumps({"data": data}))


def sync(want, limit=240):
    """Wait until the pod's copies of the ConfigMap keys in want ({key: content}) have that content"""
    start = time.time()
    while True:
        k("annotate", "pod", "-l", "app=prometheus-meta", f"ccfg-sync={time.time_ns()}", "--overwrite")
        sums = pod_exec("md5sum", *[f"{DIR}/{name}" for name in want], check=False).stdout
        have = {line.split()[1].rsplit("/", 1)[1]: line.split()[0] for line in sums.splitlines() if line.strip()}
        if all(have.get(n) == hashlib.md5(c.encode()).hexdigest() for n, c in want.items()):
            print(f"pod has the files after {time.time() - start:.0f}s", flush=True)
            return
        if time.time() - start > limit:
            sys.exit(f"pod's files not updated after {limit}s")
        time.sleep(3)


def metric(name):
    out = pod_exec("wget", "-qO-", "localhost:9090/prometheus-meta/metrics").stdout
    return [line for line in out.splitlines() if line.startswith(name)]


def main():
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "files":
        data = {n: open(os.path.join(args[0], n)).read() for n in sorted(os.listdir(args[0]))}
        patch(data)
        sync(data)
    elif cmd == "rmkey":
        k("patch", "configmap", "prometheus-meta", "--type", "json", "-p",
          json.dumps([{"op": "remove", "path": f"/data/{args[0]}"}]))
    elif cmd == "check":
        data = {"candidate.yaml": open(args[0]).read()}
        patch(data)
        sync(data)
        p = pod_exec("promtool", "check", "config", f"{DIR}/candidate.yaml", check=False, timeout=120)
        print(p.stdout + p.stderr)
        sys.exit(p.returncode)
    elif cmd == "apply":
        data = {"prometheus.yaml": open(args[0]).read()}
        patch(data)
        sync(data)
        p = pod_exec("wget", "-qO-", "--post-data=", "localhost:9090/prometheus-meta/-/reload", check=False, timeout=120)
        print(f"reload: exit {p.returncode} {p.stdout.strip()} {p.stderr.strip()}")
        ok = metric("prometheus_config_last_reload_successful ")
        print(ok)
        sys.exit(0 if ok and ok[0].split()[1] == "1" else 1)
    elif cmd == "restart":
        k("scale", "deploy/prometheus-meta", "--replicas=0")
        k("wait", "--for=delete", "pod", "-l", "app=prometheus-meta", "--timeout=600s", timeout=660, check=False)
        patch({"prometheus.yaml": open(args[0]).read()})
        k("scale", "deploy/prometheus-meta", "--replicas=1")
        start = time.time()
        while time.time() - start < 600:
            if pod_exec("wget", "-qO-", "localhost:9090/prometheus-meta/-/ready", check=False).returncode == 0:
                print(f"ready after {time.time() - start:.0f}s")
                print(metric("prometheus_config_last_reload_successful "))
                return
            time.sleep(5)
        sys.exit("not ready after 600s")
    elif cmd == "health":
        for name in ["prometheus_config_last_reload_successful ", "prometheus_remote_storage_samples_failed_total",
                     "prometheus_remote_storage_samples_total", "prometheus_notifications_errors_total",
                     "prometheus_notifications_sent_total", "prometheus_notifications_alertmanagers_discovered",
                     "prometheus_sd_discovered_targets", "prometheus_sd_kubernetes_failures_total",
                     "prometheus_tsdb_head_series "]:
            print("\n".join(metric(name)))
        up = json.loads(pod_exec("wget", "-qO-", "localhost:9090/prometheus-meta/api/v1/query?query=up").stdout)
        for r in sorted(up["data"]["result"], key=lambda r: (r["metric"].get("job", ""), r["metric"].get("instance", ""))):
            print("up", r["metric"].get("job"), r["metric"].get("instance"), r["value"][1])
        pods = json.loads(k("get", "pods", "-A", "-o", "json").stdout)
        for p in pods["items"]:
            for c in p["status"].get("containerStatuses", []):
                if c["restartCount"] or not (c["ready"] or p["status"]["phase"] == "Succeeded"):
                    print("pod", p["metadata"]["namespace"], p["metadata"]["name"], c["name"], "restarts", c["restartCount"],
                          "ready", c["ready"], p["status"]["phase"])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
