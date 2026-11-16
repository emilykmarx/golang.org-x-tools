"""Caddy's ingress controller: paramtrack's settings and hooks for this module (see ../__init__.py for what each is;
README.md here for the module's decisions and sources)."""

import json
import os
import re
import shutil
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CT = "/home/emily/projects/config_tracing"

GO_MODULE = "github.com/caddyserver/ingress"
MODULE_DIR = f"{CT}/caddyserver-ingress"
FORMAT = "json"
# The Caddy JSON the controller generates, as caddy.Load decodes it (a dependency's type: an -extra root)
ROOTS = {"caddy": {"root": "github.com/caddyserver/caddy/v2.Config",
                   "docs": f"{HERE}/documentedkeys/caddy_keys_linked.txt"}}
BINARY_PACKAGE = "./cmd/caddy"  # the controller's main package
GOLITS_PACKAGE = BINARY_PACKAGE
GOLITS_TAGS = ""
TITLE = "Caddy ingress controller, root caddy.Config"
# Untagged fields are keys (encoding/json keys them by name): the controller's secret_store storage module has only
# untagged fields (Namespace, LeaseID), which the generated JSON sets as namespace and leaseId (decision, 2026-11-12)
UNTAGGED_KEYS = True
# argmatch's search caps per call (over testbeds.py's ARGMATCH_ARGS, which has none): without them, run 2's controller
# took over 15 minutes to load the concrete config and wrote 22 GB of matches (2026-11-15); the full run of
# 2026-11-13 passed these on the command line
ARGMATCH_ARGS = {"MAXTIME": "2s", "MAXMEM": "1GB"}
# On message chains, Caddy's functions are the module's too: the controller's binary runs Caddy, whose code handles
# the messages (routes, handlers, storage); with only the controller's, most chains look alike
CHAIN_FUNCTIONS = (f"{GO_MODULE}/", "github.com/caddyserver/caddy/v2")


def extra_graph(module_dir, out_path, timeout=900):
    """Write gopls conftamer's -extra file (its ExtraGraph) for the binary's Caddy modules: the root caddy.Config,
    and an edge from each field holding modules (caddy:"namespace=..." tag) to each module registered in its
    namespace, keyed by the module's name (a module map, e.g. apps.http) or by `<inline_key>=<name>` (a module picked
    by a key inside the object, e.g. handle.handler=reverse_proxy). The registry comes from Caddy itself: registry.go
    runs with blank imports of the binary's packages. Returns the registry (modules and fields), for the record."""
    pkgs = subprocess.run(["go", "list", "-deps", "-f", "{{if not .Standard}}{{.ImportPath}}{{end}}",
                           BINARY_PACKAGE], cwd=module_dir, capture_output=True, text=True, timeout=timeout,
                          check=True).stdout.split()
    # The packages that may register modules (Caddy's and the controller's), but internal ones, which can't be
    # imported from outside their module (their init still runs: what imports them is imported)
    imports = [p for p in pkgs if (p.startswith("github.com/caddyserver/caddy/v2") or p.startswith(GO_MODULE))
               and "/internal/" not in p and not p.endswith("/internal")
               and p != f"{GO_MODULE}/{BINARY_PACKAGE.removeprefix('./')}"]
    with tempfile.TemporaryDirectory(prefix="caddy-registry-") as d:
        # A module that requires the controller (replaced by the checkout), with its go.mod's versions
        gomod = open(f"{module_dir}/go.mod").read()
        gomod = re.sub(r"^module .*$", "module paramtrack/caddyregistry", gomod, count=1, flags=re.M)
        gomod += f"\nrequire {GO_MODULE} v0.0.0\n\nreplace {GO_MODULE} => {module_dir}\n"
        open(f"{d}/go.mod", "w").write(gomod)
        shutil.copy(f"{module_dir}/go.sum", f"{d}/go.sum")
        src = open(f"{HERE}/registry.go").read().replace("//go:build ignore\n", "", 1)
        src = src.replace("\t// IMPORTS\n", "".join(f'\t_ "{p}"\n' for p in imports
                                                    if p != "github.com/caddyserver/caddy/v2"))
        open(f"{d}/main.go", "w").write(src)
        r = subprocess.run(["go", "run", "."], cwd=d, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "GOFLAGS": "-mod=mod"})
        if r.returncode != 0:
            raise RuntimeError(f"registry.go failed ({r.returncode}): {r.stderr[-3000:]}")
        registry = json.loads(r.stdout)

    edges = []
    for f in registry["fields"]:
        for m in registry["modules"]:
            ns, _, name = m["id"].rpartition(".")
            if ns != f["namespace"]:
                continue
            key = f"{f['inline_key']}={name}" if f["inline_key"] else name
            edges.append({"parent": f["parent"], "field": f["field"], "child": m["type"], "key": key})
    edges.sort(key=lambda e: (e["parent"], e["field"], e["key"]))
    with open(out_path, "w") as f:
        json.dump({"roots": [r["root"] for r in ROOTS.values()], "edges": edges}, f, indent=2)
    return registry


class CustomKeys:
    """Caddy's modules are resolved in the graph (extra_graph). No SD-like tables for now (stats.py's SD wording is a
    to-do)."""

    names = {}
    parents = ()

    def __init__(self, module_dir):
        pass

    def of(self, fk):
        return None


def docs_entry(key, typ):
    """stats.read_docs' hook for caddy_keys_*.txt: `map[...]` types are maps (their keys are `<key>.*`); `{}` (a
    module choice with no fields of its own, e.g. logging.sink.writer.output=discard) and `recursive:...` (where the
    list stops expanding a module that nests itself) aren't keys"""
    if typ == "{}" or typ.startswith("recursive"):
        return None
    if typ.startswith("map["):
        return key, "map"
    return key, typ


def docs_kind(typ):
    """Kind of Go value we'd expect for a documented type (caddy_keys_*.txt: Go-like types, e.g. string,
    int:v2.Duration, []string, map[string][]string, module_id:http.handlers)"""
    t = typ.removeprefix("[]")
    if t.startswith("bool"):
        return "bool"
    if t.startswith(("int", "uint", "float")):
        return "number"
    if t.startswith("map"):
        return "map"
    return "string"


def graph_docs_reason(k, in_graph):
    """Why a key is only in the graph (in_graph) or only in the docs"""
    return "other: " + k.split(".")[-1]


UNSET_REASONS = []
