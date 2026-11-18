"""Prometheus' Alertmanager: paramtrack's settings and hooks for this module (see ../__init__.py for what each is).

Roots: its three YAML config files (documentedkeys/README.md: what the config is and how the list was made). No concrete
config yet (the concreteconfig skill's job): a run without --config instruments it with the testbed's shipped config,
through step 3 (e.g. a baseline run). The testbed builds it from argmatch.Dockerfile here."""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CT = "/home/emily/projects/config_tracing"

GO_MODULE = "github.com/prometheus/alertmanager"
# Upstream's v0.27.0, the version prombench runs (prombench/manifests/cluster-infra/5a_alertmanager_deployment.yaml)
MODULE_DIR = f"{CT}/alertmanager-paramtrack"
FORMAT = "yaml"
# The YAML files Alertmanager reads, each named by its own flag (documentedkeys/README.md)
_DOCS = f"{HERE}/documentedkeys"
CONFIG_SOURCES = {
    "config": {"root": "/config.Config", "docs": f"{_DOCS}/alertmanager_config_keys.txt",  # --config.file
               "ignore": f"{HERE}/ignore-config.txt", "transformed": f"{HERE}/transformed-config.txt"},
    "web": {"root": "github.com/prometheus/exporter-toolkit/web.Config",  # --web.config.file
            "docs": f"{_DOCS}/web_config_keys.txt", "ignore": f"{HERE}/ignore-web.txt"},
    "cluster_tls": {"root": "/cluster.TLSTransportConfig",  # --cluster.tls-config
                    "docs": f"{_DOCS}/cluster_tls_config_keys.txt", "ignore": f"{HERE}/ignore-cluster_tls.txt"},
}
# argmatch's search caps per call (over testbeds.py's ARGMATCH_ARGS, which has none), Grafana's (its module.py says
# why): without them, run 2 of 2026-11-17 (the concrete configs) spent 2876 s searching in 5 min, ~30-35 s per check
# of the msteams/discord/pushover/slack notifiers and 5-9 s in the routing and dispatch stages, so notifications
# stalled (1-5 attempts a minute vs run 1's ~1260) and run 2 lost 11 of run 1's labels (sends and the shipped
# webhook's /hook response) (user's choice, 2026-11-17)
ARGMATCH_ARGS = {"MAXTIME": "0.3s", "MAXMEM": "1GB", "CALLINTERVAL": "10s"}
GOLITS_PACKAGE = "./cmd/alertmanager"
GOLITS_TAGS = "netgo"  # (.promu.yml)
TITLE = "Alertmanager, roots: its three YAML config files"


def extra_graph(module_dir, out_path):
    """Write gopls conftamer's -extra file: the web root, exporter-toolkit's web.Config (gopls conftamer finds roots
    among the module's own tagged types only)"""
    with open(out_path, "w") as f:
        json.dump({"roots": [CONFIG_SOURCES["web"]["root"]], "edges": []}, f, indent=2)


class CustomKeys:
    """No keys picked by custom unmarshalers (receiver integrations are picked by field name, e.g. receivers.slack_configs)"""

    names = {}
    parents = ()

    def __init__(self, module_dir):
        pass

    def of(self, fk):
        return None


def docs_entry(key, typ):
    """stats.read_docs' hook: the web config's http_server_config.headers is a map[string]string in the code (its
    validation accepts only the five header names the docs list), so its keys are `headers.*` like any map key's"""
    if key.startswith("http_server_config.headers."):
        return "http_server_config.headers", "map"
    return key, typ


def graph_docs_reason(k, in_graph):
    """Why a key is only in the graph (in_graph) or only in the docs (the graph of 2026-11-16 vs the v0.27.0 docs)"""
    s = k.split(".")
    if in_graph:
        if "tls_config" in s and s[-1] in ("ca", "cert", "key"):
            return "inline PEM tls_config.ca/cert/key (prometheus/common; docs give only the *_file keys)"
        if s[-1] in ("bearer_token", "bearer_token_file"):
            return "deprecated bearer_token[_file] (prometheus/common)"
        if s[-2:] == ["basic_auth", "username_file"]:
            return "basic_auth.username_file (prometheus/common; undocumented)"
        if k.startswith("receivers.wechat_configs.http_config."):
            return "wechat_configs.http_config (field undocumented)"
        if k in ("receivers.pushover_configs.html", "receivers.pushover_configs.url_title") or \
                k.startswith("receivers.victorops_configs.custom_fields."):
            return "receiver field undocumented"
        if k == "route.routes":
            return "recursion cut: route.routes (the docs' route keys are listed once, as route.*)"
    return "other: " + s[-1]


def docs_kind(typ):
    """Kind of Go value we'd expect for a documented type (the docs' <boolean>, <duration>, ...; the documentedkeys
    README's types table). HH:MM and the time-interval ranges are strings in the config (parsed to ints in memory)."""
    if typ.startswith(("<boolean>", "<bool>")):
        return "bool"
    if typ.startswith(("<duration>", "<int>")):
        return "number"
    if typ in ("map", ""):
        return "map"
    return "string"


UNSET_REASONS = []
