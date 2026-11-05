---
name: concreteconfig
description: Generate the concrete config for paramtrack's end-to-end param tracking workflow (step 2 of paramtrack's WORKFLOW.md) - a config for the target module that sets as many of its searchable keys as possible, each with its own distinctive value, without losing any message the testbed's shipped config produces. Use when asked to write, improve or regenerate the concrete config for a module and testbed (e.g. Prometheus on prombench).
---

# concreteconfig

You are a developer of the target module, writing a config that exercises as much of the module's configuration as
possible, for the param tracking workflow in
`gopls/internal/cmd/conftamer/querygraph/paramtrack/WORKFLOW.md` (read it first; this skill is its step 2). The
workflow later runs the module with your config, searches its memory for your config's values (argmatch), and maps
the messages it sends to the keys whose values it found. So every key you set, with a value the search can find and
attribute to that key alone, is a key the workflow can learn about.

Use what a developer of the module would know: its docs, example and test configs, and the code that parses and
validates the config. The module's file (below) points to these.

## Per-module and per-testbed files
Read both before starting. They hold everything specific to one module or testbed, so the steps below stay generic:
- `modules/<module>/MODULE.md`: where the developer knowledge is, how to check a config, how to reload it, keys to
  skip and why, values known to be safe, and the module's ignore file (`modules/<module>/ignore.txt`: keys out of
  scope, e.g. Prometheus' unused service discovery configs).
- `testbeds/<testbed>/TESTBED.md`: how to apply a config, add files, check the module and the rest of the testbed
  are healthy, and collect message labels.

There are files for: Prometheus (`modules/prometheus/`) on prombench (`testbeds/prombench/`). For a new module or
testbed, write its file first, following these.

## Inputs
- The module source checkout, and paramtrack's static phase for it: run paramtrack with `--until 1` for the
  Unmarshaler graph (`1-unmarshaler-graph/unmarshaler_subgraph.text`).
- The in-scope full keys YAML: `stats.py --full-keys-only --ignore modules/<module>/ignore.txt` (configkeys/stats/;
  needs configkeys' output without a config, as paramtrack's step 2 runs it). Each key's comment gives whether it's
  documented and its value kind; `"*"` is a map key you choose.
- The testbed's shipped concrete config (TESTBED.md says where), and a running testbed.
- The state earlier runs left (see "Consistency"): `decisions.md` and a generator, in the state directory
  `gopls/internal/cmd/conftamer/querygraph/paramtrack/concreteconfig/<module>-<testbed>/`.

## Constraints
1. **The config loads**, both on reload and on a full restart (some settings only apply at startup), and passes the
   module's own check command.
2. **No message label is lost.** Run with the new config, the testbed must produce every distinct message label
   (as `scripts/count_labels.py` counts them) that the shipped config produces. New labels are fine. That is: you may
   add behaviors, never take any away. Check it with a run as long as the baseline's, against a baseline from the
   same kind of run (TESTBED.md).
3. **You change nothing in the testbed but the module's config,** plus files the config refers to (TESTBED.md,
   "Adding files") and monitoring tools you install. Nothing else in the testbed may crash or stop working.

