"""The baseline run's files and settings (SKILL.md, "Inputs"), for the scripts that rerun paramtrack's steps on a
candidate and must match it: score.py and check_labels.py. paramtrack records its settings, defaults filled in, in
2-full-keys/settings.json (module, module dir, root, format, docs, ignore file, transformed-keys file) and
3-chains/testbed.json (testbed, --wait, --vm, argmatch's build args)."""

import json
import os
import shutil
import sys

# paramtrack's modules (moved: old runs' paths to the ignore and transformed-keys files)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "../../../../gopls/internal/cmd/conftamer/querygraph/paramtrack"))
import modules  # noqa: E402


class MissingInput(Exception):
    """The baseline run lacks a file the scripts need (e.g. it predates paramtrack recording it)"""


def _load(path, what):
    if not os.path.exists(path):
        raise MissingInput(f"no {path} ({what}): the baseline run must be paramtrack through step 3 (3-chains), "
                           f"recent enough to record it; redo it")
    return json.load(open(path))


def copy_graph(baseline, out):
    """Copy the baseline's step 1 (its graph, and the module's extra graph edges if any) to run out"""
    os.makedirs(f"{out}/1-unmarshaler-graph", exist_ok=True)
    shutil.copy(graph(baseline), f"{out}/1-unmarshaler-graph/")
    extra = f"{baseline}/1-unmarshaler-graph/extra.json"
    if os.path.exists(extra):
        shutil.copy(extra, f"{out}/1-unmarshaler-graph/")


def graph(baseline):
    path = f"{baseline}/1-unmarshaler-graph/unmarshaler_subgraph.text"
    if not os.path.exists(path):
        raise MissingInput(f"no {path}: the baseline run must be paramtrack through step 3 (3-chains)")
    # Since 2026-11-12 the graph resolves the keys custom unmarshalers pick (the module's extra_graph: e.g.
    # Prometheus' <name>_sd_configs); a graph from before has `*` there, which would now pass for full keys
    mod = modules.load(settings(baseline)["module"])
    if hasattr(mod, "extra_graph") and not os.path.exists(f"{baseline}/1-unmarshaler-graph/extra.json"):
        raise MissingInput(f"{path} predates the module's extra graph edges (no extra.json next to it): redo the "
                           f"baseline's step 1 (paramtrack --redo 1 --until 3), or the whole baseline")
    return path


def settings(baseline):
    """paramtrack's static settings (2-full-keys/settings.json)"""
    return _load(f"{baseline}/2-full-keys/settings.json", "paramtrack's static settings")


def static_args(s):
    """paramtrack arguments for the static settings s. A baseline from before paramtrack recorded the transformed-keys
    file gets paramtrack's default."""
    args = ["--module", s["module"], "--module-dir", s["module_dir"], "--root", s["root"], "--format", s["format"],
            "--docs", s["docs"], "--ignore", modules.moved(s["ignore"])]
    return args + (["--transformed", modules.moved(s["transformed"])] if "transformed" in s else [])


def testbed_args(baseline):
    """paramtrack arguments for the baseline's testbed settings (3-chains/testbed.json)"""
    tb = _load(f"{baseline}/3-chains/testbed.json", "the testbed settings")
    args = ["--testbed", tb["testbed"], "--wait", str(tb["wait"])]
    if "vm" in tb:
        args += ["--vm", tb["vm"]]
    for k, v in tb["argmatch_args"].items():
        args += ["--argmatch-arg", f"{k}={v}"]
    return args
