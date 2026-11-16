# paramtrack

Runs a workflow that outputs a relationship between configuration parameters and messages for Go module(s) running in a testbed. Specifically, for each module the workflow observes messages the module sends and receives while running in the testbed, and determines which of the module's own configuration parameters are likely to influence them: For sends, the existence and contents of the message; for receives, the way the message is handled. A parameter is considered to have influence when it is likely to be accessible when the message is sent/received. Observed messages are grouped by their "label" (see "Output"), and parameters are identified by their keys.

## Usage
```
uv run --with pandas --with matplotlib --with tabulate python paramtrack.py \
  --out <dir> \
  --module <name of a directory in `modules/`> ... \
  --config <concrete config> ... \
  [--config-files <dir> ...] \
  [--vm paramtrack-e2e] [--wait 240] [--redo <step>] [--until <step>] [--only <step> ...]
```

Output is produced for each module passed to `--module`. These modules will be configured with the corresponding `--config`, and will be instrumented in the testbed. The other modules in the testbed run with the config prombench ships with, and are not instrumented.

### Concrete configs
For each module, pass the corresponding config:
- `config`: `[<module>:]<config_source>=<path>` (config_source: a key in `ROOTS` - see the [modules README](modules/README.md))
- `--config-files`: The files in this directory are any that the concrete config references (e.g., `password_file:/etc/prometheus/config/password_file.txt`).
paramtrack copies these into the testbed using module-/testbed-specific logic (see "Running the testbed" below).

### Other module-specific inputs
The workflow should generalize well to new Go modules.

To that end, paramtrack keeps all module-specific (including testbed-specific) inputs separate from its module-agnostic functionality, in [modules](modules/README.md) and `testbeds/`.

The module-specific optional arguments (--module-dir, --format) take their defaults from the module's `module.py`.


## Steps and intermediate outputs
All programs are built locally. In "Inputs" below, "concrete config" and "documented keys" are the ones paramtrack was run with (or a copy in the concrete config case), and the other filenames refer to outputs of previous steps.

The workflow produces the following layout:

```
bin, paramtrack-commands.log
testbed/
  3-chains, 5-matches: testbed info (testbed.json, logs)
<module>/
  1-unmarshaler-graph: the module's graph
  3-chains, 5-matches, 6-label-values: the module's outputs from the testbed run (events, matches, stack log)
  <config_source (prefix of --config)>/
    2-full-keys, 4-configkeys, 7-label-keys, 8-stats: output for the keys reachable from the config_source
```

| Step | Programs | Inputs | Main outputs |
|---|---|---|---|
| `1-unmarshaler-graph` | `extra_graph` (from `modules/`), then `gopls conftamer` (with the extragraph output) | Module source code | `unmarshaler_subgraph.text` |
| `2-full-keys` | `configkeys (without --config, --txt-dir, --strings-only)`;<br>then `stats.py --full-keys-only` (with the configkeys output) | `unmarshaler_subgraph.text` for both,<br>documented keys (for `stats.py`) | `full_keys.yaml` |
| `3-chains` | testbed run 1: `contexttrack`,<br>with `argmatch` instrumenting nothing;<br>then `events_to_stacklog.py` | concrete config | `events.jsonl`,<br>`stacklog.txt` |
| `4-configkeys` | `configkeys (with --config, --txt-dir, --strings-only)` | `unmarshaler_subgraph.text`,<br>concrete config | `configkeys.csv`,<br>`param_values.txt`,<br>`param_keys.txt` |
| `5-matches` | testbed run 2: `contexttrack`,<br>with argmatch instrumenting 3's chains and searching 4's values | concrete config,<br>`stacklog.txt`,<br>`param_values.txt` | `matches.csv`,<br>`matches.calls.csv`,<br>`events.jsonl` |
| `6-label-values` | `join.py` | `matches.calls.csv`,<br>`events.jsonl` (from matches, not chains) | `joined.csv` |
| `7-label-keys` | `paramtrack` | `configkeys.csv`,<br>`param_keys.txt`,<br>`param_values.txt`,<br>`joined.csv` | `label_keys.csv`,<br>`summary.txt` |
| `8-stats` | `stats.py`;<br>then `visualize.sh` | `unmarshaler_subgraph.text`,<br>`configkeys.csv`,<br>`param_values.txt`,<br>`matches.csv`,<br>`label_keys.csv`,<br>documented keys | see STATISTICS.md |

