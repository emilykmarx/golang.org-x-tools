#!/usr/bin/env python3
"""Statistics on how well the config-tracing workflow's proxies represent what they stand for.

See STATISTICS.md (next to this file) for the questions, definitions and answers. This script computes the numbers:
it writes stats_tables.md, CSVs and charts to --out.

Run with: uv run --with pandas --with matplotlib python stats.py [flags]
"""

import argparse
import collections
import glob
import json
import os
import re
import subprocess

import matplotlib
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

STAR = "*"
CUSTOM = " (custom UnmarshalYAML)"
CT = "/home/emily/projects/config_tracing"
RESULTS = f"{CT}/golang.org-x-tools/gopls/results/prom/stats"

# Reference palette (dataviz skill, light mode)
BLUE, ORANGE, GRAY, INK, INK2 = "#2a78d6", "#eb6834", "#c9c8c2", "#0b0b0b", "#52514e"


def segs(key):
    return key.split(".")


def instance_of(key, pattern):
    """Whether concrete key `key` is an instance of `pattern` (STAR matches any one segment)."""
    a, b = segs(key), segs(pattern)
    return len(a) == len(b) and all(p == STAR or p == s for s, p in zip(a, b))


def path_nodes(path):
    return path.split(" (")[0].split(" => ")


def map_field_keys(graph_path):
    """Graph node => keys of its map fields (fields whose values are a map, by their yaml tag; the decoded struct's
    fields if it has DecodedFields). Also records the
    graph's map nodes in MAP_NODES."""
    out = {}
    for v in json.load(open(graph_path))["Vertices"]:
        if v.get("IsMap"):
            MAP_NODES.add(v["Names"][0])
        keys = set()
        # The decoded struct's fields, if a custom unmarshaler decodes into another struct (as configkeys' KeyFieldNames)
        if v.get("DecodedFields"):
            map_tags = [d.get("Tag", "") for d in v["DecodedFields"].values() if d.get("IsMap")]
        else:
            map_tags = [(v.get("Tags") or {}).get(field, "") for field in v.get("MapFields") or {}]
        for tag in map_tags:
            m = re.search(r'yaml:"([^",]*)', tag)
            if m and m.group(1) not in ("", "-"):
                keys.add(m.group(1))
        out[v["Names"][0]] = keys
    return out


MAP_FIELDS = {}  # set in main
MAP_NODES = set()  # graph nodes that are maps; set in main


def section(fk, path):
    """Section of full key fk via path: its parent key and Unmarshaler (the path's last node). As in configkeys'
    `section keys`, a map's values (<map key>.*) are in the section of the struct holding the map: for a map field,
    the Unmarshaler's, and for a map that's its own node, the node before it on the path's."""
    s = segs(fk)
    nodes = path_nodes(path)
    u = nodes[-1]
    if s[-1] == STAR and len(s) >= 2:
        if u in MAP_NODES and len(nodes) > 1:
            return f"{'.'.join(s[:-2])} @ {nodes[-2]}"
        if s[-2] in MAP_FIELDS.get(u, ()):
            return f"{'.'.join(s[:-2])} @ {u}"
    return f"{parent(fk)} @ {u}"


def parent(key):
    return ".".join(segs(key)[:-1])


def split(cell):
    return [x for x in cell.split("; ") if x] if cell else []


# Kind of value for --strings-only purposes: what step (1) searches (see configkeys keepString)
def searchable(kinds):
    return any(k in ("string", "struct") for k in kinds.split("; "))


# ---------------------------------------------------------------------------------------------------------------
# Inputs


def sd_names(prom_dir):
    """Graph type name of each service discovery config => its key under scrape_configs / alertmanagers.
    From the configs' Name() methods (discovery.RegisterConfig registers them as <Name()>_sd_configs)."""
    rx = re.compile(r'func \((?:\w+ )?\*?(\w+)\) Name\(\) string \{\s*return "(\w+)"')
    alias_rx = re.compile(r"^type (\w+) = (\w+)$", re.M)  # e.g. type KumaSDConfig = SDConfig: the graph names Y
    names = {}
    for f in glob.glob(f"{prom_dir}/discovery/**/*.go", recursive=True):
        if f.endswith("_test.go"):
            continue
        pkg = os.path.relpath(os.path.dirname(f), prom_dir)
        src = open(f).read()
        aliases = dict(alias_rx.findall(src))
        for typ, name in rx.findall(src):
            key = "static_configs" if name == "static" else f"{name}_sd_configs"
            names[f"/{pkg}.{typ}"] = key
            if typ in aliases:
                names[f"/{pkg}.{aliases[typ]}"] = key
    return names


def expand_sd(pattern, path, sd):
    """The pattern with its service discovery `*` replaced by the SD config's key, for the SD type on the path.
    Returns the pattern unchanged if the path doesn't go through discovery.Configs, and None for the catch-all
    (a path ending at discovery.Configs' dynamic unmarshaler)."""
    nodes = path_nodes(path)
    if "/discovery.Configs" not in nodes:
        return pattern
    sd_nodes = [n for n in nodes if n in sd]
    if path.endswith(CUSTOM) or not sd_nodes:
        return None
    s = segs(pattern)
    i = 1 if s[0] == "scrape_configs" else 2 if s[:2] == ["alerting", "alertmanagers"] else None
    assert i is not None and s[i] == STAR, (pattern, path)
    s[i] = sd[sd_nodes[0]]
    return ".".join(s)


