"""Where a paramtrack run's step outputs are, for the scripts that read a run (stats.py --run, the messages/ scripts,
the concreteconfig skill's scripts).
See paramtrack README for layout.

The scripts take a root's directory, <out>/<module>/<root>, as "the run". A run from before 2026-11-15 has every step
directly under --out; pass --out itself, and its steps are found there."""

import os

# The steps whose outputs are the module's (the others are each root's)
MODULE_STEPS = ("1-unmarshaler-graph", "3-chains", "5-matches", "6-label-values")
# The testbed steps: one run for all the run's modules, whose own files are in <out>/testbed/<step>
TESTBED_STEPS = ("3-chains", "5-matches")


def old(run):
    """Whether run is a run from before 2026-11-15, with every step directly under it"""
    return os.path.isdir(f"{run}/2-full-keys") and os.path.isdir(f"{run}/1-unmarshaler-graph")


def step_dir(run, step):
    """The directory of a step's outputs, for a root's directory run (<out>/<module>/<root>)"""
    if old(run) or step not in MODULE_STEPS:
        return f"{run}/{step}"
    return f"{os.path.dirname(run)}/{step}"


def testbed_dir(run, step):
    """The directory of a testbed step's own files (testbed.json, logs), for a root's directory run"""
    return f"{run}/{step}" if old(run) else f"{os.path.dirname(os.path.dirname(run))}/testbed/{step}"
