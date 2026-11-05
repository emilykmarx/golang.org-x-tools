# Decisions: Prometheus on prombench (archived)

Archived 2026-11-05: this run extended the user's fuzzed config, which was then the shipped config. The shipped config
is now upstream's (the skill's testbeds/prombench/TESTBED.md), and runs start from it and decide for themselves. This
file is an example for them (MODULE.md, "Developer knowledge"), not decisions to follow.

The concreteconfig skill's memory between runs (SKILL.md, "Consistency"). Read it first and follow it; change a
decision only for a reason you record here. One row per full key or key pattern, in the full keys YAML's order where
possible.

`gen.py` (next to this file) implements the rows below: the shipped config's text unchanged, with new list entries
appended. Rerunning it reproduces the 2026-11-04 run's config exactly, and its files except one:
`uv run --with pyyaml --with pycryptodome python gen.py prom-test-infra/prombench/fuzzed_prometheus.yaml
<out>/config.yaml <out>/files`. The exception is the fake google_iam credentials file: its throwaway RSA key is now
derived deterministically from a seed (`deterministic_rsa_key`), where the run used a generated `rsa_key.pem`, so as
not to keep a private key in the repo. When a decision changes, change `gen.py` too, so the two stay in step.

## General rules

| Key or pattern | Decision | Value or rule | Reason |
|---|---|---|---|
| `*_ref` | skip | | Parses, but fails at runtime: Prometheus sets no secret manager (MODULE.md). |
| `*.regex`, `*.proxy_url`, `*.api_server`, `*.tls_config.{ca,cert,key}`, `*.no_proxy` | keep the shipped config's values; don't add for coverage | | Stored transformed, so the search can't find them (MODULE.md). Added only where needed: `api_server` for SD configs with HTTP client settings, `proxy_url`+`no_proxy` (the shipped bypass) where `proxy_connect_header` needs them, `regex: up` in new remote writes' relabel rule. |
| number and bool keys | keep the shipped config's values; don't add | | Not searched (`--strings-only`). |
| shipped config | keep its text unchanged; only append list entries and top-level blocks | | Smallest diff; every shipped key keeps its value, so no behavior is taken away. |
| no traffic outside the testbed | rule | | Headless runs can't confirm outward-facing actions. Token endpoints point in-cluster; keys that would make Prometheus call public cloud endpoints are skipped (rows below). The VM's metadata address (169.254.169.254, Google's metadata server on the GCE VM) is allowed (Azure managed identity / SDK probes in remote writes 8 and 9): it doesn't leave the VM's environment, and the user confirmed on 2026-11-04 that querying it is fine. |
| value index | rule | `value.py <key> --index N`, N = list index of the entry for names (`job_name`, remote `name`, `url`, `required_matchers`); for other keys, N = number of earlier new entries setting the same concrete key (0 => no suffix) | value.py's `--index` rule, made deterministic. |
| `*_file` keys (new) | set | `value.py --shape path`; file added to the ConfigMap, named by the token. Content: secrets `<token>-content`; `ca_file`/`cert_file` the shipped config's throwaway cert (self-signed, so also a CA); `key_file` its key | Files must exist and parse (promtool, TLS config loading). |
| SA token / CA paths | reuse `/var/run/secrets/kubernetes.io/serviceaccount/{token,ca.crt}` where the API server must accept the request | | Only these authenticate to the API server; not distinctive (shared with shipped values), accepted since coverage outranks distinctiveness. |
| map keys (`*`) | set | `value.py <full key with *>` (label shape for label names), value `value.py <concrete key>` | SKILL.md "Values". |

## alerting.alertmanagers

| Key or pattern | Decision | Value or rule | Reason |
|---|---|---|---|
| new entries 1-8 | add | each: one shared SD config (role pod, ns default, selector `app=alertmanager`, `bearer_token_file` = SA token, `ca_file` = SA CA), `path_prefix: /alertmanager`, one auth alternative | Mutually exclusive auth methods need one entry each (MODULE.md). Same SD => one shared watch; AM accepts any credentials. |
| `authorization.{type,credentials}`, `tls_config.{cert_file,key_file}` | set (entry 1) | value.py | |
| `authorization.credentials_file` | set (entry 2) | path | |
| `basic_auth.{username,password_file}` / `{username_file,password}` | set (entries 3, 4) | value.py / path | |
| `bearer_token` | set (entry 5) | value.py | |
| `oauth2.*` | set (entry 6 full: client_id, client_secret, scopes, token_url, endpoint_params, proxy_connect_header (+ shipped proxy bypass), tls_config files + server_name; entry 7: client_id, client_secret_file, token_url) | `token_url: http://loki.default.svc:3100/<value>` | Token fetch fails in-cluster (Loki 404), so notifications from these sets fail (`prometheus_notifications_errors_total` rises; same alertmanager label as the shipped set). Accepted: they only add messages. |
| `sigv4.{region,access_key,secret_key,profile}` | set (entry 8) | value.py | AM ignores signatures; a missing profile doesn't break static credentials (checked: reload ok, sends). |
| `sigv4.role_arn` | skip | | The signer would call AWS STS (outside the testbed), and signing would fail. |
| `kubernetes_sd_configs.bearer_token_file` | set (shared SD of entries 1-8) | SA token | Works, so discovery works. |
| `kubernetes_sd_configs.{authorization.credentials, basic_auth.*, bearer_token, oauth2.*, tls_config.{cert_file,key_file}}` | set, as a second SD config in entries 1-4, 6, 7 | value.py / path | Fail to authenticate (401/403/token 404), discover nothing; the shared SD still finds the AM. |
| `kubernetes_sd_configs.kubeconfig_file` | set (entry 5's second SD) | path to an added kubeconfig (in-cluster server, SA CA and token file) | Works. Excludes `api_server` and HTTP client settings (`kubernetes.SDConfig.UnmarshalYAML`). |

## scrape_configs

| Key or pattern | Decision | Value or rule | Reason |
|---|---|---|---|
| new jobs 5-9 | add | `job_name` value.py index 5-9, `static_configs.targets: [loki.default.svc:3100]`, one auth alternative, extra SD configs (role service, selector `metadata.name=loki`) | Loki accepts any credentials. Selector values are Kubernetes field selectors, so not value.py. |
| `authorization.credentials_file`, `tls_config.{cert_file,key_file}` | set (job 5) | path | |
| `basic_auth.{username_file,password}` | set (job 6) | | |
| `bearer_token` | set (job 7) | | |
| `oauth2.*` | set (job 8 full, job 9 client_secret_file) | as for alertmanagers | Scrapes fail (`up` 0) on the token fetch; new jobs, so nothing is lost. |
| `kubernetes_sd_configs.*` alternatives | set: authorization.credentials + tls cert/key files and basic_auth username/password (job 5), basic_auth files and bearer_token (job 6), bearer_token_file = SA token (job 7, works), oauth2 full + kubeconfig_file (job 8, kubeconfig works), oauth2 secret file (job 9) | | As for alertmanagers. |

## remote_write

| Key or pattern | Decision | Value or rule | Reason |
|---|---|---|---|
| new entries 2-12 | add | sink URL, `name` value.py index 2-12, `write_relabel_configs` keeping only `up`, one auth alternative | The sink checks no credentials; `up` only keeps the added load small. |
| `authorization.*` + `tls_config.{ca_file,cert_file,key_file}`; `authorization.credentials_file`; `basic_auth.{username,password_file}`; `bearer_token`; `bearer_token_file` | set (entries 2-6) | value.py / path (fake token files) | |
| `google_iam.credentials_file` | set (entry 7) | path to a service account JSON with a throwaway RSA key and in-cluster `token_uri` | Sends (self-signed JWT); nothing outside the testbed. |
| `azuread.cloud`, `azuread.managed_identity.client_id` | set (entry 8) | `AzurePublic` (enum, default); client_id = uuid5(NAMESPACE_URL, value.py token) | client_id must be a UUID (`AzureADConfig.Validate`), so not value.py as is. |
| `azuread.sdk.tenant_id` | set (entry 9) | value.py | |
| `azuread.oauth.*` | skip | | Token requests go to login.microsoftonline.com (outside the testbed; can't be redirected). |
| `oauth2.*` | set (entries 10 full, 11 secret file) | as for alertmanagers | |
| `sigv4.{region,access_key,secret_key}` index 1, `sigv4.profile` | set (entry 12) | value.py | Kept out of the shipped sigv4 entry so that one's behavior doesn't change. |
| `sigv4.role_arn` | skip | | AWS STS, as for alertmanagers. |

## remote_read

| Key or pattern | Decision | Value or rule | Reason |
|---|---|---|---|
| new entries 1-7 | add | `url: http://<value.py remote_read.url --index N>.invalid/api/v1/read`, `name` index N, `required_matchers` with its own label and value (value.py, label shape for the name) | Inert: no query has the matcher, so they're never read (and the URL doesn't resolve). |
| auth alternatives | set: authorization.credentials_file + tls files (1), basic_auth username/password_file (2), username_file/password (3), bearer_token (4), bearer_token_file (5), oauth2 full (6), oauth2 secret file (7) | | |

## tracing

| Key or pattern | Decision | Value or rule | Reason |
|---|---|---|---|
| `tracing.endpoint` | set | `<value.py>.invalid:4317` | Doesn't resolve; `sampling_fraction` left at 0, so nothing is exported anyway. |
| `tracing.client_type`, `tracing.compression` | set | `grpc` (default), `gzip` (only valid value) | Enum-like; can't be distinctive. |
| `tracing.headers.*`, `tracing.tls_config.{ca_file,cert_file,key_file,server_name}` | set | value.py / path | TLS files are loaded at reload, so they must be valid. |
