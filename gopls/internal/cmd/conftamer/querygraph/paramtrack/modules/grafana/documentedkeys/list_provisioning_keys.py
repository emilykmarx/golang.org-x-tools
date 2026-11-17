#!/usr/bin/env python3
"""List the config keys documented for Grafana's provisioning YAML files (datasources, dashboards, plugins, alerting).

See README.md here for the sources, the output format and the judgment calls. In short: the docs give each file kind as
an example YAML block whose keys are preceded by `# <type> description` comments; the datasource docs add tables of
jsonData / secureJsonData keys. Those blocks aren't all valid YAML (e.g. Slack's `color: {{ if ... }}`), and the types
are only in comments, so this is a line-based parser, not a YAML library.

Usage: list_provisioning_keys.py [--grafana-dir DIR] [--out-dir DIR]
"""

import argparse
import json
import os
import re
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_GRAFANA = "/home/emily/projects/config_tracing/grafana-paramtrack"
PROVISIONING_MD = "docs/sources/administration/provisioning/index.md"
ALERTING_MD = "docs/sources/alerting/set-up/provision-alerting-resources/file-provisioning/index.md"
DATASOURCES_MD = "docs/sources/datasources/_index.md"

# Opaque values the docs show with example content whose keys aren't config keys: listed as `map`, children dropped.
# - apps.jsonData / secureJsonData: the example's only child is the placeholder `key: value` ("Custom per app").
# - groups.rules.data.model: the query model, "should be obtained through the API"; its keys depend on the datasource.
OPAQUE = {"apps.jsonData", "apps.secureJsonData", "groups.rules.data.model"}

# Datasource names in the jsonData tables, and the slugs of the docs' "Built-in core data sources" links => plugin ID
# (the plugin.json "id"; checked against public/app/plugins/datasource/*/plugin.json where the plugin is in the repo:
# Elasticsearch and Zipkin are external plugins in v13, so their IDs are the well-known ones).
PLUGIN_IDS = {
    "alertmanager": "alertmanager", "amazon cloudwatch": "cloudwatch", "aws-cloudwatch": "cloudwatch",
    "azure-monitor": "grafana-azure-monitor-datasource", "google-cloud-monitoring": "stackdriver",
    "graphite": "graphite", "influxdb": "influxdb", "opentsdb": "opentsdb", "prometheus": "prometheus",
    "elasticsearch": "elasticsearch", "loki": "loki", "jaeger": "jaeger", "tempo": "tempo", "zipkin": "zipkin",
    "parca": "parca", "pyroscope": "grafana-pyroscope-datasource", "mssql": "mssql", "mysql": "mysql",
    "postgresql": "grafana-postgresql-datasource", "postgres": "grafana-postgresql-datasource",
    "testdata": "grafana-testdata-datasource",
}
NOT_HTTP = {"mysql", "grafana-postgresql-datasource", "mssql"}  # the docs' note on _HTTP*_

KEY = re.compile(r"^([A-Za-z_][\w-]*)\s*:(?:\s+(.*))?$")


# ---------------------------------------------------------------------------------------------------------------
# Markdown

def md_sections(path):
    """[(heading, [lines])] for every heading level, in order."""
    out, head, lines = [], None, []
    in_code = False
    for line in open(path):
        line = line.rstrip("\n")
        if line.startswith("```"):
            in_code = not in_code
        if not in_code and re.match(r"^#{1,6} ", line):
            out.append((head, lines))
            head, lines = line.lstrip("#").strip(), []
        else:
            lines.append(line)
    out.append((head, lines))
    return out


def yaml_blocks(lines):
    """The ```yaml blocks in a section's lines."""
    blocks, cur = [], None
    for line in lines:
        if cur is None and line.strip() == "```yaml":
            cur = []
        elif cur is not None and line.strip() == "```":
            blocks.append(cur)
            cur = None
        elif cur is not None:
            cur.append(line)
    return blocks


def section(path, heading):
    found = [ls for h, ls in md_sections(path) if h == heading]
    assert len(found) == 1, (path, heading, len(found))
    return found[0]


# ---------------------------------------------------------------------------------------------------------------
# Example YAML blocks

def comment_type(text):
    """The `<type>` at the start of a comment's text (after '#'), with ", required"/", optional" dropped, or None."""
    text = text.strip()
    if not text.startswith("<"):
        return None
    depth = 0
    for i, c in enumerate(text):
        depth += {"<": 1, ">": -1}.get(c, 0)
        if depth == 0:
            typ = text[1:i]
            break
    else:  # unbalanced, e.g. "<map<string, string> a map of ..."
        typ = text[1:text.rindex(">")] + ">"
    return re.sub(r",\s*(required|optional)$", "", typ).strip()


def example_type(value, bare_items):
    """A type for a key with no `<type>` comment, from the example's value: example:<kind>."""
    v = (value or "").strip()
    if v.startswith("["):
        return "example:list"
    if not v:
        return "example:list" if bare_items else "example:none"
    if v in ("|", ">", "|-", ">-"):
        return "example:string"
    if v in ("true", "false"):
        return "example:bool"
    if re.fullmatch(r"-?\d+", v):
        return "example:int"
    return "example:string"


