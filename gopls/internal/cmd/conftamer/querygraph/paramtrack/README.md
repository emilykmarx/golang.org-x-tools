# paramtrack

Runs a workflow that outputs a relationship between configuration parameters and messages for a Go module running in a testbed. Specifically, the workflow observes messages the module sends and receives while running in the testbed, and determines which of the module's own configuration parameters are likely to influence them: For sends, the existence and contents of the message; for receives, the way the message is handled. A parameter is considered to have influence when it is likely to be accessible when the message is sent/received. Observed messages are grouped by their "label" (see "Output"), and parameters are identified by their keys.

As an example, we will use Prometheus as the module, YAML as the config format, and Prombench as the testbed.
But the workflow must generalize to other modules (including Grafana and the Kubernetes API server), JSON as the config format, and other testbeds.


## Usage
```
uv run --with pandas --with matplotlib --with tabulate python paramtrack.py --out <dir> --config <concrete config> \
  [--config-files <dir>] \
  [--module github.com/prometheus/prometheus] [--module-dir <checkout>] [--root /config.Config] [--format yaml] \
  [--docs <documented keys>] [--ignore <full keys to ignore>] [--testbed docker|prombench] [--vm paramtrack-e2e] [--wait 240] [--argmatch-arg KEY=VALUE ...] [--redo <step>] [--until <step>] [--only <step> ...]
```
The defaults are for Prometheus.

## Input files
In addition to the concrete config, paramtrack takes two module-/testbed-specific files, described below.

### `--config-files`
The files in this directory are any that the concrete config references (e.g., `password_file:/etc/prometheus/config/password_file.txt`).

paramtrack copies these into the testbed using module-/testbed-specific logic.

### `--ignore`
The keys listed in the `--ignore` file will be ignored when the `/concreteconfig` skill evaluates candidate concrete configs, and in the statistics (with one exception in the statistics - see STATISTICS.md). This file is generated for each module on an ad hoc basis; e.g. in Prometheus, we choose to ignore many of the service discovery keys - see [ignore/prometheus.txt](ignore/prometheus.txt).

However, the concrete config may still set an ignored key (e.g. if the skill finds that it must in order for the config to be valid). In this case, `argmatch` still searches for the corresponding value, and the final step before the statistics still outputs those matches.


### `--docs`
A list of keys generated from the module's documentation using the `/documentedkeys` skill.

## Steps and intermediate outputs
Each step writes one subdirectory of `--out`, named after the step. All programs are built locally. In "Inputs" below, "concrete config" and "documented keys" are the ones paramtrack was run with (or a copy in the concrete config case), and the other filenames refer to outputs of previous steps.

| Step | Programs | Inputs | Main outputs |
|---|---|---|---|
| `1-unmarshaler-graph` | `gopls conftamer` | Module source code | `unmarshaler_subgraph.text` |
| `2-full-keys` | `configkeys (without --config, --txt-dir, --strings-only)`;<br>then `stats.py --full-keys-only (with the configkeys output)` | `unmarshaler_subgraph.text` for both,<br>documented keys (for `stats.py`) | `full_keys.yaml` |
| `3-chains` | testbed run 1: `contexttrack`,<br>with `argmatch` instrumenting nothing;<br>then `events_to_stacklog.py` | concrete config | `events.jsonl`,<br>`stacklog.txt` |
| `4-configkeys` | `configkeys (with --config, --txt-dir, --strings-only)` | `unmarshaler_subgraph.text`,<br>concrete config | `configkeys.csv`,<br>`param_values.txt`,<br>`param_keys.txt` |
| `5-matches` | testbed run 2: `contexttrack`,<br>with argmatch instrumenting 3's chains and searching 4's values | concrete config,<br>`stacklog.txt`,<br>`param_values.txt` | `matches.csv`,<br>`matches.calls.csv`,<br>`events.jsonl` |
| `6-label-values` | `join.py` | `matches.calls.csv`,<br>`events.jsonl` (from matches, not chains) | `joined.csv` |
| `7-label-keys` | `paramtrack` | `unmarshaler_subgraph.text`,<br>`configkeys.csv`,<br>`param_keys.txt`,<br>`param_values.txt`,<br>`joined.csv` | `label_keys.csv`,<br>`summary.txt` |
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
- If a step's directory already exists, it will be skipped.
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
Determine which message labels the shipped config produces, so the skill can evaluate whether a candidate config loses any labels in the next step. Run once per (module, testbed) pair.
```
paramtrack.py  \
  --out $OUT_SHIPPED \
  --testbed $TESTBED \
  --config $CONFIG_SHIPPED \
  --until chains
```
#### 2. Generate new concrete config
Use the `/concreteconfig` skill to generate a concrete config that optimizes for relevant statistics, while producing the same message labels as the shipped config.

