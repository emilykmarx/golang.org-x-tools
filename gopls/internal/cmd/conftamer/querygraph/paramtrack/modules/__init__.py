"""The modules paramtrack knows: one subdirectory each (e.g. prometheus/), whose README.md is the one place for what's
specific to that module, and whose module.py holds the settings and hooks the workflow's scripts read:

- GO_MODULE, MODULE_DIR, ROOT, FORMAT, DOCS, IGNORE, TRANSFORMED: paramtrack's defaults for --module, --module-dir,
  --root, --format, --docs, --ignore and --transformed
- GOLITS_PACKAGE, GOLITS_TAGS: what stats.py's golits loads (the package compiled into the module's binary, and its
  build tags)
- TITLE: the module, for headings
- CustomKeys(module_dir): resolves the `*`s that custom unmarshalers pick (TERMINOLOGY.md, "Full key"): .names (graph
  type => the key it's under), .parents (the keys whose `*` it resolves), .full_key(pattern, nodes, ends_custom) (the
  full key for a pattern and its path's nodes, or None for a catch-all path ending at a custom unmarshaler), .of(fk)
  (the resolved key a full key is under, or None)
- docs_kind(type): the kind of Go value a documented type means (string, number, bool, map)
- graph_docs_reason(key, in_graph): why a key is only in the graph (in_graph) or only in the docs
- UNSET_REASONS: why the concreteconfig skill left target keys unset: (reason, fnmatch patterns)
"""

import glob
import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def names():
    return sorted(os.path.basename(os.path.dirname(p)) for p in glob.glob(f"{HERE}/*/module.py"))


def load(module):
    """The module.py of a module, by its directory name (e.g. prometheus) or its Go module path (GO_MODULE)"""
    for name in names():
        spec = importlib.util.spec_from_file_location(f"paramtrack_module_{name}", f"{HERE}/{name}/module.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        if module in (name, m.GO_MODULE):
            return m
    raise ValueError(f"no module {module!r} in {HERE} (known: {', '.join(names())})")


def moved(path):
    """A settings.json path as of now: runs from before 2026-11-12 recorded paramtrack/ignore/<module>.txt and
    paramtrack/transformed/<module>.txt, since moved to modules/<module>/ignore.txt and transformed.txt"""
    if path and not os.path.exists(path):
        d, f = os.path.split(path)
        kind = os.path.basename(d)
        if kind in ("ignore", "transformed") and f.endswith(".txt"):
            new = f"{HERE}/{f[:-len('.txt')]}/{kind}.txt"
            if os.path.exists(new):
                return new
    return path
