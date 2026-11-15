"""The baseline run's files and settings (SKILL.md, "Inputs"), for the scripts that rerun paramtrack's steps on a
candidate and must match it: score.py and check_labels.py. paramtrack records its settings, defaults filled in, in
2-full-keys/settings.json (module, module dir, root, format, docs, ignore file, transformed-keys file) and
3-chains/testbed.json (testbed, --wait, --vm, argmatch's build args)."""

import glob
import json
import os
import shutil
import sys

# paramtrack's modules (moved: old runs' paths to the ignore and transformed-keys files)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "../../../../gopls/internal/cmd/conftamer/querygraph/paramtrack"))
import layout  # noqa: E402  (where a run's step outputs are)
import modules  # noqa: E402


class MissingInput(Exception):
    """The baseline run lacks a file the scripts need (e.g. it predates paramtrack recording it)"""


def _load(path, what):
    if not os.path.exists(path):
        raise MissingInput(f"no {path} ({what}): the baseline run must be paramtrack through step 3 (3-chains), "
                           f"recent enough to record it; redo it")
    return json.load(open(path))


def copy_graph(baseline, out):
    """Copy the baseline's step 1 (its graph, and the module's extra graph edges if any) to run out, where paramtrack
    finds the module's step 1 (<out>/<module>/1-unmarshaler-graph)"""
    mod = modules.load(settings(baseline)["module"])
    dest = f"{out}/{os.path.basename(os.path.dirname(mod.__file__))}/1-unmarshaler-graph"
    os.makedirs(dest, exist_ok=True)
    shutil.copy(graph(baseline), dest)
    extra = f"{layout.step_dir(run_dir(baseline), '1-unmarshaler-graph')}/extra.json"
    if os.path.exists(extra):
        shutil.copy(extra, dest)


def graph(baseline):
    path = f"{layout.step_dir(run_dir(baseline), '1-unmarshaler-graph')}/unmarshaler_subgraph.text"
    if not os.path.exists(path):
        raise MissingInput(f"no {path}: the baseline run must be paramtrack through step 3 (3-chains)")
    # Since 2026-11-12 the graph resolves the keys custom unmarshalers pick (the module's extra_graph: e.g.
    # Prometheus' <name>_sd_configs); a graph from before has `*` there, which would now pass for full keys
    mod = modules.load(settings(baseline)["module"])
    if hasattr(mod, "extra_graph") and not os.path.exists(f"{layout.step_dir(run_dir(baseline), '1-unmarshaler-graph')}/extra.json"):
        raise MissingInput(f"{path} predates the module's extra graph edges (no extra.json next to it): redo the "
                           f"baseline's step 1 (paramtrack --redo 1 --until 3), or the whole baseline")
    return path


def run_dir(baseline):
    """The baseline's directory for its root: <baseline>/<module>/<root> (paramtrack since 2026-11-15), or the baseline
    itself (before). The skill handles one root for now"""
    if os.path.exists(f"{baseline}/2-full-keys"):
        return baseline
    found = sorted(glob.glob(f"{baseline}/*/*/2-full-keys"))
    if len(found) > 1:
        raise MissingInput(f"{baseline} has several roots ({', '.join(f.split('/')[-2] for f in found)}): the skill "
                           f"handles one for now")
    return os.path.dirname(found[0]) if found else f"{baseline}/?/?"


def settings(baseline):
    """paramtrack's static settings (2-full-keys/settings.json)"""
    return _load(f"{run_dir(baseline)}/2-full-keys/settings.json", "paramtrack's static settings")


def root(s):
    """The baseline's root: its name in its module's ROOTS (settings.json's target; a baseline from before has only the
    root's Go type). paramtrack takes the module's checkout and config format, and the root's docs, ignore and
    transformed-keys files, from module.py, so they must be the ones the baseline used"""
    mod = modules.load(s["module"])
    for k, v in (("module_dir", mod.MODULE_DIR), ("format", mod.FORMAT)):
        if os.path.abspath(s[k]) != os.path.abspath(v) if k == "module_dir" else s[k] != v:
            raise MissingInput(f"the baseline's {k} ({s[k]!r}) differs from module {s['module']}'s module.py ({v!r}), "
                               f"which paramtrack now uses; redo the baseline")
    name = s.get("target") or next((n for n, r in mod.ROOTS.items() if r["root"] == s["root"]), None)
    if name not in mod.ROOTS:
        raise MissingInput(f"the baseline's root {s.get('target') or s['root']} isn't in module {s['module']}'s ROOTS "
                           f"({', '.join(mod.ROOTS)}); redo the baseline")
    r = mod.ROOTS[name]
    for k in ("root", "docs", "ignore", "transformed"):
        if k in s and (modules.moved(s[k]) or "") != (r.get(k) or ""):
            raise MissingInput(f"the baseline's {k} ({s[k]!r}) differs from module {s['module']}'s ROOTS[{name!r}] "
                               f"({r.get(k)!r}), which paramtrack now uses; redo the baseline")
    return name


def static_args(s):
    """paramtrack arguments for the static settings s, but --config (the rest are module.py's: root())"""
    return ["--module", s["module"]]


def testbed_args(baseline):
    """paramtrack arguments for the baseline's testbed settings (3-chains/testbed.json)"""
    tb = _load(f"{layout.testbed_dir(run_dir(baseline), '3-chains')}/testbed.json", "the testbed settings")
    args = ["--testbed", tb["testbed"], "--wait", str(tb["wait"])]
    if "vm" in tb:
        args += ["--vm", tb["vm"]]
    for k, v in tb["argmatch_args"].items():
        args += ["--argmatch-arg", f"{k}={v}"]
    return args
