#!/usr/bin/env python3
"""Check which documented Caddy struct fields (raw/ from list_caddy_keys.py) have a
matching `json:"<key>` tag in a given Caddy source tree, to see how far the
caddyserver.com docs are from that tree's version.

Usage: python3 check_against_source.py CADDY_SRC_DIR
Prints the documented (struct type, key) pairs with no matching tag in the tree.
Only git-tracked .go files are read if the tree is a git checkout.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PREFIX = "github.com/caddyserver/caddy/v2"


def fields(v, out):
    if not isinstance(v, dict):
        return
    tn = v.get("type_name") or ""
    for f in v.get("struct_fields") or []:
        out.add((tn, f["key"]))
        fields(f["value"], out)
    for k in ("elems", "map_keys"):
        fields(v.get(k), out)


def go_files(src):
    r = subprocess.run(["git", "ls-files", "*.go"], cwd=src, capture_output=True, text=True, timeout=60)
    if r.returncode == 0 and r.stdout:
        return [src / p for p in r.stdout.split()]
    return list(src.rglob("*.go"))


def main():
    src = Path(sys.argv[1]).resolve()
    pairs = set()
    fields(json.loads((HERE / "raw" / "config_root.json").read_text())["result"]["structure"], pairs)
    for p in (HERE / "raw" / "module").glob("*.json"):
        for r in json.loads(p.read_text())["result"]:
            if r["repo"] == "https://github.com/caddyserver/caddy":
                fields(r.get("structure"), pairs)
    tags = {}  # package dir (relative) -> set of json tag names
    for f in go_files(src):
        if f.name.endswith("_test.go"):
            continue
        rel = str(f.parent.relative_to(src))
        tags.setdefault(rel, set()).update(re.findall(r'json:"([^",]+)', f.read_text(errors="replace")))
    missing, checked = [], 0
    for tn, key in sorted(pairs):
        pkg = tn.rsplit(".", 1)[0]
        if not pkg.startswith(PREFIX):
            continue  # struct from another module (certmagic, acmez, ...)
        rel = pkg[len(PREFIX):].lstrip("/") or "."
        checked += 1
        if key not in tags.get(rel, set()):
            missing.append(f"{tn}\t{key}")
    print(f"{src}: {checked} documented (struct, key) pairs in caddy/v2 packages, "
          f"{len(missing)} without a matching json tag in the struct's package", file=sys.stderr)
    print("\n".join(missing))


if __name__ == "__main__":
    main()
