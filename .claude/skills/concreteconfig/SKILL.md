---
name: concreteconfig
description: Generate a "concrete config" for paramtrack, i.e. the argument to `paramtrack --config` - see its README. The config should set as many "target keys" as possible (the in-scope keys paramtrack's search can find), each with a distinctive value, without losing any message the testbed's shipped config produces. Use when asked to write, improve or regenerate the concrete config for a module and testbed (e.g. Prometheus on prombench).
---

# concreteconfig

You are a developer of the given module, writing configs that exercise as much of the module's configuration as possible (limited to the "target keys" below), for the param tracking workflow that paramtrack runs. In the paramtrack README (`gopls/internal/cmd/conftamer/querygraph/paramtrack/README.md`), "Use with /concreteconfig" says where this skill fits in: The workflow runs the module with your config, searches its memory for your config's
values (argmatch), and maps the messages it sends to the keys whose values it found. So every key you set, with a value
the search can find and attribute to that key, is a key the workflow can learn about.

The keys that count are the *target keys*, as defined in [TERMINOLOGY.md](../../../gopls/internal/cmd/conftamer/querygraph/configkeys/stats/TERMINOLOGY.md).
The "Steps" below explain how to list the module's target keys.
Set other keys only where the module needs them, e.g. for a target key to load; they don't count.

A module may have multiple config sources, listed in `modules/<module>/module.py` as keys of the `CONFIG_SOURCES` map. Write one file per config source (except if the corresponding ignore file leaves out every key).

Use what a developer of the module would know: its docs, example and test configs, and the code that parses and
validates the config. The module's file (below) points to these.

## Inputs
If any of the inputs below doesn't exist, stop and say which in the output (see "Outputs"): don't make it yourself.

### The module's and the testbed's files
They hold everything specific to one module or testbed, so the "Steps" below stay generic. The prompt that invokes the skill points to them.
- paramtrack's `modules/<module>/MODULE.md` (`gopls/internal/cmd/conftamer/querygraph/paramtrack/`): points to the developer knowledge and other module-specific information described below. The ignore and transformed files are per config source, in module.py's `CONFIG_SOURCES`.
- paramtrack's `testbeds/<environment>/<module>/TESTBED.md` (e.g. `testbeds/prombench/prometheus/`): how to use the testbed to evaluate the config, as described below.

### The rest
- The module source checkout; a running testbed; and the *shipped configs*, i.e. the concrete configs running in the testbed when the skill is invoked (one per config source).
  - MODULE.md and TESTBED.md point to these.
- The **baseline run**: step `3-chains` of paramtrack with every module of interest instrumented and running its shipped configs (the paramtrack README, "Use with /concreteconfig", step 1). The first `scripts/score.py` call runs step 1 and 2.
 See the paramtrack README for a description of the outputs; many of them you'll only need indirectly via scripts that read them, with exceptions noted below (using the filenames referenced in the README).
  - The prompt that invokes the skill gives the baseline run's output directory and the module (its `modules/` directory name).
- The path to the output directory
  - The prompt that invokes the skill must give the path to the directory, but you can create the directory itself if it doesn't exist.

## Constraints
To report success (see "Outputs"), each of the constraints below must hold, as defined in the "Check" section for each.
### 1. Valid config
Two sub-constraints:
- The config loads, both on reload and on a full restart (some settings only apply at startup)
- The config passes the module's own check command.
- The health checks pass.

#### Check ####
Module-/testbed-specific commands to apply a config, add associated files, reload, restart, and check the module/testbed. Listed in MODULE.md and TESTBED.md.

### 2. **Comparable behavior to baseline**
Two sub-constraints:
- With the new config, the module produces every distinct message label it produced in the baseline run (with the shipped config).
  - Note that some labels depend on timing, not the config (TESTBED.md gives examples) - the check described below helps handle this. Some labels also only appear when a cluster first starts, but thus far we've only seen this affect the kubernetes API server.
- With the new config, the testbed makes limited requests outside the testbed
  - If you're considering a choice that may add new requests to third-party services:
    - If you're running interactively, ask. Else, err on the side of caution - don't make that choice, and note it in the output.
  - Requests to non-third-party servers outside the testbed are ok, e.g. to GCE's metadata server.

#### Check ####
The first two sub-constraints are checked by one script, below. The third is a judgement call.
```
scripts/check_labels.py --baseline <baseline run dir> --module <module> --config <config_source>=<new config> [--config <config_source>=<new config> ...] --out <dir>
```

Pass one `--config` per config source you set, in every call: they are all needed to produce the labels being checked.

`check_labels.py` runs paramtrack with the new config through step `3-chains`, using the same settings as the baseline run. Then, it lists the labels missing from the new config's run. It exits 1 if your module is missing labels and 2 for some other error, including testbed failure (TESTBED.md may give specific info on the definition of failure). Only your own module's labels change the exit code of `check_labels.py`, but the script also reports the other modules' labels; their missing labels don't count here (they're checked after the skill run), but mention them in `result.md`.

The check passes if any `check_labels.py` call with the arguments above exits 0, *or* if multiple such calls exit 1 but their combined output produces all the labels. That is:
  - If some call exits 1 and you suspect the missing labels are due to timing:
    - Rerun the call in another directory, and check if the union of the call's labels with the previous contains all the baseline labels. Repeat if needed, unioning across all calls. E.g., the following sequence passes the check:
    ```
    check_labels.py --baseline B --module M --config SOURCE=C --out O/labels-1
      > exit 1
    check_labels.py --baseline B --module M --config SOURCE=C --out O/labels-2
      > exit 1
    check_labels.py --baseline B --module M --compare O/labels-1 O/labels-2 --list
      > exit 0
    ```
  - If labels are still missing after unioning repeated calls:
    - If you still believe the missing labels are timing noise, stop and report the
      result as `failed`, giving details in the output.
    - Else, try a different config (see "Steps")

### 3. The testbed stays the same
You cannot change the testbed except for:
  - The module's config
  - Any files the module's config refers to (TESTBED.md, "Adding files")
  - Any monitoring tools you install

#### Check ####
N/A (but you must report the commands you ran - see "Outputs")

## Objectives, in priority order
Apply these in this order whenever two choices conflict:
1. The constraint checks
2. The configs' statistics, using the score script:
```
scripts/score.py --baseline <baseline run dir> --module <module> --config <config_source>=<candidate> [--config <config_source>=<candidate> ...] --out <dir>
```
(This runs `stats.py --config-only`) on each config source. Score all your config sources in one call: a value across multiple sources affects the statistics.
Prioritize the output in this order:
  - **Coverage of the target keys** (Q1 in configkeys/stats/STATISTICS.md): more sections touched, then more of
   each touched section set, then more keys and types.
  - **Distinctive values** (Q3): no value shared by two concrete keys (across any of the config sources), and no value that's also a string literal in
   the module's code. See "Values" below for advice.

## Advice on values
You are free to set values as you see fit to maximize the objectives. However, the advice below is often helpful.

For "free-form" values (names, label values, header values, credentials, paths of added files, hosts, and map keys you choose - common exceptions listed below), `scripts/value.py <concrete key>` is an easy way to generate distinctive values (see "Objectives"): it derives a value from the
key, so it's distinct from other keys' values and almost never a code literal. `--shape` fits it to the field
(`label`, `path`, `host`); `--index` distinguishes list entries, and `--config-source` distinguishes the same key in different config sources (a value set in two config sources counts as shared).
Use something else where the value's format is restricted (e.g. enums, URLs, durations, ...), or where a specific value is needed to satisfy one of the constraints.

When the config needs to refer to files (e.g. certs, keys) or UUIDs, you can use `scripts/files.py` to generate their contents. You can pass the key's `value.py` token as the seed, so the result is distinct for each key. For any module-specific files, MODULE.md may have advice.

### Common restrictions
Some keys will be difficult to make distinctive without violating constraint 2, which a successful config must not. That is, they may change the module's behavior in a way that loses message labels, or causes the label check to exit 2 indicating a testbed failure.

Here are some examples of such keys - MODULE.md and TESTBED.md may give advice on how they apply for the specific module and testbed.

- Keys that only support enum-like values such as actions, rules, protocols and schemes.
- Keys that are mutually exclusive (e.g. one auth method per HTTP client) can't all be set in one block. For these, where the
config has list sections (e.g. for Prometheus, scrape jobs and remote writes), add a block per mutually exclusive alternative, as long as the new
entries take nothing away (constraint 2).
- Keys with values that are referred to by other modules in the tesbed.

## Steps
### Initialize
1. Read the paramtrack README, MODULE.md, and TESTBED.md. Confirm you have the other inputs in "Inputs" above.
2. Score the shipped configs (`scripts/score.py`, one `--config` per config source). This will produce `2-full-keys/full_keys.yaml` for each config source, which shows the source's config format in YAML. A valid config file for the module will differ slightly - some modules take JSON (check `2-full-keys/settings.json` next to the yaml if you're unsure which), and the key format has differences (e.g. '*' and '=' - see [TERMINOLOGY](../../../gopls/internal/cmd/conftamer/querygraph/configkeys/stats/TERMINOLOGY.md) for details)
3. Start from copies of the shipped configs. Modify their free-form values to be distinctive (see "Objectives").

### Improve and check
4. Continue setting target keys to find the configs that best satisfy the objectives, as detailed below. Keep configs that pass the constraint checks, so you can go back to them if a new config violates a constraint or scores worse. If you encounter a failure you believe is not due to the config itself (e.g. exit code 2 from a script in `scripts`, or persistent timing noise as described in constraint 2), stop and write the outputs.

**Listing target keys:**
To identify keys that meet the multi-part "target key" definition above:
- Run `scripts/score.py`: lists each source's target keys that are not yet set in the current configs (as well as the configs' statistics)
    - To see these keys in a config's shape, see `2-full-keys/full_keys.yaml` (described above). You can use it as a template (but remember it includes non-target keys).

