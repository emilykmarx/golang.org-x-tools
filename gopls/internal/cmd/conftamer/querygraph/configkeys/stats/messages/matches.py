#!/usr/bin/env python3
"""How similar argmatch's matches are, across and within the chain groups chains.py finds.

A message's matches are the unambiguous keys whose values argmatch matched in calls that join with the message's
events:
- the join is step 6's (argmatch-labels' join.py, on 5-matches/matches.calls.csv and 5-matches/events.jsonl), redone
  here so as to keep which calls each message joins with (step 6's joined.csv drops them); checked against joined.csv;
- each match is a 5-matches/matches.csv row: the needle (value), and in its `types` column the types on the path from
  the method's receiver (argmatch was built with RECVONLY=1: only receivers are searched) to the matched value;
- a matched value's full keys are those step 7's evidence rows (7-label-keys/label_keys.csv) give it, and only the
  unambiguous ones count (TERMINOLOGY.md: every value of the key is unambiguous; unambiguous_keys, the same rule as
  stats.py's Unambiguous column). Matched values that aren't config values are left out too.

Matches are compared at three levels, each a set per message:
- "full key": the full keys of its matched values;
- "full key + holder": (full key, the last type on the path: the type holding the value);
- "full key + path": (full key, the whole type path from the receiver): two messages share it only if argmatch found
  the same key's value through the same receiver type and path.

Groups, from chains.py (on 3-chains/events.jsonl): "chain group" = messages whose chains have exactly the same module
functions (one group per distinct set); "origin" = messages sharing the outermost module function of a chain (a
message with several origins is in each). For each grouping and level, the Jaccard similarity of pairs of messages
(with at least one match) in the same group vs in different groups. Then, for the full keys matched for messages in
several chain groups: the receiver types and holder types they're found through.

Usage: uv run --with pandas --with tabulate python matches.py --run <paramtrack out dir> [--out <file.md>]
"""

import argparse
import collections
import csv
import itertools
import json
import os
import sys

import pandas as pd

import chains  # (next to this file; it imports argmatch-labels' join.py)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import stats  # noqa: E402  (configkeys/stats/stats.py: the concrete config's keys, and the ambiguity rule)

join = chains.join
csv.field_size_limit(1 << 30)
LEVELS = ["full key", "full key + holder", "full key + path"]


def unambiguous_values(run):
    vals = pd.read_csv(f"{run}/8-stats/values.csv", keep_default_na=False, dtype=str)
    return set(vals[(vals["keys"] == "1") & (vals.go_literal == "False")].value), set(vals.value)


def unambiguous_keys(run):
    """The in-scope full keys that are unambiguous, as stats.py's Unambiguous column decides (stats.unambiguous_key:
    the key has values, and each stands for one concrete key and isn't a string literal in the code, which step 8's
    values.csv says). Also returns what it read: covered_by (in-scope full key => its concrete keys), config (concrete
    key => its values and full keys, from step 4's configkeys.csv) and section_of (in-scope full key => section, from
    step 8's full_keys.csv)."""
    settings = json.load(open(f"{run}/2-full-keys/settings.json"))
    fkdf = pd.read_csv(f"{run}/8-stats/full_keys.csv", keep_default_na=False, dtype=str)
    section_of = dict(zip(fkdf.key, fkdf.section))
    stats.MAP_FIELDS.update(stats.map_field_keys(f"{run}/1-unmarshaler-graph/unmarshaler_subgraph.text"))
    _, config, _ = stats.load_configkeys(f"{run}/4-configkeys/configkeys.csv", stats.sd_names(settings["module_dir"]))
    covered_by = collections.defaultdict(set)
    for k, c in config.items():
        for fk in c["full_keys"]:
            if fk in section_of:
                covered_by[fk].add(k)
    unamb, _ = unambiguous_values(run)
    keys = {fk for fk in covered_by
            if stats.unambiguous_key(stats.key_values(fk, covered_by, config), lambda v: v in unamb)}
    return keys, covered_by, config, section_of


def needle_full_keys(run):
    """needle => the full keys step 7's evidence rows give it"""
    fks = collections.defaultdict(set)
    for r in csv.DictReader(open(f"{run}/7-label-keys/label_keys.csv", newline="")):
        if r["evidence"] == "True" and r["full_key"]:
            fks[r["needle"]].add(r["full_key"])
    return fks


