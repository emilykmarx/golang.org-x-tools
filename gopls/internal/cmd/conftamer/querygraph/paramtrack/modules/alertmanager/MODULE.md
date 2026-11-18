# Alertmanager

The concreteconfig skill's module file for Prometheus' Alertmanager (`github.com/prometheus/alertmanager`). What its
config is, its config sources and how the documented keys were listed are in
[documentedkeys/README.md](documentedkeys/README.md); module.py has the paramtrack settings.

- **Checkout:** `/home/emily/projects/config_tracing/alertmanager-paramtrack` (v0.27.0, the version prombench runs;
  same path locally and on the prombench VM). prometheus/common and exporter-toolkit are in the Go module cache: `go
  list -m -f '{{.Dir}}' github.com/prometheus/common` (and `.../exporter-toolkit`) in the checkout.
- **Module, config sources and format:** `--module alertmanager`, YAML; the skill writes a config for each config
  source it sets:
  - `config`: the config file (`--config.file`, `config.Config`);
  - `web`: the web config (`--web.config.file`, exporter-toolkit's `web.Config`); prombench has none, so its
    starting config is an empty one (TESTBED.md);
  - `cluster_tls` is out of scope: its keys are all in its ignore file (the testbed runs without clustering).
- **Ignore files:** per config source, in module.py's `CONFIG_SOURCES`, each with a comment saying why: `config`:
  [ignore-config.txt](ignore-config.txt) (email: SMTP, which no message carries); `web`: [ignore-web.txt](ignore-web.txt)
  (TLS, http2, basic auth).
- **Transformed-keys file:** `config` has [transformed-config.txt](transformed-config.txt): the URL, matcher and
  time-interval range keys, whose values are stored parsed (its comment says how), so they aren't target keys. Set them
  where the config needs them (every URL, to keep sends inside the pod); they don't count.
- **An earlier config:** `gopls/results/alertmanager/concreteconfig-2026-11-17/` (this skill's first run on
  Alertmanager): `work/gen.py` writes its configs and files, and `diff.md` gives each key's reason. Reuse its code if it
  helps (copy it into your own directory), but check its choices rather than copying them.

## Developer knowledge
In the checkout:
- `docs/configuration.md`: every key of the config file, its type, default and meaning; `docs/https.md`: the web
  config's keys.
- `config/testdata/conf.good.yml` (and the other `conf.*.yml`, with what `config/config_test.go` expects of them): what
  parses and what's rejected.
- `doc/examples/simple.yml`: a realistic config.
- The parsing and validation code, for what the docs leave out: the `UnmarshalYAML` methods in `config/config.go` and
  `config/notifiers.go` (defaults, the global settings receivers inherit, "at most one of" checks), and
  prometheus/common's `config/http_config.go` (`http_config`).
- configkeys/stats/STATISTICS.md, Q4, and module.py's `graph_docs_reason`: the keys the graph has but the docs don't,
  and why.

## Checking a config
- `amtool check-config <file>` (in the Alertmanager image): parses and validates the config file and loads its
  templates. Run it in the pod, where the files it names are (TESTBED.md: `cc_vm.py check`).
- A reload: `POST /alertmanager/-/reload`; then `alertmanager_config_last_reload_successful` must be 1. Some settings
  only apply at startup (the cluster and storage flags, not in the config), but finish with a restart anyway.
- The web config has no check command: exporter-toolkit reads it at startup (Alertmanager exits if it's invalid) and
  then on every request, so check that `/-/ready` still answers after setting it, and after a restart.

## Keys to skip, or not to count
- **Anything that notifies third parties** (constraint 2): the integrations send to outside services by default
  (`pagerduty`, `opsgenie`, `wechat`, `victorops`, `telegram` and `webex` to the defaults in
  `config.DefaultGlobalConfig`; `sns` to AWS; `pushover` to its fixed API), or to the URL you set. Don't leave any
  that an alert reaches pointed outside; "Exercising integrations" says how to make them send inside the cluster.
- **Numbers, bools and durations** (`model.Duration` is an int): not searched (`--strings-only`), so they don't count.
  Leave them at the shipped config's values, or unset.
- **`web` config source:** the ignore file's keys (TLS, http2, basic auth) need the other modules' configs changed.
- **Unreachable in v0.27.0:**
  - `discord_configs.webhook_url_file` and `msteams_configs.webhook_url_file`: `config.Load` requires `webhook_url`
    for both ("no discord webhook URL provided"), and setting both is an error.
  - `route.match`, `match_re`, `matchers`, `mute_time_intervals` and `active_time_intervals`: the root route can't
    have them (`config.Load`), and a child route's keys are `route.routes.*`, which aren't these full keys (the graph
    cuts the route's recursion at `route.routes`).
  - Every `webex_configs.http_config` auth method but `authorization`: webex requires `authorization` (its API's
    bearer token), and only one method is allowed.

## Mutually exclusive keys
- prometheus/common's `HTTPClientConfig.Validate` (each `http_config`): at most one auth method (`basic_auth`,
  `authorization`, `oauth2`, `bearer_token`/`bearer_token_file`); within one, a value and its file are exclusive
  (`password`/`password_file`, `credentials`/`credentials_file`, `client_secret`/`client_secret_file`), as are
  `ca`/`ca_file`, `cert`/`cert_file` and `key`/`key_file`.
- Each secret and its `_file` ("at most one of ... must be configured" in `config/config.go` and
  `config/notifiers.go`), in `global` (`slack_api_url`, `opsgenie_api_key`, `victorops_api_key`)
  and in the receivers (`url`, `api_url`, `webhook_url`, `api_key`, `routing_key`, `service_key`, `user_key`, `token`,
  `bot_token`).
- Cover the alternatives with extra receivers, or extra entries in a receiver's `*_configs` list.

## Exercising integrations
The workflow learns about a key from the messages its value is found on, so an integration's settings count only if
it sends. An integration that never sends (e.g. its receiver is in no route: Alertmanager doesn't even build those,
"skipping creation of receiver not referenced by any route") sets keys without giving them evidence. So:
- **Route an alert to it:** patch 0004's `benchmarkPrometheusAlwaysFiring` alert fires all the time. Routes that
  match it with `continue: true`, placed before the shipped route (below), let it reach several receivers and still
  the shipped one. Each receiver can have several `*_configs` entries.
- **Point it inside the cluster:** set its URL key (`url`, `api_url`, `webhook_url`, ...) to an in-cluster address:
  `http://amgithubnotifier/hook` (answers 500), or one nothing listens on (the request still counts: contexttrack logs
  "Request sent" before dialing). A failed notification only adds messages and retries.
- **Fixed endpoints** (`pushover`, and `pagerduty` with `service_key`, which posts to the v1 API): set
  `http_config.proxy_url` to an address that refuses connections. Go dials only the proxy, so the request is logged
  and fails without leaving the cluster. The same works for any integration, alongside its own URL: in the pod,
  `http://127.0.0.1:<port>/` refuses at once (2026-11-17: every integration's error was `proxyconnect tcp: dial tcp
  127.0.0.1:9: connect: connection refused`, PagerDuty's v1 URL included). An oauth2 block has its own `proxy_url`,
  for its token request. SNS with `sigv4.profile` fails before sending (no AWS config in the pod).
- **`global.http_config` reaches every integration without its own `http_config`, the shipped webhook included.**
  A global setting that breaks sends (e.g. oauth2 or a proxy that refuses connections) stops the shipped webhook's
  notifications too, losing the baseline's `/hook` labels (2026-11-17's first candidate). Give the shipped webhook its
  own `http_config` (e.g. `follow_redirects: true`) before changing the global one.
- **Size:** a ConfigMap holds at most 1 MiB, the config and its files together. Inline PEM certificates and keys
  (about 4 KB per `tls_config`) add up: the 2026-11-17 config, with 92 integrations, was 430 KB plus 270 KB of files.

## Values known to be safe (inert, but stored)
- Extra `inhibit_rules`, `time_intervals` and `mute_time_intervals` that match nothing prombench's alerts carry, and
  routes' `mute_time_intervals`/`active_time_intervals` that never match (a mute interval matching the current time
  would stop a route's notifications).
- `templates:` globs of files you add (TESTBED.md, "Adding files").
- Headers in the web config's `http_server_config.headers` (the five header names `docs/https.md` lists): every
  response gets them; clients ignore them.

## Values other modules refer to
- Prometheus finds Alertmanager by its pod labels and posts to `/alertmanager/api/v2/alerts` (prometheus-meta's
  config); that's the deployment and `--web.external-url`, not this config.
- The route that sends patch 0004's `benchmarkPrometheusAlwaysFiring` alert to `amgithubnotifier-bridge` every 10 s,
  and that receiver's webhook to `http://amgithubnotifier/hook`, make the notifications the baseline sees (the
  notifier answers 500: it can't reach GitHub). Keep both, and keep that alert's notifications going there; you may
  change the receiver's name if the route names it too.
