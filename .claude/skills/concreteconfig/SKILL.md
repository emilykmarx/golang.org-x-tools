---
name: concreteconfig
description: Generate a "concrete config" for paramtrack, i.e. the argument to `paramtrack --config` - see its README. The config should set as many "target keys" as possible (the in-scope keys paramtrack's search can find), each with a distinctive value, without losing any message the testbed's shipped config produces. Use when asked to write, improve or regenerate the concrete config for a module and testbed (e.g. Prometheus on prombench).
---

# concreteconfig

You are a developer of the given module, writing a config that exercises as much of the module's configuration as possible (limited to the "target keys" below), for the param tracking workflow that paramtrack runs. In the paramtrack README (`gopls/internal/cmd/conftamer/querygraph/paramtrack/README.md`), "Use with /concreteconfig" says where this skill fits in: The workflow runs the module with your config, searches its memory for your config's
values (argmatch), and maps the messages it sends to the keys whose values it found. So every key you set, with a value
the search can find and attribute to that key, is a key the workflow can learn about.

**Target keys:** the keys that count, the *target keys*, are the module's keys that are both:
- **in-scope:** not left out by the module's "ignore file" (paramtrack's `--ignore`), e.g. Prometheus ignores service discovery
  configs the testbed doesn't use.
- **searchable:** (1) of a "kind" the search covers (i.e. string or struct, satisfying configkeys' `--strings-only`), and (2) not stored transformed, e.g. Prometheus transforms regexes, URLs and PEM blocks.

The "Steps" below explain how to list the module's target keys.

Set other keys only where the module needs them, e.g. for a target key to load; they don't count. paramtrack's README
(`--ignore`) says what the workflow does with a non-target key you set.

Use what a developer of the module would know: its docs, example and test configs, and the code that parses and
validates the config. The module's file (below) points to these.

## Inputs
If any of the inputs below doesn't exist, stop and say which in the output (see "Outputs"): don't make it yourself.

### The module's and the testbed's files
They hold everything specific to one module or testbed, so the "Steps" below stay generic. The prompt that invokes the skill points to them.
- paramtrack's `modules/<module>/MODULE.md` (`gopls/internal/cmd/conftamer/querygraph/paramtrack/`): points to the developer knowledge, the ignore file, and other module-specific information described below
- paramtrack's `testbeds/<testbed>/TESTBED.md`: how to use the testbed to evaluate the config, as described below.

### The rest
- The module source checkout; a running testbed; and the *shipped config*, i.e. the concrete config running in the testbed when the skill is invoked.
  - MODULE.md and TESTBED.md point to these.
- The **baseline run**: a run of paramtrack only through step `3-chains`, with the testbed's shipped config as `--config`. See the paramtrack README for a description of the outputs; many of them you'll only need indirectly via scripts that read them, with exceptions noted below (using the filenames referenced in the README).
  - The prompt that invokes the skill gives the baseline run's output directory.
- The path to the output directory
  - The prompt that invokes the skill must give the path to the directory, but you can create the directory itself if it doesn't exist.

## Constraints
To report success (see "Outputs"), each of the constraints below must hold, as defined in the "Check" section for each.
### 1. Valid config
Two sub-constraints:
- The config loads, both on reload and on a full restart (some settings only apply at startup)
- The config passes the module's own check command.

#### Check ####
Module-/testbed-specific commands to apply a config, add associated files, reload, restart, and check the module/testbed. Listed in MODULE.md and TESTBED.md.

### 2. **Comparable behavior to baseline**
Three sub-constraints:
- The new config produces every distinct message label that the shipped config produced in the baseline run. New labels are fine.
  - Note that some labels depend on timing, not the config (TESTBED.md gives examples) - the check accounts for this.
- With the new config, the testbed is still functional (as defined by the check below)
- With the new config, the testbed makes limited requests outside the testbed
  - If you're considering a choice that may add new requests to third-party services:
    - If you're running interactively, ask. Else, err on the side of caution - don't make that choice, and note it in the output.
  - Requests to non-third-party servers outside the testbed are ok, e.g. to GCE's metadata server.

#### Check ####
The first two sub-constraints are checked by one script, below. The third is a judgement call.
```
scripts/check_labels.py --config <new config> --baseline <baseline run dir> --out <dir>
```

`check_labels.py` runs paramtrack with the new config through step `3-chains`, using the same settings as the baseline run. Then, it runs `scripts/count_labels.py`, which lists the labels missing from the new config's run. Both label scripts exit 1 for missing labels and 2 for some other error, including testbed failure (TESTBED.md may give specific info on the definition of failure).

The check passes if any `check_labels.py` call with the arguments above exits 0, *or* if multiple such calls exit 1 but their combined output produces all the labels. That is:
  - If some call exits 1 and you suspect the missing labels are due to timing:
    - Rerun the call in another directory, and check if the union of that call's "events" (an output of paramtrack's step 3) with the previous contains all the baseline labels. Repeat if needed, unioning across all calls. E.g., the following sequence passes the check:
    ```
      check_labels.py --config C --baseline B --out O/labels-1
        > exit 1
      check_labels.py --config C --baseline B --out O/labels-2
        > exit 1
      count_labels.py O/labels-1/3-chains/events.jsonl O/labels-2/3-chains/events.jsonl \
        --baseline B/3-chains/events.jsonl --list
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
2. The config's statistics, using the score script:
```
scripts/score.py --config <candidate> --baseline <baseline run dir> --out <dir>
```
(This runs `stats.py --config-only`).
Prioritize the output in this order:
  - **Coverage of the target keys** (Q1 in configkeys/stats/STATISTICS.md): more sections touched, then more of
   each touched section set, then more keys and types.
  - **Distinctive values** (Q3): no value shared by two concrete keys, and no value that's also a string literal in
   the module's code. See "Values" below for advice.

## Advice on values
You are free to set values as you see fit to maximize the objectives. However, the advice below is often helpful.

For "free-form" values (names, label values, header values, credentials, paths of added files, hosts, and map keys you choose - common exceptions listed below), `scripts/value.py <concrete key>` is an easy way to generate distinctive values (see "Objectives"): it derives a value from the
key, so it's distinct from other keys' values and almost never a code literal. `--shape` fits it to the field
(`label`, `path`, `host`); `--index` distinguishes list entries. Some

Use something else for non-free-form values, e.g. those that have a restricted format,

whose value is restricted to a specific format the token doesn't fit, or a value needed to satisfy one of the constraints.

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
1. Read the paramtrack README, MODULE.md, and TESTBED.md. Confirm you have the other inputs in "Inputs" above. Read the comments at the top of `2-full-keys/full_keys.yaml`, which is from the baseline run and shows the module's config format.
2. Score the shipped config (`scripts/score.py`).
3. Start from a copy of the shipped config. Modify its free-form values to be distinctive (see "Objectives").

### Improve and check
4. Continue setting target keys to find the config that best satisfies the objectives, as detailed below. Keep configs that pass the constraint checks, so you can go back to them if a new config violates a constraint or scores worse. If you encounter a failure you believe is not due to the config itself (e.g. exit code 2 from a script in `scripts`, or persistent timing noise as described in constraint 2), stop and write the outputs.

**Listing target keys:**
To identify keys that meet the multi-part "target key" definition above:
- First run `scripts/score.py`: lists *in-scope* keys with a target *kind*, that are not yet set in the current config (as well as the config's statistics)
    - To see these keys in the config's shape, see `2-full-keys/full_keys.yaml`. This lists all in-scope keys with their kind. You can use it as a template (but remember it includes non-target keys).
- If any of the keys `scripts/score.py` lists is marked as *transformed* in MODULE.md, ignore it.

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
5. Score the final config, and write the outputs in "Outputs" below.

## Outputs
In the output directory:
- `result.md`:
  - On the first line, write `passed`, `failed` or `stopped`:
    - `passed` means every constraint was checked on the final config and holds
    - `failed` means a constraint doesn't hold for the final config (e.g. labels missing even from the union of reruns)
    - `stopped` means an input is missing, or something outside the config broke (e.g. the testbed went down)
  - Then, provide details as applicable:
    - The commands used to check constraints and their outputs
    - If you went back to an earlier config that passed, say so, and where the failing
    candidate is
    - For `stopped`: the command that failed and its exit code
- `labels.txt`: the output of `scripts/check_labels.py ... --list` on the final config
- `config.yaml`: your final config, and `files/`: the files it refers to, as added to the testbed
- `commands.md`: the commands and code you used to apply, check and score the config
- `stats.md`: `scripts/score.py`'s output for the shipped config and for your `config.yaml`, side by side
- `diff.md`: the diff from the shipped config, with a line per change explaining the reason you either chose that value or chose to skip the key

## Running headless
If the skill is running unattended (via `scripts/run_headless.sh`), note the following:
- write logs and files with the Write tool rather than shell redirects (`>`, `| tee`), which need approval;
- write absolute paths in commands rather than shell variables (`S=...; ls $S` needs approval, since the variable
  can't be checked before the command runs);
- use `cp` without flags (`cp` with flags needs approval), and `cd` only within the allowed directories (the repo,
  `/home/emily/projects/config_tracing`, `/tmp` and the Go module cache);
- files under `.claude/` can't be written.
