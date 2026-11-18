"""Grafana: paramtrack's settings and hooks for this module (see ../__init__.py for what each is).

Roots: the four provisioning file kinds (documentedkeys/README.md: what the config is, why grafana.ini is out of scope
for now, and how the lists were made). No concrete configs yet (the concreteconfig skill's job): a run without --config
instruments it with the testbed's shipped config (testbeds.Grafana), through step 3 (e.g. a baseline run). The testbed
builds it from argmatch.Dockerfile here."""

import importlib.util
import json
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
CT = "/home/emily/projects/config_tracing"

GO_MODULE = "github.com/grafana/grafana"
# Upstream's v13.1.0, the version contexttrack's Grafana patches were made against (prombench ships 11.0.0; the testbed
# runs 13.1.0, stock or instrumented)
MODULE_DIR = f"{CT}/grafana-paramtrack"
FORMAT = "yaml"
_P = "/pkg/services/provisioning"
# (Each reader first decodes configVersion for apiVersion, then the versioned type; the lists have v1 keys only.)
# datasources' docs: the list with type=<plugin> segments, as the graph's keys have them (extra_graph); the list without
# them is datasources_keys.txt (datasources_collapsed.tsv lists the keys it collapses)
CONFIG_SOURCES = {
    "datasources": {"root": f"{_P}/datasources.configsV1", "docs": f"{HERE}/documentedkeys/datasources_keys_by_type.txt",
                   "ignore": f"{HERE}/ignore-datasources.txt"},
    "dashboards": {"root": f"{_P}/dashboards.configV1", "docs": f"{HERE}/documentedkeys/dashboards_keys.txt"},
    "plugins": {"root": f"{_P}/plugins.pluginsAsConfigV0", "docs": f"{HERE}/documentedkeys/plugins_keys.txt"},
    "alerting": {"root": f"{_P}/alerting.AlertingFileV1", "docs": f"{HERE}/documentedkeys/alerting_keys.txt",
                "ignore": f"{HERE}/ignore-alerting.txt"},
}
# argmatch's search caps per call (over testbeds.py's ARGMATCH_ARGS, which has none), as Caddy's: without them, run 2
# of 2026-11-17 (with the concrete configs) searched 277 MB in one MultiOrgAlertmanager.LoadAndSyncAlertmanagersForOrgs
# call (34 s) and 46 MB per SQLStore.WithDbSession call (6 s), and Grafana exited at startup ("multiorg alertmanager
# manager failed to warm up: context deadline exceeded"). With them alone, the next run 2 still spent 653 s searching in
# 4.6 min of startup (98 functions, most at MAXCALLS' 30 checks of ~2 s), and provisioning ran before alerting had
# written the org's default Alertmanager config ("contact points: [alerting.notification.configMissing]"). CALLINTERVAL
# spreads each function's checked calls over the run instead of the startup (user's choice, 2026-11-17). With it, the
# restarted container was still provisioning alert rules after 8.5 min (649 s of search, ~130 functions at ~2 s per
# check), past testbeds.py's READY_WITHIN: MAXTIME 0.3s (user's choice, 2026-11-17).
ARGMATCH_ARGS = {"MAXTIME": "0.3s", "MAXMEM": "1GB", "CALLINTERVAL": "10s"}
GOLITS_PACKAGE = "./pkg/cmd/grafana"
GOLITS_TAGS = "oss"  # (argmatch.Dockerfile's build)
TITLE = "Grafana, roots: its four provisioning file kinds"

GOPLS = os.path.abspath(f"{HERE}/../../../../../../..")  # the gopls module (settings_types.go needs golang.org/x/tools)
GG = "github.com/grafana/grafana"
DATASOURCE = f"{GG}/pkg/services/provisioning/datasources.upsertDataSourceFromConfigV1"  # a datasources list item
RECEIVER = f"{GG}/pkg/services/provisioning/alerting.ReceiverV1"  # a contact point's receivers list item