First, write a skill prompt that points to the results of the baseline run above and to the other inputs listed in SKILL.md ("Inputs"); also indicate whether anyone will be available to answer questions.
Then, invoke the skill in a fresh session, either headlessly or interactively.

The headless run script starts a detached `claude -p` with an allowlist of the tools the skill needs, and writes
`agent.jsonl` (the transcript) and, when it's done, `agent.exit` to the output dir:
```
.claude/skills/concreteconfig/scripts/run_headless.sh $SKILL_PROMPT $OUT_SKILL
```
The skill will itself run paramtrack with `--until chains` (via a wrapper) to compare each candidate's message labels to the baseline. It writes the final concrete config, and any files it references, to its output directory.

Before going on, check that the skill succeeded: the first line of `$OUT_SKILL/result.md` must be `passed` (see SKILL.md, "Outputs"). A headless run's `agent.exit` doesn't indicate success: it's 0 whenever the agent finishes, whatever the outcome.

#### 3. Run new concrete config
```
paramtrack.py \
  --out $OUT_CONCRETE \
  --testbed $TESTBED \
  --config $OUT_SKILL/config.yaml
```
## Output
The final output is `label_keys.csv`. It lists each message's keys, with a confidence for each. Message are identified by a *kind* and *label*, applied in [join.py](../../../../../../../dump_ancestors/argmatch-labels/README.md) using the logic in contexttrack's [message_graph.py](../../../../../../../conftamer/contexttrack/analysis/message_graph.py).

(See STATISTICS.md for terminology e.g. "full key").

Two kinds of row:
- **Evidence rows:** one per (message, needle, concrete key, full key, section), for the keys whose values argmatch matched in calls that join with the message. These keys are *evidence* for the message.
- **Inferred rows:** for each section with evidence for a message, one per other full key in the section. The key wasn't matched for the message, but its section was.

Columns:
- `message_kind`, `message_label`, `needle`: a row of `joined.csv` (`needle` is empty in inferred rows)
- `concrete_key`: a key at which the needle is a value in the concrete config (from `param_keys.txt`). A needle that stands for several concrete keys gets a row for each. Empty in inferred rows.
- `full_key`: the concrete key's full key (empty if it has none), or the inferred key
- `section`: the full key's section (`<parent key> @ <Unmarshaler>`); a row per section, if it has several
- `section_keys`: the full keys in that section, joined with `; `
- `evidence`: whether the key is evidence for the message
- `eligible`: whether the key could have been evidence: it's in scope (not left out by `--ignore`), and the concrete config sets it, with a value argmatch searched for (`param_values.txt`)
- `confidence_all`, `confidence_eligible`: 1 for evidence. For an inferred key, the share of its section's in-scope keys that are evidence for the message: out of all of them (not left out by `--ignore`), and out of its eligible keys. Blank if there are none. Reading the two together separates the causes of a low score:
  - both low: the section's eligible keys were searched for, and most weren't found for this message;
  - `confidence_all` low but `confidence_eligible` high: most of the section is unset, or unsearchable (numbers, bools), so the concrete config or argmatch's strings-only search is the gap.

  E.g. for the remote write request in the Prombench run, `remote_write.sigv4`'s `access_key`, `region` and `secret_key` are evidence. Its other 3 keys aren't set, so they get 0.5 out of all 6 keys, and 1.0 out of the 3 eligible.

These terms are defined in STATISTICS.md. Unlike configkeys' `section keys` column, an SD key's section is that of its own SD config, not combined over every SD config with the same field. Every evidence key counts fully, even when its needle stands for several concrete keys or is also a string literal in the code.

`summary.txt` counts rows (evidence and inferred), messages, full keys and sections, and lists needles with no concrete key and concrete keys with no full key.

