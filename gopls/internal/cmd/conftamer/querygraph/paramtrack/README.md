# paramtrack

Runs the end-to-end param tracking workflow in [../WORKFLOW.md](../WORKFLOW.md), from the concrete config to Message label => Full keys, and joins the outputs (step 7).

## Usage
```
cd gopls/internal/cmd/conftamer/querygraph/paramtrack
uv run --with pandas --with matplotlib python paramtrack.py --out <dir> --config <concrete config> \
  [--module github.com/prometheus/prometheus] [--module-dir <checkout>] [--root /config.Config] [--format yaml] \
  [--testbed docker] [--wait 240] [--argmatch-arg KEY=VALUE ...] [--redo <step>] [--until <step>]
```
The defaults are for Prometheus. A full run takes about 20–25 minutes, mostly the two builds and runs of the dynamic phase.

pandas is needed because step 7 uses stats.py's rules for full keys and sections ([../configkeys/stats/STATISTICS.md](../configkeys/stats/STATISTICS.md)), and matplotlib because stats.py imports it.

## Steps and outputs
Each step writes one subdirectory of `--out`, named after the step:

| Step | Program | Main output |
|---|---|---|
| `1-concrete-config` | (copies `--config`, until the /concreteconfig skill exists) | `config.yaml` |
| `2-unmarshaler-graph` | `gopls conftamer`, built from this checkout | `unmarshaler_subgraph.text` |
| `3-configkeys` | [configkeys](../configkeys/README.md) `--strings-only` | `configkeys.csv`, `param_values.txt`, `param_keys.txt` |
| `4-chains` | testbed run 1: contexttrack, with argmatch instrumenting nothing; then `events_to_stacklog.py` | `events.jsonl`, `stacklog.txt` |
| `5-matches` | testbed run 2: argmatch instruments `stacklog.txt`'s functions and searches for `param_values.txt`; contexttrack records the messages | `matches.csv`, `matches.calls.csv`, `events.jsonl` |
| `6-label-values` | argmatch-labels' `join.py` | `joined.csv` |
| `7-label-keys` | paramtrack | `label_keys.csv`, `summary.txt` |

Runs 1 and 2 are argmatch-labels' bootstrapping ([README](../../../../../../../dump_ancestors/argmatch-labels/README.md#bootstrapping)).

**Resuming:** a step whose directory exists is skipped. A step writes into `<step>.partial` and renames it when it's done, so a failed or interrupted step reruns. `--redo <step>` reruns that step and the later ones. Once any step runs, the later ones rerun too, since their inputs changed. Steps can be named by number (`5`), name (`matches`) or both (`5-matches`). `--until <step>` stops after a step, e.g. `--until 3` for the static phase only.

## `label_keys.csv`
One row per (message, needle, concrete key, full key, section), with columns:
- `message_kind`, `message_label`, `needle`: a row of `joined.csv`
- `concrete_key`: a key at which the needle is a value in the concrete config (from `param_keys.txt`). A needle that stands for several concrete keys gets a row for each.
- `full_key`: the concrete key's full key; empty if it has none
- `section`: the full key's section (`<parent key> @ <Unmarshaler>`); a row per section, if it has several
- `section_keys`: the full keys in that section, joined with `; `

These terms are defined in STATISTICS.md. Unlike configkeys' `section keys` column, an SD key's section is that of its own SD config, not combined over every SD config with the same field.

`summary.txt` counts rows, messages, full keys and sections, and lists needles with no concrete key and concrete keys with no full key.

## Testbeds
`--testbed` chooses where the module runs (`testbeds.py`):
- `docker`: Prometheus in one local container, built from `prometheus/prombench-argmatch.Dockerfile`, with the workload of argmatch-labels' `bootstrap-example/bootstrap.sh`: API queries every 5 s from 20 s on, a config reload at 30 s, and a stop at `--wait` seconds. The config's Kubernetes service account files are stand-ins, so it loads, but Kubernetes SD can't connect. The stack log and match file are copied into the Prometheus checkout, as `paramtrack-stacklog.txt` and `paramtrack-matches.txt`, because the build reads them.

argmatch's build args are bootstrap.sh's (`ARGMATCH_ARGS` in `testbeds.py`); override them with `--argmatch-arg`.
