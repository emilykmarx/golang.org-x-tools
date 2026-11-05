# Prometheus

- **Checkout:** `/home/emily/projects/config_tracing/prometheus` (v3.2.1; on the prombench VM, the same path).
- **Module and root:** `github.com/prometheus/prometheus`, `/config.Config` (paramtrack's defaults).
- **Ignore file:** `ignore.txt` here: the 53 (parent, SD config) pairs the shipped config doesn't use. In scope:
  `kubernetes_sd_configs` under `scrape_configs` and `alerting.alertmanagers`, and `dns_sd_configs`,
  `file_sd_configs` and `static_configs` under `scrape_configs`. Pass it to `stats.py` and `scripts/score.py`.

## Developer knowledge
In the checkout, and in prometheus/common at the version it uses (`go list -m -f '{{.Dir}}' github.com/prometheus/common`):
- `docs/configuration/configuration.md`: every documented key, its type, default and meaning.
- `config/testdata/conf.good.yml`: a config that sets almost every option with valid values; the other
  `config/testdata/*.good.yml` cover special cases. The `*.bad.yml` files, with the errors `config/config_test.go`
  expects for them, show what's rejected.
- `documentation/examples/`: realistic configs, including each SD mechanism.
- The parsing and validation code, for what the docs leave out: `UnmarshalYAML` and `Validate` methods in
  `config/config.go`, `discovery/<sd>/`, `model/relabel/relabel.go`, and prometheus/common's `config/http_config.go`.
- configkeys/stats/STATISTICS.md, Q4: the keys the graph has but the docs don't, and why.

## Checking a config
- `promtool check config <file>`: parses and validates, and checks that files the config names exist, so run it
  where those files are (in the testbed's pod; TESTBED.md). `promtool` is in the Prometheus image.
- A reload: `POST <web prefix>/-/reload` (Prometheus runs with `--web.enable-lifecycle`), then
  `prometheus_config_last_reload_successful` must be 1. Some settings take effect only at startup (e.g.
  `storage.tsdb`, `runtime`), so finish with a restart.

## Keys to skip, or not to count
- **`*_ref` keys** (secret-manager references, e.g. `password_ref`): they parse, but fail at runtime, since Prometheus
  sets no secret manager.
- **Values stored transformed:** the search can't find them as written, so they add nothing to the statistics. Set
  them only where the module or the shipped config needs them:
  - regexes (`relabel.Regexp`: every `regex` in relabel configs): stored as `^(?s:<regex>)$`;
  - URLs (`config.URL`, e.g. `proxy_url`, `api_server`; some, like `remote_read.url`, also keep a string copy and are
    found);
  - inline PEM blocks (`tls_config`'s `ca`, `cert`, `key`): parsed;
  - `no_proxy`: parsed into a list.
- **Numbers and bools:** not searched (`--strings-only`), so they don't count. Leave them at the shipped config's
  values, or unset.

## Mutually exclusive keys
prometheus/common's `HTTPClientConfig.Validate` allows at most one auth method per HTTP client: `basic_auth`,
`authorization`, `oauth2`, or `bearer_token`/`bearer_token_file` (and, in `remote_write`, also `sigv4`, `azuread`,
`google_iam`). Within one, a value, its file and its `_ref` are exclusive (`username` / `username_file`,
`password` / `password_file`, `credentials` / `credentials_file`, `client_secret` / `client_secret_file`), as are
`ca` / `ca_file`, `cert` / `cert_file` and `key` / `key_file`. Cover the alternatives with extra `scrape_configs` or
`remote_write` entries.

## Values known to be safe (inert, but stored)
The shipped config (`prom-test-infra/prombench/fuzzed_prometheus.yaml` locally) already uses these patterns:
- a `proxy_url` that `no_proxy` bypasses for every address Prometheus contacts, so it's never dialed;
- extra HTTP headers (`http_headers`, `X-Fuzz-*`) and URL `params` that the targets ignore;
- relabel rules that only write new target labels (`action: replace` into an unused label), never drop targets;
- credentials and signatures that the receiver doesn't check (e.g. the remote write sink ignores sigv4).
New entries (scrape jobs, remote writes, alertmanagers) should target endpoints that exist and accept them, or fail
without affecting anything else (constraint 2: they may only add messages).
