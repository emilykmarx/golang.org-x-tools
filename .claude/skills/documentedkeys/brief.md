You're helping a researcher extend a config-tracing workflow to a new Go module. The workflow statically analyzes
how a module's config file is unmarshaled into Go types, runs the module with a concrete config, and maps the
messages it sends to the config keys whose values it finds in memory. It runs on Prometheus (YAML config) so far.
The new module is {module} ({go module paths}). Your task: work out what config it takes, and produce the list of
its **documented config keys**, with a script that derives the list from documentation, so it can later be
compared with the keys the static analysis finds in the code.

## Checkouts (read-only: don't modify, don't commit)
{for each checkout: - <name>: <path> (<repo>, at <commit>)}
{if any: These untracked files are the researcher's own scratch notes: <list>. Do NOT read or use them: they may be
outdated and the researcher hasn't approved them.} Use only git-tracked files (`git ls-files`){if a website:, plus
{website} if the repos' docs aren't enough}.

## First, establish what "the config" is
Find out from the module's code and docs which config inputs a user writes, in what format (a YAML or JSON file,
Kubernetes objects or annotations, flags, environment variables, ...), and how the module loads them: which function
reads them, into which Go types (the roots of the config), and with which decoder (go-yaml v2/v3, encoding/json,
mapstructure, a custom one). If the module generates another program's config from its own (e.g. a controller
generating a server's config), say how, and which part of the other config it can produce or accept. Report this
clearly: it decides which documented keys matter.

## Then, the documented keys
Write a Python script (Python preferred; shell only for small helpers; you can't ask questions, so decide and say
why) that extracts the documented keys from the documentation sources you found. Prefer an existing
machine-readable source (an API behind a docs site, a schema, generated reference data) over scraping HTML, and
prefer reusing existing tools or well-known libraries over writing parsers; if you write from scratch, say what you
looked at first.
Represent each key as a dot-separated path from the config's root, with `*` for map keys a user chooses, and list
indices left out. Where the config is modular (a field whose value is one of several types, chosen by a key or by
the field's name), decide how to represent that and explain. The existing Prometheus list,
/home/emily/projects/config_tracing/prom-test-infra/prombench/prometheus_config_keys.txt, is one key per line:
`<dotted key>\t<documented type>`; /home/emily/projects/config_tracing/prom-test-infra/prombench/config_keys_notes.md
describes how it was made (read it for conventions). Another example, for a Kubernetes controller generating Caddy's
JSON config: /home/emily/projects/config_tracing/caddy-config-keys/README.md. Produce a list in the same format
where possible, scoped to what the module can actually be configured with (if a broader documented config is much
bigger than that, give both, and say how you decided the subset).

## Output
Put everything in {output dir} (create it): the script(s), the generated key list(s), cached fetched sources if
any, and a short README.md saying what each file is, the sources (URLs, file paths, commits, fetch date), how to
rerun, the counts, and known gaps or judgment calls. Give every command that can take more than a few seconds a
timeout (e.g. `timeout 120 ...`) and check exit codes; don't fetch more of a website than you need (no crawling if
an API or a single reference page exists). Use `uv run --with <pkg> python ...` for third-party Python packages.

## Your final report (I'll relay it)
Keep it concise: (1) what config the module takes, in what format, and how it's loaded (functions, root types,
decoder); (2) the documentation sources used and why; (3) the files written and key counts; (4) judgment calls
(modular fields, arrays, map keys, scope) and open questions for the researcher, e.g. which version to analyze if
the docs and the checkouts differ; (5) anything surprising, e.g. options found in code but not documented.