def load_configkeys(path, sd):
    """Full keys (graph patterns with SD `*`s resolved) and concrete keys, from the configkeys csv."""
    ck = pd.read_csv(path, keep_default_na=False, dtype=str)
    ck.columns = [c.replace(" ", "_") for c in ck.columns]
    full = {}  # full key => info
    catchall = set()
    for r in ck[ck.source != "config"].itertuples():
        for p in split(r.Unmarshaler_Path):
            fk = expand_sd(r.param_key, p, sd)
            if fk is None:
                catchall.add(r.param_key)
                continue
            info = full.setdefault(fk, {"pattern": r.param_key, "unmarshalers": set(), "paths": set(), "kind": r.value_kind})
            info["unmarshalers"].add(path_nodes(p)[-1])
            info["paths"].add(p)

    config = {}  # concrete key => info
    for r in ck[ck.source != "graph"].itertuples():
        info = {"values": json.loads(r.values) if r.values else [], "full_keys": set(), "unmarshalers": set(),
                "kind": r.value_kind, "paths": split(r.Unmarshaler_Path)}
        patterns = split(r.pattern) or [r.param_key]
        for p in info["paths"]:
            for pat in patterns:
                fk = expand_sd(pat, p, sd)
                if fk is not None and instance_of(r.param_key, fk) and fk in full:
                    info["full_keys"].add(fk)
                    info["unmarshalers"].add(path_nodes(p)[-1])
        config[r.param_key] = info
    return full, config, catchall


def check_sections(fkdf, configkeys_csv, out):
    """Check that the sections computed here (<parent key> @ <Unmarshaler>) have the same keys as configkeys'
    `section keys` column. Comparable where a full key's pattern resolves to just that full key (all keys outside
    service discovery, and SD keys whose pattern is in only one SD config): elsewhere configkeys combines the sections of
    all SD configs with the key. Compared as patterns (the column has them, with the SD `*`)."""
    ck = pd.read_csv(configkeys_csv, keep_default_na=False, dtype=str).set_index("param_key")
    pattern_of = dict(zip(fkdf.key, fkdf.pattern))
    n_full = fkdf.drop_duplicates("key").groupby("pattern").key.size()
    groups = fkdf.groupby("section").key.apply(lambda ks: {pattern_of[k] for k in ks})
    comparable = fkdf.drop_duplicates("key")
    comparable = comparable[comparable.pattern.map(n_full) == 1]
    mismatches = []
    for r in comparable.itertuples():
        column = {k for k in ck.loc[r.pattern, "section keys"].split("; ") if k}
        if groups[r.section] != column:
            mismatches.append(r.key)
    out.p(f"- Check: sections computed here vs configkeys' `section keys` column, for the {len(comparable)} full keys "
          f"whose pattern resolves to just that key ({(~comparable.sd).sum()} outside service discovery): "
          f"{len(mismatches)} mismatches" + (f": {mismatches}" if mismatches else ""))
    if mismatches:
        print(f"Warning: {len(mismatches)} full keys' sections differ from configkeys' section keys: {mismatches[:10]}")


def go_string_literals(module_dir):
    """String literals in the non-test Go files compiled into Prometheus, dependencies and standard library included
    (golits/), with newlines written as a literal \\n, as the values are"""
    golits = f"{os.path.dirname(os.path.abspath(__file__))}/golits"
    out = subprocess.run(["go", "run", ".", "-dir", module_dir], cwd=golits, capture_output=True, text=True,
                         timeout=600, check=True).stdout
    return {json.loads(line).replace("\n", "\\n") for line in out.splitlines()}


def read_docs(path):
    """Documented keys => type, with map keys as `<key>.*` and the docs' <placeholder> segments as `*`"""
    docs = {}
    for line in open(path):
        key, _, typ = line.rstrip("\n").partition("\t")
        key = ".".join(STAR if s.startswith("<") else s for s in segs(key))
        if typ == "map":
            key += "." + STAR
        docs[key] = typ
    # A map the docs also give children (http_headers.<string>.values): the graph has only the children
    for key in [k for k in docs if docs[k] == "map"]:
        if any(k.startswith(key + ".") for k in docs):
            del docs[key]
    return docs


def docs_kind(typ):
    """Kind of Go value we'd expect for a documented type"""
    t = typ.strip("[] ")
    if t.startswith("<boolean>"):
        return "bool"
    if any(t.startswith(x) for x in ("<duration>", "<int>", "<float>", "<size>")):
        return "number"
    if typ == "map" or typ == "":
        return "map"
    return "string"


# ---------------------------------------------------------------------------------------------------------------
# Visualizations: how far each full key, and each graph node's keys, get through the workflow

# Coverage tiers, from least to most far along
TIERS = [
    "not documented",  # in the graph, but not in the docs
    "documented",  # in the docs, but not in the concrete config
    "in concrete config",  # set by the concrete config, but no value searched by step (1)
    "searched",  # a value searched by step (1), but not matched by argmatch
    "matched",  # a value matched by argmatch
]
NO_KEYS = "no full keys"  # reachable from /config.Config, but no full key ends at the node
OTHER_ROOT = "not under /config.Config"

