#!/usr/bin/env python3
"""Runs on the prombench VM: apply and check concrete configs on Grafana's provisioning config sources (the skill's
TESTBED.md, next to this). <source> is the config source: datasources, dashboards, plugins or alerting.

  cc_vm.py files <source> <dir>     add each file in dir to the source's ConfigMap (key = file name), wait for the pod to
                                  have them (they appear in /opt/grafana-provision/<source>/)
  cc_vm.py apply <source> <config>  set the source's provisioning file, wait for the pod, POST the admin API's provisioning
                                  reload for the source; exit 1 unless it answers 200 (this is the check command too)
  cc_vm.py restart [<source> <config> ...]
                                  scale to 0, set each source's file, scale to 1, wait for /api/health (a provisioning
                                  error stops Grafana from starting, so this fails then)
  cc_vm.py health                 /api/health, the datasources, dashboards and app plugins Grafana has, and pods with
                                  restarts or not running
  cc_vm.py rmkey <source> <key>     remove a key from the source's ConfigMap

The source's ConfigMap must be mounted: prombench mounts datasources' and dashboards'; a paramtrack run with the plugins
or alerting config source mounts theirs (a fresh cluster's paramtrack run creates them without mounting them). apply and
restart mount one that isn't yet (a change to the deployment, so the pod restarts).
"""
import hashlib
import json
import os
import subprocess
import sys
import time

CTX = "kind-prombench"
NS, DEPLOY, CONTAINER, LABEL = "default", "deploy/grafana-core", "grafana-core", "app=grafana,component=core"
PROVISIONING = "/opt/grafana-provision"
# config source => its ConfigMap and the provisioning file's key in it (as paramtrack's testbeds.Grafana.CONFIG_MAPS)
CONFIG_MAPS = {"datasources": ("grafana-datasource-provision", "prometheus-datasource.yaml"),
               "dashboards": ("grafana-dashboard-provision", "prometheus-dashboard.yaml"),
               "plugins": ("grafana-plugins-provision", "plugins.yaml"),
               "alerting": ("grafana-alerting-provision", "alerting.yaml")}
# As the admin, whose password is the container's GF_SECURITY_ADMIN_PASSWORD; GF_SERVER_SERVE_FROM_SUB_PATH: /grafana
API = "http://admin:$GF_SECURITY_ADMIN_PASSWORD@localhost:3000/grafana/api"


def k(*args, timeout=120, check=True, stdin=None):
    p = subprocess.run(["kubectl", "--context", CTX, "-n", NS, *args], capture_output=True, text=True,
                       timeout=timeout, input=stdin)
    if check and p.returncode != 0:
        sys.exit(f"kubectl {' '.join(args)[:200]}: exit {p.returncode}\n{p.stderr[-3000:]}")
    return p


def pod_exec(*cmd, timeout=60, check=True):
    return k("exec", DEPLOY, "-c", CONTAINER, "--", *cmd, timeout=timeout, check=check)


def api(path, post=False, timeout=300):
    """wget's exit status is 0 only for a 2xx answer"""
    return pod_exec("sh", "-c", f'wget -qO- {"--post-data= " if post else ""}"{API}{path}"', timeout=timeout,
                    check=False)


def patch(root, data):
    k("patch", "configmap", CONFIG_MAPS[root][0], "--type", "merge", "--patch-file", "/dev/stdin",
      stdin=json.dumps({"data": data}))


def mount(root):
    """Mounts the source's ConfigMap at its provisioning directory unless it is (as testbeds.Component.mount_config_map);
    whether it had to"""
    name = CONFIG_MAPS[root][0]
    spec = json.loads(k("get", DEPLOY, "-o", "json").stdout)["spec"]["template"]["spec"]
    if any(v["name"] == name for v in spec.get("volumes") or []):
        return False
    i = next(i for i, c in enumerate(spec["containers"]) if c["name"] == CONTAINER)
    k("patch", DEPLOY, "--type", "json", "-p", json.dumps([
        {"op": "add", "path": "/spec/template/spec/volumes/-", "value": {"name": name, "configMap": {"name": name}}},
        {"op": "add", "path": f"/spec/template/spec/containers/{i}/volumeMounts/-",
         "value": {"name": name, "mountPath": f"{PROVISIONING}/{root}"}}]))
    print(f"mounted {name} at {PROVISIONING}/{root}")
    return True


def wait_health(limit=600):
    start = time.time()
    while time.time() - start < limit:
        if k("get", "--raw", "/api/v1/namespaces/default/services/grafana:grafana-web/proxy/grafana/api/health",
             check=False).returncode == 0:
            print(f"ready after {time.time() - start:.0f}s")
            return
        time.sleep(5)
    sys.exit(f"not ready after {limit}s: kubectl logs deploy/grafana-core | grep -i provision | tail")


def sync(root, want, limit=240):
    """Wait until the pod's copies of the root's ConfigMap keys in want ({key: content}) have that content"""
    start = time.time()
    while True:
        k("annotate", "pod", "-l", LABEL, f"ccfg-sync={time.time_ns()}", "--overwrite")
        sums = pod_exec("md5sum", *[f"{PROVISIONING}/{root}/{name}" for name in want], check=False).stdout
        have = {line.split()[1].rsplit("/", 1)[1]: line.split()[0] for line in sums.splitlines() if line.strip()}
        if all(have.get(n) == hashlib.md5(c.encode()).hexdigest() for n, c in want.items()):
            print(f"pod has the files after {time.time() - start:.0f}s", flush=True)
            return
        if time.time() - start > limit:
            sys.exit(f"pod's files not updated after {limit}s")
        time.sleep(3)


def main():
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "files":
        root, d = args
        data = {n: open(os.path.join(d, n)).read() for n in sorted(os.listdir(d))}
        patch(root, data)
        sync(root, data)
    elif cmd == "rmkey":
        root, key = args
        k("patch", "configmap", CONFIG_MAPS[root][0], "--type", "json", "-p",
          json.dumps([{"op": "remove", "path": f"/data/{key}"}]))
    elif cmd == "apply":
        root, config = args
        data = {CONFIG_MAPS[root][1]: open(config).read()}
        patch(root, data)
        if mount(root):  # (the new pod starts with the file)
            k("rollout", "status", DEPLOY, "--timeout=600s", timeout=660)
            wait_health()
        sync(root, data)
        p = api(f"/admin/provisioning/{root}/reload", post=True)
        print(f"reload: exit {p.returncode} {p.stdout.strip()} {p.stderr.strip()}")
        if p.returncode != 0:
            print("the error is in the pod log: kubectl logs deploy/grafana-core | grep -i provision | tail")
        sys.exit(p.returncode)
    elif cmd == "restart":
        pairs = list(zip(args[::2], args[1::2]))
        k("scale", DEPLOY, "--replicas=0")
        k("wait", "--for=delete", "pod", "-l", LABEL, "--timeout=600s", timeout=660, check=False)
        for root, config in pairs:
            patch(root, {CONFIG_MAPS[root][1]: open(config).read()})
            mount(root)
        k("scale", DEPLOY, "--replicas=1")
        wait_health()
    elif cmd == "health":
        print("health", api("/health").stdout.strip())
        for ds in json.loads(api("/datasources").stdout or "[]"):
            print("datasource", ds["name"], ds["type"], ds["uid"], ds["url"])
        print("dashboards", len(json.loads(api("/search?type=dash-db").stdout or "[]")))
        for pl in json.loads(api("/plugins?type=app").stdout or "[]"):
            print("app plugin", pl["id"], "enabled" if pl.get("enabled") else "disabled")
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