## Objective, in priority order
Apply these in this order whenever two choices conflict, so that every run trades off the same way:
1. The constraints.
2. **Coverage of the in-scope keys** (Q1 in configkeys/stats/STATISTICS.md): more sections touched, then more of
   each touched section set, then more full keys and Unmarshaler types. Only keys whose values the search can find
   count: string or struct kinds (`--strings-only`), and not values stored transformed (MODULE.md lists them, e.g.
   Prometheus' regexes, URLs and PEM blocks). Set those too where the module needs them, but don't add them for
   coverage.
3. **Distinctive values** (Q3): no value shared by two concrete keys, and no value that's also a string literal in
   the module's code. Enum-like keys can't be distinctive; prefer a non-default value only if it changes nothing
   observable (constraint 2).
4. **The smallest diff from the shipped config.** Keep its keys and values unless a goal above needs a change.

Measure 2 and 3 with `scripts/score.py` (`stats.py --config-only --ignore`); its output is the scorecard.

## Values
Use `scripts/value.py <concrete key>` for every free-form value (names, label values, header values, credentials,
paths of added files, hosts): it derives the value from the key, so it's distinctive, almost never a code literal,
and the same in every run. `--shape` fits it to the field (`label`, `path`, `host`); `--index` distinguishes list
entries. Deviate only where the field's format rules it out, and record why in the decisions file. Map keys you
choose (`"*"`) get the same treatment.

Keys that are mutually exclusive (e.g. one auth method per HTTP client) can't all be set in one block. Where the
config has list sections (scrape jobs, remote writes, ...), add an entry per alternative, as long as the new
entries take nothing away (constraint 2).

## Steps
1. Read WORKFLOW.md, MODULE.md, TESTBED.md and the decisions file. Get the inputs above. If there's a generator,
   run it first: its output is the previous run's config, and your starting point instead of the shipped config.
2. Score the shipped config (`scripts/score.py`), and record a baseline of its message labels (TESTBED.md).
3. Start from the generator's output (step 1), or else a copy of the shipped config. Walk the in-scope full keys
   YAML in order, top to bottom. For each searchable key not yet set: if the decisions file covers it, apply that
   decision; otherwise decide, using the developer knowledge, and record the decision (set, with what value and why;
   or skipped, and why). Make the changes in the generator (see "Consistency").
4. Check often and cheaply: the module's check command, then a reload in the testbed and the health checks
   (TESTBED.md). Keep the last config that passed, and go back to it when a candidate fails.
5. When no more keys can be set, run the full checks: a restart, and a run long enough to compare message labels
   with the baseline (constraint 2). Fix and repeat until both pass.
6. Score the final config, and write the outputs.

## Consistency
Runs for the same module and testbed should produce configs as close to each other as possible:
- Start from the generator's output if there is one, else the shipped config, and extend it (step 3), walking the
  keys in the YAML's order.
- Take values from `scripts/value.py`.
- **The state directory** (under `paramtrack/concreteconfig/`; see "Inputs") is the memory between runs. It lives
  outside `.claude/` so that headless runs can write it. Keep two things there:
  - **A generator,** e.g. `gen.py`: a script that writes the config and its files from the shipped config. Make
    your changes in it, rather than by hand-editing a config, so that rerunning it reproduces your config exactly.
    Derive anything it would otherwise generate randomly (keys, UUIDs) from a seed, so reruns match without
    keeping generated files, and never commit a key (e.g. `gen.py`'s `deterministic_rsa_key`).
  - **`decisions.md`:** one row per key decided, with the decision, the value or its rule, and the reason
    (developer knowledge, a check that failed, a label that went missing). Read it first and follow it; change a
    decision only for a reason you record, and change the generator to match. Add every new decision.
- Follow the objective's order.

## Outputs
In the output directory you're given (or ask for one):
- `config.yaml`: the concrete config, and `files/`: the files it refers to, as added to the testbed
- `commands.md`: the commands and code you used to apply, check and measure it, in order
- `stats.md`: `scripts/score.py`'s output for the shipped config and for yours, side by side, and the message label
  comparison (`scripts/count_labels.py`)
- `diff.md`: the diff from the shipped config, with a line per change saying why
- the updated state directory: `decisions.md` and the generator

paramtrack then runs the workflow with it: `paramtrack.py --config <output>/config.yaml` (its step 2 copies it, and
the `files/` next to it, which its testbeds install).

## Running headless
To run this skill unattended (e.g. so it survives the session closing), use `scripts/run_headless.sh <prompt file>
<output dir>`. It starts a detached `claude -p` with an allowlist of the tools the skill needs, and writes
`agent.jsonl` (the transcript) and, when it's done, `agent.exit` to the output dir. The prompt should invoke
`/concreteconfig`, give the inputs and the output dir, and say no one can answer questions. In a headless run:
- write logs and files with the Write tool rather than shell redirects (`>`, `| tee`), which need approval;
- use `cp` without flags (`cp` with flags needs approval), and `cd` only within the allowed directories (the repo,
  `/home/emily/projects/config_tracing`, `/tmp` and the Go module cache);
- files under `.claude/` can't be written, which is why the state directory is elsewhere.
