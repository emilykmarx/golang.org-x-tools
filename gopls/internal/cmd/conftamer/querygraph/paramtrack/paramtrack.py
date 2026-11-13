#!/usr/bin/env python3
"""Runs the end-to-end param tracking workflow and joins its outputs into Message label => Full keys. See README.md.

Run with: uv run --with pandas --with matplotlib --with tabulate python paramtrack.py --out DIR --config CONFIG --module MODULE
[flags]
Exits 1 if the testbed failed, or some other error occurred.
"""

import argparse
import collections
import csv
import filecmp
import json
import os
import shutil
import sys

import common
import modules  # (modules/<module>/module.py: each module's settings and hooks)
from common import ARGMATCH_LABELS, CT, GOPLS, QUERYGRAPH, log_command, run, say
from testbeds import TESTBEDS

# stats.py's full key and section rules (STATISTICS.md); step 8 runs it
STATS = f"{QUERYGRAPH}/configkeys/stats"
sys.path.insert(0, STATS)
import stats  # noqa: E402

csv.field_size_limit(1 << 30)


class Workflow:
    def __init__(self, args):
        self.a = args
        self.out = os.path.abspath(args.out)
        self.mod = modules.load(args.module)  # the module's settings and hooks (modules/<module>/module.py)

    def dir(self, step):
        return f"{self.out}/{step}"

    # ---------------------------------------------------------------------------------------------------------------
    # Static phase

    def unmarshaler_graph(self, d):
        gopls = f"{self.out}/bin/gopls"
        run(["go", "build", "-o", gopls, "."], cwd=GOPLS, log=f"{d}/build.log", timeout=600)
        cmd = [gopls, "conftamer", f"-m={self.a.module}", f"-format={self.a.format}", f"-out={d}"]
        if hasattr(self.mod, "extra_graph"):
            # Roots and edges the source doesn't show (e.g. Caddy's modules), from the module's hook
            self.mod.extra_graph(self.a.module_dir, f"{d}/extra.json")
            cmd.append(f"-extra={d}/extra.json")
        cmd += self.untagged_keys("-")
        run(cmd, cwd=self.a.module_dir, log=f"{d}/conftamer.log", timeout=900)
        if not os.path.exists(f"{d}/unmarshaler_subgraph.text"):
            raise RuntimeError(f"gopls conftamer wrote no {d}/unmarshaler_subgraph.text; see {d}/conftamer.log")

    def config_name(self):
        """Step 2's copy of the concrete config, named for its format (config.yaml, config.json)"""
        return f"config.{self.a.format}"

    def config_copy(self):
        return self.file("2-full-keys", self.config_name())

    def untagged_keys(self, dashes):
        """The flag lifting our untagged-field exception, if the module sets UNTAGGED_KEYS (gopls conftamer's
        -untagged-keys, configkeys' --untagged-keys: the graph and configkeys must agree)"""
        return [f"{dashes}untagged-keys"] if getattr(self.mod, "UNTAGGED_KEYS", False) else []

    def configkeys_bin(self):
        bin = f"{self.out}/bin/configkeys"
        os.makedirs(os.path.dirname(bin), exist_ok=True)
        run(["go", "build", "-o", bin, "."], cwd=f"{QUERYGRAPH}/configkeys", log=f"{bin}.build.log", timeout=600)
        return bin

    def config_files(self):
        """The files the concrete config refers to: --config-files, or a files/ directory next to --config"""
        if self.a.config_files:
            return self.a.config_files
        sibling = os.path.join(os.path.dirname(os.path.abspath(self.a.config)), "files")
        return sibling if os.path.isdir(sibling) else None

    def ignore(self):
        """stats.py's --ignore (the full keys out of scope) and --transformed (those stored transformed), for steps 2
        and 8"""
        return ((["--ignore", self.a.ignore] if self.a.ignore else []) +
                (["--transformed", self.a.transformed] if self.a.transformed else []))

    def step_files(self):
        """Step 2's copy of the config's files, if any"""
        d = self.file("2-full-keys", "files")
        return d if os.path.isdir(d) else None

    def full_keys(self, d):
        """The in-scope full keys YAML (every full key the graph allows, but those --ignore leaves out: configkeys
        without a config, then stats.py's SD resolution), for the concreteconfig skill; copies of the concrete
        config and its files, which the later steps read; and the static settings (settings.json)"""
        # The static settings, defaults filled in, so later runs can match them (e.g. the concreteconfig skill's
        # score.py and check_labels.py match the baseline run's)
        with open(f"{d}/settings.json", "w") as f:
            json.dump({"module": self.a.module, "module_dir": os.path.abspath(self.a.module_dir), "root": self.a.root,
                       "format": self.a.format, "docs": os.path.abspath(self.a.docs),
                       "ignore": os.path.abspath(self.a.ignore) if self.a.ignore else "",
                       "transformed": os.path.abspath(self.a.transformed) if self.a.transformed else ""}, f, indent=2)
        graph = self.file("1-unmarshaler-graph", "unmarshaler_subgraph.text")
        cmd = [self.configkeys_bin(), "--unmarshaler-subgraph", graph, "--module-prefix", self.a.module, "--format",
               self.a.format, "--output", f"{d}/configkeys_graph.csv"] + self.untagged_keys("--")
        if self.a.root:
            cmd += ["--root", self.a.root]
        run(cmd, log=f"{d}/configkeys.log", timeout=600)
        run([sys.executable, f"{STATS}/stats.py", "--full-keys-only", "--configkeys", f"{d}/configkeys_graph.csv",
             "--graph", graph, "--docs", self.a.docs, "--module", self.a.module, "--module-dir", self.a.module_dir,
             "--out", d] + self.ignore(),
            log=f"{d}/stats.log", timeout=600)
        # The concrete config is an input, generated beforehand by the /concreteconfig skill (README.md, "Use with
        # /concreteconfig")
        shutil.copy(self.a.config, f"{d}/{self.config_name()}")
        if self.config_files():
            shutil.copytree(self.config_files(), f"{d}/files")

    def check_config_copy(self, d):
        """When resuming: stop if --config or its files differ from step 2's copies, which the later steps read"""
        def tree(root):
            return {os.path.relpath(os.path.join(dp, f), root): os.path.join(dp, f)
                    for dp, _, fs in os.walk(root) for f in fs} if root and os.path.isdir(root) else {}
        given, copied = tree(self.config_files()), tree(f"{d}/files")
        if not filecmp.cmp(self.a.config, f"{d}/{self.config_name()}", shallow=False) or given.keys() != copied.keys() or \
                any(not filecmp.cmp(given[f], copied[f], shallow=False) for f in given):
            sys.exit(f"--config or its files differ from step 2's copies in {d}; to use them, rerun from step 2 "
                     f"(--redo 2)")

    def configkeys(self, d):
        graph = self.file("1-unmarshaler-graph", "unmarshaler_subgraph.text")
        cmd = [self.configkeys_bin(), "--config", self.config_copy(), "--txt-dir", d,
               "--strings-only", "--unmarshaler-subgraph", graph, "--module-prefix", self.a.module,
               "--format", self.a.format, "--output", f"{d}/configkeys.csv"] + self.untagged_keys("--")
        if self.a.root:
            cmd += ["--root", self.a.root]
        run(cmd, log=f"{d}/configkeys.log", timeout=600)

    # ---------------------------------------------------------------------------------------------------------------
    # Dynamic phase

    def testbed(self, d):
        """The testbed for a run, whose effective settings (defaults filled in) it records in <d>/testbed.json, so
        later runs can match them (e.g. the concreteconfig skill's label checks match the baseline run's)"""
        overrides = dict(kv.split("=", 1) for kv in self.a.argmatch_arg)
        tb = TESTBEDS[self.a.testbed](wait=self.a.wait, argmatch_args=overrides, vm=self.a.vm)
        settings = {"testbed": self.a.testbed, "wait": tb.wait, "argmatch_args": tb.argmatch_args}
        if self.a.testbed == "prombench":
            settings["vm"] = tb.vm
        with open(f"{d}/testbed.json", "w") as f:
            json.dump(settings, f, indent=2)
        return tb

    def chains(self, d):
        """Run 1: contexttrack records message chains; argmatch instruments nothing"""
        self.testbed(d).run(d, self.config_copy(), files=self.step_files())
        run([sys.executable, f"{ARGMATCH_LABELS}/events_to_stacklog.py", f"{d}/events.jsonl"],
            log=f"{d}/stacklog.txt", timeout=300)

    def matches(self, d):
        """Run 2: argmatch instruments the functions in run 1's chains and searches for the values; contexttrack
        records the messages to label"""
        self.testbed(d).run(d, self.config_copy(), files=self.step_files(),
                           stacklog=self.file("3-chains", "stacklog.txt"),
                           matchfile=self.file("4-configkeys", "param_values.txt"))
        if os.path.getsize(f"{d}/matches.calls.csv") == 0:
            raise RuntimeError(f"argmatch found no matches; see {d}/container.log")

    def label_values(self, d):
        run([sys.executable, f"{ARGMATCH_LABELS}/join.py", self.file("5-matches", "matches.calls.csv"),
             self.file("5-matches", "events.jsonl"), d], log=f"{d}/summary.txt", timeout=900)
        say(open(f"{d}/summary.txt").readline().strip())

    # ---------------------------------------------------------------------------------------------------------------
    # Analysis phase

    def label_keys(self, d):
        """Message label => Full keys, with a confidence for each (README.md, "label_keys.csv")"""
        full, config = stats.load_configkeys(self.file("4-configkeys", "configkeys.csv"))

        sections = {fk: sorted({stats.section(info, p) for p in info["paths"]}) for fk, info in full.items()}
        section_keys = collections.defaultdict(set)
        for fk, secs in sections.items():
            for s in secs:
                section_keys[s].add(fk)

        # Needle (a value as written in param_values.txt) => concrete keys, from param_keys.txt
        value_keys = {}
        for line in open(self.file("4-configkeys", "param_keys.txt")).read().splitlines():
            value, _, keys = line.rpartition(":")
            value_keys[value] = keys.split(",")

        # Full keys that could have been evidence: set by the concrete config, with a value step 5 searched for (in
        # scope or not: the confidences below leave out those --ignore does)
        ignore = stats.read_patterns(self.a.ignore)
        searched = set(open(self.file("4-configkeys", "param_values.txt")).read().splitlines())
        eligible = {fk for c in config.values() if any(v.replace("\n", "\\n") in searched for v in c["values"])
                    for fk in c["full_keys"]}

        # Evidence rows: each (message, needle) row of the join, with every concrete key whose value the needle is
        rows, no_key, no_full_key = [], set(), set()
        for r in csv.DictReader(open(self.file("6-label-values", "joined.csv"), newline="")):
            base = [r["message_kind"], r["message_label"], r["needle"]]
            if r["needle"] not in value_keys:
                no_key.add(r["needle"])
                rows.append(base + ["", "", "", "", "", "", "", ""])
                continue
            for k in value_keys[r["needle"]]:
                fks = sorted(config[k]["full_keys"]) if k in config else []
                if not fks:
                    no_full_key.add(k)
                    rows.append(base + [k, "", "", "", True, True, 1, 1])
                for fk in fks:
                    for sec in sections[fk]:
                        rows.append(base + [k, fk, sec, "; ".join(sorted(section_keys[sec])), True, fk in eligible,
                                            1, 1])

        # Inferred rows: every other full key in a section with evidence for the message, with the share of the
        # section's in-scope keys (not left out by --ignore) that are evidence for it, out of all of them and out of
        # those that could have been (eligible; blank if there are none)
        evidence = collections.defaultdict(set)  # (kind, label, section) => its full keys that are evidence
        for r in rows:
            if r[5]:
                evidence[(r[0], r[1], r[5])].add(r[4])
        for (kind, label, sec), ev in sorted(evidence.items()):
            keys = section_keys[sec]
            in_scope = {k for k in keys if not stats.matches_any(k, ignore)}
            conf_all = round(len(ev & in_scope) / len(in_scope), 3) if in_scope else ""
            conf_eligible = (round(len(ev & in_scope) / len(in_scope & eligible), 3) if in_scope & eligible
                             else "")
            for fk in sorted(keys - ev):
                rows.append([kind, label, "", "", fk, sec, "; ".join(sorted(keys)), False, fk in eligible,
                             conf_all, conf_eligible])
        rows.sort(key=lambda r: (r[0], r[1], r[5], not r[7], r[4], r[3], r[2]))
        with open(f"{d}/label_keys.csv", "w", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["message_kind", "message_label", "needle", "concrete_key", "full_key", "section",
                        "section_keys", "evidence", "eligible", "confidence_all", "confidence_eligible"])
            w.writerows(rows)

        labels = {(r[0], r[1]) for r in rows}
        n_ev = sum(1 for r in rows if r[7] is True)
        summary = [f"{len(rows)} rows ({n_ev} evidence, {len(rows) - n_ev} inferred from sections); {len(labels)} "
                   f"messages (kind, label); {len({r[4] for r in rows if r[4]})} distinct full keys "
                   f"({len({r[4] for r in rows if r[7] is True and r[4]})} evidence); "
                   f"{len({r[5] for r in rows if r[5]})} sections"]
        if no_key:
            summary.append(f"needles not in param_keys.txt (no concrete key): {sorted(no_key)}")
        if no_full_key:
            summary.append(f"concrete keys with no full key: {sorted(no_full_key)}")
        open(f"{d}/summary.txt", "w").write("\n".join(summary) + "\n")
        for line in summary:
            say(line)

    def stats(self, d):
        """stats.py's statistics and visuals (../configkeys/stats/STATISTICS.md) on this run's outputs (--run: the
        earlier steps' outputs and settings.json; --ignore and --transformed as given to this invocation), then
        visualize.sh's coloured graphs"""
        shutil.copy(self.file("1-unmarshaler-graph", "unmarshaler_subgraph.text"), d)  # visualize.sh reads it here
        run([sys.executable, f"{STATS}/stats.py", "--run", self.out, "--out", d] + self.ignore(), log=f"{d}/stats.log",
            timeout=900)
        run(["bash", f"{STATS}/visualize.sh", d, self.a.module], log=f"{d}/visualize.log", timeout=1200)
        say(f"tables in {self.dir('8-stats')}/stats_tables.md")

    # ---------------------------------------------------------------------------------------------------------------

    # (configkeys comes after testbed run 1, which doesn't need it, so that runs up to run 1, like the concreteconfig
    # skill's label baselines, skip it)
    STEPS = ["1-unmarshaler-graph", "2-full-keys", "3-chains", "4-configkeys", "5-matches", "6-label-values",
             "7-label-keys", "8-stats"]

    def file(self, step, name):
        return f"{self.dir(step)}/{name}"

    def run(self):
        os.makedirs(self.out, exist_ok=True)
        # paramtrack's own command line, once per invocation; each step logs the commands it runs in its commands.log
        common.COMMANDS_LOG = f"{self.out}/paramtrack-commands.log"
        log_command([sys.executable] + sys.argv)
        fns = [self.unmarshaler_graph, self.full_keys, self.chains, self.configkeys, self.matches,
               self.label_values, self.label_keys, self.stats]
        only = {self.step_index(s) for s in self.a.only}
        redo = self.step_index(self.a.redo) if self.a.redo else len(self.STEPS)
        until = self.step_index(self.a.until) if self.a.until else len(self.STEPS) - 1
        ran = False
        for i, (step, fn) in enumerate(zip(self.STEPS, fns)):
            if i > until:
                break
            d = self.dir(step)
            # Once a step has run, the later steps' outputs are out of date
            if (i >= redo or ran or i in only) and os.path.exists(d):
                shutil.rmtree(d)
            if os.path.exists(d):
                if step == "2-full-keys":
                    self.check_config_copy(d)
                say(f"{step}: done already ({d})")
                continue
            if only and i not in only:
                continue
            # Write into a partial directory, so that a step that fails or is interrupted isn't taken as done
            partial = d + ".partial"
            shutil.rmtree(partial, ignore_errors=True)
            os.makedirs(partial)
            say(f"{step}: running")
            common.COMMANDS_LOG = f"{partial}/commands.log"
            fn(partial)
            common.COMMANDS_LOG = None
            os.rename(partial, d)
            ran = True
            say(f"{step}: done ({d})")

    def step_index(self, name):
        for i, s in enumerate(self.STEPS):
            if name in (s, s.split("-", 1)[0], s.split("-", 1)[1]):
                return i
        sys.exit(f"unknown step {name}; steps: {', '.join(self.STEPS)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="Directory for every step's output (one subdirectory per step)")
    ap.add_argument("--config", required=True, help="Concrete config, e.g. the /concreteconfig skill's config.yaml "
                                                       "(README.md, \"Use with /concreteconfig\")")
    ap.add_argument("--config-files", help="Directory of files the concrete config refers to, which the testbed "
                                           "installs (default: a files/ directory next to --config, if any)")
    # The module's settings: each default is the module's (modules/<module>/module.py)
    ap.add_argument("--module", required=True,
                    help="The module: its Go module path (as in go.mod) or its modules/ directory name; known: "
                         + ", ".join(modules.names()))
    ap.add_argument("--module-dir", help="Module source checkout")
    ap.add_argument("--root", help="configkeys --root (empty: all roots)")
    ap.add_argument("--format", help="Config format: yaml (go-yaml v2) or json (encoding/json)")
    ap.add_argument("--docs", help="Documented keys, for steps 2 and 8 (stats.py --docs)")
    ap.add_argument("--ignore", help="Full keys out of scope, which steps 2 and 8 leave out (stats.py --ignore; empty: "
                                     "none)")
    ap.add_argument("--transformed", help="Full keys whose values are stored transformed, which steps 2 and 8 mark "
                                          "(stats.py --transformed; empty: none)")
    ap.add_argument("--testbed", default="docker", choices=sorted(TESTBEDS))
    ap.add_argument("--vm", default="paramtrack-e2e", help="GCE VM for --testbed prombench")
    ap.add_argument("--wait", type=int, default=240, help="Seconds to run the module under the workload, per run")
    ap.add_argument("--argmatch-arg", action="append", default=[], metavar="KEY=VALUE",
                    help="Override an argmatch build arg (ARGMATCH_KEY), e.g. DEPTH=5; repeatable")
    ap.add_argument("--redo", metavar="STEP", help="Rerun this step and the later ones, even if done (name or number)")
    ap.add_argument("--until", metavar="STEP", help="Stop after this step")
    ap.add_argument("--only", metavar="STEP", action="append", default=[],
                    help="Run only this step, rerunning it if done, from the earlier steps' outputs as they are; "
                         "repeatable. Removes the later steps' outputs, as they're out of date")
    a = ap.parse_args()
    mod = modules.load(a.module)
    a.module = mod.GO_MODULE
    for name, default in (("module_dir", mod.MODULE_DIR), ("root", mod.ROOT), ("format", mod.FORMAT),
                          ("docs", mod.DOCS), ("ignore", mod.IGNORE), ("transformed", mod.TRANSFORMED)):
        if getattr(a, name) is None:  # (not given; "" is given, e.g. --ignore "" for none)
            setattr(a, name, default)
    Workflow(a).run()


if __name__ == "__main__":
    main()
