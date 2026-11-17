# Documented config keys: Grafana provisioning files

The documented keys of Grafana's provisioning YAML files, in the format of
`../../prometheus/documentedkeys/prometheus_config_keys.txt`: one key per line, `<dotted key>\t<documented type>`.
They are meant to be compared with the full keys the static analysis finds (see "Full key" in
`configkeys/stats/TERMINOLOGY.md`). Conventions not restated here follow `../../prometheus/documentedkeys/README.md`
and `../../caddy/documentedkeys/README.md`.

## What "the config" is (and what's in scope)
Grafana v13.1.0 (`/home/emily/projects/config_tracing/grafana-paramtrack`, tag v13.1.0, commit `b309c9bb3b81`) has
these user-written inputs:
- **Provisioning YAML: in scope.** These files live in the directory named by `[paths] provisioning` (the testbed sets
  it with `GF_PATHS_PROVISIONING`). Each of the four kinds is read from its subdirectory with `go.yaml.in/yaml/v3`
  into structs with `yaml` tags. Their fields are `values.*` types, whose `UnmarshalYAML` expands `$ENV` variables.

  | Kind | Reader (`pkg/services/provisioning/...`) | Root type(s) | List |
  |---|---|---|---|
  | datasources | `datasources/config_reader.go` | `configVersion`, then `configsV1` (`configsV0` if `apiVersion` is 0) | `datasources_keys.txt` |
  | dashboards | `dashboards/config_reader.go` | `configVersion`, then `configV1` / `configV0` | `dashboards_keys.txt` |
  | plugins | `plugins/config_reader.go` | `pluginsAsConfigV0`, the only version (snake_case `org_id`, as documented), mapped to `pluginsAsConfig` | `plugins_keys.txt` |
  | alerting | `alerting/config_reader.go` | `AlertingFileV1` (`.yaml`, `.yml` or `.json` files, all through `yaml.Unmarshal`) | `alerting_keys.txt` |

  The type names are from a quick read of the readers. Check them before using them as `ROOTS`. The testbed provisions
  2 datasources (`prometheus`, `loki`) and 1 dashboard provider (`type: file`).
- **grafana.ini (INI): out of scope for now** (the researcher's decision, 2026-11-16). `setting.(*Cfg).Load` loads it
  in `loadConfiguration` (`pkg/setting/setting.go:1313`), in this order:
  1. `ini.Load` (gopkg.in/ini.v1) of `conf/defaults.ini`;
  2. the `--config` file, merged on top;
  3. `GF_<SECTION>_<KEY>` environment variables;
  4. `cfg:section.key=value` command-line arguments.

  Values are then read **one at a time by string name**, e.g. `iniFile.Section("server").Key("http_port")`, in
  `readServerSettings` and friends, and by other packages through `cfg.Raw.Section(...)` (408 `Section("` call sites
  under `pkg/`). The INI is **never struct-decoded**: there is no `MapTo`, and no `ini:` tags. A struct-tag or
  unmarshaler-graph analysis therefore sees none of its keys. For later: `conf/defaults.ini` has 114 sections and
  about 870 keys (2,509 lines). The reference doc `docs/sources/setup-grafana/configure-grafana/_index.md` covers
  87 sections and about 440 keys, without types. The testbed sets 6 INI keys, all via `GF_*` env vars.
- **Also out of scope:**
  - LDAP TOML (`pkg/services/ldap/settings.go`, BurntSushi/toml).
  - Dashboard JSON files: dashboard content, which the dashboard provider loads.
  - RBAC provisioning (`provisioning/access-control`), which is Grafana Enterprise only.

## Files
| File | What |
|---|---|
| `list_provisioning_keys.py` | Reads the docs (below) and writes all the lists |
| `datasources_keys.txt` | Datasource file keys, with `jsonData`/`secureJsonData` keys as `datasources.jsonData.<key>` (84). **Primary list** |
| `datasources_keys_by_type.txt` | Same, but each `jsonData`/`secureJsonData` key once per plugin it's documented for: `datasources.type=<plugin id>.jsonData.<key>` (347) |
| `datasources_collapsed.tsv` | The `jsonData`/`secureJsonData` keys documented for several plugins, i.e. collapsed in the primary list: key, count, plugin IDs (30) |
| `dashboards_keys.txt` | Dashboard provider file keys (11) |
| `plugins_keys.txt` | Plugin app file keys (7) |
| `alerting_keys.txt` | Alerting file keys: rules, contact points, templates, policies, mute timings, and their deletes (156) |

## Sources
All are git-tracked files in the checkout above, so nothing is fetched or cached. They match the v13.1.0 image the
testbed runs.
- `docs/sources/administration/provisioning/index.md`:
  - the example YAML blocks under "Example data source configuration file", "Custom HTTP headers for data sources",
    "Example plugin configuration file" and "Example dashboard configuration file";
  - the "JSON data" and "Secure JSON data" tables (key, type, data sources).
- `docs/sources/alerting/set-up/provision-alerting-resources/file-provisioning/index.md`: the YAML blocks under the
  five "Import ..." sections, and one block per contact point integration under "Settings".
- `docs/sources/datasources/_index.md`, section "Built-in core data sources": expands the tables' `_HTTP*_` (see below).
- `public/app/plugins/datasource/*/plugin.json`: only to check the hard-coded plugin IDs. Elasticsearch and Zipkin
  are external plugins in v13 and have no plugin.json here. The script prints them.

There's no machine-readable source: no schema, and no generated reference for the provisioning files. The docs give
each file as an example YAML block, with `# <type> description` comments before the keys. A YAML library can't read
these blocks: they aren't all valid YAML (e.g. Slack's `color: {{ if ... }}`), and the types are in comments, which
ruamel.yaml keeps but can't parse out of an invalid block. The Prometheus script's parser expects Prometheus' `[ key:
<type> ]` notation. So `list_provisioning_keys.py` is a small line-based parser (indentation plus `- ` items), using
only the standard library.

## Rerun
```sh
cd /home/emily/projects/config_tracing/golang.org-x-tools/gopls/internal/cmd/conftamer/querygraph/paramtrack/modules/grafana/documentedkeys
timeout 60 python3 -I list_provisioning_keys.py   # --grafana-dir <checkout> --out-dir <dir>
```
It runs in under a second and overwrites the lists here.

## Types
The types are the docs' own text, not normalized:
- **From `# <...>` comments**, with `, required`/`, optional` dropped: `string`, `int`, `bool`, `duration` (Go
  duration string, e.g. `60s`), `list` (a YAML list of scalars), and `map` (a `<map<string, string>>` of unstructured
  pairs: `groups.rules.annotations` and `.labels`).
