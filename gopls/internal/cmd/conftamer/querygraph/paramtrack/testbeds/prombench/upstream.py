"""prombench from upstream prometheus/test-infra at UPSTREAM_COMMIT, with this directory's patches applied: prepares
that tree, builds the prometheus-builder image paramtrack's images build on, and deploys prombench in a kind cluster on
this machine. Ported from the deploy.sh and build_prom_argmatch.sh of the fork (github.com/emilykmarx/
prometheus-test-infra, branch argmatch-prom). Each patch's message gives its reason.

Two variants (VARIANTS), by ingress controller: `nginx` (upstream's; patches/) and `caddy` (Caddy's ingress
controller, the module paramtrack studies there: patches/ but 0003, which only sets prometheus-meta's image, then
patches-caddy/).

Used by ../../testbeds.py; also runnable alone, on the machine that runs the cluster:
    python3 upstream.py deploy --variant nginx|caddy --image TAG   # deletes the cluster first if there is one
"""

import argparse
import base64
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from common import CT, log_command, popen, run, say  # noqa: E402

UPSTREAM = "https://github.com/prometheus/test-infra.git"
# Where the fork branched off; the shipped prometheus-meta config (the concreteconfig skill's baseline) is this
# commit's
UPSTREAM_COMMIT = "e85cac5480444d0bb6ac6298c1bba52392097cc7"
CLONE = f"{CT}/prometheus-test-infra-upstream"  # a clone of UPSTREAM, made if missing
HERE = os.path.dirname(os.path.abspath(__file__))


class Variant:
    def __init__(self, name, patches, domain, ingress_namespace):
        self.name = name
        self.tree = f"{CT}/paramtrack-prombench" + ("" if name == "nginx" else f"-{name}")  # remade by prepare()
        self.patches = patches  # paths, applied in order
        self.domain = domain  # DOMAIN_NAME: the ingress controller's service
        self.ingress_namespace = ingress_namespace  # tells a deployed cluster's variant


def _patches(d, skip=()):
    return [f"{HERE}/{d}/{p}" for p in sorted(os.listdir(f"{HERE}/{d}")) if not p.startswith(skip)]


VARIANTS = {
    "nginx": Variant("nginx", _patches("patches"), "ingress-nginx-controller.ingress-nginx.svc.cluster.local",
                     "ingress-nginx"),
    "caddy": Variant("caddy", _patches("patches", skip=("0003-",)) + _patches("patches-caddy"),
                     "caddy-caddy-ingress-controller.caddy-system.svc.cluster.local", "caddy-system"),
}

CLUSTER = "prombench"
CTX = f"kind-{CLUSTER}"
# Mounted at /argmatch-out in prometheus-meta (patch 0002) and in Caddy's ingress controller (0101)
ARGMATCH_OUT = f"{CT}/dump_ancestors/argmatch-out"
# The infra tool's template variables (and DOMAIN_NAME, the variant's)
VARS = {
    "CLUSTER_NAME": CLUSTER,
    "PR_NUMBER": "16075",
    "RELEASE": "v3.2.1",
    "GRAFANA_ADMIN_PASSWORD": "password",
    "GITHUB_ORG": "prometheus",
    "GITHUB_REPO": "prometheus",
    "SERVICEACCOUNT_CLIENT_EMAIL": "fakeemail",
    # Must be non-empty: amgithubnotifier exits on an empty token, and the infra tool waits for it to be ready
    # before applying later waves (2_ingress-nginx-controller, 4_kube-state-metrics)
    "OAUTH_TOKEN": base64.b64encode(b"dummy").decode(),
    "WH_SECRET": "",
    "ARGMATCH_OUT": ARGMATCH_OUT,
}

BUILDER_TAG = "prometheus-builder:conftamer"
GO_PATCHED = f"{CT}/go-conftamer-ancestry"  # the builder's build context `conftamer`
GOPATCH = "patches/send_recv_short_contents.patch"  # in GO_PATCHED


