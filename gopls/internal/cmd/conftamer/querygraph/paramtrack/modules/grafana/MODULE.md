# Grafana

The concreteconfig skill's module file for Grafana (`github.com/grafana/grafana`). What its config is (the four
provisioning file kinds; grafana.ini is out of scope), and how the documented keys were listed, are in
[documentedkeys/README.md](documentedkeys/README.md); module.py has the paramtrack settings, including the extra graph
edges for the keys Grafana reads from generic maps (`extra_graph`: datasources' `jsonData`/`secureJsonData`, contact
points' `settings`).

- **Checkout:** `/home/emily/projects/config_tracing/grafana-paramtrack` (v13.1.0; same path locally and on the
  prombench VM). The testbed runs the same version, stock or instrumented (TESTBED.md).
- **Module, config sources and format:** `--module grafana`, YAML (yaml.v3). Config sources: `datasources`, `dashboards`, `plugins`,
  `alerting`, each a provisioning file kind; the skill writes a config for each config source it sets. prombench ships
  datasources and dashboards files only; `plugins` and `alerting` start from an empty file (TESTBED.md).
- **Full keys:** a datasource's `jsonData`/`secureJsonData` keys and a contact point's `settings` keys have a `k=v`
  segment for the item's type, e.g. `datasources.type=prometheus.jsonData.httpMethod` and
  `contactPoints.receivers.type=slack.settings.url` (TERMINOLOGY.md, configkeys/stats/, gives the format).
- **Ignore files:** per config source, in module.py's `CONFIG_SOURCES`, each with a comment saying why: `datasources`:
  [ignore-datasources.txt](ignore-datasources.txt) (the SQL datasources, which aren't HTTP, and Azure Monitor);
  `alerting`: [ignore-alerting.txt](ignore-alerting.txt) (contact points that aren't HTTP, `email` and `mqtt`, or that
  post to fixed third-party endpoints, `line`, `pushover`, `telegram`, `threema`). No transformed-keys file.
- **An earlier config:** `gopls/results/grafana/concreteconfig-2026-11-17/` (this skill's first run on Grafana):
  `work/gen.py` writes its configs, and `diff.md` gives each key's reason. Reuse its code if it helps (copy it into
  your own directory), but check its choices rather than copying them.

## Developer knowledge
In the checkout:
- `docs/sources/administration/provisioning/index.md` (datasources, with the `jsonData`/`secureJsonData` tables per
  plugin; dashboards; plugins) and
  `docs/sources/alerting/set-up/provision-alerting-resources/file-provisioning/index.md` (alerting, with each contact
  point integration's settings).
- `conf/provisioning/<kind>/sample.yaml`: commented examples of each kind.
- `pkg/services/provisioning/<kind>/testdata/`: the readers' test files, valid and invalid, with what
  `<kind>/*_test.go` expects of them.
- The reading and validation code, for what the docs leave out: `pkg/services/provisioning/<kind>/config_reader.go`
  and `types.go`; for `jsonData`, each datasource plugin's settings struct (module.py's `extra_graph` lists them:
  `settings_types.json` in a run's `1-unmarshaler-graph/`); for contact point settings, the integrations in
  `github.com/grafana/alerting` (module cache).
- configkeys/stats/STATISTICS.md, Q4, and module.py's `graph_docs_reason`: the keys the graph has but the docs don't,
  and why.

## Checking a config
There's no check command for provisioning files. Grafana provisions on startup, and a provisioning error (except a
dashboard folder error) stops it from starting. So the check is:
- the admin API's provisioning reload, `POST /grafana/api/admin/provisioning/<source>/reload` (TESTBED.md: `cc_vm.py
  apply`): it rereads the source's files and answers 500 with the error if one is invalid (the pod log has details);
- then a restart, which must reach `/grafana/api/health`.

## Keys to skip, or not to count
- **Anything that contacts third parties** (constraint 2): contact point integrations send to an outside service, by
  default or at the URL you set, and datasources to theirs when queried. Don't leave any that sends pointed outside;
  "Exercising contact points and datasources" says how to make them send inside the cluster.
- **`plugins` config source:** `apps.type` must be an installed app plugin ("plugin not installed" fails provisioning, and
  so Grafana's startup); `cc_vm.py health` lists the installed ones.
- **Numbers and bools are target keys too:** the provisioning files' `values.*` types (`Int64Value`, `BoolValue`, ...)
  keep the raw string, which the search finds. Few can be distinctive: `orgId` must be 1 (the only org), and bools are
  `true`/`false`. Numbers read from `jsonData` (timeouts, connection limits) can be, e.g. 4-digit values.
- **Unreachable:**
  - `policies.matchers`, `object_matchers`, `match`, `match_re`, `mute_time_intervals`, `active_time_intervals`: the
    root policy can't have them (`definition.Route.Validate`, grafana/alerting), and a child policy's keys are
    `policies.routes.*`, which aren't these full keys (the graph cuts the recursion at `policies.routes`).
  - `resetPolicies`: the alerting provisioner resets the listed orgs' policy trees *after* provisioning `policies`
    (`provisioner.go`), so it would remove the routes that make the contact points notify.
- **Third parties through AWS STS:** cloudwatch's `jsonData.assumeRoleARN` (and `externalId`) and an `sns` contact
  point's `sigv4.role_arn` make an STS AssumeRole call to AWS's own endpoint: the datasource's `endpoint` doesn't apply
  to it, and its `proxyType`/`proxyUrl` only apply with grafana.ini's per-datasource proxy on (off by default). Every
  `sigV4Auth` false keeps the `sigV4*` strings inert (stored, unused).

## Mutually exclusive keys
- A datasource's `type` picks which `jsonData`/`secureJsonData` keys apply (`type=<plugin>`), and a contact point
  receiver's `type` picks its `settings` keys: cover each type with its own datasource or receiver. Datasource names
  (and `uid`s, if set) must be unique per org, and at most one datasource per org may be `isDefault` (the shipped
  `prometheus-meta` is).
- `deleteDatasources` entries delete datasources at provisioning: don't name one the config also provisions, unless
  it's the shipped config's own delete-and-recreate of `prometheus-meta`.

## Values
- **Interpolation:** string values go through Grafana's expanders and the environment (`values` package): `$VAR`,
  `${VAR}`, `$__env{VAR}` and `$__file{path}` are replaced, and `$$` is a literal `$`. Avoid `$` in values you want
  found as written.
- **`secureJsonData`** is encrypted before Grafana stores it, so its values may only be found in memory while
  provisioning runs.

## Exercising contact points and datasources
The workflow learns about a key from the messages its value is found on, so a contact point's settings count only if
it notifies, and a datasource's only if something queries it. One that never does sets keys without giving them
evidence.
- **Contact points** (`alerting` config source): provision an alert rule that always fires (e.g. a Prometheus query
  `vector(1)` on `prometheus-meta`, `datasourceUid: P86B77AEF8F12A5FA`) and `policies` that route it to each
  contact point (`continue: true` on a route lets later routes match too). Every alert is its own notification group
  unless the root's `group_by` says otherwise (a label all alerts share, e.g. `grafana_folder`, gives one group per
  route). Alternatives that pick different settings (slack's webhook `url` vs `token`/`recipient`/`endpointUrl`,
  wecom's webhook `url` vs the app's `corp_id`/`secret`/`endpointUrl`, sns's `topic_arn`/`phone_number`/`target_arn`)
  go in separate receivers of the contact point. Point each one's URL setting (`url`,
  `apiUrl`, `endpointUrl`, `kafkaRestProxy`, ...) at an in-cluster address, e.g. one nothing listens on: the request
  still counts (contexttrack logs "Request sent" before dialing), and a failed notification only adds messages.
  Contact points have no proxy setting, so the integrations with fixed endpoints are in the ignore file.
- **Datasources** (`datasources` config source): Grafana sends nothing for a datasource until something queries it
  (provisioning makes no HTTP request for datasources; baseline of 2026-11-17), and prombench's dashboards query only
  `prometheus-meta`. So query each new datasource from an alert rule in your `alerting` config (a rule's query names
  its datasource by `datasourceUid`: give the datasource a `uid`), or its keys load but get no evidence. Point new
  datasources at in-cluster URLs; a query that fails still counts. Only the datasource's backend must exist
  (`eval/validate.go`), not its `alerting` flag, so jaeger, parca and pyroscope can be queried too. The `alertmanager`
  datasource has no backend, and gets no traffic either: Grafana sends alerts to datasources with
  `jsonData.handleGrafanaManagedAlerts: true` only for orgs with an ngalert admin configuration (`sendAlertsTo`, set
  through the API, `/api/v1/ngalert/admin_config`, not a provisioning file; `ngalert/sender/router.go`), and the
  testbed's fresh database has none. Its keys load without evidence.
  Plugins that call a cloud API (2026-11-17's run): `stackdriver` with `authenticationType: jwt` and a dead
  `jsonData.tokenUri` fails at the token request, so the query (to `monitoring.<universeDomain>`) is never sent;
  `cloudwatch` with `authType: keys` and `jsonData.endpoint` in the pod (STS: "Keys to skip").

## Values known to be safe (inert, but stored)
- **Dashboard providers** whose `options.path` is an existing directory with no `.json` files (e.g.
  `/opt/grafana-provision/dashboards`, which holds the provisioning file), besides the shipped provider. A missing
  directory doesn't fail provisioning, but logs an error at every scan.
- **Mute timings and templates** (`muteTimes`, `templates`): stored, used only when a policy refers to them (a mute
  timing matching the current time would stop the policy's notifications).

## Values other modules refer to
- The dashboards (the `grafana-dashboards` ConfigMap, which the shipped dashboard provider loads from
  `/var/lib/grafana/dashboards`) query the datasource named `prometheus-meta`, by name and by uid `P86B77AEF8F12A5FA`
  (which Grafana derives from the name when `uid` is unset). The workload's dashboard viewer loads them through Caddy
  (`/grafana`). So keep the `prometheus-meta` datasource's name, type, URL and default `uid` (unset), and the shipped
  dashboard provider's `options.path`.
