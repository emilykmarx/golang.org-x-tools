#!/usr/bin/env python3
"""Count the distinct message labels in contexttrack's events, labelled as argmatch-labels' join.py labels them (from
contexttrack's analysis/message_graph.py), and with --baseline, list the labels the baseline run had that this run
doesn't. Given several runs' events (e.g. reruns of one candidate, when a label may depend on timing), it counts and
compares the union of their labels. The skill's hard requirement is that a new concrete config loses none (see
SKILL.md, "Constraints").

A label is (message kind, message label), without the label's `paths` field (how many distinct paths matched a route
pattern): that depends on the workload's timing as much as on the config. Kubernetes-generated pod names in a label
(`<deployment>-<pod template hash>-<5 random characters>`, e.g. in a path the module requests about its own pod) end
in `-<pod>` instead: they change with every restart. Compare runs of the same length and workload: a shorter run can miss
labels for that reason alone.

Usage: count_labels.py <events.jsonl> [<events.jsonl> ...] [--baseline <events.jsonl>] [--list]
--list prints every label (the union's, and the baseline's), sorted, besides the counts and the differences.
Exits 0 if no baseline label is missing, 1 if one is, and 2 if it couldn't count (an error reading the events, or
bad arguments).
"""

import argparse
import json
import re
import sys
import traceback

sys.path.insert(0, "/home/emily/projects/config_tracing/dump_ancestors/argmatch-labels")
import join  # noqa: E402  (argmatch-labels' join.py: message_labels)


PATHS = re.compile(r"(, )?paths: \d+\}$")  # (message_graph.py's _label_fields puts it last)
POD = re.compile(r"-[bcdfghjklmnpqrstvwxz2456789]{6,10}-[bcdfghjklmnpqrstvwxz2456789]{5}(?=[/,} ?]|$)")


def labels(path):
    events = [json.loads(line) for line in open(path) if line.strip()]
    return {(events[i]["kind"], POD.sub("-<pod>", PATHS.sub("}", label))) for i, label in
            join.message_labels(events).items()}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("events", nargs="+")
    ap.add_argument("--baseline")
    ap.add_argument("--list", action="store_true", help="print every label, not just counts and differences")
    a = ap.parse_args()
    have = set().union(*(labels(e) for e in a.events))
    print(f"{len(have)} distinct message labels in {', '.join(a.events)}")
    if a.list:
        for kind, label in sorted(have):
            print(f"  {kind} {label}")
    if not a.baseline:
        return
    base = labels(a.baseline)
    missing, new = sorted(base - have), sorted(have - base)
    print(f"baseline {a.baseline}: {len(base)}; missing here: {len(missing)}; new here: {len(new)}")
    if a.list:
        for kind, label in sorted(base):
            print(f"  baseline: {kind} {label}")
    for kind, label in missing:
        print(f"  missing: {kind} {label}")
    for kind, label in new:
        print(f"  new: {kind} {label}")
    sys.exit(1 if missing else 0)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # (not SystemExit: main's own exit codes pass through)
        traceback.print_exc()
        sys.exit(2)