def prepare(v):
    """Remakes the variant's tree: UPSTREAM_COMMIT with its patches applied, and the infra tool built"""
    if not os.path.isdir(CLONE):
        run(["git", "clone", "-q", UPSTREAM, CLONE], timeout=600)
    if subprocess.run(["git", "-C", CLONE, "cat-file", "-e", f"{UPSTREAM_COMMIT}^{{commit}}"],
                      capture_output=True).returncode != 0:
        run(["git", "-C", CLONE, "fetch", "-q", "origin"], timeout=600)
    shutil.rmtree(v.tree, ignore_errors=True)
    os.makedirs(v.tree)
    log_command(["git", "-C", CLONE, "archive", UPSTREAM_COMMIT, "|", "tar", "-x", "-C", v.tree])
    archive = subprocess.run(["git", "-C", CLONE, "archive", UPSTREAM_COMMIT], capture_output=True, check=True,
                             timeout=120).stdout
    subprocess.run(["tar", "-x", "-C", v.tree], input=archive, check=True, timeout=120)
    for p in v.patches:
        run(["git", "apply", p], cwd=v.tree, timeout=60)
    say(f"prepared {v.tree}: upstream {UPSTREAM_COMMIT[:9]} with {len(v.patches)} patches")
    run(["go", "build", "-o", "infra", "."], cwd=f"{v.tree}/infra", timeout=900,
        env={"PATH": f"{os.environ['PATH']}:/usr/local/go/bin"})


def image_exists(tag):
    return subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0


def ensure_builder(log_dir):
    """Builds BUILDER_TAG (upstream's prometheus-builder with patched Go, patch 0007, in both variants) if it isn't
    there"""
    if image_exists(BUILDER_TAG):
        return
    v = VARIANTS["nginx"]
    if not os.path.isdir(v.tree):
        prepare(v)
    say(f"building {BUILDER_TAG} (several minutes)")
    run(["docker", "build", "--progress=plain", "--build-context", f"conftamer={GO_PATCHED}", "--build-arg",
         f"GOPATCH={GOPATCH}", "-t", BUILDER_TAG, f"{v.tree}/tools/prometheus-builder"],
        log=f"{log_dir}/builder-build.log", timeout=2400)


def kubectl(*args, timeout=120, log=None):
    return run(["kubectl", "--context", CTX, *args], timeout=timeout, log=log)


def load(tag, all_nodes=False):
    """Loads a local image into the main node (where prometheus-meta runs), or into every node"""
    nodes = (run(["kind", "get", "nodes", "--name", CLUSTER], timeout=60).split() if all_nodes else
             [n.removeprefix("node/") for n in
              kubectl("get", "nodes", "-l", "cloud.google.com/gke-nodepool=main-node", "-o", "name").split()])
    say(f"loading {tag} into {', '.join(nodes)}")
    with tempfile.NamedTemporaryFile(suffix=".tar") as tar:
        run(["docker", "save", tag, "-o", tar.name], timeout=600)
        for n in nodes:
            cmd = ["docker", "exec", "-i", n, "ctr", "-n", "k8s.io", "images", "import", "-"]
            log_command(cmd + ["<", tar.name])
            with open(tar.name, "rb") as f:
                subprocess.run(cmd, stdin=f, check=True, capture_output=True, timeout=600)


def cluster_exists():
    return CLUSTER in run(["kind", "get", "clusters"], timeout=60).split()


def cluster_variant():
    """The deployed cluster's variant (by its ingress controller's namespace), or None if there's no cluster"""
    if not cluster_exists():
        return None
    for v in VARIANTS.values():
        if subprocess.run(["kubectl", "--context", CTX, "get", "namespace", v.ingress_namespace],
                          capture_output=True, timeout=60).returncode == 0:
            return v.name
    return "unknown"


def infra_cmd(v, command, subcommand, *args):
    """The infra tool's command line for `kind <command> <subcommand>`, with VARS; run it in <tree>/prombench"""
    cmd = [f"{v.tree}/infra/infra", "kind", command, subcommand]
    for k, val in {**VARS, "DOMAIN_NAME": v.domain}.items():
        cmd += ["-v", f"{k}={val}"]
    return cmd + list(args)


