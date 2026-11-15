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
import layout  # (where a run's step outputs are)
import modules  # (modules/<module>/module.py: each module's settings and hooks)
from common import ARGMATCH_LABELS, CT, GOPLS, QUERYGRAPH, log_command, run, say, source_version
from testbeds import OUTPUTS, TESTBEDS, PrombenchVM, canonical

# stats.py's full key and section rules (STATISTICS.md); step 8 runs it
STATS = f"{QUERYGRAPH}/configkeys/stats"
sys.path.insert(0, STATS)
import stats  # noqa: E402

csv.field_size_limit(1 << 30)


class Target:
    """One root of the module's config (module.py's ROOTS) that the run maps messages to, with its own concrete config
    (and files), docs, ignore and transformed files (README.md, "Several roots"). Its directory, <out>/<module>/<root>,
    has the steps run per target (PER_TARGET); the module's steps are in its parent (layout.py)"""

    def __init__(self, name, dir, root, docs, ignore, transformed, config, config_files):
        self.name, self.dir, self.root, self.docs = name, dir, root, docs
        self.ignore, self.transformed, self.config, self.config_files_arg = ignore, transformed, config, config_files

    def file(self, step, name):
        return f"{self.dir}/{step}/{name}"


class Workflow:
    def __init__(self, args, mod, targets):
        self.a = args
        self.out = os.path.abspath(args.out)
        self.mod = mod  # the module's settings and hooks (modules/<module>/module.py)
        self.targets = targets

    def dir(self, step):
        """The directory of a step not run per target: the module's, <out>/<module>/<step> (layout.py). The testbed
        steps write the testbed run's own files to <out>/testbed/<step> (units)"""
        return f"{self.out}/{self.a.module_name}/{step}"

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

    def config_copy(self, t):
        return t.file("2-full-keys", self.config_name())

    def untagged_keys(self, dashes):
        """The flag lifting our untagged-field exception, if the module sets UNTAGGED_KEYS (gopls conftamer's
        -untagged-keys, configkeys' --untagged-keys: the graph and configkeys must agree)"""
        return [f"{dashes}untagged-keys"] if getattr(self.mod, "UNTAGGED_KEYS", False) else []

    def configkeys_bin(self):
        bin = f"{self.out}/bin/configkeys"
        if getattr(self, "_configkeys_built", False):
            return bin
        os.makedirs(os.path.dirname(bin), exist_ok=True)
        run(["go", "build", "-o", bin, "."], cwd=f"{QUERYGRAPH}/configkeys", log=f"{bin}.build.log", timeout=600)
        self._configkeys_built = True
        return bin

    def config_files(self, t):
        """The files the target's concrete config refers to: --config-files, or a files/ directory next to --config"""
        if t.config_files_arg:
            return t.config_files_arg
        sibling = os.path.join(os.path.dirname(os.path.abspath(t.config)), "files")
        return sibling if os.path.isdir(sibling) else None

    def ignore(self, t):
        """stats.py's --ignore (the full keys out of scope) and --transformed (those stored transformed), for steps 2
        and 8"""
        return ((["--ignore", t.ignore] if t.ignore else []) +
                (["--transformed", t.transformed] if t.transformed else []))

    def step_files(self, t):
        """Step 2's copy of the target's config files, if any"""
        d = t.file("2-full-keys", "files")
        return d if os.path.isdir(d) else None

    def full_keys(self, t, d):
        """The in-scope full keys YAML (every full key the graph allows, but those --ignore leaves out: configkeys
        without a config, then stats.py's SD resolution), for the concreteconfig skill; copies of the concrete
        config and its files, which the later steps read; and the static settings (settings.json)"""
        # The static settings, defaults filled in, so later runs can match them (e.g. the concreteconfig skill's
        # score.py and check_labels.py match the baseline run's)
        with open(f"{d}/settings.json", "w") as f:
            json.dump(self.static_settings(t), f, indent=2)
        graph = self.file("1-unmarshaler-graph", "unmarshaler_subgraph.text")
        cmd = [self.configkeys_bin(), "--unmarshaler-subgraph", graph, "--module-prefix", self.a.module, "--format",
               self.a.format, "--output", f"{d}/configkeys_graph.csv"] + self.untagged_keys("--")
        if t.root:
            cmd += ["--root", t.root]
        run(cmd, log=f"{d}/configkeys.log", timeout=600)
        run([sys.executable, f"{STATS}/stats.py", "--full-keys-only", "--configkeys", f"{d}/configkeys_graph.csv",
             "--graph", graph, "--docs", t.docs, "--module", self.a.module, "--module-dir", self.a.module_dir,
             "--root", t.root or "", "--out", d] + self.ignore(t),
            log=f"{d}/stats.log", timeout=600)
        # The concrete config is an input, generated beforehand by the /concreteconfig skill (README.md, "Use with
        # /concreteconfig")
        shutil.copy(t.config, f"{d}/{self.config_name()}")
        if self.config_files(t):
            shutil.copytree(self.config_files(t), f"{d}/files")

    def static_settings(self, t):
        """The static settings step 2 records in the target's settings.json, defaults filled in"""
        # module_source: the module checkout's version, which the graph (step 1) is built from
        s = {"module": self.a.module, "module_dir": os.path.abspath(self.a.module_dir),
             "module_source": source_version(self.a.module_dir), "root": t.root,
             "format": self.a.format, "docs": os.path.abspath(t.docs),
             "ignore": os.path.abspath(t.ignore) if t.ignore else "",
             "transformed": os.path.abspath(t.transformed) if t.transformed else ""}
        s["target"] = t.name
        return s

    # The recorded settings a resumed run must match (check_settings), per step. Not --ignore and --transformed:
    # steps 2 and 8 read them as given to each invocation, so that step 8 can be redone with others. Not argmatch's
    # args for run 1, which instruments nothing.
    RESUME_SETTINGS = {
        "2-full-keys": ("settings.json", ["module", "module_dir", "module_source", "root", "format", "docs"]),
        "3-chains": ("testbed.json", ["testbed", "wait", "vm"]),
        "5-matches": ("testbed.json", ["testbed", "wait", "vm", "argmatch_args"]),
    }

    def check_settings(self, step, d, t=None):
        """When resuming: stop if this invocation's settings differ from those the done step recorded (the target's,
        for step 2)"""
        if step not in self.RESUME_SETTINGS:
            return
        name, fields = self.RESUME_SETTINGS[step]
        path = f"{d}/{name}"
        if not os.path.exists(path):
            say(f"{step}: no {name} (a run from before paramtrack recorded it); can't check its settings")
            return
        recorded = json.load(open(path))
        if "testbed" in recorded:
            recorded["testbed"] = canonical(recorded["testbed"])  # (runs before the testbeds were renamed)
        if "docs" in recorded:
            recorded["docs"] = modules.moved(recorded["docs"])  # (runs before the docs lists moved)
        current = self.static_settings(t) if name == "settings.json" else self.testbed_settings(self.make_testbed())
        diff = []
        for k in fields:
            if k == "module_source" and k not in recorded:
                continue  # (runs before paramtrack recorded it, 2026-11-14)
            r, c = recorded.get(k), current.get(k)
            if isinstance(r, dict) and isinstance(c, dict):  # (argmatch's args: just the ones that differ)
                diff += [f"{k} {a}: recorded {r.get(a)!r}, now {c.get(a)!r}" for a in sorted(r.keys() | c.keys())
                         if r.get(a) != c.get(a)]
            elif r != c:
                diff.append(f"{k}: recorded {r!r}, now {c!r}")
        if diff:
            sys.exit(f"{step} was run with other settings ({path}):\n  " + "\n  ".join(diff) +
                     f"\nrerun it with these (--redo {step.split('-', 1)[0]}), or resume with the recorded ones")

    @staticmethod
    def same_config(config_a, files_a, config_b, files_b):
        """Whether two concrete configs and their files directories (or None) have the same contents"""
        def tree(root):
            return {os.path.relpath(os.path.join(dp, f), root): os.path.join(dp, f)
                    for dp, _, fs in os.walk(root) for f in fs} if root and os.path.isdir(root) else {}
        ta, tb = tree(files_a), tree(files_b)
        return filecmp.cmp(config_a, config_b, shallow=False) and ta.keys() == tb.keys() and \
            all(filecmp.cmp(ta[f], tb[f], shallow=False) for f in ta)

    def check_config_copy(self, t, d):
        """When resuming: stop if the target's --config or its files differ from step 2's copies, which the later
        steps read"""
        if not self.same_config(t.config, self.config_files(t), f"{d}/{self.config_name()}", f"{d}/files"):
            sys.exit(f"--config or its files differ from step 2's copies in {d}; to use them, rerun from step 2 "
                     f"(--redo 2)")

    def configkeys(self, t, d):
        graph = self.file("1-unmarshaler-graph", "unmarshaler_subgraph.text")
        cmd = [self.configkeys_bin(), "--config", self.config_copy(t), "--txt-dir", d,
               "--strings-only", "--unmarshaler-subgraph", graph, "--module-prefix", self.a.module,
               "--format", self.a.format, "--output", f"{d}/configkeys.csv"] + self.untagged_keys("--")
        if t.root:
            cmd += ["--root", t.root]
        run(cmd, log=f"{d}/configkeys.log", timeout=600)

    # ---------------------------------------------------------------------------------------------------------------
    # Dynamic phase

    def testbed(self, d):
        """The testbed for a run, whose effective settings (defaults filled in) it records in <d>/testbed.json, so
        later runs can match them (e.g. the concreteconfig skill's label checks match the baseline run's)"""
        tb = self.make_testbed()
        with open(f"{d}/testbed.json", "w") as f:
            json.dump(self.testbed_settings(tb), f, indent=2)
        return tb

    def make_testbed(self):
        overrides = dict(kv.split("=", 1) for kv in self.a.argmatch_arg)
        return TESTBEDS[self.a.testbed](wait=self.a.wait, argmatch_args=overrides, vm=self.a.vm)

    def testbed_settings(self, tb):
        """The testbed's effective settings, as recorded in testbed.json"""
        settings = {"testbed": self.a.testbed, "wait": tb.wait, "argmatch_args": tb.argmatch_args}
        if isinstance(tb, PrombenchVM):
            settings["vm"] = tb.vm
        return settings

    def testbed_config(self):
        """The concrete config (step 2's copy) and its files for the testbed runs. The testbeds take one config; with
        several targets, theirs must be the same (several roots over one file), until a testbed takes several"""
        t0 = self.targets[0]
        for t in self.targets[1:]:
            if not self.same_config(self.config_copy(t0), self.step_files(t0), self.config_copy(t),
                                    self.step_files(t)):
                sys.exit(f"targets {t0.name} and {t.name} have different concrete configs, but testbed "
                         f"{self.a.testbed} takes one")
        return self.config_copy(t0), self.step_files(t0)

    def matchfile(self, d):
        """The values run 2 searches for: step 4's param_values.txt, all the targets' (in d, with several)"""
        if len(self.targets) == 1:
            return self.targets[0].file("4-configkeys", "param_values.txt")
        values = []
        for t in self.targets:
            values += open(t.file("4-configkeys", "param_values.txt")).read().splitlines()
        path = f"{d}/param_values.txt"
        with open(path, "w") as f:
            f.writelines(v + "\n" for v in dict.fromkeys(values))
        return path

    # A testbed run's outputs that are the module's (the rest, e.g. testbed.json and the logs, are the testbed run's).
    # NOT DECIDED YET: some files that stay in <out>/testbed/<step> are the module's too (its pod/container log, its
    # image build log, Caddy's generated/running/load configs); with several modules in one testbed run (the combined
    # testbed), they need a per-module name or place
    MODULE_OUTPUTS = OUTPUTS + ("build_source.json",)

    def module_outputs(self, tb, d):
        """Move the module's outputs of a testbed run from the testbed run's directory tb to the module's, d"""
        for f in self.MODULE_OUTPUTS:
            if os.path.exists(f"{tb}/{f}"):
                shutil.move(f"{tb}/{f}", f"{d}/{f}")

    def chains(self, tb, d):
        """Run 1: contexttrack records message chains; argmatch instruments nothing. The testbed run's files go to tb,
        the module's (events, and the stack log of their chains) to d"""
        config, files = self.testbed_config()
        self.testbed(tb).run(tb, config, files=files)
        self.module_outputs(tb, d)
        run([sys.executable, f"{ARGMATCH_LABELS}/events_to_stacklog.py", f"{d}/events.jsonl"],
            log=f"{d}/stacklog.txt", timeout=300)

    def matches(self, tb, d):
        """Run 2: argmatch instruments the functions in run 1's chains and searches for the values; contexttrack
        records the messages to label. The testbed run's files go to tb, the module's (matches, events) to d"""
        config, files = self.testbed_config()
        self.testbed(tb).run(tb, config, files=files, stacklog=self.file("3-chains", "stacklog.txt"),
                            matchfile=self.matchfile(d))
        self.module_outputs(tb, d)
        if os.path.getsize(f"{d}/matches.calls.csv") == 0:
            raise RuntimeError(f"argmatch found no matches; see {tb}")

    def label_values(self, d):
        run([sys.executable, f"{ARGMATCH_LABELS}/join.py", self.file("5-matches", "matches.calls.csv"),
             self.file("5-matches", "events.jsonl"), d], log=f"{d}/summary.txt", timeout=900)
        say(open(f"{d}/summary.txt").readline().strip())

    # ---------------------------------------------------------------------------------------------------------------
    # Analysis phase

    @staticmethod
    def param_keys(t):
        """Needle (a value as written in param_values.txt) => the target's concrete keys, from its param_keys.txt"""
        value_keys = {}
        for line in open(t.file("4-configkeys", "param_keys.txt")).read().splitlines():
            value, _, keys = line.rpartition(":")
            value_keys[value] = keys.split(",")
        return value_keys

    def label_keys(self, t, d):
        """Message label => Full keys, with a confidence for each (README.md, "label_keys.csv"), for one target"""
        full, config = stats.load_configkeys(t.file("4-configkeys", "configkeys.csv"))

        sections = {fk: sorted({stats.section(info, p) for p in info["paths"]}) for fk, info in full.items()}
        section_keys = collections.defaultdict(set)
        for fk, secs in sections.items():
            for s in secs:
                section_keys[s].add(fk)

        value_keys = self.param_keys(t)
        # The other targets' needles: evidence for them, not this one (a needle no target has stays, keyless)
        others = {v for o in self.targets if o is not t for v in self.param_keys(o)} - value_keys.keys()

        # Full keys that could have been evidence: set by the concrete config, with a value step 5 searched for (in
        # scope or not: the confidences below leave out those --ignore does)
        ignore = stats.read_patterns(t.ignore)
        meta = stats.meta_keys(full, self.mod)  # (out of scope, like --ignore's)
        searched = set(open(t.file("4-configkeys", "param_values.txt")).read().splitlines())
        eligible = {fk for c in config.values() if any(v.replace("\n", "\\n") in searched for v in c["values"])
                    for fk in c["full_keys"]}

        # Evidence rows: each (message, needle) row of the join, with every concrete key whose value the needle is
        rows, no_key, no_full_key = [], set(), set()
        for r in csv.DictReader(open(self.file("6-label-values", "joined.csv"), newline="")):
            base = [r["message_kind"], r["message_label"], r["needle"]]
            if r["needle"] in others:
                continue
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
        # section's in-scope keys (not left out by --ignore, nor meta keys) that are evidence for it, out of all of them and out of
        # those that could have been (eligible; blank if there are none)
        evidence = collections.defaultdict(set)  # (kind, label, section) => its full keys that are evidence
        for r in rows:
            if r[5]:
                evidence[(r[0], r[1], r[5])].add(r[4])
        for (kind, label, sec), ev in sorted(evidence.items()):
            keys = section_keys[sec]
            in_scope = {k for k in keys if not stats.matches_any(k, ignore) and k not in meta}
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

    def stats(self, t, d):
        """stats.py's statistics and visuals (../configkeys/stats/STATISTICS.md) on the target's outputs (--run: the
        earlier steps' outputs and settings.json; --ignore and --transformed as given to this invocation), then
        visualize.sh's coloured graphs"""
        shutil.copy(self.file("1-unmarshaler-graph", "unmarshaler_subgraph.text"), d)  # visualize.sh reads it here
        run([sys.executable, f"{STATS}/stats.py", "--run", t.dir, "--out", d] + self.ignore(t), log=f"{d}/stats.log",
            timeout=900)
        run(["bash", f"{STATS}/visualize.sh", d, self.a.module], log=f"{d}/visualize.log", timeout=1200)
        say(f"tables in {t.file('8-stats', 'stats_tables.md')}")

    # ---------------------------------------------------------------------------------------------------------------

    # (configkeys comes after testbed run 1, which doesn't need it, so that runs up to run 1, like the concreteconfig
    # skill's label baselines, skip it)
    STEPS = ["1-unmarshaler-graph", "2-full-keys", "3-chains", "4-configkeys", "5-matches", "6-label-values",
             "7-label-keys", "8-stats"]
    # The steps each target runs; the others are the module's (layout.py)
    PER_TARGET = {"2-full-keys", "4-configkeys", "7-label-keys", "8-stats"}

    def file(self, step, name):
        return f"{self.dir(step)}/{name}"

    def units(self, step):
        """What running a step means: (arguments for its function, the directories it writes, a label), once per
        target for a step run per target; else once, the testbed steps writing the testbed run's directory too"""
        if step in self.PER_TARGET:
            return [((t,), [f"{t.dir}/{step}"], f"{step} ({t.name})") for t in self.targets]
        if step in layout.TESTBED_STEPS:
            return [((), [f"{self.out}/testbed/{step}", self.dir(step)], step)]
        return [((), [self.dir(step)], step)]

    def run(self):
        os.makedirs(self.out, exist_ok=True)
        # paramtrack's own command line, once per invocation; each step logs the commands it runs in its commands.log
        common.COMMANDS_LOG = f"{self.out}/paramtrack-commands.log"
        log_command([sys.executable] + sys.argv)
        if any(os.path.exists(f"{self.out}/{s}") for s in ("1-unmarshaler-graph", "2-full-keys")):
            sys.exit(f"{self.out} is a run from before paramtrack's layout of 2026-11-15 (<out>/<module>/<step>, "
                     f"<out>/<module>/<root>/<step>); start a new --out (its steps' outputs still work with "
                     f"stats.py --run {self.out})")
        fns = [self.unmarshaler_graph, self.full_keys, self.chains, self.configkeys, self.matches,
               self.label_values, self.label_keys, self.stats]
        only = {self.step_index(s) for s in self.a.only}
        redo = self.step_index(self.a.redo) if self.a.redo else len(self.STEPS)
        until = self.step_index(self.a.until) if self.a.until else len(self.STEPS) - 1
        ran = False
        for i, (step, fn) in enumerate(zip(self.STEPS, fns)):
            if i > until:
                break
            ran_before = ran
            for args, dirs, what in self.units(step):
                # Once a step has run, the later steps' outputs are out of date. A step is done if it wrote all its
                # directories; one that wrote only some reruns
                stale = i >= redo or ran_before or i in only
                runs = not only or i in only
                if stale or (runs and not all(os.path.exists(d) for d in dirs)):
                    for d in dirs:
                        if os.path.exists(d):
                            shutil.rmtree(d)
                if all(os.path.exists(d) for d in dirs):
                    t = args[0] if args else None
                    if step == "2-full-keys":
                        self.check_config_copy(t, dirs[0])
                    self.check_settings(step, dirs[0], t)
                    say(f"{what}: done already ({', '.join(dirs)})")
                    continue
                if only and i not in only:
                    continue
                # Write into partial directories, so that a step that fails or is interrupted isn't taken as done
                partials = [d + ".partial" for d in dirs]
                for p in partials:
                    shutil.rmtree(p, ignore_errors=True)
                    os.makedirs(p)
                say(f"{what}: running")
                common.COMMANDS_LOG = f"{partials[0]}/commands.log"
                fn(*args, *partials)
                common.COMMANDS_LOG = None
                for p, d in zip(partials, dirs):
                    os.rename(p, d)
                ran = True
                say(f"{what}: done ({', '.join(dirs)})")

    def step_index(self, name):
        for i, s in enumerate(self.STEPS):
            if name in (s, s.split("-", 1)[0], s.split("-", 1)[1]):
                return i
        sys.exit(f"unknown step {name}; steps: {', '.join(self.STEPS)}")


