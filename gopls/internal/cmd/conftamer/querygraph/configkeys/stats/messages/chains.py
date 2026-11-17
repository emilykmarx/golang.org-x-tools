#!/usr/bin/env python3
"""How similar the chains of a run's messages are.

The messages are the run's sent messages: its 5-matches/events.jsonl events (by default; --step) of kind Request sent
or Response sent,
labelled as argmatch-labels' join.py labels them (as count_labels.py does), each label reduced to its path or route
pattern (reduce_label: its api_id, method, paths and code fields dropped), so the labels that differ only in those
fields are one message. A message's events are those whose label reduces to it.

An event's chain is its stack (the goroutine that sent it, innermost first), then the frames in its ancestry (the
stacks that created that goroutine, and so on, up to main), as join.py parses them: the code path from the send to the
root.
Similarity is measured on the module's own functions (MODULE_FUNCTIONS: the function-name prefixes of the run's module,
its module.py's CHAIN_FUNCTIONS, by default its Go module, from its 2-full-keys/settings.json, e.g.
github.com/prometheus/prometheus/...), since
nearly every request shares the net/http frames:
- per message: its events, its distinct chains (full, and module functions only), and its origin(s): the outermost
  module function of each chain (e.g. notifier.(*Manager).Run);
- per pair of messages: the Jaccard similarity of the sets of module functions on their chains.

Usage: uv run --with pandas --with tabulate python chains.py --run <paramtrack out dir> [--out <file.md>]
"""

import argparse
import collections
import csv
import itertools
import json
import os
import re
import sys

import pandas as pd

sys.path.insert(0, "/home/emily/projects/config_tracing/dump_ancestors/argmatch-labels")
import join  # noqa: E402  (argmatch-labels' join.py: message_labels)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../../paramtrack"))
import layout  # noqa: E402  (paramtrack's: where a run's step outputs are)
import modules  # noqa: E402  (paramtrack's modules: CHAIN_FUNCTIONS)

MODULE = "github.com/prometheus/prometheus/"  # (message_chains sets it to the run's Go module; short strips it)
MODULE_FUNCTIONS = (MODULE,)  # (message_chains sets it to the run's module's CHAIN_FUNCTIONS)
KINDS = ("Request sent", "Response sent")  # the sent messages


def reduce_label(label):
    """A message label reduced to its path or route pattern: no braces, field names, api_id, method, paths or code
    (e.g. `{api_id: k8s.io, method: GET, path: /api/v1/pods}` => `/api/v1/pods`). Received responses name their path
    `req.URL.Path` (e.g. `{req.Method: GET, req.URL.Path: /version, resp.StatusCode: 200}` => `/version`)."""
    fields = dict(f.split(": ", 1) for f in label.strip("{}").split(", "))
    return fields.get("path") or fields.get("pattern") or fields.get("req.URL.Path")


def chain(ev):
    """The event's chain: the functions of its frames, innermost first, as argmatch-labels' join.py parses its stack
    and ancestry (step 6 joins on these frames): each physical frame's inlined functions, then its own; without
    runtime.goexit, and without join.py's "created by" and "...elided..." entries"""
    return tuple(n for kind, _, _, names in join.chain(ev["stack"], ev.get("ancestry") or "") if kind == "frame"
                 for n in names if n != "runtime.goexit")


# A closure's or go statement wrapper's suffix (pkg.F.func1, pkg.F.gowrap2, pkg.F.func1.3): argmatch, like stacklog's
# parser, names a closure by the function that defines it
_CLOSURE = re.compile(r"(\.(func|gowrap)\d+(\.\d+)*)+$")


def declared(fn):
    """The function declaration fn's frame is in, as argmatch names it: without a closure's suffix, and without a
    generic function's or receiver's [...] (argmatch keeps a generic receiver's, but not a generic function's)"""
    return _CLOSURE.sub("", fn).replace("[...]", "")


