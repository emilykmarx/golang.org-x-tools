---
name: concreteconfig
description: Generate the concrete config for paramtrack's end-to-end param tracking workflow (run before paramtrack's workflow; see paramtrack's README) - a config for the target module that sets as many of its searchable keys as possible, each with its own distinctive value, without losing any message the testbed's shipped config produces. Use when asked to write, improve or regenerate the concrete config for a module and testbed (e.g. Prometheus on prombench).
---

# concreteconfig

You are a developer of the target module, writing a config that exercises as much of the module's configuration as
possible, for the param tracking workflow that paramtrack runs
(`gopls/internal/cmd/conftamer/querygraph/paramtrack/README.md`: read it first; "Use with /concreteconfig" there says
where this skill fits in). The workflow later runs the module with your config, searches its memory for your config's
values (argmatch), and maps the messages it sends to the keys whose values it found. So every key you set, with a value
the search can find and attribute to that key alone, is a key the workflow can learn about.

Use what a developer of the module would know: its docs, example and test configs, and the code that parses and
validates the config. The module's file (below) points to these.

## Per-module and per-testbed files
Read both before starting. They hold everything specific to one module or testbed, so the steps below stay generic:
- `modules/<module>/MODULE.md`: where the developer knowledge is, how to check a config, how to reload it, keys to
  skip and why, values known to be safe, and the module's ignore file (keys out of scope, e.g. Prometheus' unused
  service discovery configs; paramtrack's `--ignore`).
- `testbeds/<testbed>/TESTBED.md`: how to apply a config, add files, check the module and the rest of the testbed
  are healthy, and collect message labels.

There are files for: Prometheus (`modules/prometheus/`) on prombench (`testbeds/prombench/`). For a new module or
testbed, write its file first, following these.

## Inputs
- The module source checkout, the testbed's shipped concrete config (TESTBED.md says where), and a running testbed.
- The baseline run: paramtrack with the shipped config, through step 3 (TESTBED.md, "Message labels"; reuse one
  you're given; its `--ignore` must be the module's ignore file, paramtrack's default for Prometheus). Its step
  directories have the Unmarshaler graph (`1-unmarshaler-graph/unmarshaler_subgraph.text`), the in-scope full keys
  YAML (`2-full-keys/full_keys.yaml`), and the message label baseline (`3-chains/events.jsonl`). In the full
  keys YAML, each key's comment gives whether it's documented and its value kind; `"*"` is a map key you choose.

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
Apply these in this order whenever two choices conflict:
1. The constraints.
2. **Coverage of the in-scope keys** (Q1 in configkeys/stats/STATISTICS.md): more sections touched, then more of
   each touched section set, then more full keys and Unmarshaler types. Only keys whose values the search can find
   count: string or struct kinds (`--strings-only`), and not values stored transformed (MODULE.md lists them, e.g.
   Prometheus' regexes, URLs and PEM blocks). Set those too where the module needs them, but don't add them for
   coverage.
3. **Distinctive values** (Q3): no value shared by two concrete keys, and no value that's also a string literal in
   the module's code. That includes the keys the shipped config already sets: change their values too, where
   constraint 2 still holds. Enum-like keys can't be distinctive; prefer a non-default value only if it changes
   nothing observable (constraint 2).

Measure 2 and 3 with `scripts/score.py` (`stats.py --config-only --ignore`); its output is the scorecard.

## Values
Use `scripts/value.py <concrete key>` for every free-form value (names, label values, header values, credentials,
paths of added files, hosts): it derives the value from the key, so it's distinctive, almost never a code literal,
and the same for the same key. `--shape` fits it to the field (`label`, `path`, `host`); `--index` distinguishes list
entries. Deviate only where the field's format rules it out, and say why in `diff.md`. Map keys you
choose (`"*"`) get the same treatment.

For the contents of files the config refers to, use `scripts/files.py` (TLS certs and keys, private keys, IDs that
must be UUIDs), seeded with the key's `value.py` token so each file is distinct and the same for the same key, and
the module file's helpers for module-specific files (MODULE.md).

Keys that are mutually exclusive (e.g. one auth method per HTTP client) can't all be set in one block. Where the
config has list sections (scrape jobs, remote writes, ...), add an entry per alternative, as long as the new
entries take nothing away (constraint 2).

## Steps
1. Read paramtrack's README, MODULE.md and TESTBED.md. Get the inputs above.
2. Score the shipped config (`scripts/score.py`).
3. Start from a copy of the shipped config. Give its free-form values distinctive ones (objective 3). Then, for each
   searchable key not yet set, decide, using the developer knowledge: set it, with what value, or skip it, and why
   (MODULE.md lists keys to skip). `scripts/score.py` lists the keys not yet set: the rows of its
   `config_full_keys.csv` with `in_config` False and `searchable` True. The in-scope full keys YAML shows the same
   keys in the config's shape.
4. Check often and cheaply: the module's check command, then a reload in the testbed and the health checks
   (TESTBED.md). Keep the last config that passed, and go back to it when a candidate fails.
5. When no more keys can be set, run the full checks: a restart, and a run long enough to compare message labels
   with the baseline (constraint 2; `scripts/check_labels.py`). Fix and repeat until both pass.
6. Score the final config, and write the outputs.

## Outputs
In the output directory you're given (or ask for one):
- `config.yaml`: the concrete config, and `files/`: the files it refers to, as added to the testbed
- `commands.md`: the commands and code you used to apply, check and measure it, in order
- `stats.md`: `scripts/score.py`'s output for the shipped config and for yours, side by side, and the message label
  comparison (`scripts/count_labels.py`)
- `diff.md`: the diff from the shipped config, with a line per change saying why, including each key skipped and why

paramtrack then runs the workflow with it: `paramtrack.py --config <output>/config.yaml` (its step 2 copies it, and
the `files/` next to it, which its testbeds install).

## Running headless
To run this skill unattended (e.g. so it survives the session closing), use `scripts/run_headless.sh <prompt file>
<output dir>`. It starts a detached `claude -p` with an allowlist of the tools the skill needs, and writes
`agent.jsonl` (the transcript) and, when it's done, `agent.exit` to the output dir. The prompt should invoke
`/concreteconfig`, give the inputs and the output dir, and say no one can answer questions. In a headless run:
- write logs and files with the Write tool rather than shell redirects (`>`, `| tee`), which need approval;
- write absolute paths in commands rather than shell variables (`S=...; ls $S` needs approval, since the variable
  can't be checked before the command runs);
- use `cp` without flags (`cp` with flags needs approval), and `cd` only within the allowed directories (the repo,
  `/home/emily/projects/config_tracing`, `/tmp` and the Go module cache);
- files under `.claude/` can't be written.
