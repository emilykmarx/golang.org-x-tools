#!/usr/bin/env python3
"""Runs the end-to-end param tracking workflow (WORKFLOW.md) and joins its outputs into Message label => Full keys.
See README.md.

Run with: uv run --with pandas --with matplotlib --with tabulate python paramtrack.py --out DIR --config CONFIG [flags]
"""

import argparse
import collections
import csv
import os
import shutil
import sys

from common import ARGMATCH_LABELS, CT, GOPLS, QUERYGRAPH, run, say
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

    def dir(self, step):
        return f"{self.out}/{step}"

    # ---------------------------------------------------------------------------------------------------------------
    # Static phase

    def unmarshaler_graph(self, d):
        gopls = f"{self.out}/bin/gopls"
        run(["go", "build", "-o", gopls, "."], cwd=GOPLS, log=f"{d}/build.log", timeout=600)
        run([gopls, "conftamer", f"-m={self.a.module}", f"-format={self.a.format}", f"-out={d}"],
            cwd=self.a.module_dir, log=f"{d}/conftamer.log", timeout=900)
        if not os.path.exists(f"{d}/unmarshaler_subgraph.text"):
            raise RuntimeError(f"gopls conftamer wrote no {d}/unmarshaler_subgraph.text; see {d}/conftamer.log")

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

    def step_files(self):
        """Step 2's copy of the config's files, if any"""
        d = self.file("2-concrete-config", "files")
        return d if os.path.isdir(d) else None

    def concrete_config(self, d):
        """The full keys YAML (every full key the graph allows: configkeys without a config, then stats.py's SD
        resolution), and the concrete config"""
        graph = self.file("1-unmarshaler-graph", "unmarshaler_subgraph.text")
        cmd = [self.configkeys_bin(), "--unmarshaler-subgraph", graph, "--module-prefix", self.a.module, "--format",
               self.a.format, "--output", f"{d}/configkeys_graph.csv"]
        if self.a.root:
            cmd += ["--root", self.a.root]
        run(cmd, log=f"{d}/configkeys.log", timeout=600)
        run([sys.executable, f"{STATS}/stats.py", "--full-keys-only", "--configkeys", f"{d}/configkeys_graph.csv",
             "--graph", graph, "--docs", self.a.docs, "--prometheus", self.a.module_dir, "--out", d],
            log=f"{d}/stats.log", timeout=600)
        # TODO: generate it from full_keys.yaml with the /concreteconfig skill (WORKFLOW.md); for now, the input
        shutil.copy(self.a.config, f"{d}/config.yaml")
        if self.config_files():
            shutil.copytree(self.config_files(), f"{d}/files")

    def configkeys(self, d):
        graph = self.file("1-unmarshaler-graph", "unmarshaler_subgraph.text")
        cmd = [self.configkeys_bin(), "--config", self.file("2-concrete-config", "config.yaml"), "--txt-dir", d,
               "--strings-only", "--unmarshaler-subgraph", graph, "--module-prefix", self.a.module,
               "--format", self.a.format, "--output", f"{d}/configkeys.csv"]
        if self.a.root:
            cmd += ["--root", self.a.root]
        run(cmd, log=f"{d}/configkeys.log", timeout=600)

    # ---------------------------------------------------------------------------------------------------------------
    # Dynamic phase

    def testbed(self):
        overrides = dict(kv.split("=", 1) for kv in self.a.argmatch_arg)
        return TESTBEDS[self.a.testbed](wait=self.a.wait, argmatch_args=overrides, vm=self.a.vm)

    def chains(self, d):
        """Run 1: contexttrack records message chains; argmatch instruments nothing"""
        self.testbed().run(d, self.file("2-concrete-config", "config.yaml"), files=self.step_files())
        run([sys.executable, f"{ARGMATCH_LABELS}/events_to_stacklog.py", f"{d}/events.jsonl"],
            log=f"{d}/stacklog.txt", timeout=300)

    def matches(self, d):
        """Run 2: argmatch instruments the functions in run 1's chains and searches for the values; contexttrack
        records the messages to label"""
        self.testbed().run(d, self.file("2-concrete-config", "config.yaml"), files=self.step_files(),
                           stacklog=self.file("4-chains", "stacklog.txt"),
                           matchfile=self.file("3-configkeys", "param_values.txt"))
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
        graph = self.file("1-unmarshaler-graph", "unmarshaler_subgraph.text")
        stats.MAP_FIELDS.update(stats.map_field_keys(graph))
        sd = stats.sd_names(self.a.module_dir)
        full, config, _ = stats.load_configkeys(self.file("3-configkeys", "configkeys.csv"), sd)

        sections = {fk: sorted({stats.section(fk, p) for p in info["paths"]}) for fk, info in full.items()}
        section_keys = collections.defaultdict(set)
        for fk, secs in sections.items():
            for s in secs:
                section_keys[s].add(fk)

        # Needle (a value as written in param_values.txt) => concrete keys, from param_keys.txt
        value_keys = {}
        for line in open(self.file("3-configkeys", "param_keys.txt")).read().splitlines():
            value, _, keys = line.rpartition(":")
            value_keys[value] = keys.split(",")

        # Full keys that could have been evidence: set by the concrete config, with a value step 5 searched for
        searched = set(open(self.file("3-configkeys", "param_values.txt")).read().splitlines())
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
                        rows.append(base + [k, fk, sec, "; ".join(sorted(section_keys[sec])), True, True, 1, 1])

        # Inferred rows: every other full key in a section with evidence for the message, with the share of the
        # section's keys that are evidence for it, out of all its keys and out of those that could have been
        evidence = collections.defaultdict(set)  # (kind, label, section) => its full keys that are evidence
        for r in rows:
            if r[5]:
                evidence[(r[0], r[1], r[5])].add(r[4])
        for (kind, label, sec), ev in sorted(evidence.items()):
            keys = section_keys[sec]
            conf_all = round(len(ev) / len(keys), 3)
            conf_eligible = round(len(ev) / len(keys & eligible), 3)
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
        """stats.py's statistics and visuals (../configkeys/stats/STATISTICS.md) on this run's outputs, then
        visualize.sh's coloured graphs"""
        # stats.py only uses the needles of its argmatch input. matches.csv is too big for pandas, and its rows have
        # more fields than its header, so write its distinct needles. (In a directory named after step 5, which
        # stats.py prints as the argmatch run's name.)
        matched = set()
        with open(self.file("5-matches", "matches.csv"), newline="") as f:
            r = csv.reader(f)
            ni = next(r).index("needle")
            for row in r:
                if len(row) > ni:
                    matched.add(row[ni])
        os.makedirs(f"{d}/5-matches")
        with open(f"{d}/5-matches/matched_needles.csv", "w", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["needle"])
            w.writerows([n] for n in sorted(matched))

        graph = self.file("1-unmarshaler-graph", "unmarshaler_subgraph.text")
        shutil.copy(graph, d)  # visualize.sh reads it from its results directory
        run([sys.executable, f"{STATS}/stats.py", "--configkeys", self.file("3-configkeys", "configkeys.csv"),
             "--param-values", self.file("3-configkeys", "param_values.txt"), "--graph", graph, "--docs", self.a.docs,
             "--prometheus", self.a.module_dir,
             "--argmatch", f"{d}/5-matches/matched_needles.csv",
             "--label-keys", self.file("7-label-keys", "label_keys.csv"), "--out", d], log=f"{d}/stats.log", timeout=900)
        run(["bash", f"{STATS}/visualize.sh", d], log=f"{d}/visualize.log", timeout=1200)
        say(f"{len(matched)} distinct needles matched; tables in {self.dir('8-stats')}/stats_tables.md")

    # ---------------------------------------------------------------------------------------------------------------

    STEPS = ["1-unmarshaler-graph", "2-concrete-config", "3-configkeys", "4-chains", "5-matches", "6-label-values",
             "7-label-keys", "8-stats"]

    def file(self, step, name):
        return f"{self.dir(step)}/{name}"

    def run(self):
        os.makedirs(self.out, exist_ok=True)
        fns = [self.unmarshaler_graph, self.concrete_config, self.configkeys, self.chains, self.matches,
               self.label_values, self.label_keys, self.stats]
        redo = self.step_index(self.a.redo) if self.a.redo else len(self.STEPS)
        until = self.step_index(self.a.until) if self.a.until else len(self.STEPS) - 1
        ran = False
        for i, (step, fn) in enumerate(zip(self.STEPS, fns)):
            if i > until:
                break
            d = self.dir(step)
            # Once a step has run, the later steps' outputs are out of date
            if (i >= redo or ran) and os.path.exists(d):
                shutil.rmtree(d)
            if os.path.exists(d):
                say(f"{step}: done already ({d})")
                continue
            # Write into a partial directory, so that a step that fails or is interrupted isn't taken as done
            partial = d + ".partial"
            shutil.rmtree(partial, ignore_errors=True)
            os.makedirs(partial)
            say(f"{step}: running")
            fn(partial)
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
    ap.add_argument("--config", required=True, help="Concrete config (until /concreteconfig generates it)")
    ap.add_argument("--config-files", help="Directory of files the concrete config refers to, which the testbed "
                                           "installs (default: a files/ directory next to --config, if any)")
    ap.add_argument("--module", default="github.com/prometheus/prometheus", help="Module as in go.mod")
    ap.add_argument("--module-dir", default=f"{CT}/prometheus", help="Module source checkout")
    ap.add_argument("--root", default="/config.Config", help="configkeys --root (empty: all roots)")
    ap.add_argument("--format", default="yaml", help="Config format (yaml; json not yet supported by configkeys)")
    ap.add_argument("--docs", default=f"{CT}/prom-test-infra/prombench/prometheus_config_keys.txt",
                    help="Documented keys, for steps 2 and 8 (stats.py --docs)")
    ap.add_argument("--testbed", default="docker", choices=sorted(TESTBEDS))
    ap.add_argument("--vm", default="paramtrack-e2e", help="GCE VM for --testbed prombench")
    ap.add_argument("--wait", type=int, default=240, help="Seconds to run the module under the workload, per run")
    ap.add_argument("--argmatch-arg", action="append", default=[], metavar="KEY=VALUE",
                    help="Override an argmatch build arg (ARGMATCH_KEY), e.g. DEPTH=5; repeatable")
    ap.add_argument("--redo", metavar="STEP", help="Rerun this step and the later ones, even if done (name or number)")
    ap.add_argument("--until", metavar="STEP", help="Stop after this step")
    Workflow(ap.parse_args()).run()


if __name__ == "__main__":
    main()
