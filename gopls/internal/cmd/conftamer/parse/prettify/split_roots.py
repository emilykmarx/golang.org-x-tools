"""Split a .gv into weakly connected components, then into per-root subgraphs. Called by attrs.go
with --split-roots; see README.md for the output files.

Usage: split_roots.py <in.gv> <outdir> <stem> [misc_max_nodes]   (roots on stdin, one per line)
"""
import glob
import os
import re
import shutil
import sys

import igraph
import pydot


def unquote(s):
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    return s


def read_dot(path):
    # igraph can't read DOT, so parse with pydot and convert
    (dot,) = pydot.graph_from_dot_file(path)
    g = igraph.Graph(directed=True)
    idx = {}

    def add_vertex(name, attrs):
        name = unquote(name)
        if name in ("node", "edge", "graph"):
            return None
        if name not in idx:
            idx[name] = len(idx)
            g.add_vertex(name)
        v = g.vs[idx[name]]
        for k, val in attrs.items():
            v[k] = unquote(val)
        return idx[name]

    for n in dot.get_nodes():
        add_vertex(n.get_name(), n.get_attributes())
    edges, eattrs = [], []
    for e in dot.get_edges():
        edges.append((add_vertex(e.get_source(), {}), add_vertex(e.get_destination(), {})))
        eattrs.append({k: unquote(v) for k, v in e.get_attributes().items()})
    g.add_edges(edges)
    for e, attrs in zip(g.es, eattrs):
        for k, v in attrs.items():
            e[k] = v
    return g


def main():
    in_gv, outdir, stem = sys.argv[1], sys.argv[2], sys.argv[3]
    misc_max_nodes = int(sys.argv[4]) if len(sys.argv) > 4 else 10
    roots = {l.strip() for l in sys.stdin if l.strip()}
    g = read_dot(in_gv)
    # don't leave stale files from previous runs
    shutil.rmtree(outdir, ignore_errors=True)
    for prefix in (f"{stem}_misc", f"{stem}_cycles"):
        for f in glob.glob(glob.escape(prefix) + "*.gv"):
            os.remove(f)

    comps = sorted(g.connected_components(mode="weak"), key=len, reverse=True)
    small = [v for c in comps if len(c) <= misc_max_nodes for v in c]
    large = [c for c in comps if len(c) > misc_max_nodes]
    misc = g.induced_subgraph(small)
    misc_gv = f"{stem}_misc_{misc.vcount()}nodes.gv"
    misc.write_dot(misc_gv)
    print(f"misc: {sum(len(c) <= misc_max_nodes for c in comps)} components, "
          f"{misc.vcount()} nodes, {misc.ecount()} edges -> {misc_gv}")

    unreachable = []
    for i, comp in enumerate(large):
        compdir = os.path.join(outdir, f"comp{i}")
        os.makedirs(compdir)
        comp_roots = sorted(g.vs[v]["name"] for v in comp if g.vs[v]["name"] in roots)
        print(f"comp{i}: {len(comp)} nodes, {len(comp_roots)} roots")
        comp_misc, n_comp_misc = set(), 0
        covered = set()
        for root in comp_roots:
            reachable = g.subcomponent(g.vs.find(name=root), mode="out")
            covered.update(reachable)
            if len(reachable) <= misc_max_nodes:
                comp_misc.update(reachable)
                n_comp_misc += 1
                continue
            sub = g.induced_subgraph(reachable)
            fname = f"{sub.vcount()}nodes_{re.sub(r'[^A-Za-z0-9._-]', '_', root)}.gv"
            sub.write_dot(os.path.join(compdir, fname))
            print(f"  {root}: {sub.vcount()} nodes, {sub.ecount()} edges -> {fname}")
        if comp_misc:
            sub = g.induced_subgraph(sorted(comp_misc))
            fname = f"misc_{sub.vcount()}nodes.gv"
            sub.write_dot(os.path.join(compdir, fname))
            print(f"  misc: {n_comp_misc} roots, {sub.vcount()} nodes, {sub.ecount()} edges -> {fname}")
        unreachable.extend(v for v in comp if v not in covered)

    if unreachable:
        sub = g.induced_subgraph(sorted(unreachable))
        cycles_gv = f"{stem}_cycles_{sub.vcount()}nodes.gv"
        sub.write_dot(cycles_gv)
        print(f"cycles: {sub.vcount()} nodes unreachable from any root, {sub.ecount()} edges -> {cycles_gv}")


if __name__ == "__main__":
    main()