def joined_calls(run):
    """(kind, message) => the argmatch calls its events join with, as join.py joins them (its main(), keeping the
    call IDs), and the (kind, label) of each message event"""
    calls = list(csv.DictReader(open(f"{run}/5-matches/matches.calls.csv", newline="")))
    events = [json.loads(line) for line in open(f"{run}/5-matches/events.jsonl") if line.strip()]
    labels = join.message_labels(events)

    def key(frames):
        return tuple(t[:3] for t in frames)

    index = collections.defaultdict(list)
    for c in calls:
        ch = join.chain(c["stack"], c["ancestry"])
        index[(ch[0][1], c["function"], key(ch[1:]))].append(c["call"])
    out = collections.defaultdict(set)
    for ei, e in enumerate(events):
        if ei not in labels or e["kind"] not in chains.KINDS:
            continue
        msg = (e["kind"], chains.reduce_label(labels[ei]))
        ch = join.chain(e["stack"], e["ancestry"])
        for i, (kind, gid, _, names) in enumerate(ch):
            if kind != "frame":
                continue
            older = key(ch[i + 1:])
            for fn in set(names):
                out[msg].update(index.get((gid, fn, older), []))
    return out


def call_matches(run, wanted_calls, kept, config_values):
    """call ID => set of (needle, types path) of its matches whose needle is in kept (matches.csv, streamed: its rows
    have more fields than its header, the rest being the types); and counts of matches left out"""
    out = collections.defaultdict(set)
    left_out = collections.Counter()
    paths = {}  # interned type paths
    with open(f"{run}/5-matches/matches.csv", newline="") as f:
        r = csv.reader(f)
        h = next(r)
        ni, ci, ti = h.index("needle"), h.index("call"), h.index("types")
        for row in r:
            if len(row) <= ci or row[ci] not in wanted_calls:
                continue
            n = row[ni]
            if n not in config_values:
                left_out["not a config value"] += 1
            elif n not in kept:
                left_out["no unambiguous full key"] += 1
            else:
                t = tuple(row[ti:])
                out[row[ci]].add((n, paths.setdefault(t, t)))
    return out, left_out


def short(t):
    """A type without its module path prefix, for the tables"""
    return t.replace("github.com/prometheus/prometheus/", "").replace("github.com/prometheus/", "")


