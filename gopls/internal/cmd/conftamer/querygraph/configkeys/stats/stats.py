#!/usr/bin/env python3
"""Statistics on how well the config-tracing workflow's proxies represent what they stand for.

See STATISTICS.md (next to this file) for the questions, definitions and answers. This script computes the numbers:
it writes stats_tables.md, CSVs and charts to --out.

Run with: uv run --with pandas --with matplotlib python stats.py [flags]
"""

import argparse
import collections
import csv
import fnmatch
import glob
import json
import os
import re
import subprocess
import sys

import matplotlib
import pandas as pd

# paramtrack's per-module settings and hooks (paramtrack/modules/<module>/module.py)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../paramtrack"))
import modules  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

STAR = "*"
# configkeys' suffix for a path ending at a custom unmarshaler (match.go's customSuffix), by format
CUSTOM_SUFFIXES = (" (custom UnmarshalYAML)", " (custom UnmarshalJSON)")

# Reference palette (dataviz skill, light mode)
BLUE, ORANGE, GRAY, INK, INK2 = "#2a78d6", "#eb6834", "#c9c8c2", "#0b0b0b", "#52514e"
AQUA = "#1baf7a"  # categorical slot 3 (after blue, orange); below 3:1 on the surface, so its marks get a visible label


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
def searchable_kind(kinds):
    return any(k in ("string", "struct") for k in kinds.split("; "))


def matches_any(fk, patterns):
    return any(fnmatch.fnmatchcase(fk, p) for p in patterns)


def key_values(fk, covered_by, config):
    """The values of full key fk in the concrete config: those of its concrete keys (covered_by: full key => concrete
    keys; config: concrete key => info), with newlines escaped as in param_values.txt and values.csv"""
    return [v.replace("\n", "\\n") for k in covered_by.get(fk, ()) for v in config[k]["values"]]


def unambiguous_key(values, unambiguous_value):
    """Whether a key with these values is unambiguous (TERMINOLOGY.md): it has values, and each is unambiguous
    (unambiguous_value(v): no other concrete key has v, and v isn't a string literal in the module's code)"""
    return bool(values) and all(unambiguous_value(v) for v in values)


# ---------------------------------------------------------------------------------------------------------------
# Inputs


def load_configkeys(path, custom):
    """Full keys (graph patterns with the custom `*`s resolved by custom, the module's CustomKeys) and concrete keys,
    from the configkeys csv."""
    ck = pd.read_csv(path, keep_default_na=False, dtype=str)
    ck.columns = [c.replace(" ", "_") for c in ck.columns]
    full = {}  # full key => info
    catchall = set()
    for r in ck[ck.source != "config"].itertuples():
        for p in split(r.Unmarshaler_Path):
            fk = custom.full_key(r.param_key, path_nodes(p), p.endswith(CUSTOM_SUFFIXES))
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
                fk = custom.full_key(pat, path_nodes(p), p.endswith(CUSTOM_SUFFIXES))
                if fk is not None and instance_of(r.param_key, fk) and fk in full:
                    info["full_keys"].add(fk)
                    info["unmarshalers"].add(path_nodes(p)[-1])
        config[r.param_key] = info
    return full, config, catchall


def read_patterns(path):
    """Patterns of full keys (--ignore: to leave out of every statistic; --transformed: whose values are stored
    transformed): one per line, matched with fnmatch, so `*` matches anything, dots included (e.g.
    `scrape_configs.azure_sd_configs.*`); blank lines and # comments skipped"""
    if not path:
        return []
    return [line.strip() for line in open(path) if line.strip() and not line.lstrip().startswith("#")]


def apply_ignore(full, config, patterns):
    """Remove the full keys matching patterns from full, and from each concrete key's full keys; drop the concrete
    keys left with none. Returns the numbers of full keys and concrete keys removed."""
    ignored = {fk for fk in full if matches_any(fk, patterns)}
    for fk in ignored:
        del full[fk]
    dropped = 0
    for k in list(config):
        c = config[k]
        if c["full_keys"] & ignored:
            c["full_keys"] -= ignored
            c["unmarshalers"] = set().union(*(full[fk]["unmarshalers"] for fk in c["full_keys"]))
            if not c["full_keys"]:
                del config[k]
                dropped += 1
    return len(ignored), dropped


def check_sections(fkdf, n_full, configkeys_csv, out):
    """Check that the sections computed here (<parent key> @ <Unmarshaler>) have the same keys as configkeys'
    `section keys` column. Comparable where a full key's pattern resolves to just that full key (all keys outside
    service discovery, and SD keys whose pattern is in only one SD config): elsewhere configkeys combines the sections of
    all SD configs with the key. n_full counts each pattern's full keys before --ignore, since configkeys' column
    still combines the ignored SD configs' sections. Compared as patterns (the column has them, with the SD `*`)."""
    ck = pd.read_csv(configkeys_csv, keep_default_na=False, dtype=str).set_index("param_key")
    pattern_of = dict(zip(fkdf.key, fkdf.pattern))
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


