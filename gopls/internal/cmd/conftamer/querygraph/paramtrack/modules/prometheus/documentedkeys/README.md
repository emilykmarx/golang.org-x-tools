# Notes on prometheus_config_keys.{txt,yaml} (keys from the docs)

Notes from the session that generated `list_config_keys.py` to list Prometheus' documented keys (from an ad-hoc prompt predating the /documentedkeys skill).

## Source
- The input is `/home/emily/projects/config_tracing/prometheus/docs/configuration/configuration.md`.
- That checkout is at `v3.2.1-16-g8a4cb4fef`, 16 commits past the v3.2.1 tag. Compare
  it against source from the same checkout, not upstream v3.2.1.
- The keys come only from the docs. No Go code was read to produce them.

## What counts as a key
- **Listed:** every documented key that takes a value directly, 2,671 in total.
  - Each line of `prometheus_config_keys.txt` is `dotted.path<TAB>type`.
  - The type is the docs' text with `[ ... ]` and `| default ...` removed, e.g.
    `<string>`, `[<string>, ...]`, `<filepath_glob> ...`. It is not normalized, so the
    same type can appear in several spellings.
- **Not listed: keys that only group other keys.** There are 310 of these, e.g.
  `tls_config`, `basic_auth`, `queue_config`, `kubernetes_sd_configs` and
  `scrape_configs` itself. Their children are listed.
- **Maps with user-chosen key names:** 220 of the listed keys, with type `map`, e.g.
  `global.external_labels`, `scrape_configs.params`, `remote_write.headers`,
  `http_headers`, `proxy_connect_header` and `consul_sd_configs.node_meta`.
  - The user-chosen key is not a key itself.
  - Where the docs give the map's entries child keys, the path keeps the docs'
    placeholder as written, e.g. `scrape_configs.http_headers.<string>.values`. That
    only happens for `http_headers`, which has 84 occurrences. The others have no
    children in the list.
  - In the YAML, those 84 `http_headers` are mappings, not `{}`, so the YAML has 2,587
    `{}` leaves and the `.txt` has 2,671 lines.
- **Lists are invisible:** a path has no list index or `-`.
  `scrape_configs.kubernetes_sd_configs.selectors.role` is a key inside a list of
  selectors inside a list of SD configs inside a list of scrape configs.

## Shared blocks are counted once per place
- A reference to another section is expanded at that point, recursively, starting from
  the top-level block under "## Configuration file". References look like
  `[ <http_config> ]`, `[ - <scrape_config> ... ]`, `tls_config: [ <tls_config> ]` or
  `[ tsdb: <tsdb> ]`.
- **`<http_config>` is inlined.** Its keys sit directly on the parent, e.g.
  `scrape_configs.proxy_url`, with no `http_config` level. This matches how the docs
  write it, and should match Go's `yaml:",inline"`.
- **`<http_config>` contains `oauth2`,** which itself contains `tls_config`, proxy
  fields and `http_headers`. So every place with HTTP settings has two `tls_config`s:
  `X.tls_config.*` and `X.oauth2.tls_config.*`. `oauth2.tls_config.*` alone accounts for
  420 keys.
- **Service-discovery blocks are counted twice,** under `scrape_configs.*_sd_configs` and
  under `alerting.alertmanagers.*_sd_configs`, each with the full `<http_config>`
  expanded where the docs include it. This is the bulk of the list: `scrape_configs` has
  about 1,400 keys and `alerting` about 1,400.
  - In Go the SD configs are not fields with yaml tags on the parent. They are
    registered through `discovery.RegisterConfig` (`discovery/registry.go`) and
    unmarshalled by `(*discovery.Configs).UnmarshalYAML` (`discovery/discovery.go:111`).
    Code-side tools may therefore see them
    differently, e.g. flattened into the parent. The example
    `/tmp/dump_params_out/params.yaml` put every SD key directly under `alertmanagers`.
    If the source-side list does that too, merge `*_sd_configs` and `static_configs`
    into the parent before comparing.
