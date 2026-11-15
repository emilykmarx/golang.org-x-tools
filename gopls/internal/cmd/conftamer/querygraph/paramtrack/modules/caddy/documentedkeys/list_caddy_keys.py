#!/usr/bin/env python3
"""List Caddy's documented JSON config keys from caddyserver.com's docs API.

See README.md (same directory) for conventions, sources and caveats.

Usage:
  python3 list_caddy_keys.py [--refetch] [--ingress-dir DIR]

Steps:
  1. Fetch (or reuse from raw/) /api/modules, /api/docs/config/ and
     /api/docs/module/<id> for every module whose repo is caddyserver/caddy.
  2. Walk the documented structure from the root, expanding modules in place.
  3. Write caddy_keys_full.txt, caddy_keys_full_jsonpaths.txt and, if
     --ingress-dir is given, caddy_keys_linked.txt (modules linked into the
     controller binary, from `go list -deps ./cmd/caddy`).
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "raw"
SITE = "https://caddyserver.com"
STD_REPO = "https://github.com/caddyserver/caddy"


def fetch(url_path, dest, refetch):
    if dest.exists() and not refetch:
        return json.loads(dest.read_text())
    url = SITE + url_path
    print(f"fetching {url}", file=sys.stderr)
    with urllib.request.urlopen(url, timeout=60) as resp:
        if resp.status != 200:
            raise SystemExit(f"{url}: HTTP {resp.status}")
        body = resp.read()
    data = json.loads(body)
    if data.get("status_code") != 200 or data.get("result") is None:
        raise SystemExit(f"{url}: unexpected response {body[:200]!r}")
    dest.write_bytes(body)
    time.sleep(0.2)  # be polite
    return data


def load_sources(refetch):
    RAW.mkdir(exist_ok=True)
    (RAW / "module").mkdir(exist_ok=True)
    modules = fetch("/api/modules", RAW / "modules.json", refetch)["result"]
    std = {}  # module ID -> {"package": ..., "structure": ...}
    for mid, variants in sorted(modules.items()):
        for v in variants:
            if v["repo"] == STD_REPO:
                std[mid] = {"package": v["package"]}
    for mid in std:
        res = fetch(f"/api/docs/module/{mid}", RAW / "module" / f"{mid}.json", refetch)["result"]
        mine = [r for r in res if r["repo"] == STD_REPO]
        if len(mine) != 1:
            raise SystemExit(f"module {mid}: {len(mine)} variants from {STD_REPO}")
        std[mid]["structure"] = mine[0].get("structure")
    root = fetch("/api/docs/config/", RAW / "config_root.json", refetch)["result"]["structure"]
    return root, std


def short(type_name):
    return type_name.rsplit("/", 1)[-1] if type_name else ""


def tstr(v):
    """Compact type string for a documented value."""
    if v is None:
        return "any"
    t = v.get("type") or "any"  # interface{} / json.RawMessage values have no type
    if t == "array":
        return "[]" + tstr(v.get("elems"))
    if t == "map":
        return f"map[{tstr(v.get('map_keys'))}]{tstr(v.get('elems'))}"
    if t == "module":
        return f"module({v.get('module_namespace')})"
    if t == "module_map":
        return f"module_map({v.get('module_namespace')})"
    if t == "struct":
        return short(v.get("type_name")) or "struct"
    tn = short(v.get("type_name"))
    return f"{t}:{tn}" if tn else t


def has_children(v):
    """True if a value has documented keys below it (so it isn't a leaf)."""
    if v is None:
        return False
    t = v.get("type")
    if t == "struct":
        return bool(v.get("struct_fields"))
    if t in ("array", "map"):
        return has_children(v.get("elems"))
    return t in ("module", "module_map")


class Walker:
    def __init__(self, std):
        self.std = std
        self.by_ns = {}
        for mid in std:
            ns, _, name = mid.rpartition(".")
            self.by_ns.setdefault(ns, []).append((name, mid))
        # rows: (path tuple, type, frozenset of module IDs on the way)
        self.rows = []
        self.used_modules = set()

    def emit(self, path, typ, mods):
        self.rows.append((tuple(path), typ, mods))

    def walk(self, v, path, types, mods):
        """types: struct type names on the ancestor chain (cycle guard);
        mods: module IDs on the ancestor chain."""
        if v is None:
            self.emit(path, "{}", mods)
            return
        t = v.get("type")
        if t == "struct":
            tn = v.get("type_name") or ""
            if tn and tn in types:
                self.emit(path, f"recursive:{short(tn)}", mods)
                return
            fields = v.get("struct_fields") or []
            if not fields:
                if path:
                    self.emit(path, "{}", mods)
                return
            for f in fields:
                self.walk(f["value"], path + [f["key"]], types | {tn}, mods)
        elif t == "array":
            if has_children(v.get("elems")):
                self.walk(v["elems"], path, types, mods)  # list index left out
            else:
                self.emit(path, tstr(v), mods)
        elif t == "map":
            if has_children(v.get("elems")):
                self.walk(v["elems"], path + ["*"], types, mods)
            else:
                self.emit(path, tstr(v), mods)
        elif t in ("module", "module_map") and v.get("module_namespace") in self.ns_of(mods):
            # A module namespace nested inside a module of the same namespace
            # (encoder wrapping an encoder, "not" matcher holding matchers):
            # not expanded again; see README.
            self.emit(path, "recursive:" + tstr(v), mods)
        elif t == "module":
            ns = v.get("module_namespace")
            inline = v.get("module_inline_key")
            cands = self.by_ns.get(ns, [])
            if not inline:
                raise SystemExit(f"module without inline key at {'.'.join(path)}")
            # The key that selects the module, e.g. handle.handler.
            self.emit(path + [inline], f"module_id:{ns}" + ("" if cands else ":no-standard-modules"), mods)
            for name, mid in sorted(cands):
                self.expand_module(mid, path + [f"{inline}={name}"], types, mods)
        elif t == "module_map":
            ns = v.get("module_namespace")
            cands = self.by_ns.get(ns, [])
            if not cands:
                self.emit(path, tstr(v) + ":no-standard-modules", mods)
            for name, mid in sorted(cands):
                self.expand_module(mid, path + [name], types, mods)
        else:
            self.emit(path, tstr(v), mods)

    @staticmethod
    def ns_of(mods):
        return {m.rpartition(".")[0] for m in mods}

    def expand_module(self, mid, path, types, mods):
        if mid in mods:
            self.emit(path, f"recursive:{mid}", mods)
            return
        self.used_modules.add(mid)
        s = self.std[mid]["structure"]
        if s is None or (s.get("type") == "struct" and not s.get("struct_fields")):
            self.emit(path, "{}", mods | {mid})  # module with no config of its own
            return
        self.walk(s, path, types, mods | {mid})


def strip_module_segments(path):
    return tuple(p for p in path if "=" not in p)


def linked_packages(ingress_dir):
    cmd = ["go", "list", "-deps", "./cmd/caddy"]
    out = subprocess.run(cmd, cwd=ingress_dir, capture_output=True, text=True, timeout=300,
                         env={**__import__("os").environ, "GOFLAGS": "-mod=readonly"})
    if out.returncode != 0:
        raise SystemExit(f"{' '.join(cmd)} failed in {ingress_dir}:\n{out.stderr}")
    return set(out.stdout.split())


def write(name, rows):
    with open(HERE / name, "w") as f:
        for path, typ in rows:
            f.write(f"{'.'.join(path)}\t{typ}\n")
    print(f"{name}: {len(rows)} keys", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refetch", action="store_true", help="re-download raw/ from caddyserver.com")
    ap.add_argument("--ingress-dir", help="caddyserver/ingress checkout; enables caddy_keys_linked.txt")
    args = ap.parse_args()

    root, std = load_sources(args.refetch)
    w = Walker(std)
    w.walk(root, [], frozenset(), frozenset())

    seen = set()
    full = []
    for path, typ, _ in w.rows:
        if path in seen:
            raise SystemExit(f"duplicate key {'.'.join(path)}")
        seen.add(path)
        full.append((path, typ))
    write("caddy_keys_full.txt", full)

    # Same keys with the module-choice segments removed: the literal JSON paths.
    jp = {}
    for path, typ in full:
        jp.setdefault(strip_module_segments(path), set()).add(typ)
    write("caddy_keys_full_jsonpaths.txt", [(p, " | ".join(sorted(ts))) for p, ts in jp.items()])

    unreached = sorted(set(std) - w.used_modules)
    print(f"standard modules: {len(std)}, reached from root: {len(w.used_modules)}, "
          f"not reached: {unreached}", file=sys.stderr)

    if args.ingress_dir:
        pkgs = linked_packages(args.ingress_dir)
        linked = {mid for mid, m in std.items() if m["package"] in pkgs}
        print(f"modules linked into the controller: {len(linked)}: {sorted(linked)}", file=sys.stderr)
        (HERE / "linked_modules.txt").write_text("".join(f"{m}\n" for m in sorted(linked)))
        write("caddy_keys_linked.txt", [(p, t) for p, t, mods in w.rows if mods <= linked])


if __name__ == "__main__":
    main()