# Node colours: bins of the fraction of the node's full keys that argmatch matched (upper bound, legend text, colour).
# Dataviz reference palette: an ordinal ramp of blue steps 250-650, validated with validate_palette.js --ordinal.
MATCHED_BINS = [
    (0, "0% matched", "#86b6ef"),
    (0.1, "up to 10% matched", "#5598e7"),
    (1 / 3, "10-33% matched", "#2a78d6"),
    (2 / 3, "33-67% matched", "#1c5cab"),
    (1, "67-100% matched", "#104281"),
]
NODE_GREYS = {NO_KEYS: "#c9c8c2", OTHER_ROOT: "#e4e3df"}  # nodes with no full keys


def node_size(n_keys):
    """Diameter in inches of a node with n_keys full keys: area grows with the number of keys"""
    return round(0.25 + 0.075 * n_keys ** 0.5, 3) if n_keys else 0.2


def key_tier(r):
    """Index into TIERS of how far a full key (row of full_keys.csv) gets"""
    return 4 if r.matched else 3 if r.searched else 2 if r.in_config else 1 if r.documented else 0


def node_rows(fkdf, graph_path):
    """Attributes per graph node (for prettify's --node-attrs), from the full keys in fkdf: each node's full keys are
    those whose Unmarshaler it is"""
    graph = json.load(open(graph_path))
    adj = collections.defaultdict(set)
    for e in graph["Edges"]:
        adj[e["Source"]].add(e["Target"])
    reachable, todo = {"/config.Config"}, ["/config.Config"]
    while todo:
        for t in adj[todo.pop()] - reachable:
            reachable.add(t)
            todo.append(t)
    rows = []
    for v in graph["Vertices"]:
        node = v["Names"][0]
        ks = fkdf[fkdf.unmarshaler == node].drop_duplicates("key")
        matched_fraction = round(ks.matched.mean(), 3) if len(ks) else 0
        if len(ks):
            tier = TIERS[max(key_tier(r) for r in ks.itertuples())]
            matched_bin, color = next((text, c) for bound, text, c in MATCHED_BINS if matched_fraction <= bound)
        else:
            tier = NO_KEYS if node in reachable else OTHER_ROOT
            matched_bin, color = tier, NODE_GREYS[tier]
        size = node_size(len(ks))
        rows.append({"node": node, "coverage": tier, "matched_bin": matched_bin,
                     # Graphviz attributes: a circle sized by the number of full keys, filled by matched_fraction's
                     # bin (visualize.sh moves prettify's label outside the circle, where it fits)
                     "shape": "circle", "fixedsize": "true", "width": size, "height": size, "style": "filled",
                     "color": color, "fillcolor": color,
                     "full_keys": len(ks), "documented": int(ks.documented.sum()), "in_config": int(ks.in_config.sum()),
                     "searched": int(ks.searched.sum()), "matched": int(ks.matched.sum()),
                     # Fractions of the node's full keys, e.g. for sizing nodes in gephi (the tier is the furthest key's,
                     # so a type shared by many sections, like TLSConfig, is "matched" if any one of its keys is)
                     "in_config_fraction": round(ks.in_config.mean(), 3) if len(ks) else 0,
                     "matched_fraction": matched_fraction})
    return pd.DataFrame(rows).sort_values("node")


def sd_key_of(fk, sd_keys):
    """The SD config key (e.g. azure_sd_configs) a full key is under, or None"""
    s = segs(fk)
    if s[0] == "scrape_configs" and len(s) > 2 and s[1] in sd_keys:
        return s[1]
    if s[:2] == ["alerting", "alertmanagers"] and len(s) > 3 and s[2] in sd_keys:
        return s[2]
    return None


def write_full_keys_yaml(keys, path, tiers):
    """Keys-only YAML: nested mappings down to each full key (rows of `keys`), `{}` at the leaves (as in
    prometheus_config_keys.yaml), with each key's kind as a comment, and each mapping's counts. With tiers, each key's
    comment also gives its coverage tier, and each mapping's counts are per tier; without (--full-keys-only, before
    there's a concrete config), whether each key is documented."""
    tree = {}
    for r in keys.sort_values("key").itertuples():
        t = tree
        for seg in segs(r.key)[:-1]:
            t = t.setdefault(seg, {})
            assert isinstance(t, dict), f"{r.key}: a prefix is also a full key"
        assert segs(r.key)[-1] not in t, f"{r.key}: also a prefix of another full key"
        t[segs(r.key)[-1]] = r
    counted = TIERS[1:] if tiers else ["documented"]

    def counts(t):
        c = collections.Counter()
        for v in t.values():
            if isinstance(v, dict):
                c.update(counts(v))
            else:
                c["keys"] += 1
                c["documented"] += v.documented
                if tiers:
                    c["in concrete config"] += v.in_config
                    c["searched"] += v.searched
                    c["matched"] += v.matched
        return c

    def quote(seg):
        return f'"{seg}"' if seg == STAR else seg

    lines = ["# Generated by querygraph/configkeys/stats/stats.py: the full keys (see STATISTICS.md), from /config.Config."]
    if tiers:
        lines += ["# Each key's comment says how far it gets: " + " < ".join(TIERS) + "; then its value kind.",
                  "# Each mapping's comment counts its keys that are documented, in the concrete config, searched and "
                  "matched."]
    else:
        lines += ["# Each key's comment says whether it's documented, then its value kind (\"*\" is any map key).",
                  "# Each mapping's comment counts its keys, and those documented."]

    def walk(t, indent):
        for seg in sorted(t):
            v = t[seg]
            pad = "    " * indent
            if isinstance(v, dict):
                c = counts(v)
                lines.append(f"{pad}{quote(seg)}:  # {c['keys']} keys: " +
                             ", ".join(f"{c[name]} {name}" for name in counted))
                walk(v, indent + 1)
            elif tiers:
                undoc = "; not documented" if not v.documented and v.tier != TIERS[0] else ""
                lines.append(f"{pad}{quote(seg)}: {{}}  # {v.tier}{undoc}; {v.kind}")
            else:
                lines.append(f"{pad}{quote(seg)}: {{}}  # {'documented' if v.documented else 'not documented'}; "
                             f"{v.kind}")

    walk(tree, 0)
    open(path, "w").write("\n".join(lines) + "\n")