def chan_types(path):
    """argmatch's chans output (matches.chans.csv): function (as declared names it) => {named type => the directions
    (send, recv, both) of its channel parameters with it}, for parameters whose element type has named types"""
    out = collections.defaultdict(lambda: collections.defaultdict(set))
    for row in csv.DictReader(open(path)):
        for t in filter(None, row["named_types"].split(";")):
            out[row["function"].replace("[...]", "")][t].add(row["direction"])
    return out


def message_chan_types(chains, table):
    """(kind, message) => {named type => (function, direction) for each function on its chains with a channel
    parameter of it}, from chains (message_chains) and table (chan_types)"""
    out = {}
    for k, cs in chains.items():
        found = collections.defaultdict(set)
        for f in {declared(f) for c in cs for f in c}:
            for t, dirs in table.get(f, {}).items():
                found[t] |= {(f, d) for d in dirs}
        out[k] = found
    return out


def short(fn):
    return fn.removeprefix(MODULE)


# The testbed run whose events chains are read from: step 5 (testbed run 2), the run argmatch searched, so that the
# chains and chain groups are of the same process run as the matches (matches.py joins step 5's events with argmatch's
# calls). Step 3 (testbed run 1) has the same fields, and gave the same chain groups for the 2026-11-09 run.
STEP = "5-matches"


def message_chains(run, step=STEP, kinds=KINDS):
    """(kind, message) => the chains of its events in run's <step>/events.jsonl, for messages of the given kinds (the
    sent ones by default). Sets MODULE and MODULE_FUNCTIONS to the run's module's."""
    global MODULE, MODULE_FUNCTIONS
    MODULE = json.load(open(f"{layout.step_dir(run, '2-full-keys')}/settings.json"))["module"] + "/"
    MODULE_FUNCTIONS = tuple(getattr(modules.load(MODULE.rstrip("/")), "CHAIN_FUNCTIONS", (MODULE,)))
    events = [json.loads(line) for line in open(f"{layout.step_dir(run, step)}/events.jsonl") if line.strip()]
    chains = collections.defaultdict(list)
    for i, label in join.message_labels(events).items():
        if events[i]["kind"] in kinds:
            chains[(events[i]["kind"], reduce_label(label))].append(chain(events[i]))
    return chains


def message_context_ids(run, step=STEP, kinds=KINDS):
    """(kind, message) => the context IDs of its events in run's <step>/events.jsonl (contexttrack's context_id: the
    request's context, e.g. id:10), for messages of the given kinds, labelled and reduced as message_chains does"""
    return {k: {cid for cid, _ in es} for k, es in message_exchanges(run, step, kinds).items()}


def message_exchanges(run, step=STEP, kinds=KINDS):
    """(kind, message) => its events' (context ID, (req.Method, req.URL.Path)), as message_context_ids. A request and
    its response have the same all three (req.URL.Host is only in the request)"""
    events = [json.loads(line) for line in open(f"{layout.step_dir(run, step)}/events.jsonl") if line.strip()]
    out = collections.defaultdict(set)
    for i, label in join.message_labels(events).items():
        if events[i]["kind"] in kinds:
            cid = (events[i].get("context") or {}).get("context_id")
            if cid:
                m = events[i].get("message") or {}
                out[(events[i]["kind"], reduce_label(label))].add((cid, (m.get("req.Method"), m.get("req.URL.Path"))))
    return out


def module_functions(cs):
    """The module functions on chains cs, and their origins (each chain's outermost module function)"""
    mod = [tuple(f for f in c if f.startswith(MODULE_FUNCTIONS)) for c in cs]
    return set().union(*map(set, mod)) if mod else set(), sorted({short(c[-1]) for c in mod if c})