def targets(a, mod, out):
    """The run's targets: the roots of the module's ROOTS that --config names (<name>=<path>), each under
    <out>/<module>/<name>"""
    def split(values, flag):
        named = {}
        for v in values:
            name, sep, path = v.partition("=")
            if not sep or name not in mod.ROOTS:
                sys.exit(f"{flag} {v}: give <root>=<path>, for a root of module {a.module} (ROOTS: "
                         f"{', '.join(mod.ROOTS)})")
            if name in named:
                sys.exit(f"{flag}: root {name} given twice")
            named[name] = path
        return named

    configs, files = split(a.config, "--config"), split(a.config_files, "--config-files")
    if set(files) - set(configs):
        sys.exit(f"--config-files for roots with no --config: {', '.join(sorted(set(files) - set(configs)))}")
    ts = []
    for name, config in configs.items():
        r = mod.ROOTS[name]
        ts.append(Target(name, f"{out}/{a.module_name}/{name}", r["root"], r["docs"], r.get("ignore", ""),
                         r.get("transformed", ""), config, files.get(name)))
    return ts


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="Directory for every step's output (one subdirectory per step)")
    ap.add_argument("--config", required=True, action="append", metavar="ROOT=PATH",
                    help="Concrete config for a root of the module (its module.py's ROOTS), e.g. the /concreteconfig "
                         "skill's config.yaml (README.md, \"Use with /concreteconfig\"); once per root the run covers")
    ap.add_argument("--config-files", action="append", default=[], metavar="ROOT=PATH",
                    help="Directory of files a root's concrete config refers to, which the testbed installs (default: "
                         "a files/ directory next to its --config, if any)")
    # The module, whose module.py (modules/<module>/) has its settings: checkout, config format, roots
    ap.add_argument("--module", required=True,
                    help="The module: its Go module path (as in go.mod) or its modules/ directory name; known: "
                         + ", ".join(modules.names()))
    ap.add_argument("--testbed", default="docker", type=canonical, choices=sorted(TESTBEDS),
                    help="where the module runs (testbeds.py; earlier names, e.g. prombench, still work)")
    ap.add_argument("--vm", default="paramtrack-e2e", help="GCE VM for the prombench-* testbeds")
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
    # The module's settings come from its module.py: its checkout, config format and roots (ROOTS: targets())
    a.module_dir, a.format = mod.MODULE_DIR, mod.FORMAT
    a.module_name = os.path.basename(os.path.dirname(mod.__file__))  # its modules/ directory name: <out>/<it>
    Workflow(a, mod, targets(a, mod, os.path.abspath(a.out))).run()


if __name__ == "__main__":
    main()
