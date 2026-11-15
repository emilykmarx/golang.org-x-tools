""" Manages all module-specific info used by the paramtrack programs.
Each module has a directory under this one, with a module.py holding
settings (e.g. the path to the module source) and hooks (e.g. info about custom unmarshalers)
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
    paramtrack/transformed/<module>.txt, since moved to modules/<module>/ignore.txt and transformed.txt; runs from
    before 2026-11-14 recorded documented-key lists outside paramtrack (config_tracing/caddy-config-keys/,
    prom-test-infra/prombench/), since moved to modules/<module>/documentedkeys/ with the same names"""
    if path and not os.path.exists(path):
        d, f = os.path.split(path)
        kind = os.path.basename(d)
        if kind in ("ignore", "transformed") and f.endswith(".txt"):
            new = f"{HERE}/{f[:-len('.txt')]}/{kind}.txt"
            if os.path.exists(new):
                return new
        found = glob.glob(f"{HERE}/*/documentedkeys/{f}")
        if len(found) == 1:
            return found[0]
    return path
