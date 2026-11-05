#!/usr/bin/env python3
"""Count the distinct message labels in contexttrack's events, labelled as argmatch-labels' join.py labels them (from
contexttrack's analysis/message_graph.py), and with --baseline, list the labels the baseline run had that this run
doesn't. The skill's hard requirement is that a new concrete config loses none (see SKILL.md, "Constraints").

A label is (message kind, message label). Compare runs of the same length and workload: a shorter run can miss
labels for that reason alone.

Usage: count_labels.py <events.jsonl> [--baseline <events.jsonl>]
Exits 1 if labels are missing from the baseline.
"""

import argparse
import json
import sys

sys.path.insert(0, "/home/emily/projects/config_tracing/dump_ancestors/argmatch-labels")
import join  # noqa: E402  (argmatch-labels' join.py: message_labels)


def labels(path):
    events = [json.loads(line) for line in open(path) if line.strip()]
    return {(events[i]["kind"], label) for i, label in join.message_labels(events).items()}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("events")
    ap.add_argument("--baseline")
    a = ap.parse_args()
    have = labels(a.events)
    print(f"{len(have)} distinct message labels in {a.events}")
    if not a.baseline:
        return
    base = labels(a.baseline)
    missing, new = sorted(base - have), sorted(have - base)
    print(f"baseline {a.baseline}: {len(base)}; missing here: {len(missing)}; new here: {len(new)}")
    for kind, label in missing:
        print(f"  missing: {kind} {label}")
    for kind, label in new:
        print(f"  new: {kind} {label}")
    sys.exit(1 if missing else 0)


if __name__ == "__main__":
    main()