def groups_of(funcs):
    """Messages with exactly the same (non-empty) set of module functions, from funcs: (kind, message) => set"""
    same = collections.defaultdict(list)
    for k, fs in funcs.items():
        if fs:
            same[frozenset(fs)].append(k)
    return list(same.values())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True, help="a paramtrack run: a root's directory, <out>/<module>/<root> (paramtrack's layout.py; a run from before 2026-11-15: its --out) (reads <step>/events.jsonl)")
    ap.add_argument("--step", default=STEP, help="the testbed step whose events to read (default %(default)s; or "
                                                 "3-chains)")
    ap.add_argument("--out", help="write the report here (markdown) rather than to stdout")
    a = ap.parse_args()

    chains = message_chains(a.run, a.step)

    lines = [f"<!-- Generated by configkeys/stats/messages/chains.py --run {a.run} --step {a.step} -->",
             f"# Chains of the sent messages ({a.run}, {a.step}/events.jsonl)", ""]
    rows, funcs = [], {}
    for kind, m in sorted(chains):
        cs = chains.get((kind, m), [])
        funcs[(kind, m)], origins = module_functions(cs)
        rows.append({"kind": kind, "message": m, "events": len(cs), "distinct chains": len(set(cs)),
                     "distinct module chains": len({tuple(f for f in c if f.startswith(MODULE_FUNCTIONS)) for c in cs}),
                     "origins": "; ".join(origins) or "-"})
    df = pd.DataFrame(rows)
    missing = df[df.events == 0]
    lines += [f"- Messages: {len(df)}; events: {df.events.sum()}; messages with no events in this run: {len(missing)}"
              + "".join(f"\n  - {r.kind}: `{r.message}`" for r in missing.itertuples()), ""]

    # Per message
    lines += ["## Per message", "", df.to_markdown(index=False), ""]

    # Origins: which messages share one
    by_origin = collections.defaultdict(list)
    for r in df.itertuples():
        for o in r.origins.split("; "):
            by_origin[o].append(r.message)
    og = pd.DataFrame([(o, len(ms), "; ".join(ms)) for o, ms in sorted(by_origin.items(), key=lambda x: -len(x[1]))],
                      columns=["origin (outermost module function)", "messages", "which"])
    lines += ["## Messages by origin", "", og.to_markdown(index=False), ""]

    # Pairwise similarity of module functions, within each kind and across all messages
    present = [k for k in funcs if funcs[k]]
    sims = []
    for x, y in itertools.combinations(present, 2):
        u = funcs[x] | funcs[y]
        sims.append({"a": f"{x[0]}: {x[1]}", "b": f"{y[0]}: {y[1]}", "same kind": x[0] == y[0],
                     "jaccard": round(len(funcs[x] & funcs[y]) / len(u), 3)})
    sd = pd.DataFrame(sims)
    bins = [-0.001, 0, 0.25, 0.5, 0.75, 0.999, 1]
    labels = ["0", "(0, 0.25]", "(0.25, 0.5]", "(0.5, 0.75]", "(0.75, 1)", "1 (same functions)"]
    dist = pd.DataFrame({
        "pairs of the same kind": pd.cut(sd[sd["same kind"]].jaccard, bins, labels=labels).value_counts(sort=False),
        "pairs of different kinds": pd.cut(sd[~sd["same kind"]].jaccard, bins, labels=labels).value_counts(
            sort=False)})
    lines += ["## Pairs of messages: Jaccard similarity of their module functions", "",
              f"- Pairs: {len(sd)} (of messages with events); median {sd.jaccard.median():.2f}, "
              f"mean {sd.jaccard.mean():.2f}", "", dist.rename_axis("jaccard").to_markdown(), ""]
    # Groups of messages with exactly the same module functions
    groups = [[f"{k[0]}: {k[1]}" for k in g] for g in groups_of(funcs) if len(g) > 1]
    lines += [f"- Groups of messages whose chains have exactly the same module functions: {len(groups)}"]
    lines += [f"  - {len(ms)}: " + "; ".join(f"`{m}`" for m in ms) for ms in sorted(groups, key=len, reverse=True)]
    lines.append("")

    text = "\n".join(lines)
    if a.out:
        open(a.out, "w").write(text)
        print(f"wrote {a.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
