---
name: documentedkeys
description: Start onboarding a new Go module to paramtrack's param tracking workflow, in a fresh background agent - work out what config the module takes (and in what format), and write a script that derives the module's documented config keys from its documentation, plus the generated list. Use when asked to find a module's documented keys, or to start running the workflow on a new module (e.g. Caddy's ingress controller, Grafana, the Kubernetes API server).
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
  project's config code (e.g. Caddy's ingress controller and Caddy), all of them. Note each one's commit
  (`git -C <dir> log --oneline -1`).
- **Documentation website,** if the repos' docs might not be enough (e.g. https://caddyserver.com/).
- **Output directory:** default `/home/emily/projects/config_tracing/<module>-config-keys/`, outside the checkouts.

## 2. Check the checkouts for the user's notes
Run `git -C <dir> status --short` in each checkout. Untracked files that don't look generated (notes, scratch
configs, logs) may be the user's own and outdated. The agent can't ask, so list them in its brief as off-limits,
unless the user says to use some (ask, naming them, if any look relevant).

## 3. Launch the agent
Fill in [brief.md](brief.md)'s `{placeholders}` and pass it as the prompt. Then tell the user it's running and what
it will produce; don't wait or poll: the completion notification brings its report.

## 4. When it reports
- Spot-check its claims before relaying: the files exist, the counts match (`wc -l`), the checkouts are unmodified
  (`git status`), and anything surprising holds in the code.
- Relay the report: what config the module takes and how it's loaded, the sources, the files and counts, its
  judgment calls, and the open questions (which are the user's to decide).
- Save a project memory for the module (what was found, where, the open questions), linked from MEMORY.md.

## Examples
- Prometheus (YAML): `prom-test-infra/prombench/prometheus_config_keys.txt`, made as
  `prom-test-infra/prombench/config_keys_notes.md` describes.
- Caddy's ingress controller (Kubernetes objects, generating Caddy JSON):
  `/home/emily/projects/config_tracing/caddy-config-keys/` (its README.md), from the brief this skill generalizes
  (2026-11-06).