- **From the datasource tables:** `string`, `boolean`, `number`, `array`.
- **`map` for opaque objects** whose example children aren't keys:
  - `apps.jsonData` and `apps.secureJsonData`: "Custom per app", and the example child is literally `key: value`;
  - `groups.rules.data.model`: the query model, "should be obtained through the API", whose keys depend on the
    datasource.
- **`example:<kind>`:** the key has no type comment, so the kind comes from the example value: `example:string`,
  `example:int`, `example:bool`, or `example:list` (a flow list or bare `- x` items). This covers `apiVersion`,
  `prune`, `deleteDatasources.*`, `groups.rules.data.refId`/`datasourceUid`, `muteTimes.time_intervals.*`,
  `resetPolicies`, Slack's `text`, and the custom HTTP header keys.

For the workflow's classification:
- string: `string`, `duration`, `example:string`
- number: `int`, `number`, `example:int`
- bool: `bool`, `boolean`, `example:bool`
- map: `map`
- lists of strings: `list`, `array`, `example:list`

## Conventions and judgment calls
- **Each list is rooted at its file's top level,** e.g. `datasources.url`, `providers.options.path` and
  `groups.rules.uid`. `apiVersion` is listed in each list.
- **Leaves only.** Lists are invisible, as in the other modules. Groups such as `datasources.jsonData`,
  `providers.options`, `contactPoints.receivers.settings` and MQTT/webhook `tlsConfig` (documented as a plain `<map>`
  with child keys) are not listed. Their children are.
- **apiVersion:** only the v1 keys are listed (camelCase, e.g. `orgId`, `basicAuth`), with no `apiVersion=1` segment.
  The docs don't cover v0 files (snake_case: `org_id`, `basic_auth`, `json_data`, ...), although the code still reads
  them (`configsV0`).