def infra(v, *args, log, timeout):
    return run(infra_cmd(v, *args), cwd=f"{v.tree}/prombench", log=log, timeout=timeout)


def deploy(variant, image, log_dir):
    """Deploys the variant of prombench from scratch (deleting the cluster first if there is one), with `image` (a
    local image: the module's, prometheus-meta's or Caddy's ingress controller's) loaded into the main node before
    the manifests that run it are applied. About 5 minutes."""
    v = VARIANTS[variant]
    os.makedirs(log_dir, exist_ok=True)
    with open("/proc/sys/fs/inotify/max_user_instances") as f:
        if int(f.read()) < 512:
            # kube-proxy and promtail crash-loop with the default 128 (vm_setup.sh makes it permanent)
            run(["sudo", "sysctl", "fs.inotify.max_user_instances=512"], timeout=60)
    os.makedirs(ARGMATCH_OUT, exist_ok=True)  # must exist before the cluster is created
    # Writable by any user: Caddy's ingress controller runs as root but drops every capability (so it can't write to
    # a directory it doesn't own)
    os.chmod(ARGMATCH_OUT, 0o777)
    prepare(v)
    if cluster_exists():
        say(f"deleting the kind cluster {CLUSTER}")
        infra(v, "cluster", "delete", "-f", "manifests/cluster_kind.yaml", log=f"{log_dir}/cluster-delete.log",
              timeout=600)
    say(f"creating the kind cluster {CLUSTER}")
    infra(v, "cluster", "create", "-f", "manifests/cluster_kind.yaml", log=f"{log_dir}/cluster-create.log",
          timeout=1200)
    kubectl("taint", "nodes", f"{CLUSTER}-control-plane", "node-role.kubernetes.io/control-plane-")
    load(image)

    # cluster-infra (including the ingress controller) in the background, and the benchmark's waves 1-6, which don't
    # depend on it, meanwhile
    say("applying cluster-infra and the benchmark")
    bg = popen(infra_cmd(v, "resource", "apply", "-f", "manifests/cluster-infra"), cwd=f"{v.tree}/prombench",
               stdout=open(f"{log_dir}/cluster-infra.log", "w"), stderr=subprocess.STDOUT)
    try:
        bench = f"{v.tree}/prombench/manifests/prombench/benchmark"
        early = [a for f in sorted(os.listdir(bench)) if f[0] in "123456" for a in ("-f", f"{bench}/{f}")]
        infra(v, "resource", "apply", *early, log=f"{log_dir}/benchmark-1-6.log", timeout=2400)
        # Ingress creation is rejected by the ingress-nginx admission webhook (failurePolicy: Fail) until the
        # controller is ready, so the ingress routes wait for cluster-infra (with nginx; no harm with caddy)
        code = bg.wait(timeout=2400)
    except BaseException:
        bg.kill()
        raise
    if code != 0:
        tail = "".join(open(f"{log_dir}/cluster-infra.log").readlines()[-20:])
        raise RuntimeError(f"applying cluster-infra exited {code}:\n{tail}(see {log_dir}/cluster-infra.log)")
    infra(v, "resource", "apply", "-f", f"{bench}/7_nginx-ingress-routes.yaml", "-f", f"{bench}/8_loadgen.yaml",
          log=f"{log_dir}/benchmark-7-8.log", timeout=1200)
    say(f"prombench deployed ({variant})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["deploy", "prepare"])
    ap.add_argument("--variant", choices=sorted(VARIANTS), default="nginx")
    ap.add_argument("--image", default="prometheus-conftamer:v3.2.1",
                    help="local image the module's deployment runs (prometheus-meta's, or Caddy's ingress controller's)")
    ap.add_argument("--logs", default="/tmp/paramtrack-prombench-deploy-logs")
    a = ap.parse_args()
    if a.action == "prepare":
        prepare(VARIANTS[a.variant])
    else:
        start = time.time()
        deploy(a.variant, a.image, a.logs)
        say(f"took {time.time() - start:.0f}s")


if __name__ == "__main__":
    main()
