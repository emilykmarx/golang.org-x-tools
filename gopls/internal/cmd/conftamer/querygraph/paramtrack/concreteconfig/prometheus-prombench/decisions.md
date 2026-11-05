# Decisions: Prometheus on prombench

The concreteconfig skill's memory between runs (SKILL.md, "Consistency"). Read it first and follow it; change a
decision only for a reason you record here. One row per full key or key pattern, in the full keys YAML's order where
possible. First written 2026-11-05 by a headless run starting from upstream's shipped config (TESTBED.md); the
archived fuzzed-base run (`../prometheus-prombench-fuzzed-base-2026-11-04/`) was read as an example only.

`gen.py` (next to this file) implements the rows below; rerunning it reproduces the config and its files byte for
byte (keys derived from seeds; no key is kept in the repo):
`uv run --with pyyaml --with pycryptodome --with cryptography python gen.py <shipped.yaml> <out>/config.yaml <out>/files`.
When a decision changes, change `gen.py` too, so the two stay in step.

## General rules

| Key or pattern | Decision | Value or rule | Reason |
|---|---|---|---|
| shipped config | keep its text unchanged; only insert keys into `global`, append list entries, and add top-level blocks | | Smallest diff; every shipped key keeps its value, so no behavior is taken away. Shipped SD configs are not touched: e.g. adding `namespaces` would turn `/api/v1/pods` into `/api/v1/namespaces/.../pods`, losing a baseline label. |
| coverage via new entries | rule | each section's keys are set in new list entries (alertmanagers, scrape jobs, remote writes, remote reads); the first new entry of each list holds the entry-wide keys (headers, proxy, relabel rules, enums) | Sections count once however many entries set them, so new entries reach the same coverage as editing shipped ones, without risk to the shipped behavior. |
| `*_ref` | skip | | Parses, but fails at runtime: Prometheus sets no secret manager (MODULE.md). |
| `*.regex`, `*.proxy_url`, `*.api_server`, `*.tls_config.{ca,cert,key}`, `*.no_proxy` | don't add for coverage | | Stored transformed (MODULE.md). Set only where needed: `api_server` in SD configs with HTTP client settings (`kubernetes.SDConfig.UnmarshalYAML`), `proxy_url`+`no_proxy` where `proxy_connect_header` needs them, `regex: up` in the remote writes' keep rule. Inline PEM (`ca`, `cert`, `key`) never set: the `*_file` forms cover the same sections. |
| number and bool keys | don't add | | Not searched (`--strings-only`). |
| enum-like string keys | set to the default (or the only value that works here) | `api_version: v2`, `scheme: http`, `metric_name_validation_scheme: utf8`, `scrape_protocols` = the default list, `fallback_scrape_protocol: PrometheusText0.0.4`, `protobuf_message: prometheus.WriteRequest`, `azuread.cloud: AzurePublic`, `tracing.client_type: grpc`, `tracing.compression: gzip`, `otlp.translation_strategy: UnderscoreEscapingWithSuffixes`, `dns_sd_configs.type: A`, relabel `action: replace` (or `keep` where it must filter), SD `role` = what the entry discovers | Can't be distinctive; the default changes nothing observable (objective 3). |
| free-form values | set | `value.py <concrete key>`; label names `--shape label`; map keys `value.py <full key with *>` (label shape for label names), their values `value.py <concrete key>` | SKILL.md "Values". |
| value index | rule | names (`job_name`, remote `name`): `--index` = list index; other keys: `--index` = number of earlier new entries setting the same concrete key | value.py's `--index`, made deterministic. |
| `*_file` keys (new) | set | `value.py --shape path`; the file is added to the ConfigMap under its token. Secrets: `<token>-content`; `ca_file`/`cert_file`: a throwaway self-signed cert (also its own CA); `key_file`: its key (both from `gen.py`'s `det_cert`, seed `ccfg-throwaway-tls`) | Files must exist and parse (promtool, TLS config loading). |
| log file keys (`query_log_file`, `scrape_failure_log_file`) | set | `value.py --shape path --dir /data` | Prometheus writes them; the ConfigMap mount is read-only, `/data` (the TSDB volume) is writable. |
| SA token / CA paths | reuse `/var/run/secrets/kubernetes.io/serviceaccount/{token,ca.crt}` where the API server must accept the request | | Only these authenticate to the API server; not distinctive (code literals), accepted since coverage outranks distinctiveness. |
| `proxy_url`, `no_proxy` | set where `proxy_connect_header` is | `proxy_url: http://<value.py proxy_url key>.invalid:3128`; `no_proxy: localhost,127.0.0.1,10.0.0.0/8,172.16.0.0/12,.svc,.cluster.local,<value.py no_proxy key>.invalid` | `no_proxy` bypasses the proxy for every address Prometheus contacts here (pod IPs 10.244/16, kind nodes 172.18/16, `*.svc`), so it's never dialed. Per-key hosts make each value distinct (candidate c1 had one value for 12 keys). |
| no traffic outside the testbed | rule | | Headless runs can't confirm outward-facing actions. Token endpoints point in-cluster (Loki, which answers 404); keys that would make Prometheus call public cloud endpoints are skipped (rows below). The VM's metadata address (169.254.169.254) is allowed for the Azure entries: it doesn't leave the VM's environment, and the user confirmed on 2026-11-04 that querying it is fine (recorded in the archived run's decisions). |

## global

| Key or pattern | Decision | Value or rule | Reason |
|---|---|---|---|
| `external_labels.*` | set | `ccfg_global_external_labels: <value.py>` | Adds a label to alerts and remote-written samples; no message removed. |
| `metric_name_validation_scheme`, `scrape_protocols` | set | defaults (`utf8`; the v3 default list) | Enum-like. |
| `query_log_file`, `scrape_failure_log_file` | set | `/data/<value.py>` | See log file rule. |
| numbers (`scrape_interval` kept at the shipped `5s`, others) | keep / don't add | | |

## alerting

| Key or pattern | Decision | Value or rule | Reason |
|---|---|---|---|
| `alert_relabel_configs.*` (top level) | set: one rule | `source_labels` (label shape), `separator`, `target_label` (label shape), `replacement` value.py; `action: replace` | Only writes a new label onto alerts (default regex matches the empty source value). |
| alertmanagers: new entries 1-9 | add | each: the same SD config (role pod, `api_server` kubernetes.default.svc:443, ns `default`, selector `app=alertmanager` / `status.phase=Running`, `bearer_token_file` = SA token, `ca_file` = SA CA) and `path_prefix: /alertmanager`, plus one auth alternative | Mutually exclusive auth methods need one entry each (MODULE.md). Same SD config => one shared watch; Alertmanager accepts any credentials. `path_prefix` makes them post to the same `/alertmanager/api/v2/alerts` as the shipped entry. Checked: alertmanagers discovered 1 -> 10. |
| entry 1: `api_version`, `scheme`, `http_headers.*.{values,secrets,files}`, `proxy_connect_header.*` (+ proxy), `relabel_configs.*`, `alert_relabel_configs.*`, `authorization.{type,credentials}`, `tls_config.{ca_file,cert_file,key_file,server_name}` | set | value.py / path; relabel rules as the top-level one | TLS settings unused (http). |
| entries 2-6: `authorization.credentials_file`; `basic_auth.{username,password_file}`; `basic_auth.{username_file,password}`; `bearer_token`; `bearer_token_file` | set | value.py / path | `bearer_token_file` gets an added file, so its value is distinct from the shipped SA token path. |
| entries 7, 8: `oauth2.*` | set (7 full: client_id, client_secret, scopes, token_url, endpoint_params, proxy_connect_header (+ proxy), tls_config files + server_name; 8: client_id, client_secret_file, token_url) | `token_url: http://loki.default.svc:3100/<value.py>` | Token fetch fails in-cluster (Loki 404), so these sets' notifications fail (`prometheus_notifications_errors_total` rises; same alertmanager label as the shipped set). They only add messages. |
| entry 9: `sigv4.{region,access_key,secret_key,profile}` | set | value.py | Alertmanager ignores signatures. |
| `sigv4.role_arn` | skip | | The signer would call AWS STS, outside the testbed. |
| `kubernetes_sd_configs.bearer_token_file` | set (shared SD) | SA token | Works, so discovery works. |
| `kubernetes_sd_configs.{authorization.{type,credentials}, tls_config.{cert_file,key_file,server_name}, http_headers.*, proxy_connect_header.*}` (entry 1); `basic_auth.{username,password}` (2); `basic_auth.{username_file,password_file}` (3); `bearer_token` (4); `authorization.credentials_file` (6); `oauth2.*` full (7) / secret file (8) | set, as a second SD config in those entries | value.py / path | Fail to authenticate (401, TLS name mismatch, token 404), so they discover nothing; the shared SD still finds the AM. |
| `kubernetes_sd_configs.kubeconfig_file` | set (entry 5's second SD) | path to an added kubeconfig (in-cluster server, SA CA and token file) | Works. Excludes `api_server` and HTTP client settings. |
| `kubernetes_sd_configs.{role, namespaces.names, selectors.*}` | set (all new SD configs) | `pod`, `default`, `role: pod`, `label: app=alertmanager`, `field: status.phase=Running` | Kubernetes syntax and real names, so not value.py; they scope discovery to the Alertmanager pod. |

## scrape_configs

| Key or pattern | Decision | Value or rule | Reason |
|---|---|---|---|
| new jobs (list index 4-11) | add | `job_name` value.py index 4-11, `static_configs.targets: [loki.default.svc:3100]`, one auth alternative, one or two SD configs (role service, ns `default`, selector `app=loki` / `metadata.name=loki`, `api_server` kubernetes.default.svc.cluster.local:443) | Loki accepts any credentials and serves /metrics. Targets and selectors are real names (they must work), so not value.py. `api_server` differs from the alertmanagers' so the value is distinct (same API server, a name in its cert). |
| job 4: `scheme`, `fallback_scrape_protocol`, `metric_name_validation_scheme`, `scrape_protocols` | set | enum defaults | |
| job 4: `params.*`, `scrape_failure_log_file`, `http_headers.*`, `proxy_connect_header.*` (+ proxy), `static_configs.labels.*`, `relabel_configs.*`, `metric_relabel_configs.*`, `authorization.{type,credentials}`, `tls_config.{ca_file,cert_file,key_file,server_name}` | set | value.py / path (`/data` for the log); relabel rules only write a new label | Loki ignores unknown params and headers; TLS unused (http). |
| job 4: `dns_sd_configs.{names,type}` | set | `loki-headless.default.svc.cluster.local`, `A` (port 3100) | A real name, not value.py's `.invalid`: the discovered target is scraped, so the key influences messages. Distinct anyway (not a code literal, one key). |
| job 4: `file_sd_configs.files` | set | value.py path + `.json`, an added file listing the Loki target | File SD names must end in .json/.yml/.yaml (`discovery/file`). |
| job 4: `kubernetes_sd_configs.authorization.credentials_file` (SA token, works); second SD: `authorization.{type,credentials}`, `tls_config.{cert_file,key_file,server_name}`, `http_headers.*`, `proxy_connect_header.*` | set | | Second SD fails to authenticate. `authorization.type` left unset in the working one (default Bearer) rather than the literal `Bearer`. |
| jobs 5-11: `basic_auth.{username,password_file}` (5), `basic_auth.{username_file,password}` (6), `bearer_token` + `metrics_path` (7), `bearer_token_file` (8), `authorization.credentials_file` (9), `oauth2.*` full (10), `oauth2` secret file (11) | set | value.py / path | `metrics_path: /<value.py>` (job 7) rather than the literal `/metrics`: Loki answers 404, so job 7 is down; a new job, so nothing is lost. oauth2 jobs are down (token 404). |
| their SD alternatives: `basic_auth.{username,password}` (5), `basic_auth` files (6), `bearer_token` (7), `bearer_token_file` = SA token (8, works), `kubeconfig_file` (9, works) + `authorization.credentials_file` (9, fails), `oauth2` full (10) / secret file (11) | set | | As for alertmanagers. |

## remote_write

| Key or pattern | Decision | Value or rule | Reason |
|---|---|---|---|
| new entries 0-11 | add | `url` = the prombench sink (`prometheus-sink.prombench-16075.svc:9011/sink/prw`), `name` value.py index 0-11, `write_relabel_configs` keeping only `up`, one auth alternative | The sink checks no credentials; `up` keeps the added load small. The shipped config has no remote write, so every remote write message is new. |
| entry 0: `headers.*`, `protobuf_message`, `http_headers.*`, `proxy_connect_header.*` (+ proxy), a second `write_relabel_configs` rule (as the relabel rules above), `authorization.{type,credentials}`, `tls_config.{ca_file,cert_file,key_file,server_name}` | set | value.py / path | |
| entries 1-5: `authorization.credentials_file`; `basic_auth.{username,password_file}`; `basic_auth.{username_file,password}`; `bearer_token`; `bearer_token_file` | set | value.py / path | |
| entry 6: `google_iam.credentials_file` | set | path to a service account JSON with a throwaway RSA key (`det_rsa`, seed = the token) and an in-cluster `token_uri` (Loki) | Token fetch fails in-cluster (404); nothing leaves the testbed. |
| entry 7: `azuread.cloud`, `azuread.managed_identity.client_id` | set | `AzurePublic` (enum, default); client_id = uuid5(NAMESPACE_URL, value.py token) | client_id must be a UUID (`AzureADConfig.Validate`), so not value.py as is. Token from 169.254.169.254 fails. |
| entry 8: `azuread.sdk.tenant_id` | set | value.py | |
| `azuread.oauth.*` | skip | | Token requests go to login.microsoftonline.com (outside the testbed; can't be redirected). |
| entries 9, 10: `oauth2.*` | set (9 full, 10 secret file) | as for alertmanagers | |
| entry 11: `sigv4.{region,access_key,secret_key,profile}` | set | value.py | The sink ignores signatures. |
| `sigv4.role_arn` | skip | | AWS STS, as for alertmanagers. |
| note: reload time | accepted | | Entries that can't send (6-10) take up to the flush deadline (1 min for samples + 1 min for metadata) to stop when a reload changes them or removes them (e.g. going back to the shipped config: several minutes). paramtrack's reloads don't change them (they toggle a bool in the first scrape job), so its runs are unaffected. Seen in candidate c2's reload over c1: `remote_storage=2m0s`. |

## remote_read

| Key or pattern | Decision | Value or rule | Reason |
|---|---|---|---|
| new entries 0-7 | add | `url: http://<value.py remote_read.url --index N>.invalid/api/v1/read`, `name` index N, `required_matchers` with its own label (label shape) and value | Inert: no query has the matcher, so they're never read (and the URL doesn't resolve). Nothing in the testbed serves remote read to prometheus-meta that a query would use. |
| entry 0: `headers.*`, `http_headers.*`, `proxy_connect_header.*` (+ proxy), `authorization.{type,credentials}`, `tls_config` files + `server_name` | set | | |
| entries 1-7: `authorization.credentials_file`, `basic_auth` (two forms), `bearer_token`, `bearer_token_file`, `oauth2` full / secret file | set | | |

## Other top-level keys

| Key or pattern | Decision | Value or rule | Reason |
|---|---|---|---|
| `rule_files` | keep the shipped value | | Already set. |
| `scrape_config_files` | set | `value.py --shape path` (no such file) | Globbed; matching nothing adds no jobs. |
| `tracing.{endpoint, client_type, compression, headers.*, tls_config.{ca_file,cert_file,key_file,server_name}}` | set | `<value.py>.invalid:4317`, `grpc`, `gzip`, value.py / path | Inert: the endpoint doesn't resolve and `sampling_fraction` is left at 0. TLS files are loaded at reload, so they must be valid. |
| `otlp.promote_resource_attributes`, `otlp.translation_strategy` | set | value.py; `UnderscoreEscapingWithSuffixes` (default) | Inert: the OTLP receiver isn't enabled. |
| `storage.*`, `runtime.gogc` | skip | | Numbers only. |
