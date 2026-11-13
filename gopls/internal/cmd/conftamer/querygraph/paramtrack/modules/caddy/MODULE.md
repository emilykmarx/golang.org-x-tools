# Caddy ingress controller

The concreteconfig skill's module file for Caddy's ingress controller (`github.com/caddyserver/ingress`). The
module's decisions (version, what "the config" is, how its keys are resolved) are in [README.md](README.md); read it
first.

- **Checkout:** `/home/emily/projects/config_tracing/caddyserver-ingress` (commit faf444a; same path locally and on the
  prombench VM). It runs Caddy v2.11.4, from the Go module cache: `go list -m -f '{{.Dir}}'
  github.com/caddyserver/caddy/v2` in the checkout (`$HOME/go/pkg/mod/github.com/caddyserver/caddy/v2@v2.11.4`).
- **Module, root and format:** `--module caddy` (module.py: `github.com/caddyserver/ingress`, root
  `github.com/caddyserver/caddy/v2.Config`, JSON). The config is Caddy's JSON config (`caddy.Config`), which the
  controller otherwise generates from the cluster's Ingresses and its ConfigMap; the testbed loads yours in its place.
- **Full keys:** TERMINOLOGY.md (configkeys/stats/) gives their format: a module picked by a key in its object is a
  `k=v` segment (e.g. `apps.http.servers.*.routes.handle.handler=reverse_proxy.upstreams.dial`), a module in a module
  map is its name (`apps.http`), and selector keys (`...handle.handler`) are keys too. Map keys you choose are `*`.
- **Ignore file and transformed-keys file:** none (every full key is in scope; none is marked transformed).

## Developer knowledge
- Caddy's JSON docs, as served by caddyserver.com's docs API: cached in
  `/home/emily/projects/config_tracing/caddy-config-keys/raw/` (`config_root.json`, `module/<id>.json`: each
  module's fields, types and descriptions). Its README says how they were fetched.
- The Caddy source (module cache, above): each module's struct and its doc comments are the reference
  (`modules/caddyhttp/...`, `modules/caddytls/...`, `modules/logging/...`, `caddyconfig/...`). `Provision` and
  `Validate` methods say what's rejected.
- Example JSON configs: `caddytest/integration/caddyfile_adapt/*.caddyfiletest` in the Caddy source: each is a
  Caddyfile and the JSON it adapts to (227 files, covering most HTTP handlers, matchers, TLS and logging options).
- The controller's own modules: `pkg/storage` (`storage.module=secret_store`: fields `namespace`, `leaseId`; no json
  tags, so encoding/json matches them case-insensitively) and `pkg/proxy` (`proxy_protocol` listener wrapper), in the
  checkout. What the controller itself generates is the shipped config (TESTBED.md).
- `/home/emily/projects/config_tracing/caddy-config-keys/caddy_keys_linked.txt`: the documented keys of the modules
  linked into the controller (paramtrack's `--docs` for this module).

## Checking a config
Caddy's admin API checks a config when it loads it: `POST /load` adapts nothing (it's JSON already), provisions
every module, starts the apps, and on any error keeps the running config and answers with the error. So the check is
loading it (TESTBED.md: `cc_vm.py apply`); then the running config (`GET /config/`) must equal it. Some settings only
apply at startup, so finish with a restart (`cc_vm.py restart`). There's no `caddy validate` here: the controller's
binary has no CLI, and a stock caddy binary lacks the controller's modules.

## Keys to skip, or not to count
- **Anything that contacts third parties** (constraint 2): ACME issuers (`tls.automation.policies.issuers.module=acme`
  and `zerossl`), and anything that makes Caddy manage certificates for hostnames, which by default it gets from
  Let's Encrypt/ZeroSSL ("automatic HTTPS": a server whose routes match `host` gets certificates for those hosts unless
  `automatic_https.disable` is true, or a TLS policy uses the `internal` issuer). Prefer `internal` issuers, and set
  `automatic_https.disable` on servers you add. Also skip `reverse_proxy` upstreams outside the cluster, `forward_auth`
  to outside servers, and log writers that send off the VM (`net` to outside addresses).
- **The admin API** (`admin.*`): the testbed loads configs through it at `localhost:2019`. Don't disable it, move it,
  or set `admin.config.load` (which would replace the config periodically). `admin.identity`/`admin.remote` need
  certificates and ACME; skip them.
- **Numbers, bools and durations** (`caddy.Duration` is an int, or a string Caddy parses): not searched
  (`--strings-only`), so they don't count; leave them at the shipped config's values, or unset.

## Mutually exclusive keys
A module chosen by an inline key (`handler`, `module`, `provider`, `output`, `policy`, ...) takes one value per
object, so each alternative needs its own object: e.g. one `handle` entry per handler, one route per set of
matchers, one issuer per entry of `issuers`. Lists and module maps give room for alternatives: extra routes in a
server, extra servers in `apps.http.servers` (each listening on its own unused port), extra `logging.logs` entries,
extra TLS automation policies (each with its own `subjects`).

## Values known to be safe (inert, but stored)
- An extra server (`apps.http.servers.<name>`) listening on an unused port (e.g. `:8xxx`), with
  `automatic_https.disable: true`: nothing sends to it, so its routes, matchers and handlers are provisioned but never
  run. A route there can use any handler and matcher module.
- Extra matchers and headers in the shipped server's routes that don't change which requests match (a new route
  before the controller's that matches nothing the workload sends, e.g. a path no client requests).
- Log writers to files in the container (`/tmp/...`: `/tmp` is writable) or to stderr/stdout.
- TLS: certificates and keys from files (`scripts/files.py` makes them; install them under `/tmp/paramtrack-files/`,
  TESTBED.md), `internal` issuers, and TLS connection policies on the extra servers.
