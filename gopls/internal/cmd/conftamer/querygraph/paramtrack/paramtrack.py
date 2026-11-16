#!/usr/bin/env python3
"""Runs the end-to-end param tracking workflow and joins its outputs into Message label => Full keys. See README.md.

Run with: uv run --with pandas --with matplotlib --with tabulate python paramtrack.py --out DIR --module MODULE
--config [MODULE:]ROOT=CONFIG [flags]
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
from common import ARGMATCH_LABELS, GOPLS, QUERYGRAPH, log_command, run, say, source_version
import testbeds
from testbeds import OUTPUTS, PrombenchVM

# stats.py's full key and section rules (STATISTICS.md); step 8 runs it
STATS = f"{QUERYGRAPH}/configkeys/stats"
sys.path.insert(0, STATS)
import stats  # noqa: E402

csv.field_size_limit(1 << 30)


class Module:
    """One of the run's modules (--module), with the settings of its module.py (modules/<name>/module.py: mod). Its
    directory, <out>/<name>, has the steps run per module (PER_MODULE, and its outputs of the testbed steps); its
    targets' are under it (layout.py)"""

    def __init__(self, mod, out):
        self.mod = mod
        self.go = mod.GO_MODULE  # its Go module path
        self.src, self.format = mod.MODULE_DIR, mod.FORMAT  # its checkout, its config format
        self.name = os.path.basename(os.path.dirname(mod.__file__))  # its modules/ directory name
        self.dir = f"{out}/{self.name}"
        self.targets = []

    def file(self, step, name):
        return f"{self.dir}/{step}/{name}"


class Target:
    """One root of a module's config (module.py's ROOTS) that the run maps messages to, with its own concrete config
    (and files), docs, ignore and transformed files (README.md, "Several roots"). Its directory, <out>/<module>/<root>,
    has the steps run per target (PER_TARGET); the module's steps are in its parent (layout.py)"""

    def __init__(self, m, name, root, docs, ignore, transformed, config, config_files):
        self.m, self.name, self.dir, self.root, self.docs = m, name, f"{m.dir}/{name}", root, docs
        self.ignore, self.transformed, self.config, self.config_files_arg = ignore, transformed, config, config_files

    def file(self, step, name):
        return f"{self.dir}/{step}/{name}"


