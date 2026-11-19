---
name: documentedkeys
description: Start onboarding a new Go module to paramtrack's param tracking workflow, in a fresh background agent - work out what config the module takes (and in what format), and write a script that derives the module's documented config keys from its documentation, plus the generated list. Use when asked to find a module's documented keys, or to start running the workflow on a new module (e.g. Grafana, the Kubernetes API server).
---

# documentedkeys

paramtrack's workflow compares the keys its static analysis finds with the keys the module documents (stats.py's
`--docs`; Q4 in configkeys/stats/STATISTICS.md). For a new module, the documented keys come first, and finding them
means working out what the module's config even is. This skill runs that investigation in a **fresh background
agent** (the Agent tool, `general-purpose`, `run_in_background: true`), not a fork: it needs none of this session's
context, and the main session stays free meanwhile.


## 1. Gather the inputs
Ask the user only for what you can't find:
- **Module:** its name, and its Go module path(s).
- **Checkouts:** local paths, usually under `/home/emily/projects/config_tracing/`; for a module that imports another
  project's config code (e.g. Caddy's ingress controller and Caddy), all of them. Note the most recent tag in the checkout. If there are no recent tags, ask the user.
- **Documentation website,** if the repos' docs might not be enough (e.g. https://caddyserver.com/).
- **Output directory:** default to a `documentedkeys` directory under the module's directory in paramtrack (e.g. [here](../../../gopls/internal/cmd/conftamer/querygraph/paramtrack/modules/prometheus/documentedkeys) for Prometheus).

## 2. Check the checkouts for the user's notes
Run `git -C <dir> status --short` in each checkout. Untracked files that don't look generated (notes, scratch
configs, logs) may be the user's own and outdated. The agent can't ask, so list them in its brief as off-limits,
unless the user says to use some (ask, naming them, if any look relevant).

## 3. Launch the agent
Record what the run starts from in the output directory, `local_build_versions.json` (the git versions of the tools'
and the module's checkouts): `../concreteconfig/scripts/versions.py <output dir> --checkout <name>=<checkout path>`, one
`--checkout` per checkout from step 1 (the module has no module.py yet, so not `--module`).
Fill in [brief.md](brief.md)'s `{placeholders}` and pass it as the prompt. Then tell the user it's running and what
it will produce; don't wait or poll: the completion notification brings its report.

## 4. When it reports
- Spot-check its claims before relaying: the files exist, the counts match (`wc -l`), the checkouts are unmodified
  (`git status`), and anything surprising holds in the code.
- Relay the report: what config the module takes and how it's loaded, the sources, the files and counts, its
  judgment calls, and the open questions (which are the user's to decide).
- Save a project memory for the module (what was found, where, the open questions), linked from MEMORY.md.
- Wire the list into paramtrack: create the module's directory and `module.py` under paramtrack's `modules/` if they
  don't exist yet (`modules/__init__.py` lists the settings and hooks; start from an existing module's), then set its
  `DOCS` to the list, and write its `docs_kind` for the list's types (the README lists them); add a `docs_entry` hook
  only if some lines still need normalizing.