def parse_block(lines, prefix=()):
    """{path tuple: {"type", "value", "children", "bare"}} for every key in an example YAML block.

    Nesting follows the column of a key's text (after any "- "), so a list item's keys line up with its first key.
    A `# <type>` comment applies to the next key; a blank line or a non-type comment line doesn't reset it, a key does.
    Block scalars (`key: |`) skip their indented content.
    """
    nodes = {}
    stack = [(-1, tuple(prefix))]
    pending = None
    scalar_indent = None  # inside a block scalar whose key is at this column
    for raw in lines:
        if not raw.strip():
            continue
        m = re.match(r"^(\s*)(.*)$", raw)
        col, text = len(m.group(1)), m.group(2)
        if scalar_indent is not None:
            if col > scalar_indent:
                continue
            scalar_indent = None
        if text.startswith("#"):
            t = comment_type(text[1:])
            if t is not None:
                pending = t
            continue
        dash = re.match(r"^(?:-(?:\s+|$))*", text).group(0)
        is_item = bool(dash)
        col, text = col + len(dash), text[len(dash):]
        km = KEY.match(text)
        while stack[-1][0] >= col:
            stack.pop()
        parent = stack[-1][1]
        if not km:  # a bare list item ("- abc", "- ['a', '=', 'b']"): a value of the parent key
            if is_item and parent in nodes:
                nodes[parent]["bare"] = True
            pending = None
            continue
        key, value = km.group(1), (km.group(2) or "")
        value = re.sub(r"\s+#.*$", "", value)
        path = parent + (key,)
        nodes[path] = {"type": pending, "value": value, "children": False, "bare": False}
        if parent in nodes:
            nodes[parent]["children"] = True
        pending = None
        if value.strip() in ("|", ">", "|-", ">-"):
            scalar_indent = col
        stack.append((col, path))
    return nodes


def leaves(nodes):
    """{dotted key: type} for the keys that take a value (no child keys), with OPAQUE keys as maps."""
    out = {}
    # A `<map<K, V>>` comment (annotations, labels) is a map of unstructured pairs: its example children aren't keys.
    # A plain `<map>` with children (jsonData, MQTT's tlsConfig) is a structured object: its children are keys.
    opaque = OPAQUE | {".".join(p) for p, n in nodes.items() if (n["type"] or "").startswith("map<")}
    for path, n in nodes.items():
        key = ".".join(path)
        if any(key.startswith(o + ".") for o in opaque):
            continue
        if key in opaque:
            out[key] = "map"
            continue
        if n["children"]:
            continue  # only groups other keys
        typ = n["type"] or example_type(n["value"], n["bare"])
        if typ.startswith("map"):
            typ = "map"  # unstructured pairs (annotations, labels)
        out[key] = typ
    return out


# ---------------------------------------------------------------------------------------------------------------
# Datasources' jsonData / secureJsonData tables

def md_table(lines):
    """Rows (list of cell strings) of the first markdown table in lines, without header and separator."""
    rows = [l for l in lines if l.startswith("|")]
    return [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows[2:]]


def core_plugins(grafana):
    """Plugin IDs of the docs' "Built-in core data sources" (DATASOURCES_MD), from their link slugs."""
    ids = []
    in_core = False
    for line in open(os.path.join(grafana, DATASOURCES_MD)):
        if line.startswith("## "):
            in_core = line.strip() == "## Built-in core data sources"
        elif in_core and (m := re.match(r"^- \[.*\]\(([\w-]+)/\)", line)):
            ids.append(PLUGIN_IDS[m.group(1)])
    return ids


def check_plugin_ids(grafana):
    """Every ID in PLUGIN_IDS that has a plugin.json in the repo matches it (others are external plugins)."""
    root = os.path.join(grafana, "public/app/plugins/datasource")
    repo_ids = {json.load(open(os.path.join(root, d, "plugin.json")))["id"]
                for d in os.listdir(root) if os.path.exists(os.path.join(root, d, "plugin.json"))}
    return sorted(set(PLUGIN_IDS.values()) - repo_ids)


def table_plugins(cell, http):
    """Plugin IDs named in a table's "Data source" cell, with _HTTP\\*_ expanded."""
    out = []
    for name in re.split(r",|\band\b", cell):
        name = name.strip()
        if not name:
            continue
        if name == r"_HTTP\*_":
            out += http
        else:
            out.append(PLUGIN_IDS[name.lower()])
    return out


def json_tables(grafana):
    """{"jsonData"/"secureJsonData": {key: (type, [plugin IDs])}} from the provisioning doc's tables."""
    md = os.path.join(grafana, PROVISIONING_MD)
    http = [p for p in core_plugins(grafana) if p not in NOT_HTTP]
    out = {}
    for field, heading in (("jsonData", "JSON data"), ("secureJsonData", "Secure JSON data")):
        out[field] = {}
        for name, typ, cell, _desc in md_table(section(md, heading)):
            out[field][name.strip("`")] = (typ, table_plugins(cell, http))
    return out, http