def go_string_literals(module_dir, mod):
    """String literals in the non-test Go files compiled into the module (mod.GOLITS_PACKAGE, mod.GOLITS_TAGS), dependencies and standard library included
    (golits/), with newlines written as a literal \\n, as the values are"""
    golits = f"{os.path.dirname(os.path.abspath(__file__))}/golits"
    out = subprocess.run(["go", "run", ".", "-dir", module_dir, "-tags", mod.GOLITS_TAGS, mod.GOLITS_PACKAGE], cwd=golits, capture_output=True, text=True,
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

    def transformed_note(r):
        return "; stored transformed" if r.transformed else ""

    lines = ["# Generated by querygraph/configkeys/stats/stats.py: the full keys (see STATISTICS.md), from /config.Config."]
    if tiers:
        lines += ["# Each key's comment says how far it gets: " + " < ".join(TIERS) + "; then its value kind, and "
                  "whether it's stored transformed (stats.py --transformed).",
                  "# Each mapping's comment counts its keys that are documented, in the concrete config, searched and "
                  "matched."]
    else:
        lines += ["# Each key's comment says whether it's documented, then its value kind (\"*\" is any map key), "
                  "and whether it's stored transformed (stats.py --transformed).",
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
                lines.append(f"{pad}{quote(seg)}: {{}}  # {v.tier}{undoc}; {v.kind}{transformed_note(v)}")
            else:
                lines.append(f"{pad}{quote(seg)}: {{}}  # {'documented' if v.documented else 'not documented'}; "
                             f"{v.kind}{transformed_note(v)}")

    walk(tree, 0)
    open(path, "w").write("\n".join(lines) + "\n")


def write_visuals(fkdf, fkdf_all, config, covered_by, searched, matched, custom, args, out):
    """Write full_keys.csv with searched/matched columns, node_coverage.csv (attributes per graph node, for
    prettify's --node-attrs, from every full key, --ignore or not) and node_coverage_keyed.csv (from the in-scope
    ones), sd_configs.csv, and full_keys.yaml (a keys-only YAML of the in-scope full keys, annotated)"""
    def values_of(fk):
        return {v.replace("\n", "\\n") for k in covered_by.get(fk, ()) for v in config[k]["values"]}

    for f in (fkdf, fkdf_all):
        f["searched"] = f.key.map(lambda fk: bool(values_of(fk) & searched))
        f["matched"] = f.key.map(lambda fk: bool(values_of(fk) & matched))
        f["tier"] = [TIERS[key_tier(r)] for r in f.itertuples()]
    fkdf.to_csv(f"{args.out}/full_keys.csv", index=False)
    keys = fkdf.drop_duplicates("key")

    # Graph nodes: tier of the furthest full key ending at the node. The whole graph counts every full key, even
    # those --ignore leaves out (shown as not in the config).
    nodes = node_rows(fkdf_all, args.graph)
    nodes.to_csv(f"{args.out}/node_coverage.csv", index=False)

    # For the keyed graph: the in-scope full keys. Leave out the SD config nodes with none of them under the SD
    # config's key, wherever they end (e.g. static_configs' keys all end at targetgroup.Group): those --ignore leaves
    # out under every parent key.
    under = keys.assign(sd_key=keys.key.map(custom.of))
    sd_df = pd.DataFrame([{"node": n, "sd_key": k, "keys": int((under.sd_key == k).sum()),
                           "keys_matched": int(under[under.sd_key == k].matched.sum())} for n, k in sorted(custom.names.items())
                          if n in set(nodes.node)])
    sd_df["left_out"] = sd_df["keys"] == 0
    sd_df.to_csv(f"{args.out}/sd_configs.csv", index=False)
    node_rows(fkdf, args.graph).to_csv(f"{args.out}/node_coverage_keyed.csv", index=False)

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


# Confidence bins: half-open tenths ([0.5, 0.6) is 50-<60%), then exactly 1 (an inferred key whose section's
# eligible keys are all evidence)
CONF_BINS = [f"{10 * i}-<{10 * (i + 1)}%" for i in range(10)] + ["100%"]


def conf_bin(c):
    return CONF_BINS[-1] if c >= 1 else CONF_BINS[min(int(c * 10), 9)]


def confidence(path, out, chart, ignore):
    """The confidence of paramtrack's message => full key mappings (its label_keys.csv; paramtrack's README): one
    mapping per (message kind, message label, full key, section); evidence mappings have confidence 1, inferred ones
    the share of their section's keys that are evidence for the message, out of all its keys and out of its eligible
    ones. Leaves out the mappings to full keys matching the ignore patterns (--ignore). Writes the distribution of
    both for the inferred mappings, and a grouped bar chart of it."""
    lk = pd.read_csv(path, keep_default_na=False, dtype=str)
    lk = lk[lk.full_key != ""]
    ignored = lk.full_key.map(lambda fk: matches_any(fk, ignore))
    if ignore:
        out.p(f"- Left out (--ignore): {ignored.sum()} rows, for {lk[ignored].full_key.nunique()} ignored full keys")
    lk = lk[~ignored]
    m = lk.groupby(["message_kind", "message_label", "full_key", "section"]).agg(
        evidence=("evidence", lambda e: (e == "True").any()), eligible=("eligible", "first"),
        confidence_all=("confidence_all", "first"), confidence_eligible=("confidence_eligible", "first")).reset_index()
    inf = m[~m.evidence].copy()
    for c in ("confidence_all", "confidence_eligible"):
        inf[c] = inf[c].astype(float)
    out.p(f"- Mappings (message, full key, section): {len(m)}; evidence (confidence 1): {m.evidence.sum()}; "
          f"inferred: {len(inf)}, of them eligible (could have been evidence): {(inf.eligible == 'True').sum()}; "
          f"messages: {m[['message_kind', 'message_label']].drop_duplicates().shape[0]}")
    for c in ("confidence_all", "confidence_eligible"):
        out.p(f"- Inferred `{c}`: median {inf[c].median():.2f}, mean {inf[c].mean():.2f}; exactly 1: "
              f"{pct((inf[c] >= 1).sum(), len(inf))}")
    dist = pd.DataFrame({c: inf[c].map(conf_bin).value_counts().reindex(CONF_BINS, fill_value=0)
                         for c in ("confidence_all", "confidence_eligible")}).rename_axis("confidence")
    n_ev = int(m.evidence.sum())
    table = dist.copy()
    table.loc["evidence (1)"] = [n_ev, n_ev]
    out.p("\nMappings by confidence (inferred by bin; evidence mappings, all 1, in the last row):")
    out.table(table, index=True)

    # All mappings: the evidence ones (confidence 1 in both columns) stack on the 100% bars
    fig, ax = plt.subplots(figsize=(9, 3.6), facecolor="#fcfcfb")
    ax.set_facecolor("#fcfcfb")
    x = range(len(CONF_BINS))
    w = 0.4
    for i, (c, color, label) in enumerate([("confidence_all", BLUE, "inferred, out of all the section's keys"),
                                           ("confidence_eligible", ORANGE, "inferred, out of its eligible keys "
                                            "(set in the config, with a searched value)")]):
        xs = [j + (i - 0.5) * w for j in x]
        ax.bar(xs, dist[c], width=w, color=color, label=label, edgecolor="#fcfcfb", linewidth=2)
        ax.bar(xs[-1], n_ev, bottom=dist[c].iloc[-1], width=w, color=AQUA, edgecolor="#fcfcfb", linewidth=2,
               label="evidence (confidence 1)" if i == 0 else None)
    ax.text(len(CONF_BINS) - 1, n_ev + dist.values[-1].max(), f"{n_ev} evidence", ha="center", va="bottom",
            fontsize=8, color=INK2)
    ax.set_xticks(list(x), CONF_BINS, rotation=0)
    ax.set_xlabel("confidence (1 for evidence; for inferred, the share of the section's keys that are evidence)",
                  color=INK2, fontsize=9)
    ax.set_ylabel("mappings", color=INK2, fontsize=9)
    style(ax, f"Confidence of all {len(m)} message => full key mappings ({n_ev} evidence, {len(inf)} inferred)")
    ax.grid(axis="x", visible=False)
    ax.grid(axis="y", color="#ecebe7", linewidth=0.8)
    ax.set_ylim(0, 1.15 * (n_ev + dist.values[-1].max()))  # room for the legend
    handles, labels = ax.get_legend_handles_labels()
    order = [labels.index(lb) for lb in labels if not lb.startswith("evidence")] + \
        [labels.index(lb) for lb in labels if lb.startswith("evidence")]  # the inferred pair, then evidence
    ax.legend([handles[i] for i in order], [labels[i] for i in order], frameon=False, fontsize=9, loc="upper left")
    fig.tight_layout()
    fig.savefig(chart, dpi=150)
    plt.close(fig)


# ---------------------------------------------------------------------------------------------------------------


def property_table(rows, props, in_scope):
    """Keys, Sections, Keys per section and Min. confidence per property (TERMINOLOGY.md): rows has a key and
    section column; props is a list of (name, set of keys), each a subset of the previous; in_scope is the in-scope
    keys. A section has a property if one of its keys does. Keys per section is the median/mean, over the sections
    with the property, of their keys with it. Min. confidence is the median/mean, over the keys with the property, of
    1 / the number of in-scope keys in the key's section (the confidence its section's keys get if it's the only
    evidence, as in label_keys.csv's confidence_all); not for the first column, whose keys needn't be in scope. A %
    is of the previous column."""
    rows = rows[["key", "section"]].drop_duplicates()
    in_scope_size = rows[rows.key.isin(in_scope)].groupby("section").key.nunique()
    section_of = dict(zip(rows.key, rows.section))
    table, prev = {}, None
    for name, keys in props:
        r = rows[rows.key.isin(keys)]
        per_sec = r.groupby("section").key.nunique()
        n = (len(keys), len(per_sec))
        cells = [f"{x}" if prev is None else f"{x} ({100 * x / p:.0f}%)" if p else f"{x}" for x, p in
                 zip(n, prev or (None, None))]
        cells.append(f"{per_sec.median():g}/{per_sec.mean():.1f}" if len(per_sec) else "-")
        conf = pd.Series([] if prev is None else [100 / in_scope_size[section_of[k]] for k in keys], dtype=float)
        cells.append("-" if conf.empty else f"{conf.median():.1f}%/{conf.mean():.1f}%")
        table[name] = cells
        prev = n
    return pd.DataFrame(table, index=["Keys", "Sections", "Keys per section", "Min. confidence"])


def dropout_reasons(name_from, keys_from, name_to, keys_to, reasons):
    """Why keys in one column of the property table (keys_from) aren't in the next (keys_to): reasons is a list of
    (reason, predicate on a key). Each dropped key counts once, under the combination of reasons that apply to it
    ("none of these" if none does), so the shares add up to 100%."""
    dropped = keys_from - keys_to
    combos = collections.Counter(" + ".join(r for r, applies in reasons if applies(k)) or "none of these"
                                 for k in dropped)
    df = pd.DataFrame([(r[:1].upper() + r[1:], pct(n, len(dropped))) for r, n in combos.most_common()],
                      columns=["Drop reason", "Dropped"])
    return f"{name_from} -> {name_to}", df


def side_by_side_html(tables):
    """(title, DataFrame with Drop reason and Dropped columns) tables as one HTML table, side by side: a column pair
    per table, headed by its title and "Dropped", with a line after each pair (markdown tables can't go side by side)"""
    bar = ' style="border-right: 1px solid"'
    lines = ["<table>", "<tr>" + "".join(f"<th>{t}</th><th{bar}>Dropped</th>" for t, _ in tables) + "</tr>"]
    for i in range(max(len(df) for _, df in tables)):
        cells = []
        for _, df in tables:
            reason, dropped = (df.iloc[i]["Drop reason"], df.iloc[i]["Dropped"]) if i < len(df) else ("", "")
            cells.append(f"<td>{reason}</td><td{bar}>{dropped}</td>")
        lines.append("<tr>" + "".join(cells) + "</tr>")
    return "\n".join(lines + ["</table>"])


def fill_from_run(args):
    """Fills in the path arguments not given from paramtrack run args.run (paramtrack's README lists its steps'
    outputs): the inputs paramtrack's step 8 passes. Raises FileNotFoundError for a missing one the mode needs."""
    run = args.run

    def need(path):
        if not os.path.exists(path):
            raise FileNotFoundError(f"no {path}")
        return path

    settings = json.load(open(need(f"{run}/2-full-keys/settings.json")))
    args.configkeys = args.configkeys or need(f"{run}/4-configkeys/configkeys.csv")
    args.param_values = args.param_values or need(f"{run}/4-configkeys/param_values.txt")
    args.graph = args.graph or need(f"{run}/1-unmarshaler-graph/unmarshaler_subgraph.text")
    args.docs = args.docs or settings["docs"]
    args.module = args.module or settings["module"]
    args.module_dir = args.module_dir or settings["module_dir"]
    args.ignore = args.ignore or modules.moved(settings["ignore"]) or None
    args.transformed = args.transformed or modules.moved(settings.get("transformed")) or None
    if not args.config_only:
        args.argmatch = args.argmatch or need(f"{run}/5-matches/matches.csv")
        lk = f"{run}/7-label-keys/label_keys.csv"
        args.label_keys = args.label_keys or (lk if os.path.exists(lk) else None)


def read_needles(path):
    """The distinct needles in an argmatch csv's needle column. Read with csv rather than pandas: paramtrack's
    matches.csv is too big for pandas, and its rows have more fields than its header."""
    needles = set()
    csv.field_size_limit(1 << 30)  # (matches.csv has long fields)
    with open(path, newline="") as f:
        r = csv.reader(f)
        ni = next(r).index("needle")
        for row in r:
            if len(row) > ni:
                needles.add(row[ni])
    return needles


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    # (No defaults: a missing argument mustn't silently use another run's files or another module's. --run fills in
    # the ones not given from a paramtrack run.)
    ap.add_argument("--run", help="A paramtrack --out directory: the path arguments not given come from its steps' "
                                  "outputs and its 2-full-keys/settings.json (see fill_from_run)")
    ap.add_argument("--configkeys", help="configkeys csv (with --config)")
    ap.add_argument("--param-values", help="values step (1) searches for (configkeys --txt-dir --strings-only); "
                                           "required unless --full-keys-only")
    ap.add_argument("--graph", help="the Unmarshaler Subgraph configkeys was run on")
    ap.add_argument("--docs", help="documented keys (e.g. list_config_keys.py's)")
    ap.add_argument("--module", help="the module: its Go module path or its paramtrack/modules/ directory name "
                                     "(e.g. prometheus), whose module.py has its settings and hooks")
    ap.add_argument("--module-dir", "--prometheus", help="module checkout the graph was built from")
    ap.add_argument("--argmatch", help="needle_types.csv of an argmatch run (step (1) results), or a csv with a needle "
                                       "column (e.g. paramtrack's 5-matches/matches.csv); required unless "
                                       "--config-only or --full-keys-only")
    ap.add_argument("--out", required=True)
    ap.add_argument("--label-keys", help="paramtrack's label_keys.csv (step 7): adds the distribution of its mappings' "
                                         "confidence (STATISTICS.md, \"TO BE CATEGORIZED\")")
    ap.add_argument("--ignore", help="File of full key patterns to leave out of every statistic, one per line, "
                                     "matched with fnmatch (e.g. scrape_configs.azure_sd_configs.*)")
    ap.add_argument("--transformed", help="File of full key patterns whose values are stored transformed, so argmatch "
                                          "can't find them as written (e.g. *.regex), in --ignore's format: adds the "
                                          "transformed and target (searchable kind and not transformed) columns")
    ap.add_argument("--config-only", action="store_true",
                    help="Only the statistics the concrete config controls (STATISTICS.md, \"Statistics the concrete "
                         "config controls\"), to config_stats.md; --argmatch is unused")
    ap.add_argument("--full-keys-only", action="store_true",
                    help="Only write full_keys.yaml, annotated with what's known without a concrete config (from "
                         "--configkeys run without --config; --param-values and --argmatch are unused)")
    args = ap.parse_args()
    if args.run:
        if args.full_keys_only:
            ap.error("--run is for steps 4 on (--full-keys-only is step 2's, before the run has a configkeys csv)")
        try:
            fill_from_run(args)
        except FileNotFoundError as e:
            ap.error(f"--run {args.run}: {e}")
    for name in ("configkeys", "graph", "docs", "module", "module_dir"):
        if not getattr(args, name):
            ap.error(f"--{name.replace('_', '-')} is required (or --run)")
    if not args.full_keys_only and not args.param_values:
        ap.error("--param-values is required (unless --full-keys-only)")
    if not (args.config_only or args.full_keys_only) and not args.argmatch:
        ap.error("--argmatch is required (unless --config-only or --full-keys-only)")
    cfg_only = args.config_only
    out = Out(f"{args.out}/{'config_stats.md' if cfg_only else 'stats_tables.md'}")
    out.p("<!-- Generated by querygraph/configkeys/stats/stats.py; see STATISTICS.md there for what these mean. -->")
    mod = modules.load(args.module)
    out.p(f"# Statistics the concrete config controls ({mod.TITLE})" if cfg_only else
          f"# Statistics tables ({mod.TITLE})")

    MAP_FIELDS.update(map_field_keys(args.graph))
    custom = mod.CustomKeys(args.module_dir)
    full, config, catchall = load_configkeys(args.configkeys, custom)
    full_all = dict(full)  # before --ignore: for the whole-graph visualization
    n_full = collections.Counter(info["pattern"] for info in full.values())  # (for check_sections)
    ignore = read_patterns(args.ignore)
    transformed = read_patterns(args.transformed)
    n_ignored, n_dropped = apply_ignore(full, config, ignore)
    docs_all = read_docs(args.docs)
    docs = {k: t for k, t in docs_all.items() if not matches_any(k, ignore)}

    # Per full key: section, top-level section, coverage by the config
    covered_by = collections.defaultdict(set)  # full key => concrete keys
    for k, info in config.items():
        for fk in info["full_keys"]:
            covered_by[fk].add(k)

    def full_key_rows(full, docs):
        rows = []
        for fk, info in sorted(full.items()):
            for u, sec in sorted({(path_nodes(p)[-1], section(fk, p)) for p in info["paths"]}):
                rows.append({"key": fk, "pattern": info["pattern"], "unmarshaler": u, "kind": info["kind"],
                             "top": segs(fk)[0], "section": sec, "map_key": segs(fk)[-1] == STAR,
                             "in_config": fk in covered_by, "documented": fk in docs,
                             "searchable_kind": searchable_kind(info["kind"]),
                             "transformed": matches_any(fk, transformed),
                             # The concreteconfig skill's target keys (in scope, given --ignore)
                             "target": searchable_kind(info["kind"]) and not matches_any(fk, transformed),
                             "sd": custom.of(fk) is not None})
        return pd.DataFrame(rows)

    fkdf = full_key_rows(full, docs)
    if args.full_keys_only:
        write_full_keys_yaml(fkdf.drop_duplicates("key"), f"{args.out}/full_keys.yaml", tiers=False)
        print(f"wrote {args.out}/full_keys.yaml: {fkdf.key.nunique()} full keys")
        return
    searched = set(open(args.param_values).read().splitlines())
    if not cfg_only:  # (write_visuals rewrites it with the coverage columns)
        fkdf.to_csv(f"{args.out}/full_keys.csv", index=False)
    else:  # Per full key, with whether the config sets it and searches one of its values: e.g. the searchable keys a
        # candidate doesn't set yet (the concreteconfig skill). Its own name, so as not to replace a full run's.
        def is_searched(fk):
            return any(v.replace("\n", "\\n") in searched for k in covered_by.get(fk, ()) for v in config[k]["values"])
        fkdf.assign(searched=fkdf.key.map(is_searched)).to_csv(f"{args.out}/config_full_keys.csv", index=False)
    keys = fkdf.drop_duplicates("key")

    # =============================================================================================================
    out.h("Inputs")
    out.p(f"- Full keys (graph patterns, SD `*` resolved by Name()): {len(full)}; excluded catch-all patterns of "
          f"discovery.Configs' dynamic unmarshaler: {', '.join(sorted(catchall))}")
    out.p(f"- SD config types found (Name()): {len(set(custom.names.values()))}")
    if args.ignore:
        out.p(f"- Ignored (--ignore {args.ignore}): {n_ignored} full keys; {n_dropped} concrete keys left with no "
              f"full key dropped")
    if args.transformed:
        out.p(f"- Stored transformed (--transformed {args.transformed}): {keys.transformed.sum()} full keys, "
              f"{keys[keys.transformed].searchable_kind.sum()} of them of searchable kind")
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
                                       top=("top", "first"), searchable_kind=("searchable_kind", "sum")).reset_index()
    secs["covered"] = secs.in_config > 0
    check_sections(fkdf, n_full, args.configkeys, out)
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
    for parent_key in custom.parents:
        for name in sorted(set(custom.names.values())):
            prefix = f"{parent_key}.{name}."
            ks = keys[keys.key.str.startswith(prefix)]
            sd_rows.append({"parent": parent_key, "sd": name, "full_keys": len(ks), "in_config": ks.in_config.sum()})
    sddf = pd.DataFrame(sd_rows)
    used = sddf[sddf.in_config > 0]
    out.p(f"- Service discovery configs used by the config: {len(used)} of {len(sddf)} (parent, SD) pairs: "
          + ", ".join(f"{r.parent}.{r.sd} ({r.in_config}/{r.full_keys})" for r in used.itertuples()))
    out.p(f"  - Their keys are {pct(sddf.full_keys.sum(), len(keys))} of all full keys")

    # Summary
    s_ = fkdf.groupby("section").in_config.any()
    t_ = fkdf.groupby("unmarshaler").in_config.any()
    tgt = keys[keys.target]
    tgt_s = fkdf[fkdf.target].groupby("section").in_config.any()  # in the config: one of its target keys is set
    summary = pd.DataFrame([
        ("Full keys", len(keys), keys.in_config.sum()),
        ("Sections", len(s_), s_.sum()),
        ("Unmarshaler types with full keys", len(t_), t_.sum()),
        ("Target keys (searchable kind" + (", not transformed)" if args.transformed else ")"), len(tgt),
         tgt.in_config.sum()),
        ("Sections with a target key", len(tgt_s), tgt_s.sum()),
    ], columns=["", "full config (graph)", "in the config"]).assign(
        coverage=lambda d: (100 * d["in the config"] / d["full config (graph)"]).round(0).astype(int).astype(str) + "%")
    out.p("\nSummary" + (" (in scope: without the --ignore keys)" if args.ignore else "") + ":")
    out.table(summary)

    # Kinds (not with --config-only: numbers and bools only matter if argmatch searches them)
    if not cfg_only:
        kinds = keys.groupby("kind").agg(full_keys=("key", "size"), in_config=("in_config", "sum"))
        out.p("\nBy value kind:")
        out.table(kinds.sort_values("full_keys", ascending=False), index=True)
        out.p(f"- Full keys whose values step (1) could find (kind string or struct): "
              f"{pct(keys.searchable_kind.sum(), len(keys))}; of those in the config: "
              f"{pct(keys[keys.in_config].searchable_kind.sum(), keys.in_config.sum())}")
        if args.transformed:
            out.p(f"- Target keys (searchable kind, not stored transformed): {pct(keys.target.sum(), len(keys))}; "
                  f"of those in the config: {pct(keys[keys.in_config].target.sum(), keys.in_config.sum())}")

    # How much of each section it touches the concrete config sets (Q1: coverage)
    cov = secs[secs.covered].copy()
    cov["fraction_in_config"] = (cov.in_config / cov["size"]).round(2)
    out.p(f"- In sections with a concrete key, fraction of the section's keys in the config: median "
          f"{cov.fraction_in_config.median():.2f}, mean {cov.fraction_in_config.mean():.2f}")
    out.p(f"- Sections where the config sets exactly one key: {pct((cov.in_config == 1).sum(), len(cov))}")

    # =============================================================================================================
    if not cfg_only:  # Q2 doesn't depend on the concrete config
        out.h("Q2. One key vs its section")
        out.p("Section sizes (number of full keys in the section):")
        size_desc = pd.DataFrame({
            "all sections": secs["size"].describe(),
            "sections with a concrete key": secs[secs.covered]["size"].describe()}).round(1)
        out.table(size_desc, index=True)
        out.p(f"- Sections with no key step (1) could find (no string/struct kind): "
              f"{pct((secs.searchable_kind == 0).sum(), len(secs))}, "
              f"holding {secs[secs.searchable_kind == 0]['size'].sum()} keys")
        out.p(f"  - of the sections with a concrete key: {pct((cov.searchable_kind == 0).sum(), len(cov))}")
        # Covered sections whose concrete keys are all unsearchable
        cfg_searchable = collections.defaultdict(int)
        for r in fkdf[fkdf.in_config].itertuples():
            if r.searchable_kind:
                cfg_searchable[r.section] += 1
        cov["config_searchable"] = cov.section.map(lambda s: cfg_searchable.get(s, 0))
        out.p(f"- Sections with concrete keys, none of them searchable: "
              f"{pct((cov.config_searchable == 0).sum(), len(cov))}"
              + ": " + ", ".join(f"`{s}`" for s in cov[cov.config_searchable == 0].section))
        secs.to_csv(f"{args.out}/sections.csv", index=False)
        out.p("\nLargest sections:")
        out.table(secs.sort_values("size", ascending=False).head(10)[["section", "size", "in_config",
                                                                       "searchable_kind"]])

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

    lits = go_string_literals(args.module_dir, mod)
    matched = set() if cfg_only else read_needles(args.argmatch)

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
                # Every full key of the value is stored transformed: argmatch can't find it as written
                "transformed_only": bool(fks) and all(matches_any(fk, transformed) for fk in fks),
                "key_list": "; ".join(sorted(ks))}

    vals = pd.DataFrame([describe(v, ks) for v, ks in occ.items()])
    if cfg_only:
        vals = vals.drop(columns="matched_by_argmatch")
        out.p(f"- Distinct config values: {len(vals)}; searched by step (1): {vals.searched.sum()}")
    else:
        out.p(f"- Distinct config values: {len(vals)}; searched by step (1): {vals.searched.sum()}; "
              f"matched by argmatch ({os.path.basename(os.path.dirname(args.argmatch))}): "
              f"{vals.matched_by_argmatch.sum()}")
        not_in_config = sorted(matched - set(vals.value))
        if not_in_config:
            out.p(f"  - argmatch needles not among the current config values: {not_in_config}")
    rows = []
    groups = [("all values", vals), ("searched", vals[vals.searched])]
    if not cfg_only:
        groups.append(("matched by argmatch", vals[vals.matched_by_argmatch]))
    for name, sub in groups:
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
    # (--config-only writes its own, so as not to replace a full run's)
    vals.to_csv(f"{args.out}/{'config_values.csv' if cfg_only else 'values.csv'}", index=False)
    sig = {"searched": vals[vals.searched].signal.value_counts()}
    if not cfg_only:
        sig["matched by argmatch"] = vals[vals.matched_by_argmatch].signal.value_counts()
    out.p("What a match on a value tells us (first category that applies):")
    out.table(pd.DataFrame(sig).fillna(0).astype(int), index=True)
    if not cfg_only:  # matters only with argmatch's -exact=false
        out.p(f"- Searched values that are substrings of another config value (matter with -exact=false): "
              f"{pct(vals[vals.searched].substring_of_other_value.sum(), vals.searched.sum())}")
    if args.transformed:
        tv = vals[vals.searched & vals.transformed_only]
        out.p(f"- Searched values whose keys are all stored transformed (--transformed; not findable as written): "
              f"{pct(len(tv), vals.searched.sum())}"
              + ("" if cfg_only else f"; matched by argmatch anyway: {tv.matched_by_argmatch.sum()}"))
    out.p(f"- Searched values of length <= 4: {pct((vals[vals.searched].length <= 4).sum(), vals.searched.sum())}")

    # Keys per value chart (searched values, by whether argmatch matched them)
    if not cfg_only:
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

    stages = [
        ("graph (full config)", "-", "-", len(keys), len(secs), len(types)),
        ("concrete config", len(vals), len(config), *reach(set(vals.value))[1:]),
        ("searched by step (1)", vals.searched.sum(), *reach(set(vals[vals.searched].value))),
    ]
    if not cfg_only:
        stages.append(("matched by argmatch", vals.matched_by_argmatch.sum(),
                       *reach(set(vals[vals.matched_by_argmatch].value))))
    chain = pd.DataFrame(stages, columns=["stage", "values", "concrete keys", "full keys", "sections",
                                          "Unmarshaler types with full keys"])
    out.p("What each stage can tell us about (a value stands for all of its keys):")
    out.table(chain)

    # =============================================================================================================
    out.h("Keys and sections by property")
    # Nested: each column also has the previous columns' properties (TERMINOLOGY.md)
    def unambiguous(fk):
        return unambiguous_key(key_values(fk, covered_by, config), lambda v: len(occ[v]) == 1 and v not in lits)

    all_rows = full_key_rows(full_all, docs_all)
    in_scope = set(keys.key)
    target = set(keys[keys.target].key)
    concrete = target & set(keys[keys.in_config].key)
    props = [("Full", set(all_rows.key)), ("In-scope", in_scope), ("Target", target), ("Concrete", concrete),
             ("Unambiguous", {fk for fk in concrete if unambiguous(fk)})]
    out.p("Each column's keys also have the previous columns' properties; a % is of the previous column. "
          "Keys per section: median/mean over the sections with the property. Min. confidence: median/mean, over the "
          "keys with the property, of 1 / the in-scope keys in the key's section" +
          ("" if args.ignore else " (no --ignore: every full key is in scope)") + ".")
    out.table(property_table(all_rows, props, in_scope), index=True)

    # Why keys drop out from one column to the next, where there's more than one reason
    by_key = keys.set_index("key")

    def values_of(fk):
        return key_values(fk, covered_by, config)

    pairs = [
        (props[1], props[2], [("unsearchable kind", lambda k: not by_key.searchable_kind[k]),
                              ("stored transformed", lambda k: by_key.transformed[k])]),
        (props[2], props[3], [(r, lambda k, ps=ps: matches_any(k, ps)) for r, ps in mod.UNSET_REASONS]),
        (props[3], props[4], [("no values", lambda k: not values_of(k)),
                              ("a value shared with another concrete key", lambda k: any(len(occ[v]) > 1
                                                                                         for v in values_of(k))),
                              ("a value that's a string literal in the code", lambda k: any(v in lits
                                                                                            for v in values_of(k)))]),
    ]
    out.p("\nWhy keys drop out from one column to the next (each dropped key counts once, under the combination of "
          "reasons that apply to it):\n")
    out.p(side_by_side_html([dropout_reasons(name_from, keys_from, name_to, keys_to, reasons)
                             for (name_from, keys_from), (name_to, keys_to), reasons in pairs]))
    if cfg_only:
        out.write()
        print(f"wrote {out.path}")
        return

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

    # Why keys are in only one of the two (the module's graph_docs_reason)
    for name, ks, in_graph in (("Documented but not in the graph", docs_only, False),
                               ("In the graph but not documented", graph_only, True)):
        c = collections.Counter(mod.graph_docs_reason(k, in_graph) for k in ks)
        out.p(f"\n{name}, by reason:")
        out.table(pd.DataFrame(c.most_common(), columns=["reason", "keys"]))

    # Kind agreement
    rows = []
    for k in sorted(both):  # (sorted: the same csv every run)
        if docs[k] == "map":
            continue  # the docs give no type for the map's values
        gk = full[k]["kind"]
        dk = mod.docs_kind(docs[k])
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

    if args.label_keys:
        out.h("Confidence of message => full key mappings (paramtrack's label_keys.csv)")
        confidence(args.label_keys, out, f"{args.out}/confidence_distribution.png", ignore)
    write_visuals(fkdf, full_key_rows(full_all, docs_all), config, covered_by, searched, matched, custom, args, out)

    out.write()
    print(f"wrote {out.path}")


if __name__ == "__main__":
    main()
