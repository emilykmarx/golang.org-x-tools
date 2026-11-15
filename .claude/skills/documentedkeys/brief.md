You're helping a researcher extend a config-tracing workflow to a new Go module. The workflow statically analyzes
how a module's config file is unmarshaled into Go types, runs the module with a concrete config, and maps the
messages it sends to the config keys whose values it finds in memory. It runs on Prometheus (YAML config) and Caddy's ingress controller (JSON) so far.
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
mapstructure, a custom one). If the user-facing config source is transformed into another format before the module loads it (e.g. a controller generating a server's config), say how, and which part of the other config the module can produce or accept. Report this clearly: it decides which documented keys matter.
Scope the list to what the module can actually be configured with (if a broader documented config is much
bigger than that, give both, and say how you decided the subset)

## Then, the documented keys
Write a Python script (Python preferred; shell only for small helpers; you can't ask questions, so decide and say
why) that extracts the documented keys from the documentation sources you found. Prefer an existing
machine-readable source (an API behind a docs site, a schema, generated reference data) over scraping HTML, and
prefer reusing existing tools or well-known libraries over writing parsers; if you write from scratch, say what you
looked at first.

### List format
The list should have one key per line, with its type:
`<key>\t<documented type>`

No comments or blank lines. Every line must be a key that takes a value: don't list objects that only group other
keys, a module choice with no fields of its own, or the points where you stopped expanding a recursive structure
(describe those in the README instead).

Represent each key as a dot-separated path from the config's root, with `*` for placeholders (below), and list
indices left out. Where the config is modular (a field whose value is one of several types, chosen by a key/value or by the field's name), follow the examples below where possible - if not possible, decide how to represent it and explain.

The workflow compares your keys with its own "full keys" as exact strings, so match their format: see "Full key" in
[TERMINOLOGY.md](../../../gopls/internal/cmd/conftamer/querygraph/configkeys/stats/TERMINOLOGY.md).

Details:
- The dot-separated segments can't contain a `.`, `<`, or `*` unless it's a placeholder
- Placeholders: If the segment can be any string, chosen by the user, write it as <...> or *
  - e.g. in Prometheus:
    - `http_headers.<string>.values` - the user picks the header name.
  - e.g. in Caddy:
    - `apps.http.servers.*.routes...` - the user picks the server name
- Maps whose keys the user picks: if the docs give the fields of the map's values, list them with a placeholder
  segment (`http_headers.<string>.values`); otherwise list the map itself with the type `map` (exactly that word: the
  workflow then reads it as `<key>.*`)
- If the segment can be one of a set of strings, write all the possibilities
  - e.g. in Caddy:
    - the segment after `apps.` can be `http`, `tls`, or one of the other "app" modules
- If the segment selects which other segments are valid in the rest of the key, write all the possibilities
  - e.g. in Caddy:
    - if the segment after `apps.` is `http`, the following segment could be `http_port`, `https_port`, or one of the other keys the http module supports
    - whereas if it is `tls`, the following segment could be `certificates`, `automation`, or one of the other keys the tls module supports
- If a value in a key-value pair selects which other segments are valid in the rest of the key, insert the value as a `key=value` segment
  - e.g. in this portion of a valid Caddy JSON config file:
  ```
  "handle": [
    {
      "handler": "reverse_proxy",
      "upstreams": [{"dial": "localhost:8080"}]
    }
  ]
  ```
    - the value selected for the `handler` key determines the options for the rest of the segment - e.g. `upstreams.dial` is valid above since the `reverse_proxy` module supports them, but if the value of `handler` is instead `static_response`, the valid segments include `response_code`
    - thus, the key should be `...handle.handler=reverse_proxy.upstreams.dial`
    - list the selecting key itself too (`...handle.handler`), with its documented type: it's a key the user sets
    - this prevents two keys from identifying different information, e.g. if there were another module `reverse_proxy_2` that also supported `upstreams.dial` (but may handle it very differently than `reverse_proxy` does)


### Examples
See the `documentedkeys` directories for [Prometheus](../../../gopls/internal/cmd/conftamer/querygraph/paramtrack/modules/prometheus/documentedkeys) and the [Caddy ingress controller](../../../gopls/internal/cmd/conftamer/querygraph/paramtrack/modules/caddy/documentedkeys), a Kubernetes controller that generates and uses Caddy's
JSON config (note that for the workflow's purposes the Caddy controller is one "module", but Caddy uses the term "module" to mean a plugin).

The Prometheus list is `prometheus_config_keys.txt`, generated by `list_config_keys.py` using only the configuration doc in the source code.

The Caddy controller list is `caddy_keys_linked.txt` (as described in the README, the agent also produced `caddy_keys_full_jsonpaths.txt` which formats inline modules differently - the workflow chose not to use this format, so you should not either).

Produce a list in the same format as these examples where possible. Also read the READMEs in these directories, which explain how the lists were made - follow their conventions.

To see how the workflow reads the list: see `read_docs` in [stats.py](../../../gopls/internal/cmd/conftamer/querygraph/configkeys/stats/stats.py). The hook referenced there is implemented in a per-module file that provides module-specific information needed by the workflow (`module.py` in the parent directory of `documentedkeys` for Prometheus and Caddy), which may not exist yet for this skill's module by the time the skill runs.

## Output
Put everything in {output dir} (create it if needed): the script(s), the generated key list(s), cached fetched sources if
any, and a short README.md saying what each file is, the sources (URLs, file paths, commits, fetch date), how to
rerun, the counts, the distinct types in the list and what each means (e.g. `<duration>`, `[]string`: the workflow
classifies each as string, number, bool or map), and known gaps or judgment calls. Give every command that can take more than a few seconds a
timeout (e.g. `timeout 120 ...`) and check exit codes; don't fetch more of a website than you need (no crawling if
an API or a single reference page exists). Use `uv run --with <pkg> python ...` for third-party Python packages.

## Your final report (I'll relay it)
Keep it concise: (1) what config the module takes, in what format, and how it's loaded (functions, root types,
decoder); (2) the documentation sources used and why; (3) the files written and key counts; (4) judgment calls
(modular fields, arrays, map keys, scope) and open questions for the researcher, e.g. which version to analyze if
the docs and the checkouts differ; (5) anything surprising, e.g. options found in code but not documented.