## Testbeds
`--testbed` chooses where the module runs (`testbeds.py`):
- `docker`: Prometheus in one local container, built from `prometheus/prombench-argmatch.Dockerfile`, with the workload of argmatch-labels' `bootstrap-example/bootstrap.sh`: API queries every 5 s from 20 s on, a config reload at 30 s, and a stop at `--wait` seconds. The config's Kubernetes service account files are stand-ins, so it loads, but Kubernetes SD can't connect.
- `prombench`: prombench's prometheus-meta, on the GCE VM `--vm` (default `paramtrack-e2e`). paramtrack copies `testbeds.py`, `common.py` and the run's inputs to `/tmp/paramtrack-testbed` on the VM, runs the testbed there over ssh, and copies the outputs back. On the VM, it does what the argmatch experiment's `run_on_vm.sh` does ([README](../../../../../../../dump_ancestors/argmatch/experiment/README.md#what-a-run-does)): it builds `prometheus-conftamer:v3.2.1`, loads it into the main node, restarts prometheus-meta, runs the dashboard viewer and the config reloads, and copies `argmatch-out`. It also sets the prometheus-meta config map to the concrete config, and sets `CONFTAMER_EVENTS` on the deployment, before the restart. The VM needs:
  - the checkouts that `prometheus-test-infra/prombench/vm_setup.sh` makes. They're built as they are on the VM, so push the changes you want, or copy them there.
  - prombench deployed with `deploy.sh`, which needs `images/conftamer-images.tar` (see `build_prom_argmatch.sh`).

Both testbeds install step 2's `files/` where the config expects them, `/etc/prometheus/config/`: the `prombench` testbed adds each file to the prometheus-meta ConfigMap before restarting it (it's mounted there), and the `docker` testbed mounts the directory there.

Both testbeds copy the stack log and match file into the Prometheus checkout, as `paramtrack-stacklog.txt` and `paramtrack-matches.txt`, because the build reads them.

argmatch's build args are bootstrap.sh's (`ARGMATCH_ARGS` in `testbeds.py`); override them with `--argmatch-arg`.

Each testbed step (`3-chains`, `5-matches`) records its effective settings in `testbed.json`: the testbed, `--wait`, `--vm` (for `prombench`) and argmatch's build args, defaults filled in. Step 2 likewise records the static ones in `2-full-keys/settings.json`: `--module`, `--module-dir`, `--root`, `--format`, `--docs` and `--ignore`. The concreteconfig skill's `score.py` and `check_labels.py` read these, so it evaluates candidates using the same settings as the baseline.

## Adding modules and testbeds

### Reproducibility
The end-to-end workflow should be relatively reproducible, with exceptions due to e.g. timing-dependent labels, and the behavior of the agents running the skills. To that end:
- When adding a module/testbed, either commit the corresponding files, or make the process to create them reproducible with committed inputs:
  - `--ignore`, `--docs`
  - Inputs passed to skill prompts
- Once we have results for the paper, we should:
  - Commit the corresponding workflow settings files (see "Testbeds"), `--config`, and `--config-files`
  - Add the version of the module checkout to the workflow settings

### Baseline behavior
Keep in mind that when choosing concrete config values, the skill is only constrained by SKILL.md ("Constraints"). The intent of these constraints is that the modified config produces behavior that is a superset of the shipped config, so that it does not artifically reduce test coverage. In terms of the end-to-end workflow, "coverage" is concretely (1) the set of message labels the module sends/receives, and (2) the data accessible at that time.

So, strictly speaking we should verify the skill's config produces matches that are a superset of the shipped config's. However, to make the workflow cheaper (i.e. less likely to involve a long loop of checking configs), for the testbed behavior constraint we only require that (1) the config loses no message labels and (2) the testbed run does not exit 2.

Thus, when adding modules/testbeds, try to make these requirements good proxies for coverage, i.e.: (1) the baseline run should produce all message labels of interest (e.g. in Prometheus, alerting and dashboards), and (2) the check script should verify anything about the testbed behavior likely to impact the message-related data (e.g. in Prometheus, that the alerting and dashboards work).

## Command logs
paramtrack logs the commands it runs, each line with the time, the working directory and the command, shell-quoted:
- `<out>/paramtrack-commands.log`: paramtrack's own command line, once per invocation.
- `<step>/commands.log`: every command the step runs (builds, programs, and for the `prombench` testbed the `gcloud compute ssh` calls that copy inputs, start the testbed and copy outputs back). The paths in it are `<step>.partial`, where the step writes until it's done.
- `<step>/vm-commands.log`, for the `prombench` testbed's steps: the commands the testbed runs on the VM (the build, loading the image, restarting prometheus-meta, the workload, the capture), copied back with its other outputs.

Commands that only check on progress (polling the VM, prometheus-meta's readiness, the docker container's state) aren't logged.
