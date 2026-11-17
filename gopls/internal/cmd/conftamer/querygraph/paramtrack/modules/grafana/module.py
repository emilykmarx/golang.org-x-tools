"""Grafana: paramtrack's settings and hooks for this module (see ../__init__.py for what each is).

Roots: the four provisioning file kinds (documentedkeys/README.md: what the config is, why grafana.ini is out of scope
for now, and how the lists were made). No concrete configs yet (the concreteconfig skill's job): a run without --config
instruments it with the testbed's shipped config (testbeds.Grafana), through step 3 (e.g. a baseline run). The testbed
builds it from argmatch.Dockerfile here."""

import os

HERE = os.path.dirname(os.path.abspath(__file__))
CT = "/home/emily/projects/config_tracing"

GO_MODULE = "github.com/grafana/grafana"
# Upstream's v13.1.0, the version contexttrack's Grafana patches were made against (prombench ships 11.0.0; the testbed
# runs 13.1.0, stock or instrumented)
MODULE_DIR = f"{CT}/grafana-paramtrack"
FORMAT = "yaml"
_P = "/pkg/services/provisioning"
# (Each reader first decodes configVersion for apiVersion, then the versioned type; the lists have v1 keys only.)
# datasources' docs: the list without type=<plugin> segments (datasources_keys_by_type.txt has them;
# datasources_collapsed.tsv lists the keys that differ)
ROOTS = {
    "datasources": {"root": f"{_P}/datasources.configsV1", "docs": f"{HERE}/documentedkeys/datasources_keys.txt"},
    "dashboards": {"root": f"{_P}/dashboards.configV1", "docs": f"{HERE}/documentedkeys/dashboards_keys.txt"},
    "plugins": {"root": f"{_P}/plugins.pluginsAsConfigV0", "docs": f"{HERE}/documentedkeys/plugins_keys.txt"},
    "alerting": {"root": f"{_P}/alerting.AlertingFileV1", "docs": f"{HERE}/documentedkeys/alerting_keys.txt"},
}
GOLITS_PACKAGE = "./pkg/cmd/grafana"
GOLITS_TAGS = "oss"  # (argmatch.Dockerfile's build)
TITLE = "Grafana, roots: its four provisioning file kinds"


class CustomKeys:
    """No keys picked by custom unmarshalers (not reviewed yet: e.g. contact points' settings are decoded per integration type at run time)"""

    names = {}
    parents = ()

    def __init__(self, module_dir):
        pass

    def of(self, fk):
        return None


# Generic maps (values.JSONValue, StringMapValue) that the code decodes into typed structs, or reads by key, only later
# (at run time): the documented keys under them can't be in the graph
LATER_TYPED = {"datasources.jsonData.": "datasources' jsonData (decoded by each datasource plugin)",
               "datasources.secureJsonData.": "datasources' secureJsonData (read by each datasource plugin)",
               "providers.options.": "dashboard providers' options (read by key)",
               "contactPoints.receivers.type=": "contact points' settings (decoded per integration type)",
               "policies.": "notification policies (decoded into definitions.Route from JSON)"}


def graph_docs_reason(k, in_graph):
    """Why a key is only in the graph (in_graph) or only in the docs (the graph of 2026-11-16 vs the v13.1.0 docs)"""
    s = k.split(".")
    if in_graph:
        if s[-1] == "*" and any(k.startswith(p) or p.startswith(k[:-1]) for p in LATER_TYPED):
            return "generic map the code types later (its documented keys are docs-only)"
        if k == "groups.rules.dasboardUid":
            return "dasboardUid: a misspelled alias of dashboardUid, kept on purpose"
        return "field undocumented"
    if k == "apiVersion":
        return "apiVersion: read by a separate decode (configVersion) that picks the versioned type"
    for prefix, what in LATER_TYPED.items():
        if k.startswith(prefix):
            return "under a generic map the code types later: " + what
    if k in ("prune", "templates.name", "templates.template"):
        return "field without a yaml tag (yaml.v3 reads its lowercased name; UNTAGGED_KEYS is False)"
    if k.startswith("muteTimes.time_intervals."):
        return "below a decode into another struct (conftamer doesn't follow its fields' types)"
    return "other: " + s[-1]


def docs_kind(typ):
    """Kind of Go value we'd expect for a documented type (documentedkeys/README.md's classification; lists are their
    elements' kind, string)"""
    t = typ.removeprefix("example:")
    if t in ("bool", "boolean"):
        return "bool"
    if t in ("int", "number"):
        return "number"
    if t == "map":
        return "map"
    return "string"


UNSET_REASONS = []