**Setting target keys:**
- To decide whether to set a target key and to what value, use:
  - Developer knowledge
  - MODULE.md: lists keys to skip, and values known to be safe
- Record the reason you chose the value (or to skip) for the final output
- You may set non-target keys if needed to make the config satisfy the constraints

**Evaluating the objectives**:
Some checks are slower than others - TESTBED.md has estimates.
Perform faster checks early and often, e.g. the module's config check command. The message label check may take several minutes.

### Output
5. Score the final configs, and write the outputs in "Outputs" below.

## Outputs
In the output directory:
- `result.md`:
  - On the first line, write `passed`, `failed` or `stopped`:
    - `passed` means every constraint was checked on the final configs and holds
    - `failed` means a constraint doesn't hold for the final configs (e.g. labels missing even from the union of reruns)
    - `stopped` means an input is missing, or something outside the config broke (e.g. the testbed went down)
  - Then, provide details as applicable:
    - The commands used to check constraints and their outputs
    - For `passed`, the directory with the results of the passing check (`check_labels.py --out`), or multiple if it passed by unioning timing-dependent labels
    - If you went back to an earlier config that passed, say so, and where the failing
    candidate is
    - For `stopped`: the command that failed and its exit code
- `labels.txt`: the output of `scripts/check_labels.py ... --list` on the final configs
- `<config_source>/config.<format>` (`config.yaml`, `config.json`, ...: the module's config format): your final configs, and `files/`: the files it refers to, as added to the testbed
- `commands.md`: the commands and code you used to apply, check and score the configs
- `local_build_versions.json`: what the run ran from (the git versions of the tools' and the module's checkouts): run `scripts/versions.py <output dir> --module <module>` first (`scripts/run_headless.sh` does it for headless runs)
- `stats.md`: `scripts/score.py`'s output for the shipped configs and for yours, side by side
- `diff.md`: the diff from the shipped config, with a line per change explaining the reason you either chose that value or chose to skip the key

## Running headless
If the skill is running unattended (via `scripts/run_headless.sh`), note the following:
- write logs and files with the Write tool rather than shell redirects (`>`, `| tee`), which need approval;
- write absolute paths in commands rather than shell variables (`S=...; ls $S` needs approval, since the variable
  can't be checked before the command runs);
- use `cp` without flags (`cp` with flags needs approval), and `cd` only within the allowed directories (the repo,
  `/home/emily/projects/config_tracing`, `/tmp` and the Go module cache);
- files under `.claude/` can't be written.