- **`<relabel_config>` is expanded under every key that uses it:**
  `scrape_configs.relabel_configs`, `scrape_configs.metric_relabel_configs`,
  `alerting.alert_relabel_configs`, `alerting.alertmanagers.relabel_configs`,
  `alerting.alertmanagers.alert_relabel_configs` and
  `remote_write.write_relabel_configs`.

## Doc quirks that are in the output as-is
None of these are fixed. Each is a candidate for a docs-vs-code difference.
- **SD lists differ between the two parents.** `<scrape_config>` lists no
  `vultr_sd_configs`, and `<alertmanager_config>` lists no `kuma_sd_configs`. Each parent
  has 27 SD blocks. The code almost certainly allows every registered SD in both
  places.
- **No SD lists in `remote_write` or `remote_read`,** which is correct.
- **`metric_name_validation_scheme` has no colon** in the docs
  (`[ metric_name_validation_scheme <string> | default "utf8" ]`), both in `global` and
  in `<scrape_config>`. The parser accepts `key <type>` for this reason.
- **Triton writes `role : <string>`,** with a space before the colon. It is parsed as
  `role`.
- **`<metric_relabel_configs>` and `<alert_relabel_configs>`** are `###` sections with
  no code block. They contribute nothing themselves and are reached through
  `<relabel_config>` references.
- **`<file_sd_config>` and `<http_sd_config>`** contain JSON examples of the target
  file or response format. Only the last code block in each section is used, which is
  the config block, so the JSON keys (`targets`, `labels`) are not listed as config keys
  there.
- **`static_configs`** comes from the `<static_config>` section: `targets` and `labels`.
- **Things the docs call types are not always types,** e.g. `job_name: <job_name>`,
  `role: <openstack_role>`, `google_iam.credentials_file: <file_name>`, and `protobuf_message`,
  whose type is the literal `<prometheus.WriteRequest | io.prometheus.write.v2.Request>`.
- **The docs give some list-valued keys as bare item lines,** e.g. `rule_files:` then
  `[ - <filepath_glob> ... ]`, and `scaleway tags_filter:` then `[ - <string> ]`. These
  are listed as leaf keys whose type is the item text.
- **Not in the docs, so not listed:** the example params.yaml has a top-level
  `loaded` key. That is `Config.loaded` (`config/config.go:275`), an unexported field set
  after validation, not a config key.

## Parser heuristics
The docs' YAML-like blocks aren't valid YAML, so `list_config_keys.py` is heuristic. Its
output was spot-checked, not exhaustively verified.
- **Nesting** follows the indentation before any `[`, so `  [ a: <int> ]` and `  b:` are
  siblings.
  - Exception: a line continuing a list item lines up with the item's text column.
    Examples are `[ - role: <string>` followed by `    [ label: <string> ]` in
    kubernetes selectors, and `[ - name: <string>` followed by `      values: ...` in
    docker/dockerswarm/ec2 filters.
  - Another exception: a bare value line directly after a `key:` with no value, at the
    same indent, belongs to that key. This is for `tags_filter:` / `[ - <string> ]`.
- **What was checked by eye** against the docs: the top level; `<scrape_config>`,
  `<http_config>`, `<tls_config>` and `<oauth2>`; the kubernetes, docker, dockerswarm,
  ec2, consul, marathon, scaleway and triton SD blocks; remote_write including `azuread`
  and `sigv4`; remote_read; storage; tracing.
- **Earlier bugs, now fixed:**
  - Bracketed siblings were nested under unbracketed keys, so `global.query_log_file`
    ended up under `external_labels`.
  - Closing brackets were stripped from list types, giving `[<string>, ...` instead of
    `[<string>, ...]`.
- **If the counts change after an edit,** diff the old and new `.txt` to see which paths
  moved. A nesting bug tends to keep the total the same and change only the paths.