def write_visuals(fkdf, config, covered_by, searched, matched, sd, args, out):
    """Write full_keys.csv with searched/matched columns, node_coverage.csv and node_coverage_keyed.csv (attributes
    per graph node, for prettify's --node-attrs), sd_configs.csv, and full_keys.yaml (a keys-only YAML of the full
    keys, annotated)"""
    def values_of(fk):
        return {v.replace("\n", "\\n") for k in covered_by.get(fk, ()) for v in config[k]["values"]}

    fkdf["searched"] = fkdf.key.map(lambda fk: bool(values_of(fk) & searched))
    fkdf["matched"] = fkdf.key.map(lambda fk: bool(values_of(fk) & matched))
    fkdf["tier"] = [TIERS[key_tier(r)] for r in fkdf.itertuples()]
    fkdf.to_csv(f"{args.out}/full_keys.csv", index=False)
    keys = fkdf.drop_duplicates("key")

    # Graph nodes: tier of the furthest full key ending at the node
    nodes = node_rows(fkdf, args.graph)
    nodes.to_csv(f"{args.out}/node_coverage.csv", index=False)

    # For the keyed graph: leave out the SD configs none of whose full keys argmatch matched (all the keys under the
    # SD config's key, wherever they end: e.g. static_configs' keys all end at targetgroup.Group), and every full key
    # under them (e.g. scrape_configs.azure_sd_configs.tls_config.ca_file, whose Unmarshaler is TLSConfig).
    sd_keys = set(sd.values())
    under = keys.assign(sd_key=keys.key.map(lambda fk: sd_key_of(fk, sd_keys)))
    sd_df = pd.DataFrame([{"node": n, "sd_key": k, "keys": int((under.sd_key == k).sum()),
                           "keys_matched": int(under[under.sd_key == k].matched.sum())} for n, k in sorted(sd.items())
                          if n in set(nodes.node)])
    sd_df["left_out"] = sd_df.keys_matched == 0
    sd_df.to_csv(f"{args.out}/sd_configs.csv", index=False)
    left_out_keys = set(sd_df[sd_df.left_out].sd_key)
    keyed = fkdf[fkdf.key.map(lambda fk: sd_key_of(fk, sd_keys) not in left_out_keys)]
    node_rows(keyed, args.graph).to_csv(f"{args.out}/node_coverage_keyed.csv", index=False)

    # Legend entries for the node colours, in order (for visualize.sh)
    pd.DataFrame([(text, c) for _, text, c in MATCHED_BINS] + list(NODE_GREYS.items()),
                 columns=["matched_bin", "color"]).to_csv(f"{args.out}/node_legend.csv", index=False)
    out.h("Visualizations")
    out.p("Graph nodes by coverage tier (the furthest tier of the full keys ending at the node):")
    out.table(nodes.coverage.value_counts().rename_axis("coverage").reset_index())
    out.p("Full keys by coverage tier:")
    out.table(keys.tier.value_counts().rename_axis("tier").reset_index())

    write_full_keys_yaml(keys, f"{args.out}/full_keys.yaml", tiers=True)


# ---------------------------------------------------------------------------------------------------------------
# Output helpers


class Out:
    def __init__(self, path):
        self.lines = []
        self.path = path

    def h(self, text, level=2):
        self.lines += ["", "#" * level + " " + text, ""]

    def p(self, text=""):
        self.lines.append(text)

    def table(self, df, index=False):
        self.lines += ["", df.to_markdown(index=index), ""]

    def write(self):
        open(self.path, "w").write("\n".join(self.lines).lstrip() + "\n")


def pct(a, b):
    return f"{a}/{b} ({100 * a / b:.0f}%)" if b else f"{a}/0"


def style(ax, title):
    ax.set_title(title, loc="left", color=INK, fontsize=11)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRAY)
    ax.tick_params(colors=INK2, labelsize=9)
    ax.grid(axis="x", color="#ecebe7", linewidth=0.8)
    ax.set_axisbelow(True)


def stacked_barh(df, cols, colors, labels, title, xlabel, path):
    """Horizontal bars, one per row of df, stacked over cols, with the total at the end of each bar"""
    fig, ax = plt.subplots(figsize=(8, 0.35 * len(df) + 1.2), facecolor="#fcfcfb")
    ax.set_facecolor("#fcfcfb")
    left = [0] * len(df)
    for col, color, label in zip(cols, colors, labels):
        ax.barh(df.index, df[col], left=left, color=color, label=label, height=0.6, edgecolor="#fcfcfb", linewidth=2)
        left = [a + b for a, b in zip(left, df[col])]
    for y, (total, first) in enumerate(zip(left, df[cols[0]])):
        ax.text(total + max(left) * 0.01, y, f"{first}/{total}", va="center", fontsize=8, color=INK2)
    ax.invert_yaxis()
    ax.set_xlabel(xlabel, color=INK2, fontsize=9)
    style(ax, title)
    ax.legend(frameon=False, fontsize=9, loc="lower right")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


