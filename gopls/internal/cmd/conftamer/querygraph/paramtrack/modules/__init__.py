"""The modules paramtrack knows: one subdirectory each (e.g. prometheus/), whose README.md is the one place for what's
specific to that module, and whose module.py holds the settings and hooks the workflow's scripts read:

- GO_MODULE, MODULE_DIR, ROOT, FORMAT, DOCS, IGNORE, TRANSFORMED: paramtrack's defaults for --module, --module-dir,
  --root, --format, --docs, --ignore and --transformed
- GOLITS_PACKAGE, GOLITS_TAGS: what stats.py's golits loads (the package compiled into the module's binary, and its
  build tags)
- TITLE: the module, for headings
- CustomKeys(module_dir): the keys custom unmarshalers pick (resolved in the graph by extra_graph's edges), for
  stats.py's tables of them: .names (graph type => the key it's under), .parents (the keys they're under), .of(fk)
  (the picked key a full key is under, or None)
- docs_kind(type): the kind of Go value a documented type means (string, number, bool, map)
- graph_docs_reason(key, in_graph): why a key is only in the graph (in_graph) or only in the docs
- UNSET_REASONS: why the concreteconfig skill left target keys unset: (reason, fnmatch patterns)
- UNTAGGED_KEYS (optional, default False): lift our exception for untagged fields (gopls conftamer -untagged-keys,
  configkeys --untagged-keys; see ct.UntaggedAreKeys)
- extra_graph(module_dir, out_path) (optional): write gopls conftamer's -extra file (roots and edges the source code
  doesn't show: the keys a custom unmarshaler or run-time decoding picks, e.g. Prometheus' SD configs, Caddy's
  modules); paramtrack's step 1 runs it into its directory
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
