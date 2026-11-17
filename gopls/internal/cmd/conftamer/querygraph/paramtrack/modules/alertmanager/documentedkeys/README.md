# Documented config keys: Alertmanager

Documented config keys of `github.com/prometheus/alertmanager`, derived from the docs only, for comparison with the
keys the static analysis finds. Format and conventions follow
[`../../prometheus/documentedkeys/README.md`](../../prometheus/documentedkeys/README.md); this file only says where
Alertmanager differs.

## What "the config" is
- **The config file** (`--config.file`, default `alertmanager.yml`): YAML, read by `config.LoadFile` ->
  `config.Load` (`config/config.go:172-205`) with `yaml.UnmarshalStrict` from **gopkg.in/yaml.v2** (v2.4.0) into
  **`config.Config`** (`config/config.go:301`). Loaded at startup and on reload (SIGHUP, `/-/reload`) through
  `config.Coordinator` (`config/coordinator.go:96`). Many types have custom `UnmarshalYAML`s (defaults and
  validation, `Secret`, `URL`, `Regexp`, `Matchers`, time intervals), and `HTTPClientConfig`/`TLSConfig` come from
  `github.com/prometheus/common/config` v0.46.0, `SigV4Config` from `common/sigv4` v0.1.0.
  This is the list the workflow should use: `alertmanager_config_keys.txt`, root `config.Config`.
- Other inputs. The first two are roots too (`../module.py`; `--cluster.tls-config`'s keys are all in its ignore
  file, `../ignore-cluster_tls.txt`, since the testbed runs without clustering):
  - `--web.config.file`: YAML, exporter-toolkit v0.11.0 `web.Config`, also `yaml.UnmarshalStrict` (yaml.v2), read on
    every HTTP request. Documented in `docs/https.md` -> `web_config_keys.txt`.
  - `--cluster.tls-config` (experimental): YAML, `cluster.TLSTransportConfig` (`cluster/tls_config.go:26-44`),
    yaml.v2 `UnmarshalStrict`. Documented in `docs/https.md` -> `cluster_tls_config_keys.txt`.
  - 25 kingpin flags in `cmd/alertmanager/main.go` plus exporter-toolkit's web flags. Documented only by their
    `--help` strings in code, so not listed.
  - Template files (`templates:`) are Go templates, not config keys.
- The prombench testbed (`paramtrack-prombench/prombench/manifests/cluster-infra/5a_alertmanager_deployment.yaml`)
  uses only the config file plus 4 flags (`--web.external-url`, `--config.file`, `--cluster.listen-address=`,
  `--storage.path`); no web config, and clustering is off.

## Files
| File | What |
|---|---|
| `list_config_keys.py` | Writes the three lists from the docs (standard library only) |
| `alertmanager_config_keys.txt` | Keys of the config file, root `config.Config` (657) |
| `web_config_keys.txt` | Keys of the `--web.config.file` file (16) |
| `cluster_tls_config_keys.txt` | Keys of the `--cluster.tls-config` file (14) |

## Sources
- `docs/configuration.md` and `docs/https.md` in `/home/emily/projects/config_tracing/alertmanager-paramtrack`, tag
  v0.27.0, commit `0aa3c2aad14cff039931923ab16b26b7481783b5` (read 2026-11-16). This is the version prombench runs,
  so docs and code match. https://prometheus.io/docs/alerting/latest/configuration/ renders the same markdown for the
  latest release and wasn't needed or fetched.