def extra_graph(module_dir, out_path, timeout=1800):
    """Write gopls conftamer's -extra file: edges from the generic maps the provisioning files decode a datasource's
    jsonData/secureJsonData and a contact point's settings into, to what the code later decodes them into, each keyed
    `type=<id>` before the field (the item's type picks it; e.g. datasources.type=cloudwatch.jsonData.region):
    - generated (settings_types.go, run on the checkout): datasources' jsonData types, contact points' settings types
      (or, for a struct declared in the function that decodes them, its fields);
    - hand-picked (by_key.py): keys read by key from the maps, and the notification policies' decode.
    Writes settings_types.json next to out_path, for the record."""
    r = subprocess.run(["go", "run", f"{HERE}/settings_types.go", module_dir], cwd=GOPLS, capture_output=True,
                       text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"settings_types.go failed ({r.returncode}): {r.stderr[-3000:]}")
    generated = json.loads(r.stdout)
    with open(f"{os.path.dirname(out_path)}/settings_types.json", "w") as f:
        json.dump(generated, f, indent=2)
    spec = importlib.util.spec_from_file_location("grafana_by_key", f"{HERE}/by_key.py")
    by_key = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(by_key)

    edges = []
    for plugin, types in generated["datasources"].items():
        edges += [{"parent": DATASOURCE, "field": "JSONData", "child": t, "key": "", "before": f"type={plugin}"}
                  for t in types]
    for integration, target in generated["contact_points"].items():
        before = f"type={integration}"
        if "type" in target:
            edges.append({"parent": RECEIVER, "field": "Settings", "child": target["type"], "key": "", "before": before})
        for key, f in target.get("fields", {}).items():  # (a struct declared in NewConfig: its fields one by one)
            edges.append({"parent": RECEIVER, "field": "Settings", "child": f.get("type") or by_key.KIND_TYPES[f["kind"]],
                          "key": key, "before": before})
    for plugin, field, key, kind in by_key.keys():
        edges.append({"parent": DATASOURCE, "field": field, "child": by_key.KIND_TYPES[kind], "key": key,
                      "before": f"type={plugin}"})
    edges += [{"parent": p, "field": f, "child": c, "key": k} for p, f, c, k in by_key.OTHER_EDGES]
    edges.sort(key=lambda e: (e["parent"], e["field"], e.get("before", ""), e["key"], e["child"]))
    with open(out_path, "w") as f:
        json.dump({"roots": [], "edges": edges}, f, indent=2)
    return generated


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
# Generic maps the code reads by key, or not in Go at all (the documented keys under them can't be in the graph)
BY_KEY = {"providers.options.": "dashboard providers' options (read by key: dashboards/file_reader.go:77, 87)",
          "apps.jsonData.": "app plugins' jsonData (read by each app plugin)",
          "apps.secureJsonData.": "app plugins' secureJsonData (read by each app plugin)"}
# Datasource plugins whose documented keys can't be in the graph at all
NO_SOURCE = {"elasticsearch": "external plugin, no source here", "zipkin": "external plugin, no source here",
             "grafana-testdata-datasource": "testdata reads no settings"}


def graph_docs_reason(k, in_graph):
    """Why a key is only in the graph (in_graph) or only in the docs (the graph of 2026-11-17, with extra_graph's
    edges, vs the v13.1.0 docs)"""
    s = k.split(".")
    if in_graph:
        if s[-1] == "*" and any(k.startswith(p) for p in BY_KEY):
            return "generic map read by key (its documented keys are docs-only)"
        if k.startswith("datasources.type="):
            return "read by the plugin (or the SDK, for the plugins using its HTTP client), undocumented for it"
        if k.startswith("contactPoints.receivers.type="):
            return "decoded for the integration, undocumented"
        if k == "groups.rules.dasboardUid":
            return "dasboardUid: a misspelled alias of dashboardUid, kept on purpose"
        return "field undocumented"
    if k == "apiVersion":
        return "apiVersion: read by a separate decode (configVersion) that picks the versioned type"
    for prefix, what in BY_KEY.items():
        if k.startswith(prefix):
            return "under a generic map read by key: " + what
    if k.startswith("datasources.type="):
        plugin = s[1].removeprefix("type=")
        if plugin in NO_SOURCE:
            return NO_SOURCE[plugin]
        if s[-1].startswith(("httpHeaderName", "httpHeaderValue")) and not s[-1].endswith("1"):
            return "httpHeaderName/Value<i> for i > 1 (by_key.py lists i = 1)"
        return "not read by the plugin's Go code (frontend only, or a plugin that doesn't take the option)"
    if k.startswith("contactPoints.receivers.type="):
        return "not a field of what the integration decodes its settings into"
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