# ---------------------------------------------------------------------------------------------------------------
# The four file kinds

def datasources(grafana):
    """(keys, keys with type=<plugin> segments, collapsed {key: plugins})."""
    md = os.path.join(grafana, PROVISIONING_MD)
    [example] = yaml_blocks(section(md, "Example data source configuration file"))
    [headers] = yaml_blocks(section(md, "Custom HTTP headers for data sources"))
    nodes = parse_block(example)
    keys = leaves(nodes)
    header_keys = leaves(parse_block(headers))
    example_plugin = nodes[("datasources", "type")]["value"].strip()  # graphite
    tables, http = json_tables(grafana)

    plugins = {}  # datasources.<field>.<key> => plugin IDs, for the type= variant
    for k, typ in header_keys.items():
        if k.startswith(("datasources.jsonData.", "datasources.secureJsonData.")):
            keys.setdefault(k, typ)
            plugins[k] = http  # custom headers: an HTTP feature (README)
    for field, rows in tables.items():
        for name, (typ, ps) in rows.items():
            k = f"datasources.{field}.{name}"
            keys[k] = typ  # the table's type wins over the example's comment
            plugins[k] = ps
    for k in keys:
        if k.startswith(("datasources.jsonData.", "datasources.secureJsonData.")) and k not in plugins:
            plugins[k] = [example_plugin]

    by_type = {}
    for k, typ in keys.items():
        if k in plugins:
            rest = k[len("datasources."):]
            for p in dict.fromkeys(plugins[k]):
                by_type[f"datasources.type={p}.{rest}"] = typ
        else:
            by_type[k] = typ
    collapsed = {k: list(dict.fromkeys(ps)) for k, ps in plugins.items() if len(set(ps)) > 1}
    return keys, by_type, collapsed


def dashboards(grafana):
    md = os.path.join(grafana, PROVISIONING_MD)
    [example] = yaml_blocks(section(md, "Example dashboard configuration file"))
    return leaves(parse_block(example))


def plugins_(grafana):
    md = os.path.join(grafana, PROVISIONING_MD)
    [example] = yaml_blocks(section(md, "Example plugin configuration file"))
    return leaves(parse_block(example))


ALERTING_SECTIONS = ["Import alert rules", "Import contact points", "Import notification template groups",
                     "Import notification policies", "Import mute timings"]


def alerting(grafana):
    md = os.path.join(grafana, ALERTING_MD)
    keys = {}
    for heading in ALERTING_SECTIONS:
        for block in yaml_blocks(section(md, heading)):
            keys.update(leaves(parse_block(block)))
    # Contact point settings: one block per integration under "### Settings", each `type: <x>` then `settings:`.
    # Their keys go under contactPoints.receivers.type=<x>.settings (README: k=v segments)
    generic_settings = "contactPoints.receivers.settings."
    keys = {k: t for k, t in keys.items() if not k.startswith(generic_settings)}  # the generic example's url
    in_settings = False
    for h, lines in md_sections(md):
        if h == "Settings":
            in_settings = True
        elif h and h.startswith("Import ") or h == "Template variable interpolation":
            in_settings = False
        if not in_settings:
            continue
        for block in yaml_blocks(lines):
            first = KEY.match(block[0])
            assert first and first.group(1) == "type", block[0]
            typ = first.group(2).strip()
            nodes = parse_block(block[1:], prefix=("contactPoints", "receivers", f"type={typ}"))
            keys.update(leaves(nodes))
    return keys


# ---------------------------------------------------------------------------------------------------------------

def check(keys):
    for k in keys:
        for seg in k.split("."):
            assert seg and "*" not in seg and "<" not in seg, k


def write(path, keys):
    check(keys)
    with open(path, "w") as f:
        for k, t in keys.items():
            f.write(f"{k}\t{t}\n")
    print(f"{os.path.basename(path)}: {len(keys)} keys")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--grafana-dir", default=DEFAULT_GRAFANA)
    ap.add_argument("--out-dir", default=HERE)
    a = ap.parse_args()
    g, o = a.grafana_dir, a.out_dir

    external = check_plugin_ids(g)
    print("plugin IDs with no plugin.json in the repo (external plugins):", ", ".join(external))
    ds, ds_by_type, collapsed = datasources(g)
    write(os.path.join(o, "datasources_keys.txt"), ds)
    write(os.path.join(o, "datasources_keys_by_type.txt"), ds_by_type)
    with open(os.path.join(o, "datasources_collapsed.tsv"), "w") as f:
        for k, ps in sorted(collapsed.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            f.write(f"{k}\t{len(ps)}\t{','.join(ps)}\n")
    print(f"datasources_collapsed.tsv: {len(collapsed)} keys documented for several plugins")
    write(os.path.join(o, "dashboards_keys.txt"), dashboards(g))
    write(os.path.join(o, "plugins_keys.txt"), plugins_(g))
    write(os.path.join(o, "alerting_keys.txt"), alerting(g))


if __name__ == "__main__":
    main()