- No machine-readable source exists (the website renders the repo's markdown). The script is a copy of
  `../../prometheus/documentedkeys/list_config_keys.py`, since the docs share its format, with the changes below.

## Rerun
```sh
cd /home/emily/projects/config_tracing/golang.org-x-tools/gopls/internal/cmd/conftamer/querygraph/paramtrack/modules/alertmanager/documentedkeys
timeout 60 python3 list_config_keys.py   # --docs-dir DIR, --out-dir DIR; prints counts and recursion cuts to stderr
```
It exits non-zero if a section has more than one candidate code block or a key is listed twice.

## Changes from the Prometheus script
- Sections start at any heading level (`#### \`<oauth2>\``, `##### \`<action_confirm_field_config>\``), and any other
  heading ends one, so `#### Example` blocks aren't taken. A section's block is its only ```` ```yaml ```` block
  (`<webhook_config>` also has a JSON payload example), else its only block (https.md's are untagged).
- `[ - <discord_config>, ... ]` (comma before `...`) is a reference.
- Defaults without `|` (`[ message: <tmpl_string> default = ... ]`) are stripped, and the continuation lines of
  pagerduty's multi-line `details` default are skipped.
- `{ <string>: <tmpl_string>, ... }` values (email `headers`, opsgenie/pagerduty `details`) have type `map`.
- Keys may contain upper case and `-` (https.md's `Content-Security-Policy` etc.).
- Recursion is cut instead of an error (below). No YAML output.

## Conventions and judgment calls
- **No `k=v` segments.** Nothing in the config selects a Go type by value: receiver integrations are chosen by field
  name (`receivers.slack_configs...`), and `type` keys (`responders.type`, `authorization.type`,
  `slack_configs.actions.type`) are plain strings.
- **Recursion:** `route.routes` (`[ - <route> ... ]`) isn't expanded and isn't listed, so the route keys appear once,
  as `route.*`. Child routes have the same keys. This matches `configkeys`, which never revisits a node on the
  current path (`configkeys/README.md`, "Cycles").
- **`mute_time_intervals`** (deprecated) references `<mute_time_interval>`, which has no section. It is expanded as
  `<time_interval>`, since the Go types have the same fields (`config/config.go:265-286`).
- **`times: [ - <time_range> ...]`** is defined only in prose ("specified like so: `start_time: HH:MM` /
  `end_time: HH:MM`"); the script supplies those two keys, typed `HH:MM` as written.
- **Lists of scalars** given as bare item lines are leaf keys typed by the item text (`templates`,
  `route.mute_time_intervals`, `weekdays`, ...), as in Prometheus. In https.md `[ - <string> ]` gives type `<string>`
  (`cipher_suites`, `curve_preferences`).
- **Maps:** `match`, `match_re`, `target_match*`, `source_match*`, `sns_configs.attributes`, `oauth2.endpoint_params`,
  `proxy_connect_header`, email `headers`, `details` and `basic_auth_users` have type `map`. No documented map has
  structured entries, so no key has a `<placeholder>` segment.
- **Shared blocks are expanded at every place:** `<http_config>` (36 keys, including `oauth2` and two `tls_config`s)
  under `global` and 11 receiver types: 432 of the 657 keys. `<tls_config>` (7 keys) appears 25 times.
- **https.md lists both files' `tls_server_config` in full**; they're separate roots, so the keys aren't merged.

## Types
Not normalized; the docs' own spelling (`<bool>` and `<boolean>` both occur). Classification for the workflow
(the module's `docs_kind` hook, in `../module.py`):
| Type | Meaning | Kind |
|---|---|---|
| `<string>`, `<filepath>`, `<filename>`, `<secret>`, `<tmpl_string>`, `<tmpl_secret>` | strings (`tmpl_*` are template-expanded; `<secret>` is `config.Secret`/`common.Secret`) | string |
| `HH:MM` | time of day (`timeinterval.TimeRange`, held as minutes `int`s in memory) | string |
| `<boolean>`, `<bool>` | boolean | bool |
| `<duration>`, `<int>` | duration (`model.Duration`), integer (`telegram chat_id`, `webhook max_alerts`) | number |
| `map` | map with user-chosen keys | map |
| `<string> ...`, `<filepath> ...`, `'[' <labelname>, ... ']'`, `'[' <string>, ... ']'`, `<matcher> ...` | list of strings (`matchers` are parsed into `labels.Matcher`s) | string |
| `<weekday_range> ...`, `<days_of_month_range> ...`, `<month_range> ...`, `<year_range> ...` | list of strings like `'monday:friday'`, `'1:5'` (parsed into `int` ranges) | string |

## Known gaps (docs vs code, from a check of yaml tags; left as-is)
In code (v0.27.0, common v0.46.0) but not documented, so not listed:
- `receivers.wechat_configs.http_config.*` (`WechatConfig.HTTPConfig`, `config/notifiers.go:526`).
- `receivers.victorops_configs.custom_fields` (`config/notifiers.go:645`) and `receivers.pushover_configs.url_title`
  (`config/notifiers.go:699`).
- From common's `HTTPClientConfig`: `bearer_token`, `bearer_token_file` (deprecated), `basic_auth.username_file`,
  and `tls_config.ca`, `cert`, `key` (inline PEM).
- Web config: the docs give five fixed header names under `http_server_config.headers`, but the code's field is
  `map[string]string` (exporter-toolkit `web/tls_config.go:106`).