# ---------------------------------------------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--configkeys", default=f"{RESULTS}/configkeys.csv", help="configkeys csv (with --config)")
    ap.add_argument("--param-values", default=f"{RESULTS}/param_values.txt",
                    help="values step (1) searches for (configkeys --txt-dir --strings-only)")
    ap.add_argument("--graph", default=f"{RESULTS}/unmarshaler_subgraph.text",
                    help="the Unmarshaler Subgraph configkeys was run on")
    ap.add_argument("--docs", default=f"{CT}/prom-test-infra/prombench/prometheus_config_keys.txt",
                    help="documented keys (list_config_keys.py)")
    ap.add_argument("--prometheus", default=f"{CT}/prometheus", help="Prometheus checkout the graph was built from")
    ap.add_argument("--argmatch", default=f"{CT}/dump_ancestors/argmatch-experiment/sweep-c30-1/needle_types.csv",
                    help="needle_types.csv of an argmatch run (step (1) results)")
    ap.add_argument("--out", default=RESULTS)
    ap.add_argument("--full-keys-only", action="store_true",
                    help="Only write full_keys.yaml, annotated with what's known without a concrete config (from "
                         "--configkeys run without --config; --param-values and --argmatch are unused)")
    args = ap.parse_args()
    out = Out(f"{args.out}/stats_tables.md")
    out.p("<!-- Generated by querygraph/configkeys/stats/stats.py; see STATISTICS.md there for what these mean. -->")
    out.p("# Statistics tables (Prometheus, root /config.Config)")

    MAP_FIELDS.update(map_field_keys(args.graph))
    sd = sd_names(args.prometheus)
    full, config, catchall = load_configkeys(args.configkeys, sd)
    docs = read_docs(args.docs)

    # Per full key: section, top-level section, coverage by the config
    covered_by = collections.defaultdict(set)  # full key => concrete keys
    for k, info in config.items():
        for fk in info["full_keys"]:
            covered_by[fk].add(k)
    sd_keys = set(sd.values())

    def under_sd(fk):
        """Whether the key is in a service discovery config"""
        s = segs(fk)
        return (s[0] == "scrape_configs" and len(s) > 2 and s[1] in sd_keys) or \
            (s[:2] == ["alerting", "alertmanagers"] and len(s) > 3 and s[2] in sd_keys)

    fk_rows = []
    for fk, info in sorted(full.items()):
        for u, sec in sorted({(path_nodes(p)[-1], section(fk, p)) for p in info["paths"]}):
            fk_rows.append({"key": fk, "pattern": info["pattern"], "unmarshaler": u, "kind": info["kind"],
                            "top": segs(fk)[0], "section": sec, "map_key": segs(fk)[-1] == STAR,
                            "in_config": fk in covered_by, "documented": fk in docs,
                            "searchable": searchable(info["kind"]), "sd": under_sd(fk)})
    fkdf = pd.DataFrame(fk_rows)
    if args.full_keys_only:
        write_full_keys_yaml(fkdf.drop_duplicates("key"), f"{args.out}/full_keys.yaml", tiers=False)
        print(f"wrote {args.out}/full_keys.yaml: {fkdf.key.nunique()} full keys")
        return
    searched = set(open(args.param_values).read().splitlines())
    fkdf.to_csv(f"{args.out}/full_keys.csv", index=False)
    keys = fkdf.drop_duplicates("key")

    # =============================================================================================================
    out.h("Inputs")
    out.p(f"- Full keys (graph patterns, SD `*` resolved by Name()): {len(full)}; excluded catch-all patterns of "
          f"discovery.Configs' dynamic unmarshaler: {', '.join(sorted(catchall))}")
    out.p(f"- SD config types found (Name()): {len(set(sd.values()))}")
    out.p(f"- Concrete keys: {len(config)}; with a full key: {sum(1 for c in config.values() if c['full_keys'])}")
    unresolved = sorted(k for k, c in config.items() if not c["full_keys"])
    if unresolved:
        out.p(f"- Concrete keys with no full key: {unresolved}")
    out.p(f"- Documented keys: {len(docs)}")
    out.p(f"- Values searched by step (1) (strings-only): {len(searched)}")

    # =============================================================================================================
    out.h("Q1. Concrete config vs full config")
    n_cov = keys.in_config.sum()
    out.p(f"- Full keys instantiated by the config: {pct(n_cov, len(keys))}")
    out.p(f"  - excluding map keys (`*`): {pct(keys[~keys.map_key].in_config.sum(), (~keys.map_key).sum())}")
    out.p(f"  - documented full keys: {pct(keys[keys.documented].in_config.sum(), keys.documented.sum())}")
    by_top = keys.groupby("top").agg(full_keys=("key", "size"), in_config=("in_config", "sum")).sort_values(
        "full_keys", ascending=False)
    by_top["not_in_config"] = by_top.full_keys - by_top.in_config
    by_top["%"] = (100 * by_top.in_config / by_top.full_keys).round(0).astype(int)
    out.p("\nBy top-level section:")
    out.table(by_top, index=True)
    stacked_barh(by_top, ["in_config", "not_in_config"], [BLUE, GRAY], ["in the config", "not in the config"],
                 "Full keys per top-level section, by whether the config sets them", "full keys",
                 f"{args.out}/q1_keys_by_top_section.png")

    # Sections
    secs = fkdf.groupby("section").agg(size=("key", "nunique"), in_config=("in_config", "sum"),
                                       top=("top", "first"), searchable=("searchable", "sum")).reset_index()
    secs["covered"] = secs.in_config > 0
    check_sections(fkdf, args.configkeys, out)
    out.p(f"- Sections (keys with the same parent key and Unmarshaler) with at least one key in the config: "
          f"{pct(secs.covered.sum(), len(secs))}")
    sec_top = secs.groupby("top").agg(sections=("section", "size"), covered=("covered", "sum"))
    out.table(sec_top.sort_values("sections", ascending=False), index=True)

    # Types
    types = fkdf.groupby("unmarshaler").agg(keys=("key", "nunique"), in_config=("in_config", "sum")).reset_index()
    types["covered"] = types.in_config > 0
    out.p(f"- Unmarshaler types with full keys (last node of a full key's path) with a key in the config: "
          f"{pct(types.covered.sum(), len(types))}")
    out.p("  - Types with no key in the config: " + ", ".join(
        f"`{t}` ({n})" for t, n in types[~types.covered].sort_values("keys", ascending=False)[["unmarshaler", "keys"]]
        .itertuples(index=False)))

    # SD configs
    sd_rows = []
    for parent_key in ("scrape_configs", "alerting.alertmanagers"):
        for name in sorted(set(sd.values())):
            prefix = f"{parent_key}.{name}."
            ks = keys[keys.key.str.startswith(prefix)]
            sd_rows.append({"parent": parent_key, "sd": name, "full_keys": len(ks), "in_config": ks.in_config.sum()})
    sddf = pd.DataFrame(sd_rows)
    used = sddf[sddf.in_config > 0]
    out.p(f"- Service discovery configs used by the config: {len(used)} of {len(sddf)} (parent, SD) pairs: "
          + ", ".join(f"{r.parent}.{r.sd} ({r.in_config}/{r.full_keys})" for r in used.itertuples()))
    out.p(f"  - Their keys are {pct(sddf.full_keys.sum(), len(keys))} of all full keys")

    # Summary, with and without service discovery
    def summary(f):
        k = f.drop_duplicates("key")
        s_ = f.groupby("section").in_config.any()
        t_ = f.groupby("unmarshaler").in_config.any()
        return pd.DataFrame([
            ("Full keys", len(k), k.in_config.sum()),
            ("Sections", len(s_), s_.sum()),
            ("Unmarshaler types with full keys", len(t_), t_.sum()),
        ], columns=["", "full config (graph)", "in the config"]).assign(
            coverage=lambda d: (100 * d["in the config"] / d["full config (graph)"]).round(0).astype(int).astype(str) + "%")

    out.p("\nSummary:")
    out.table(summary(fkdf))
    out.p("Summary excluding service discovery (keys in a `*_sd_configs` or `static_configs` block; a type counts if it "
          "ends a non-SD key):")
    out.table(summary(fkdf[~fkdf.sd]))

    # Kinds
    kinds = keys.groupby("kind").agg(full_keys=("key", "size"), in_config=("in_config", "sum"))
    out.p("\nBy value kind:")
    out.table(kinds.sort_values("full_keys", ascending=False), index=True)
    out.p(f"- Full keys whose values step (1) could find (kind string or struct): "
          f"{pct(keys.searchable.sum(), len(keys))}; of those in the config: "
          f"{pct(keys[keys.in_config].searchable.sum(), keys.in_config.sum())}")

    # =============================================================================================================
    out.h("Q2. One key vs its section")
    out.p("Section sizes (number of full keys in the section):")
    size_desc = pd.DataFrame({
        "all sections": secs["size"].describe(),
        "sections with a concrete key": secs[secs.covered]["size"].describe()}).round(1)
    out.table(size_desc, index=True)
    cov = secs[secs.covered].copy()
    cov["fraction_in_config"] = (cov.in_config / cov["size"]).round(2)
    out.p(f"- In sections with a concrete key, fraction of the section's keys in the config: median "
          f"{cov.fraction_in_config.median():.2f}, mean {cov.fraction_in_config.mean():.2f}")
    out.p(f"- Sections where the config sets exactly one key: {pct((cov.in_config == 1).sum(), len(cov))}")
    out.p(f"- Sections with no key step (1) could find (no string/struct kind): "
          f"{pct((secs.searchable == 0).sum(), len(secs))}, holding {secs[secs.searchable == 0]['size'].sum()} keys")
    out.p(f"  - of the sections with a concrete key: {pct((cov.searchable == 0).sum(), len(cov))}")
    # Covered sections whose concrete keys are all unsearchable
    cfg_searchable = collections.defaultdict(int)
    for r in fkdf[fkdf.in_config].itertuples():
        if r.searchable:
            cfg_searchable[r.section] += 1
    cov["config_searchable"] = cov.section.map(lambda s: cfg_searchable.get(s, 0))
    out.p(f"- Sections with concrete keys, none of them searchable: {pct((cov.config_searchable == 0).sum(), len(cov))}"
          + ": " + ", ".join(f"`{s}`" for s in cov[cov.config_searchable == 0].section))
    secs.to_csv(f"{args.out}/sections.csv", index=False)
    out.p("\nLargest sections:")
    out.table(secs.sort_values("size", ascending=False).head(10)[["section", "size", "in_config", "searchable"]])

    # Ambiguous unmarshalers for concrete keys
    multi = {k: c for k, c in config.items() if len(c["unmarshalers"]) > 1}
    out.p(f"- Concrete keys with more than one Unmarshaler (after resolving SD `*`s): {len(multi)}"
          + "".join(f"\n  - `{k}`: {', '.join(sorted(c['unmarshalers']))}" for k, c in sorted(multi.items())))

    # Size buckets chart
    bins = [0, 1, 2, 5, 10, 20, 40, 1000]
    labels = ["1", "2", "3-5", "6-10", "11-20", "21-40", ">40"]
    secs["bucket"] = pd.cut(secs["size"], bins=bins, labels=labels)
    b = secs.groupby("bucket", observed=False).agg(covered=("covered", "sum"), total=("section", "size"))
    b["not_covered"] = b.total - b.covered
    stacked_barh(b, ["covered", "not_covered"], [BLUE, GRAY], ["has a key in the config", "no key in the config"],
                 "Sections by size (number of keys)", "sections", f"{args.out}/q2_section_sizes.png")

    # =============================================================================================================
    out.h("Q3. Values vs keys")
    occ = collections.defaultdict(set)  # value => concrete keys
    for k, c in config.items():
        for v in c["values"]:
            occ[v.replace("\n", "\\n")].add(k)

    lits = go_string_literals(args.prometheus)
    argm = pd.read_csv(args.argmatch, keep_default_na=False, dtype=str)
    matched = set(argm.needle)

    def describe(v, ks):
        fks = set().union(*(config[k]["full_keys"] for k in ks))
        us = set().union(*(config[k]["unmarshalers"] for k in ks))
        secs_ = {section(fk, p) for fk in fks for p in full[fk]["paths"]}
        last = {segs(k)[-1] for k in ks}
        if len(ks) == 1:
            sim = "one key"
        elif len(us) == 1:
            sim = "same Unmarshaler type"
        elif len(last) == 1:
            sim = "same field name, different types"
        else:
            sim = "different fields and types"
        others = [o for o in occ if o != v]
        return {"value": v, "keys": len(ks), "full_keys": len(fks), "sections": len(secs_), "unmarshalers": len(us),
                "top_sections": len({segs(k)[0] for k in ks}), "similarity": sim, "searched": v in searched,
                "matched_by_argmatch": v in matched, "go_literal": v in lits, "length": len(v),
                "substring_of_other_value": any(v in o for o in others),
                "key_list": "; ".join(sorted(ks))}

    vals = pd.DataFrame([describe(v, ks) for v, ks in occ.items()])
    vals.to_csv(f"{args.out}/values.csv", index=False)
    out.p(f"- Distinct config values: {len(vals)}; searched by step (1): {vals.searched.sum()}; "
          f"matched by argmatch ({os.path.basename(os.path.dirname(args.argmatch))}): {vals.matched_by_argmatch.sum()}")
    not_in_config = sorted(matched - set(vals.value))
    if not_in_config:
        out.p(f"  - argmatch needles not among the current config values: {not_in_config}")
    rows = []
    for name, sub in (("all values", vals), ("searched", vals[vals.searched]),
                      ("matched by argmatch", vals[vals.matched_by_argmatch])):
        rows.append({"values": name, "n": len(sub),
                     ">1 key": pct((sub["keys"] > 1).sum(), len(sub)),
                     ">1 full key": pct((sub.full_keys > 1).sum(), len(sub)),
                     ">1 section": pct((sub.sections > 1).sum(), len(sub)),
                     ">1 Unmarshaler type": pct((sub.unmarshalers > 1).sum(), len(sub)),
                     ">1 top-level section": pct((sub.top_sections > 1).sum(), len(sub)),
                     "mean keys": round(sub["keys"].mean(), 2), "max keys": sub["keys"].max()})
    out.p("\nAmbiguity: values that stand for more than one key, section or type")
    out.table(pd.DataFrame(rows))
    out.p("How similar the keys of a value are (searched values):")
    out.table(vals[vals.searched].similarity.value_counts().rename_axis("similarity").reset_index())
    out.p("Values whose keys have different fields and types (searched):")
    out.table(vals[vals.searched & (vals.similarity == "different fields and types")]
              .sort_values("keys", ascending=False)[["value", "keys", "key_list"]].head(25))
    out.p(f"- Searched values that also appear as a string literal in the code compiled into Prometheus (golits/) "
          f"(so a match may not come from the config): {pct(vals[vals.searched].go_literal.sum(), vals.searched.sum())}"
          + ": " + ", ".join(f"`{v}`" for v in sorted(vals[vals.searched & vals.go_literal].value)))
    def signal(r):
        if r.go_literal:
            return "also a string literal in the source"
        if r["keys"] == 1:
            return "one key, not a literal"
        if r.similarity == "same Unmarshaler type":
            return "several keys, same Unmarshaler type"
        return "several keys and types"

    vals["signal"] = vals.apply(signal, axis=1)
    sig = pd.DataFrame({"searched": vals[vals.searched].signal.value_counts(),
                        "matched by argmatch": vals[vals.matched_by_argmatch].signal.value_counts()}).fillna(0)
    out.p("What a match on a value tells us (first category that applies):")
    out.table(sig.astype(int), index=True)
    out.p(f"- Searched values that are substrings of another config value (matter with -exact=false): "
          f"{pct(vals[vals.searched].substring_of_other_value.sum(), vals.searched.sum())}")
    out.p(f"- Searched values of length <= 4: {pct((vals[vals.searched].length <= 4).sum(), vals.searched.sum())}")

    # Keys per value chart (searched values)
    sv = vals[vals.searched].copy()
    sv["bucket"] = pd.cut(sv["keys"], bins=[0, 1, 2, 3, 6, 12, 100], labels=["1", "2", "3", "4-6", "7-12", ">12"])
    kb = sv.groupby("bucket", observed=False).agg(total=("value", "size"), matched=("matched_by_argmatch", "sum"))
    kb["not_matched"] = kb.total - kb.matched
    stacked_barh(kb, ["matched", "not_matched"], [BLUE, GRAY], ["matched by argmatch", "not matched"],
                 "Searched values by the number of concrete keys they stand for", "values",
                 f"{args.out}/q3_keys_per_value.png")

    # The chain: full keys -> concrete keys -> searched -> matched
    def reach(vs):
        ks = set().union(*(occ[v] for v in vs)) if len(vs) else set()
        fks = set().union(*(config[k]["full_keys"] for k in ks)) if ks else set()
        ss = {section(fk, p) for fk in fks for p in full[fk]["paths"]}
        ts = {u for fk in fks for u in full[fk]["unmarshalers"]}
        return len(ks), len(fks), len(ss), len(ts)

    chain = pd.DataFrame([
        ("graph (full config)", "-", "-", len(keys), len(secs), len(types)),
        ("concrete config", len(vals), len(config), *reach(set(vals.value))[1:]),
        ("searched by step (1)", vals.searched.sum(), *reach(set(vals[vals.searched].value))),
        ("matched by argmatch", vals.matched_by_argmatch.sum(), *reach(set(vals[vals.matched_by_argmatch].value))),
    ], columns=["stage", "values", "concrete keys", "full keys", "sections", "Unmarshaler types with full keys"])
    out.p("What each stage can tell us about (a value stands for all of its keys):")
    out.table(chain)

    # =============================================================================================================
    out.h("Q4. Graph keys vs documented keys")
    graph_keys = set(keys.key)
    both = graph_keys & set(docs)
    out.p(f"- Graph full keys that are documented: {pct(len(both), len(graph_keys))}")
    out.p(f"- Documented keys in the graph: {pct(len(both), len(docs))}")

    def group(k):
        """Group a key by what it is, for keys in only one of the two"""
        s = segs(k)
        last = s[-1] if s[-1] != STAR else s[-2] + ".*"
        return last

    docs_only = sorted(set(docs) - graph_keys)
    graph_only = sorted(graph_keys - set(docs))
    pd.DataFrame({"key": docs_only, "type": [docs[k] for k in docs_only]}).to_csv(f"{args.out}/docs_only.csv",
                                                                                index=False)
    keys[keys.key.isin(graph_only)].to_csv(f"{args.out}/graph_only.csv", index=False)
    for name, ks in (("Documented but not in the graph", docs_only), ("In the graph but not documented", graph_only)):
        out.p(f"\n{name}: {len(ks)}, by last segment (top 30):")
        c = collections.Counter(group(k) for k in ks)
        out.table(pd.DataFrame(c.most_common(30), columns=["last segment", "keys"]))
        sec = collections.Counter(".".join(s for s in segs(k)[:3]) for k in ks)
        out.p("By first three segments (top 15): " + ", ".join(f"`{s}` ({n})" for s, n in sec.most_common(15)))

    # Why keys are in only one of the two
    def category(k, in_graph):
        s = segs(k)
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

    for name, ks, in_graph in (("Documented but not in the graph", docs_only, False),
                               ("In the graph but not documented", graph_only, True)):
        c = collections.Counter(category(k, in_graph) for k in ks)
        out.p(f"\n{name}, by reason:")
        out.table(pd.DataFrame(c.most_common(), columns=["reason", "keys"]))

    # Kind agreement
    rows = []
    for k in both:
        if docs[k] == "map":
            continue  # the docs give no type for the map's values
        gk = full[k]["kind"]
        dk = docs_kind(docs[k])
        rows.append({"key": k, "docs type": docs[k], "docs kind": dk, "graph kind": gk})
    kd = pd.DataFrame(rows)
    ct = pd.crosstab(kd["docs kind"], kd["graph kind"])
    out.p("\nValue kinds of keys in both, except maps (documented type vs Go kind):")
    out.table(ct, index=True)
    odd = kd[~kd.apply(lambda r: r["docs kind"] == r["graph kind"] or (r["docs kind"] == "string" and r["graph kind"]
                                                                       in ("string", "struct")), axis=1)]
    odd.to_csv(f"{args.out}/kind_disagreements.csv", index=False)
    out.p("Disagreements, by documented type and Go kind:")
    out.table(odd.groupby(["docs type", "docs kind", "graph kind"]).size().rename("keys").reset_index()
              .sort_values("keys", ascending=False))

    # Concrete keys vs docs
    cfg_undoc = sorted(k for k, c in config.items() if not any(fk in docs for fk in c["full_keys"]))
    out.p(f"- Concrete keys whose full key isn't documented: {len(cfg_undoc)}: " + ", ".join(f"`{k}`" for k in cfg_undoc))

    write_visuals(fkdf, config, covered_by, searched, matched, sd, args, out)

    out.write()
    print(f"wrote {out.path}")


if __name__ == "__main__":
    main()