- **Datasource `jsonData`/`secureJsonData`:** the core code decodes these into generic maps, and each plugin reads its
  own keys. Two variants:
  - `datasources_keys.txt` has no `k=v`: `datasources.jsonData.tlsSkipVerify`.
  - `datasources_keys_by_type.txt` adds `type=<plugin id>`, e.g. `datasources.type=prometheus.jsonData.httpMethod`.
    The other datasource keys, such as `datasources.url`, have no `k=v` in either list. `datasources.type` itself is
    listed in both.
  - The plugins for each key come from the table's "Data source" column, mapped to plugin IDs by a hard-coded table
    in the script (e.g. Amazon CloudWatch is `cloudwatch`, PostgreSQL is `grafana-postgresql-datasource`).
    - `_HTTP*_` means, per the docs' note, "all core data source plugins except MySQL, PostgreSQL, and MSSQL". That is
      the 16 plugins in the "Built-in core data sources" list minus those three. It includes TestData, taken
      literally.
    - The custom header keys (`httpHeaderName1/2`, `httpHeaderValue1/2`) aren't in the tables, so they get the
      `_HTTP*_` set. The docs show only 1 and 2 of the `httpHeaderName<N>` pattern, and those are what's listed.
- **Keys that collapse in the primary list** (`datasources_collapsed.tsv`, 30 keys):
  - **Shared HTTP options, 16–19 plugins:** `tlsSkipVerify`, `tlsAuth`, `tlsAuthWithCACert`, `serverName`,
    `timeout`, `keepCookies`, the custom headers, and `secureJsonData.{tlsCACert,tlsClientCert,tlsClientKey,
    basicAuthPassword}`.
    - For the HTTP plugins these look like **the same setting, read by shared code**: e.g. `tlsSkipVerify` is read
      once in `pkg/services/datasources/service/datasource.go` to build every HTTP datasource's client.
    - MySQL, PostgreSQL and MSSQL decode the same names into **their own tagged structs** (`pkg/tsdb/*/sqleng/
      sql_engine.go`, `json:"tlsSkipVerify"`). The meaning is the same, but the code and Go types differ, so a
      code-side analysis may see per-plugin keys there.
  - **`secureJsonData.password`, 19 plugins: not clearly one setting.** For MySQL, PostgreSQL and MSSQL it's the
    database password (`DecryptedSecureJSONData["password"]` in each plugin). For HTTP plugins it's the legacy
    password field, used only by some (e.g. InfluxDB, Elasticsearch).
  - **`timeInterval`, 6 plugins:** the same meaning (minimum query interval) everywhere.
  - **`maxOpenConns`, `maxIdleConns`, `connMaxLifetime`, `sslRootCertFile`:** the SQL plugins' connection settings.
    The meaning is the same, but each SQL plugin decodes them separately.
  - **`sigV4*`, `manageAlerts`, `alertmanagerUid`, 2 plugins each:** the same feature (the SDK's SigV4 middleware;
    alerting's ruler integration).

  Overall, no collapsed key mixes unrelated settings, except arguably `password`. The SQL plugins' per-plugin structs
  are the case where `type=` segments would match the code better.
- **Contact points:** each integration's settings are under `contactPoints.receivers.type=<type>.settings.<key>`,
  e.g. `contactPoints.receivers.type=slack.settings.url`. The selector `contactPoints.receivers.type` is listed too.
  The generic example's `contactPoints.receivers.settings.url` is dropped, since `prometheus-alertmanager`'s block
  covers it.
- **Dashboard provider `type`:** no `k=v`. The only provider type is `file` (`dashboards/dashboard.go`). `options` is
  a map that the file reader reads.
- **Recursion:** `policies.routes` (child policies, "the same as the root policy") appears only commented out in the
  docs. It isn't expanded or listed: its keys would be `policies.routes.<root policy key>`, at any depth.
- **Not used:** the "Supported settings" section of `administration/provisioning/index.md`, which covers legacy alert
  notifiers and `secure_settings`. The v13 code has no `notifiers` provisioner, and the alerting doc above covers
  today's contact points. The "Template variable interpolation" example block in the alerting doc is not used either,
  since it repeats keys without a `type`.

## Known gaps (code vs docs)
These are `yaml` tags in `pkg/services/provisioning/{datasources,dashboards,plugins,alerting}` with no matching
documented key segment. They come from a quick grep, not a full comparison:
- **datasources:** `correlations`, plus the v0 snake_case names.
- **alerting** (rule and policy fields the docs' examples omit): `isPaused`, `keepFiringFor`,
  `missing_series_evals_to_resolve`, `notification_settings`, `record` (recording rules: `metric`, `from`,
  `targetDatasourceUid`), `relativeTimeRange` (named in the docs' prose only), `queryType`, `active_time_intervals`,
  and `dasboardUid` (a "grandfathered typo" alias of `dashboardUid`).
- **Contact point settings** are not struct-tagged in the provisioning package, since they're decoded per integration
  later. The docs give 19 integrations. The code may support more (e.g. SNS, Webex, Oncall), and more settings
  per integration.
