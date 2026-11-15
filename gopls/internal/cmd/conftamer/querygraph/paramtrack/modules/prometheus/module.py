"""Prometheus: paramtrack's settings and hooks for this module (see ../__init__.py for what each is; README.md here for
the module's decisions and sources)."""

import glob
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
CT = "/home/emily/projects/config_tracing"

GO_MODULE = "github.com/prometheus/prometheus"
MODULE_DIR = f"{CT}/prometheus"
FORMAT = "yaml"
ROOTS = {"config": {"root": "/config.Config", "docs": f"{HERE}/documentedkeys/prometheus_config_keys.txt",
                    "ignore": f"{HERE}/ignore.txt", "transformed": f"{HERE}/transformed.txt"}}
GOLITS_PACKAGE = "./cmd/prometheus"
GOLITS_TAGS = "netgo,builtinassets,stringlabels"  # Prometheus' build tags (.promu.yml)
TITLE = "Prometheus, root /config.Config"
UNTAGGED_KEYS = False  # our exception for untagged fields holds


def extra_graph(module_dir, out_path):
    """Write gopls conftamer's -extra file: an edge from each `discovery.Configs` field (ScrapeConfig's and
    AlertmanagerConfig's ServiceDiscoveryConfigs, yaml:"-", which their custom UnmarshalYAML fills) to each SD config
    type, keyed by the key that picks it (<Name()>_sd_configs: a module map, as in Caddy's apps.http). Returns the
    edges."""
    sd = CustomKeys(module_dir)
    # The structs with a discovery.Configs field, from the config package's source
    parents = []
    for f in sorted(glob.glob(f"{module_dir}/config/*.go")):
        if f.endswith("_test.go"):
            continue
        struct = None
        for line in open(f):
            if m := re.match(r"^type (\w+) struct \{", line):
                struct = m.group(1)
            elif line.startswith("}"):
                struct = None
            elif struct and (m := re.match(r"^\s+(\w+)\s+discovery\.Configs\b", line)):
                parents.append((f"{GO_MODULE}/config.{struct}", m.group(1)))
    edges = [{"parent": p, "field": field, "child": GO_MODULE + typ, "key": key}
             for p, field in parents for typ, key in sorted(sd.names.items()) if typ not in sd.aliases]
    with open(out_path, "w") as f:
        json.dump({"roots": [], "edges": edges}, f, indent=2)
    return edges


class CustomKeys:
    """Service discovery: discovery.Configs' custom UnmarshalYAML picks each SD config by its key, which
    discovery.RegisterConfig makes <Name()>_sd_configs (static_configs for "static"), under scrape_configs and
    alerting.alertmanagers. extra_graph resolves them in the graph; this tells which keys are SD configs' (stats.py's
    SD tables)."""

    parents = ("scrape_configs", "alerting.alertmanagers")

    def __init__(self, module_dir):
        # Graph type name of each SD config => its key, from the configs' Name() methods
        rx = re.compile(r'func \((?:\w+ )?\*?(\w+)\) Name\(\) string \{\s*return "(\w+)"')
        alias_rx = re.compile(r"^type (\w+) = (\w+)$", re.M)  # e.g. type KumaSDConfig = SDConfig: the graph names Y
        self.names = {}
        self.aliases = set()  # names[] keys that are aliases (the graph names their targets)
        for f in glob.glob(f"{module_dir}/discovery/**/*.go", recursive=True):
            if f.endswith("_test.go"):
                continue
            pkg = os.path.relpath(os.path.dirname(f), module_dir)
            src = open(f).read()
            aliases = dict(alias_rx.findall(src))
            for typ, name in rx.findall(src):
                key = "static_configs" if name == "static" else f"{name}_sd_configs"
                self.names[f"/{pkg}.{typ}"] = key
                if typ in aliases:
                    self.names[f"/{pkg}.{aliases[typ]}"] = key
                    self.aliases.add(f"/{pkg}.{typ}")

    def of(self, fk):
        """The SD config key (e.g. azure_sd_configs) a full key is under, or None"""
        s = fk.split(".")
        keys = set(self.names.values())
        if s[0] == "scrape_configs" and len(s) > 2 and s[1] in keys:
            return s[1]
        if s[:2] == ["alerting", "alertmanagers"] and len(s) > 3 and s[2] in keys:
            return s[2]
        return None


def docs_kind(typ):
    """Kind of Go value we'd expect for a documented type (the docs' <boolean>, <duration>, ...)"""
    t = typ.strip("[] ")
    if t.startswith("<boolean>"):
        return "bool"
    if any(t.startswith(x) for x in ("<duration>", "<int>", "<float>", "<size>")):
        return "number"
    if typ == "map" or typ == "":
        return "map"
    return "string"


def graph_docs_reason(k, in_graph):
    """Why a key is only in the graph (in_graph) or only in the docs"""
    s = k.split(".")
    if in_graph:
        if s[-1].endswith("_ref"):
            return "secret-manager reference (*_ref)"
        if s[-1] in ("bearer_token", "bearer_token_file"):
            return "deprecated bearer_token[_file]"
        if k.startswith("alerting.alertmanagers.kuma_sd_configs."):
            return "kuma_sd_configs under alertmanagers (docs list it only under scrape_configs)"
        if k.startswith("scrape_configs.vultr_sd_configs."):
            return "vultr_sd_configs under scrape_configs (docs list it only under alertmanagers)"
        return "other: " + s[-1]
    if ".oauth2.http_headers." in k:
        return "oauth2.http_headers (docs; not a field of OAuth2 in prometheus/common v0.62.0)"
    return "other: " + k


# Why the concreteconfig skill left target keys unset: the 2026-11-08 run's reasons (its diff.md). fnmatch patterns,
# as in --ignore.
UNSET_REASONS = [
    ("secret-manager reference (*_ref): fails at runtime", ["*_ref"]),
    ("would contact endpoints outside the testbed", ["*.sigv4.role_arn", "remote_write.azuread.oauth.*"]),
]
