"""The baseline run (SKILL.md, "Inputs"; paramtrack's README, "Use with /concreteconfig", step 1), for the scripts that
rerun paramtrack's steps on a candidate and must match it: score.py and check_labels.py. The baseline is one run for
the testbed: paramtrack through step 3 (3-chains) with every module of interest instrumented and running its shipped
configs, reused by every module's skill run. paramtrack records its testbed settings (testbed, instrumented modules,
--wait, --vm) in <baseline>/testbed/3-chains/testbed.json, and each module's events in
<baseline>/<module>/3-chains/events.jsonl. A skill run covers one module, all its config sources (roots()); the module's
other settings (checkout, format, each config source's root type, docs, ignore and transformed-keys files) are its module.py's."""

import json
import os
import shutil
import sys

# paramtrack's modules
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "../../../../gopls/internal/cmd/conftamer/querygraph/paramtrack"))
import layout  # noqa: E402,F401  (where a run's step outputs are; check_labels.py uses it)
import modules  # noqa: E402
import testbeds  # noqa: E402  (testbed.json's settings of earlier testbeds: recorded)


class MissingInput(Exception):
    """The baseline run lacks a file the scripts need, or doesn't fit the skill's module"""


def _load(path, what):
    if not os.path.exists(path):
        raise MissingInput(f"no {path} ({what}): the baseline run must be paramtrack through step 3 (3-chains), "
                           f"with paramtrack's layout of 2026-11-15 or later; redo it")
    return json.load(open(path))


def testbed(baseline):
    """The baseline's testbed settings (testbed/3-chains/testbed.json), checked against paramtrack's testbed"""
    path = f"{baseline}/testbed/3-chains/testbed.json"
    tb = testbeds.recorded(_load(path, "the testbed settings"))
    if tb["testbed"] != testbeds.NAME:
        raise MissingInput(f"the baseline ran on testbed {tb['testbed']} ({path}), which paramtrack no longer has "
                           f"(it has {testbeds.NAME}); redo the baseline")
    return tb


def module(baseline, name):
    """The skill's module (its module.py, loaded by name), checked to be one the baseline instrumented"""
    try:
        mod = modules.load(name)
    except ValueError as e:
        raise MissingInput(str(e))
    dirname = os.path.basename(os.path.dirname(mod.__file__))
    tb = testbed(baseline)
    if dirname not in tb["modules"]:
        raise MissingInput(f"the baseline didn't instrument {dirname} (it instrumented {', '.join(tb['modules'])}): "
                           f"redo it with every module of interest")
    events(baseline, dirname)
    return mod, dirname


def events(baseline, name):
    """A module's events in the baseline (<baseline>/<module>/3-chains/events.jsonl)"""
    path = f"{baseline}/{name}/3-chains/events.jsonl"
    if not os.path.exists(path):
        raise MissingInput(f"no {path}: the baseline run must be paramtrack through step 3 (3-chains)")
    return path


def roots(mod):
    """The module's config sources a skill run configures: its CONFIG_SOURCES but those wholly out of scope (an ignore file with `*`,
    e.g. Alertmanager's cluster_tls)"""
    def all_ignored(r):
        return bool(r.get("ignore")) and "*" in {line.strip() for line in open(r["ignore"])}
    return [name for name, r in mod.CONFIG_SOURCES.items() if not all_ignored(r)]


def parse_configs(values, mod, flag="--config"):
    """{config source: path} from --config <source>=<path> arguments, each a config source of the module's"""
    out = {}
    for v in values:
        root, sep, path = v.partition("=")
        if not sep or root not in mod.CONFIG_SOURCES:
            raise MissingInput(f"{flag} {v}: give <source>=<path>, with a config source of the module's ({', '.join(mod.CONFIG_SOURCES)})")
        if root in out:
            raise MissingInput(f"{flag}: config source {root} given twice")
        out[root] = os.path.abspath(path)
    return out


def testbed_args(baseline):
    """paramtrack arguments for the baseline's testbed settings: every module it instrumented (the skill's runs
    instrument the same ones, the others with their shipped configs), --wait and --vm. (Not argmatch's args: the
    skill's runs stop after testbed run 1, which instruments nothing)"""
    tb = testbed(baseline)
    args = [a for m in tb["modules"] for a in ("--module", m)] + ["--wait", str(tb["wait"])]
    if "vm" in tb:
        args += ["--vm", tb["vm"]]
    return args


def graph_cache(baseline, name):
    """Where the module's step 1 (its graph, and its extra graph edges if any) is kept for the skill's runs: in the
    baseline, where paramtrack would put it (<baseline>/<module>/1-unmarshaler-graph). A baseline with shipped configs
    has none: the first score.py builds it and keeps it there"""
    return f"{baseline}/{name}/1-unmarshaler-graph"


def copy_graph(baseline, name, out):
    """Copy the module's kept step 1 to run out, where paramtrack finds it; False if none is kept yet"""
    src = graph_cache(baseline, name)
    if not os.path.exists(f"{src}/unmarshaler_subgraph.text"):
        return False
    dest = f"{out}/{name}/1-unmarshaler-graph"
    if not os.path.exists(dest):
        shutil.copytree(src, dest)
    return True


def keep_graph(baseline, name, out):
    """Keep run out's step 1 for the module in the baseline (graph_cache)"""
    src, dest = f"{out}/{name}/1-unmarshaler-graph", graph_cache(baseline, name)
    if os.path.exists(f"{src}/unmarshaler_subgraph.text") and not os.path.exists(dest):
        shutil.copytree(src, dest)
