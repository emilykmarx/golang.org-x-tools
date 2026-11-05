#!/usr/bin/env python3
"""Print the canonical distinctive value for a concrete key, so that every run of the skill picks the same value for
the same key (see SKILL.md, "Values").

The value is a token derived from the concrete key: `ccfg-` + the key, lowercased, with every run of characters
other than [a-z0-9] replaced by `-`. A list entry after the first gets `-<index>` (concrete keys leave out list
indices, so e.g. two scrape jobs' `scrape_configs.job_name` would otherwise get the same value). --shape fits the
token to what the field accepts:
  plain   the token, e.g. ccfg-remote-write-sigv4-access-key
  label   a Prometheus label name ([a-zA-Z_][a-zA-Z0-9_]*): - becomes _
  path    a file added to the pod (see the testbed's TESTBED.md, "Adding files"): <dir>/<token>
  host    a DNS name that can't resolve: <token>.invalid

Usage: value.py <concrete key> [--index N] [--shape plain|label|path|host] [--dir /etc/prometheus/config]
"""

import argparse
import re


def token(key, index=0):
    t = "ccfg-" + re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")
    return f"{t}-{index}" if index else t


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("key")
    ap.add_argument("--index", type=int, default=0, help="List entry index, for entries after the first")
    ap.add_argument("--shape", default="plain", choices=["plain", "label", "path", "host"])
    ap.add_argument("--dir", default="/etc/prometheus/config", help="With --shape path: where added files are")
    a = ap.parse_args()
    t = token(a.key, a.index)
    print({"plain": t, "label": t.replace("-", "_"), "path": f"{a.dir}/{t}", "host": f"{t}.invalid"}[a.shape])


if __name__ == "__main__":
    main()