class Workflow:
    def __init__(self, args, mods):
        self.a = args
        self.out = os.path.abspath(args.out)
        self.mods = mods  # the run's modules (Module)

    # ---------------------------------------------------------------------------------------------------------------
    # Static phase

    def unmarshaler_graph(self, m, d):
        gopls = f"{self.out}/bin/gopls"
        run(["go", "build", "-o", gopls, "."], cwd=GOPLS, log=f"{d}/build.log", timeout=600)
        cmd = [gopls, "conftamer", f"-m={m.go}", f"-format={m.format}", f"-out={d}"]
        if hasattr(m.mod, "extra_graph"):
            # Roots and edges the source doesn't show (e.g. Caddy's modules), from the module's hook
            m.mod.extra_graph(m.src, f"{d}/extra.json")
            cmd.append(f"-extra={d}/extra.json")
        cmd += self.untagged_keys(m, "-")
        run(cmd, cwd=m.src, log=f"{d}/conftamer.log", timeout=900)
        if not os.path.exists(f"{d}/unmarshaler_subgraph.text"):
            raise RuntimeError(f"gopls conftamer wrote no {d}/unmarshaler_subgraph.text; see {d}/conftamer.log")

    @staticmethod
    def config_name(t):
        """Step 2's copy of the target's concrete config, named for its format (config.yaml, config.json)"""
        return f"config.{t.m.format}"

    def config_copy(self, t):
        return t.file("2-full-keys", self.config_name(t))

    @staticmethod
    def untagged_keys(m, dashes):
        """The flag lifting our untagged-field exception, if the module sets UNTAGGED_KEYS (gopls conftamer's
        -untagged-keys, configkeys' --untagged-keys: the graph and configkeys must agree)"""
        return [f"{dashes}untagged-keys"] if getattr(m.mod, "UNTAGGED_KEYS", False) else []

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
        m = t.m
        graph = m.file("1-unmarshaler-graph", "unmarshaler_subgraph.text")
        cmd = [self.configkeys_bin(), "--unmarshaler-subgraph", graph, "--module-prefix", m.go, "--format",
               m.format, "--output", f"{d}/configkeys_graph.csv"] + self.untagged_keys(m, "--")
        if t.root:
            cmd += ["--root", t.root]
        run(cmd, log=f"{d}/configkeys.log", timeout=600)
        run([sys.executable, f"{STATS}/stats.py", "--full-keys-only", "--configkeys", f"{d}/configkeys_graph.csv",
             "--graph", graph, "--docs", t.docs, "--module", m.go, "--module-dir", m.src,
             "--root", t.root or "", "--out", d] + self.ignore(t),
            log=f"{d}/stats.log", timeout=600)
        # The concrete config is an input, generated beforehand by the /concreteconfig skill (README.md, "Use with
        # /concreteconfig")
        shutil.copy(t.config, f"{d}/{self.config_name(t)}")
        if self.config_files(t):
            shutil.copytree(self.config_files(t), f"{d}/files")

    def static_settings(self, t):
        """The static settings step 2 records in the target's settings.json, defaults filled in"""
        # module_source: the module checkout's version, which the graph (step 1) is built from
        s = {"module": t.m.go, "module_dir": os.path.abspath(t.m.src),
             "module_source": source_version(t.m.src), "root": t.root,
             "format": t.m.format, "docs": os.path.abspath(t.docs),
             "ignore": os.path.abspath(t.ignore) if t.ignore else "",
             "transformed": os.path.abspath(t.transformed) if t.transformed else ""}
        s["config_source"] = t.name
        return s

    # The recorded settings a resumed run must match (check_settings), per step. Not --ignore and --transformed:
    # steps 2 and 8 read them as given to each invocation, so that step 8 can be redone with others. Not argmatch's
    # args for run 1, which instruments nothing.
    RESUME_SETTINGS = {
        "2-full-keys": ("settings.json", ["module", "module_dir", "module_source", "root", "format", "docs"]),
        "3-chains": ("testbed.json", ["testbed", "modules", "wait", "vm"]),
        "5-matches": ("testbed.json", ["testbed", "modules", "wait", "vm", "argmatch_args"]),
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
        recorded = testbeds.recorded(json.load(open(path)))  # (runs from before the testbed was renamed)
        if "docs" in recorded:
            recorded["docs"] = modules.moved(recorded["docs"])  # (runs before the docs lists moved)
        current = self.static_settings(t) if name == "settings.json" else self.testbed_settings(self.make_testbed())
        diff = []
        for k in fields:
            if k == "module_source" and k not in recorded:
                continue  # (runs before paramtrack recorded it, 2026-11-14)
            r, c = recorded.get(k), current.get(k)
            if isinstance(r, dict) and isinstance(c, dict):  # (argmatch's args, each module's: just those that differ)
                for a in sorted(r.keys() | c.keys()):
                    ra, ca = r.get(a), c.get(a)
                    if isinstance(ra, dict) and isinstance(ca, dict):
                        diff += [f"{k} {a} {x}: recorded {ra.get(x)!r}, now {ca.get(x)!r}"
                                 for x in sorted(ra.keys() | ca.keys()) if ra.get(x) != ca.get(x)]
                    elif ra != ca:
                        diff.append(f"{k} {a}: recorded {ra!r}, now {ca!r}")
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
        if not self.same_config(t.config, self.config_files(t), f"{d}/{self.config_name(t)}", f"{d}/files"):
            sys.exit(f"--config or its files differ from step 2's copies in {d}; to use them, rerun from step 2 "
                     f"(--redo 2)")

    def configkeys(self, t, d):
        m = t.m
        graph = m.file("1-unmarshaler-graph", "unmarshaler_subgraph.text")
        cmd = [self.configkeys_bin(), "--config", self.config_copy(t), "--txt-dir", d,
               "--strings-only", "--unmarshaler-subgraph", graph, "--module-prefix", m.go,
               "--format", m.format, "--output", f"{d}/configkeys.csv"] + self.untagged_keys(m, "--")
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
        return PrombenchVM(wait=self.a.wait, vm=self.a.vm)

    def testbed_settings(self, tb):
        """The testbed's effective settings, as recorded in testbed.json: modules are the instrumented ones (the
        run's; the testbed's others run stock), argmatch_args each one's"""
        return {"testbed": testbeds.NAME, "modules": sorted(m.name for m in self.mods), "wait": tb.wait,
                "argmatch_args": {m.name: testbeds.argmatch_args(m.mod) for m in sorted(self.mods, key=lambda m: m.name)},
                "vm": tb.vm}

    def testbed_config(self, m):
        """The module's concrete configs (step 2's copies; one per target, by config root) and their files for the
        testbed runs"""
        return {"configs": {t.name: self.config_copy(t) for t in m.targets},
                "files": {t.name: self.step_files(t) for t in m.targets if self.step_files(t)},
                "argmatch_args": testbeds.argmatch_args(m.mod)}

    @staticmethod
    def matchfile(m, d):
        """The values run 2 searches for in the module: step 4's param_values.txt, all its targets' (in d, with
        several)"""
        if len(m.targets) == 1:
            return m.targets[0].file("4-configkeys", "param_values.txt")
        values = []
        for t in m.targets:
            values += open(t.file("4-configkeys", "param_values.txt")).read().splitlines()
        path = f"{d}/param_values.txt"
        with open(path, "w") as f:
            f.writelines(v + "\n" for v in dict.fromkeys(values))
        return path

    # A testbed run's outputs that paramtrack moves to the module's directory, <out>/<module>/<step>. The module's
    # other files (its pod log, image build log, Caddy's generated/running/load configs) stay with the testbed run's,
    # in <out>/testbed/<step>/<module>/.
    MODULE_OUTPUTS = OUTPUTS + ("build_source.json",)

    def module_outputs(self, tb, m, d):
        """Move the module's outputs of a testbed run from the testbed run's directory tb to the module's, d"""
        for f in self.MODULE_OUTPUTS:
            if os.path.exists(f"{tb}/{m.name}/{f}"):
                shutil.move(f"{tb}/{m.name}/{f}", f"{d}/{f}")

    def chains(self, tb, *ds):
        """Run 1: contexttrack records message chains; argmatch instruments nothing. The testbed run's files go to tb,
        each module's (events, and the stack log of their chains) to its directory in ds"""
        self.testbed(tb).run(tb, {m.name: self.testbed_config(m) for m in self.mods})
        for m, d in zip(self.mods, ds):
            self.module_outputs(tb, m, d)
            run([sys.executable, f"{ARGMATCH_LABELS}/events_to_stacklog.py", f"{d}/events.jsonl"],
                log=f"{d}/stacklog.txt", timeout=300)

    def matches(self, tb, *ds):
        """Run 2: argmatch instruments the functions in run 1's chains and searches for the values; contexttrack
        records the messages to label. The testbed run's files go to tb, each module's (matches, events) to its
        directory in ds"""
        specs = {m.name: {**self.testbed_config(m), "stacklog": m.file("3-chains", "stacklog.txt"),
                          "matchfile": self.matchfile(m, d)} for m, d in zip(self.mods, ds)}
        self.testbed(tb).run(tb, specs)
        for m, d in zip(self.mods, ds):
            self.module_outputs(tb, m, d)
            if os.path.getsize(f"{d}/matches.calls.csv") == 0:
                raise RuntimeError(f"argmatch found no matches in {m.name}; see {tb}/{m.name}")

    def label_values(self, m, d):
        run([sys.executable, f"{ARGMATCH_LABELS}/join.py", m.file("5-matches", "matches.calls.csv"),
             m.file("5-matches", "events.jsonl"), d], log=f"{d}/summary.txt", timeout=900)
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
        # The module's other targets' needles: evidence for them, not this one (a needle no target has stays,
        # keyless). (Other modules' needles aren't in this module's matches: each module's argmatch searches for its
        # own.)
        others = {v for o in t.m.targets if o is not t for v in self.param_keys(o)} - value_keys.keys()

        # Full keys that could have been evidence: set by the concrete config, with a value step 5 searched for (in
        # scope or not: the confidences below leave out those --ignore does)
        ignore = stats.read_patterns(t.ignore)
        meta = stats.meta_keys(full, t.m.mod)  # (out of scope, like --ignore's)
        searched = set(open(t.file("4-configkeys", "param_values.txt")).read().splitlines())
        eligible = {fk for c in config.values() if any(v.replace("\n", "\\n") in searched for v in c["values"])
                    for fk in c["full_keys"]}

        # Evidence rows: each (message, needle) row of the join, with every concrete key whose value the needle is
        rows, no_key, no_full_key = [], set(), set()
        for r in csv.DictReader(open(t.m.file("6-label-values", "joined.csv"), newline="")):
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
        shutil.copy(t.m.file("1-unmarshaler-graph", "unmarshaler_subgraph.text"), d)  # visualize.sh reads it here
        run([sys.executable, f"{STATS}/stats.py", "--run", t.dir, "--out", d] + self.ignore(t), log=f"{d}/stats.log",
            timeout=900)
        run(["bash", f"{STATS}/visualize.sh", d, t.m.go], log=f"{d}/visualize.log", timeout=1200)
        say(f"tables in {t.file('8-stats', 'stats_tables.md')}")

    # ---------------------------------------------------------------------------------------------------------------

    # (configkeys comes after testbed run 1, which doesn't need it, so that runs up to run 1, like the concreteconfig
    # skill's label baselines, skip it)
    STEPS = ["1-unmarshaler-graph", "2-full-keys", "3-chains", "4-configkeys", "5-matches", "6-label-values",
             "7-label-keys", "8-stats"]
    # The steps each target runs; the others are each module's (layout.py), the testbed steps for all the run's
    # modules at once
    PER_TARGET = {"2-full-keys", "4-configkeys", "7-label-keys", "8-stats"}

    def units(self, step):
        """What running a step means: (arguments for its function, the directories it writes, a label), once per
        target for a step run per target; once for all modules for a testbed step, which writes the testbed run's
        directory and each module's; else once per module"""
        if step in self.PER_TARGET:
            return [((t,), [f"{t.dir}/{step}"], f"{step} ({t.m.name}/{t.name})")
                    for m in self.mods for t in m.targets]
        if step in layout.TESTBED_STEPS:
            return [((), [f"{self.out}/testbed/{step}"] + [f"{m.dir}/{step}" for m in self.mods], step)]
        return [((m,), [f"{m.dir}/{step}"], f"{step} ({m.name})") for m in self.mods]

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
                    t = args[0] if args and isinstance(args[0], Target) else None
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


def targets(a, mods):
    """The run's targets, in each module's targets: the roots of its ROOTS that --config names
    ([<module>:]<root>=<path>; the module is needed only if several of the run's modules have the root)"""
    def split(values, flag):
        named = {}
        for v in values:
            name, sep, path = v.partition("=")
            mname, _, root = name.rpartition(":")
            ms = [m for m in mods if root in m.mod.ROOTS and mname in ("", m.name, m.go)]
            if not sep or len(ms) != 1:
                roots = ", ".join(f"{m.name}:{r}" for m in mods for r in m.mod.ROOTS)
                sys.exit(f"{flag} {v}: give [<module>:]<root>=<path>, for a root of one of the run's modules "
                         f"({roots})" + (f"; several have root {root}: name the module" if len(ms) > 1 else ""))
            if (ms[0], root) in named:
                sys.exit(f"{flag}: root {ms[0].name}:{root} given twice")
            named[(ms[0], root)] = path
        return named

    configs, files = split(a.config, "--config"), split(a.config_files, "--config-files")
    if set(files) - set(configs):
        sys.exit("--config-files for roots with no --config: " +
                 ", ".join(sorted(f"{m.name}:{r}" for m, r in set(files) - set(configs))))
    for (m, name), config in configs.items():
        r = m.mod.ROOTS[name]
        m.targets.append(Target(m, name, r["root"], r["docs"], r.get("ignore", ""), r.get("transformed", ""), config,
                                files.get((m, name))))
    for m in mods:
        if not m.targets:
            sys.exit(f"module {m.name}: no --config for any of its roots ({', '.join(m.mod.ROOTS)})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="Directory for every step's output (layout.py)")
    ap.add_argument("--config", required=True, action="append", metavar="[MODULE:]ROOT=PATH",
                    help="Concrete config for a root of a module (its module.py's ROOTS), e.g. the /concreteconfig "
                         "skill's config.yaml (README.md, \"Use with /concreteconfig\"); once per root the run covers. "
                         "MODULE is needed only if several of the run's modules have the root")
    ap.add_argument("--config-files", action="append", default=[], metavar="[MODULE:]ROOT=PATH",
                    help="Directory of files a root's concrete config refers to, which the testbed installs (default: "
                         "a files/ directory next to its --config, if any)")
    # The modules, whose module.py (modules/<module>/) has their settings: checkout, config format, roots
    ap.add_argument("--module", required=True, action="append",
                    help="A module to instrument: its Go module path (as in go.mod) or its modules/ directory name; "
                         "repeatable (the testbed's other modules run stock); known: " + ", ".join(modules.names()))
    ap.add_argument("--vm", default="paramtrack-e2e", help="GCE VM the testbed (testbeds.py) runs on")
    ap.add_argument("--wait", type=int, default=240, help="Seconds to run the modules under the workload, per run")
    ap.add_argument("--redo", metavar="STEP", help="Rerun this step and the later ones, even if done (name or number)")
    ap.add_argument("--until", metavar="STEP", help="Stop after this step")
    ap.add_argument("--only", metavar="STEP", action="append", default=[],
                    help="Run only this step, rerunning it if done, from the earlier steps' outputs as they are; "
                         "repeatable. Removes the later steps' outputs, as they're out of date")
    a = ap.parse_args()
    out = os.path.abspath(a.out)
    mods = []
    for name in a.module:
        m = Module(modules.load(name), out)
        if any(o.name == m.name for o in mods):
            sys.exit(f"--module {name}: module {m.name} given twice")
        if m.name not in testbeds.COMPONENTS:
            sys.exit(f"--module {name}: the testbed doesn't run {m.name} (testbeds.py's COMPONENTS: "
                     f"{', '.join(testbeds.COMPONENTS)})")
        mods.append(m)
    targets(a, mods)
    Workflow(a, mods).run()


if __name__ == "__main__":
    main()