def jaccard(a, b):
    return len(a & b) / len(a | b) if a | b else 0.0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True, help="paramtrack --out directory (steps 3, 5, 6, 7 and 8)")
    ap.add_argument("--out", help="write the report here (markdown) rather than to stdout")
    a = ap.parse_args()

    msgs = chains.read_messages()
    ch = chains.message_chains(a.run)
    funcs, origins = {}, {}
    for k in msgs:
        funcs[k], origins[k] = chains.module_functions(ch.get(k, []))

    _, config_values = unambiguous_values(a.run)
    ukeys = unambiguous_keys(a.run)[0]
    # needle => its unambiguous full keys; a match counts only through those
    fks = {n: {fk for fk in f if fk in ukeys} for n, f in needle_full_keys(a.run).items()}
    kept = {n for n, f in fks.items() if f}
    msg_calls = joined_calls(a.run)
    wanted = set().union(*(msg_calls.get(k, set()) for k in msgs))
    cm, left_out = call_matches(a.run, wanted, kept, config_values)

    # Check against step 6: the (kind, reduced label, needle) of the joined matches
    step6 = set()
    for r in csv.DictReader(open(f"{a.run}/6-label-values/joined.csv", newline="")):
        if r["message_kind"] in chains.KINDS and r["needle"] in kept:
            step6.add((r["message_kind"], chains.reduce_label(r["message_label"]), r["needle"]))
    here = {(k[0], k[1], n) for k in msgs for c in msg_calls.get(k, ()) for n, _ in cm.get(c, ())}
    step6 = {x for x in step6 if (x[0], x[1]) in set(msgs)}
    if here != step6:
        print(f"warning: the join here differs from step 6's joined.csv: {len(here - step6)} extra, "
              f"{len(step6 - here)} missing (kind, message, needle)", file=sys.stderr)

    # Per message: matches at each level
    sets = {lvl: {} for lvl in LEVELS}
    sites = {}  # (kind, message) => set of (full key, types path)
    for k in msgs:
        s = {(fk, t) for c in msg_calls.get(k, ()) for n, t in cm.get(c, ()) for fk in fks.get(n, ())}
        sites[k] = s
        sets["full key"][k] = {fk for fk, _ in s}
        sets["full key + holder"][k] = {(fk, t[-1] if t else "") for fk, t in s}
        sets["full key + path"][k] = s

    def name(k):
        return f"{k[0]}: {k[1]}"

    chain_group = {}
    for i, g in enumerate(sorted(chains.groups_of(funcs), key=len, reverse=True)):
        for k in g:
            chain_group[k] = [f"chain group {i + 1}"]
    for k in msgs:
        chain_group.setdefault(k, [f"alone: {k[1]}"])
    origin_group = {k: origins[k] or ["-"] for k in msgs}
    with_matches = [k for k in msgs if sites[k]]

    lines = [f"<!-- Generated by configkeys/stats/messages/matches.py --run {a.run} -->",
             f"# argmatch's matches across and within the chain groups ({a.run})", "",
             f"- Messages: {len(msgs)}; with at least one match of an unambiguous key: {len(with_matches)}",
             "- Matches (matches.csv rows of joined calls) left out: "
             + ", ".join(f"{n} {r}" for r, n in left_out.items()),
             "- Join check against step 6's joined.csv (needles with an unambiguous key): "
             + ("same (kind, message, needle) set" if here == step6 else
                f"{len(here - step6)} extra, {len(step6 - here)} missing"), ""]

    rows = [{"message": name(k), "chain group": "; ".join(chain_group[k]), "origin": "; ".join(origin_group[k]),
             "joined calls": len(msg_calls.get(k, ())),
             **{lvl: len(sets[lvl][k]) for lvl in LEVELS},
             "receiver types": len({t[0] for _, t in sites[k] if t})} for k in msgs]
    lines += ["## Per message", "", "Matches at each level (distinct elements):", "",
              pd.DataFrame(rows).to_markdown(index=False), ""]

    def pair_table(groups):
        out = []
        for lvl in LEVELS:
            same, diff = [], []
            for x, y in itertools.combinations(with_matches, 2):
                (same if set(groups[x]) & set(groups[y]) else diff).append(jaccard(sets[lvl][x], sets[lvl][y]))
            for label, js in (("same group", same), ("different groups", diff)):
                s = pd.Series(js, dtype=float)
                out.append({"level": lvl, "pairs": label, "n": len(s),
                            "median": round(s.median(), 2) if len(s) else "-",
                            "mean": round(s.mean(), 2) if len(s) else "-",
                            "share nothing": f"{(s == 0).sum()}/{len(s)}",
                            "identical": f"{(s == 1).sum()}/{len(s)}"})
        return pd.DataFrame(out)

    def group_table(groups, lvl):
        members = collections.defaultdict(list)
        for k in with_matches:
            for g in groups[k]:
                members[g].append(k)
        out = []
        for g, ks in sorted(members.items(), key=lambda x: -len(x[1])):
            union = set().union(*(sets[lvl][k] for k in ks))
            common = set.intersection(*(sets[lvl][k] for k in ks))
            pj = [jaccard(sets[lvl][x], sets[lvl][y]) for x, y in itertools.combinations(ks, 2)]
            out.append({"group": g, "messages with matches": len(ks), "union": len(union),
                        "in every message": len(common), "mean pair jaccard": round(sum(pj) / len(pj), 2) if pj
                        else "-", "messages": "; ".join(k[1] for k in ks)})
        return pd.DataFrame(out)

    for title, groups in (("Chain groups (exactly the same module functions)", chain_group),
                          ("Origins (outermost module function)", origin_group)):
        lines += [f"## {title}", "", "Jaccard similarity of pairs of messages with matches, at each level:", "",
                  pair_table(groups).to_markdown(index=False), "",
                  "Per group, at level \"full key + path\":", "",
                  group_table(groups, "full key + path").to_markdown(index=False), ""]

    # Why full keys are shared across chain groups: the receivers and holders they're found through
    groups_of_key = collections.defaultdict(set)
    via = collections.defaultdict(lambda: collections.defaultdict(set))  # (kind of type, type) => key => groups
    for k in with_matches:
        g = chain_group[k][0]
        for fk, t in sites[k]:
            groups_of_key[fk].add(g)
            if t:
                via[("receiver", t[0])][fk].add(g)
                via[("holder", t[-1])][fk].add(g)
    shared = {fk for fk, gs in groups_of_key.items() if len(gs) > 1}
    rows = []
    for (what, t), keys in via.items():
        ks = [fk for fk in keys if fk in shared]
        if ks:
            rows.append({"through": what, "type": short(t), "shared full keys found through it": len(ks),
                         "chain groups": len(set().union(*(keys[fk] for fk in ks)))})
    vt = pd.DataFrame(rows).sort_values(["through", "shared full keys found through it"], ascending=[False, False])
    lines += ["## Full keys matched for messages in several chain groups: what they're found through", "",
              f"- Full keys matched for messages in more than one chain group: {len(shared)} of {len(groups_of_key)}",
              "", "By receiver type and by holder type (top 15 each; a key found through several counts for each):", ""]
    for what in ("receiver", "holder"):
        lines += [vt[vt.through == what].head(15).to_markdown(index=False), ""]
    # Per shared key: through how many distinct paths, and do different groups use the same path?
    rows = []
    for fk in sorted(shared, key=lambda f: -len(groups_of_key[f])):
        paths = collections.defaultdict(set)
        for k in with_matches:
            for f, t in sites[k]:
                if f == fk:
                    paths[t].add(chain_group[k][0])
        rows.append({"full key": fk, "chain groups": len(groups_of_key[fk]), "distinct type paths": len(paths),
                     "groups reached through the most shared path": max(len(gs) for gs in paths.values())})
    lines += ["Per full key matched in several chain groups (top 30): the type paths it's found through, and how many "
              "of its chain groups the most common one reaches:", "",
              pd.DataFrame(rows).head(30).to_markdown(index=False), ""]

    text = "\n".join(lines)
    if a.out:
        open(a.out, "w").write(text)
        print(f"wrote {a.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