### Programs
The links below lead to the program's README if it has one.
- [gopls conftamer](../../../conftamer.go) (no README)
- [configkeys](../configkeys/README.md)
- [contexttrack](../../../../../../../conftamer/contexttrack/README.md)
- [argmatch](../../../../../../../dump_ancestors/argmatch/README.md)
- [events_to_stacklog.py](../../../../../../../dump_ancestors/argmatch-labels/README.md#bootstrapping)
- [join.py](../../../../../../../dump_ancestors/argmatch-labels/README.md)
- [paramtrack](paramtrack.py) (step 7 is `label_keys()`)
- [stats.py](../configkeys/stats/stats.py) (no README; STATISTICS.md describes its output)
- [visualize.sh](../configkeys/stats/visualize.sh) (no README; STATISTICS.md describes its output)

### Example
See [WORKFLOW_EXAMPLE.md](docs/WORKFLOW_EXAMPLE.md) for an example of each step's output.

### Running parts of the workflow
Steps can be named by number (`5`), name (`matches`) or both (`5-matches`).
- If a step's directory already exists for that module or (module, config_source), it will be skipped.
  - Specifically, per-module steps are skipped if they exist for that module, while per-(module, config_source) steps are skipped if they exist for that (module, config_source)
  - (For the two testbed run steps, they also need to exist under `testbed`)
  - If a step failed or was interrupted, it will not be skipped (since a step writes into `<step>.partial` and renames it when it's done)
- `--redo <step>` reruns that step and the later ones. Once any step runs, the later ones rerun too, since their inputs changed.
- `--until <step>` stops after a step, e.g. `--until 3` after testbed run 1 (step 4, which run 1 doesn't need, comes after it, so such runs skip it).
- `--only <step>` (repeatable) runs just the given steps, from the earlier steps' outputs as they are, e.g. `--only 2 --only 4` for configkeys on a new config without a testbed run.
- Step 2 copies paramtrack's `--config` and `--config-files` to the step subdirectory
  - This allows rerunning the workflow later even if the original file passed to `--config` is gone
  - All later steps of the workflow use the copy
  - If Step 2's directory already exists, it checks that it matches `--config` - if not, it exits with an error saying to use `--redo 2`

## Use with /concreteconfig

To run paramtrack to evaluate a concrete config output by the [`/concreteconfig`](../../../../../../.claude/skills/concreteconfig/SKILL.md) skill:

(below `uv run` is elided. The environment variables are illustrative, not built in to the workflow.)

#### 1. Run shipped config ("baseline run")
Determine which message labels the shipped config produces, so the skill can evaluate whether a candidate config loses any labels in the next step. Run once per testbed, instrumenting all modules of interest and passing their shipped configs, to produce a baseline that can be reused for every module.
```
paramtrack.py  \
  --out $OUT_SHIPPED \
  --module $ALL_PLANNED_MODULES \
  --config $CONFIG_SHIPPED \
  --until chains
```
#### 2. Generate new concrete config
Use the `/concreteconfig` skill to generate a concrete config that optimizes for relevant statistics, while producing the same message labels for its module as the shipped config.

First, write a skill prompt that points to the results of the baseline run above and to the other inputs listed in SKILL.md ("Inputs"); also indicate whether anyone will be available to answer questions.
Then, invoke the skill in a fresh session, either headlessly or interactively.

The headless run script starts a detached `claude -p` with an allowlist of the tools the skill needs, and writes
`agent.jsonl` (the transcript) and, when it's done, `agent.exit` to the output dir:
```
.claude/skills/concreteconfig/scripts/run_headless.sh $SKILL_PROMPT $OUT_SKILL
```
The skill will itself run paramtrack with `--until chains` (via a wrapper) to compare each candidate's message labels to the baseline. This run only instruments and modifies the config of the skill's own module (i.e., all other modules are not instrumented and run with their shipped configs).

It writes the final concrete config, and any files it references, to its output directory.

Before going on, check that the skill succeeded: the first line of `$OUT_SKILL/result.md` must be `passed` (see SKILL.md, "Outputs"). A headless run's `agent.exit` doesn't indicate success: it's 0 whenever the agent finishes, whatever the outcome.

#### 3. Run new concrete config
```
paramtrack.py \
  --out $OUT_CONCRETE \
  --testbed $TESTBED \
  --config $OUT_SKILL/config.yaml|json
```
This runs the full workflow, which duplicates the testbed run the skill did on the passing config - to reuse that, pass the output directory the skill used for `check_labels.py` instead of a new one.

## Output
The final output is `label_keys.csv`. It lists each message's keys, with a confidence for each. Message are identified by a *kind* and *label*, applied in [join.py](../../../../../../../dump_ancestors/argmatch-labels/README.md) using the logic in contexttrack's [message_graph.py](../../../../../../../conftamer/contexttrack/analysis/message_graph.py).

See [TERMINOLOGY](../configkeys/stats/TERMINOLOGY.md) for terminology e.g. "full key".

Two kinds of row:
- **Evidence rows:** one per (message, needle, concrete key, full key, section), for the keys whose values argmatch matched, in calls that join with the message. These keys are *evidence* for the message. (Includes both ambiguous and unambiguous matches.)
- **Inferred rows:** for each section with evidence for a message, one per other full key in the section. The key wasn't matched for the message, but its section was.

Columns:
- `message_kind`, `message_label`, `needle`: a row of `joined.csv` (`needle` is empty in inferred rows)
- `concrete_key`: a key at which the needle is a value in the concrete config (from `param_keys.txt`). A needle that stands for several concrete keys gets a row for each. Empty in inferred rows.
- `full_key`: the concrete key's full key (empty if it has none), or the inferred key
- `section`: the full key's section; a row per section, if it has several
- `section_keys`: the full keys in that section (joined with `; `)
- `evidence`: whether the key is evidence for the message
- `eligible`: whether the key could have been evidence for any message - see TERMINOLOGY.md
- `confidence_all`, `confidence_eligible`: 1 for an evidence row. For an inferred row, the share of its section's in-scope keys that are evidence for the message: out of all of them (`confidence_all`), and out of its eligible keys (`confidence_eligible`).

`summary.txt` counts rows (evidence and inferred), messages, full keys and sections, and lists needles with no concrete key and concrete keys with no full key.

## Running the testbed
The testbed runs on the GCE VM `--vm` (default `paramtrack-e2e`). Paramtrack orchestrates it over ssh as follows (all part of `paramtrack.py`):
- Copy the inputs to the VM
- Deploy the cluster ([testbeds/prombench/upstream.py](testbeds/prombench/upstream.py), about 5 minutes) or reuse the one already running on the VM (the workload attempts to force a reused cluster to produce the same messages, e.g. by expiring certs)
- For each `--module` (not necessarily in this order), using the info in TESTBED.md
  - Build the instrumented image and load it into the cluster
  - Install the `--config` and `--config-files`
  - Restart the pod
- Run the workload
- Wait for `--wait`
- Copy the outputs from the VM

For each module, the argmatch build args default to `ARGMATCH_ARGS` in `testbeds.py`. Modules can override them in module.py.

Each testbed step (`3-chains`, `5-matches`) records its effective settings in `testbed.json`: the arguments to paramtrack and argmatch's build args, defaults filled in. Step 2 records the per-module settings in `2-full-keys/settings.json`: a snapshot of the settings in module.py; the module version from the checkout's git log; and the filename passed to `--config`. It does not record the contents of files, however.
The concreteconfig skill reads the per-module settings, so it evaluates candidates using the same settings as the baseline, and `stats.py` reads them to get the module info.

To set up a fresh VM, see [testbeds/prombench/vm_setup.sh](testbeds/prombench/vm_setup.sh). The checkouts are built as they are on the VM, so push the changes you want, or copy them there.


## Adding modules and testbeds
To add a module, first run the `/documentedkeys` skill to investigate the module's config.

### Reproducibility
The end-to-end workflow should be relatively reproducible, with exceptions due to e.g. timing-dependent labels, and the behavior of the agents running the skills. To that end:
- When adding a module/testbed, either commit the corresponding files, or make the process to create them reproducible with committed inputs:
  - The module-specific files referenced in module.py (e.g. the ignore list)
  - Inputs passed to skill prompts
- Once we have results for the paper, we should:
  - Commit the corresponding workflow settings files (see "Testbeds"), `--config`, and `--config-files`

### Baseline behavior
Keep in mind that when choosing concrete config values, the skill is only constrained by SKILL.md ("Constraints"). The intent of these constraints is that the modified config produces behavior that is a superset of the shipped config, so that it does not artifically reduce test coverage. In terms of the end-to-end workflow, "coverage" is concretely (1) the set of message labels the module sends/receives, and (2) the data accessible at that time.

So, strictly speaking we should verify the skill's config produces matches that are a superset of the shipped config's. However, to make the workflow cheaper (i.e. less likely to involve a long loop of checking configs), for the testbed behavior constraint we only require that (1) the config loses no message labels and (2) the testbed run does not exit 2.

Thus, when adding modules/testbeds, try to make these requirements good proxies for coverage, i.e.: (1) the baseline run should produce all message labels of interest (e.g. in Prometheus, alerting and dashboards), and (2) the check script should verify anything about the testbed behavior likely to impact the message-related data (e.g. in Prometheus, that the alerting and dashboards work).

Note that the label check in the skill run (described in "Use with concreteconfig" above) does not guarantee that the skill's config is "valid", for several reasons:
  - The baseline does not produce some behaviors such as Caddy's certificate renewal, so if the skill's config causes the module to misbehave with regard to these, comparing against the baseline doesn't tell us that.
  - The skill only checks the message labels of its *own* module against the baseline, so it's possible it produces a concrete config that maintains its own module's message labels but messes up other modules. It also runs its new config alongside the other modules' shipped configs, so it's possible the new config could mess up behavior in other modules that only shows up with their expanded configs.
    - A more strict baseline would be: When running the skill, run all modules onboarded thus far with their skill's config and instrument them. Compare all instrumented modules' labels against the baseline. Once the skill finishes, treat that run as the new baseline. This would be a more thorough check that each expanded config is "compatible" with other modules' expanded configs.

## Command logs
paramtrack logs the commands it runs, each line with the time, the working directory and the command, shell-quoted:
- `<out>/paramtrack-commands.log`: paramtrack's own command line, once per invocation.
- `<step>/commands.log`: every command the step runs (builds, programs, and for the prombench-* testbeds the `gcloud compute ssh` calls that copy inputs, start the testbed and copy outputs back). The paths in it are `<step>.partial`, where the step writes until it's done.
- `<step>/vm-commands.log`, for the prombench-* testbeds' steps: the commands the testbed runs on the VM (the build, loading the image, restarting prometheus-meta, the workload, the capture), copied back with its other outputs.

Commands that only check on progress (polling the VM, prometheus-meta's readiness, the docker container's state) aren't logged.
