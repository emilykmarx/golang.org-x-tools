Terms used in `paramtrack`'s data and docs.

# Keys

## Full key
- A key as `configkeys` outputs it, using the graph and the `--root`.
- Identifies a primitive value a user populates (e.g. `scrape_configs` in Prometheus is not a full key)
  - Exceptions: for values that are list elements or map values, the key identifies every list element or map value
- Any module-specific resolution is applied
- The format is generally a concatenation (with '.') of JSON or YAML keys ("segments" below) in the module's config file, from the root to user-populated values, with the following exceptions:
  - `*`: a key segment the user chooses
    - e.g. `global.external_labels.*` in Prometheus, `apps.http.servers.*.listen` in Caddy
  - Lists add no segment
    - e.g. `scrape_configs.job_name` in Prometheus (which appears in the config file for every scrape job)
  - `k=v`: a pair of JSON key and value
    - e.g. `apps.http.servers.*.routes.handle.handler=reverse_proxy.upstreams.dial` in Caddy
      - The corresponding JSON: `"handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": "app.default.svc:8080"}]}]`
    - Used when a value in a key-value pair selects which other segments are valid in the rest of the key
    - This prevents two keys from identifying different information, e.g. if there were another module `reverse_proxy_2` that also supported `upstreams.dial` (but may handle it very differently than `reverse_proxy` does)
  - Case: the case of the full key may not match the config file
    - e.g. `storage.module=secret_store.LeaseID` in Caddy
      - The corresponding JSON: `"storage": {"module": "secret_store", "namespace": "caddy-system", "leaseId": "lease-1"}`
- Sometimes shortened to "key" in docs

## Selector key
- A full key whose value picks a Caddy module.
  - e.g. in the `k=v` example above, `apps.http.servers.*.routes.handle.handler` is its own full key, since the user sets it to a primitive value (in this case, the module's name), such as `reverse_proxy`
- TODO depending on how we end up using it - may expand to include Prometheus k8s

## Meta key
- A key the graph would produce that doesn't represent a config parameter
  - Present when a type containing config also contains non-config types, e.g. types embedded for API compatibility (e.g. k8s `ObjectMeta`)

# Section
- The full keys of one Unmarshaler at one place in the config file
- For most keys, this means the key's last segment corresponds to one of the Unmarshaler's **own** fields (not its childrens')
  - e.g. `alerting.alertmanagers.timeout` is in `alerting.alertmanagers @ /config.AlertmanagerConfig`
  - whereas `alerting.alertmanagers.sigv4.region` is in a different section: `alerting.alertmanagers.sigv4 @ sigv4.SigV4Config`
  - and the inlined `alerting.alertmanagers.bearer_token` is in `alerting.alertmanagers @ …/common/config.HTTPClientConfig`
- For map values, the second-to-last segment corresponds to the field and the last segment is a map key
  - E.g. `global.external_labels.*` is in `global @ /config.GlobalConfig`
- Caddy modules introduce the other exceptions (below)
- Named as `<key prefix> @ <Unmarshaler>`, where the key prefix is the part of the key that leads to the Unmarshaler's object, e.g. `alerting.alertmanagers @ /config.AlertmanagerConfig`
- Defined by `configkeys` - downstream programs reuse the definition
- Each full key is in exactly one section, e.g. `scrape_configs.kubernetes_sd_configs.role` is only in `scrape_configs.kubernetes_sd_configs @ /discovery/kubernetes.SDConfig`

## Caddy modules
(Note "module" here is Caddy's definition, not paramtrack's.)

Caddy modules introduce nuance in the definition of section. The section `apps.http.servers.*.routes @ github.com/caddyserver/caddy/v2/modules/caddyhttp.Route` demonstrates:

| Key postfix (prefix is `apps.http.servers.*.routes.`) | Why it's in `Route`'s section |
|---|---|
| `group`, `terminal` | `Route`'s own fields |
| `handle.handler` | selector key |
| `match.host`, `match.method`, `match.path`, `match.protocol` | modules whose type is "scalar-like" (see `configkeys` README - these examples are string or `[]string`) |
| `match.header.*`, `match.query.*`, `match.vars.*`, `handle.handler=vars.*` | map keys of modules whose type is a map (`MatchHeader`, `MatchQuery`, `VarsMatcher`, and the `vars` handler) |

# Properties of full keys and sections
The definitions below apply to keys.
A section has the property if at least one of its keys does.

Where numbers appear under a definition, all must be satisfied.

- **In-scope**
  - Not left out by the module's `--ignore` file (see paramtrack [README](../../paramtrack/README.md))
  - e.g. Prometheus on Prombench ignores most service discovery configs (other than kubernetes, http, and a few others)
- **Target**
  - (1) In-scope
  - (2) Of a "kind" argmatch searches for
    - i.e. string or struct, satisfying `configkeys --strings-only`
  - (3) Not left out by the module's `--transformed` file (see paramtrack [README](../../paramtrack/README.md))
  - (Some of the workflow code uses "target" to mean a {module, root} pair)
- **Concrete**
  - Set in the "concrete config", i.e. a config file runnable in the testbed
- **Eligible**
  - (1) Concrete (else argmatch cannot search for it)
  - (2) Of a "kind" argmatch searches for

# Properties of values and sections
The definitions below apply to values.

A key has the property if all of its values do (some keys have multiple values, e.g. lists).
A section has the property if at least one of its values does.

- **Unambiguous**
  - (1) No other key in the module's concrete config(s) has the same value
  - (2) The value is not a string literal in the module code

# Messages
- See the argmatch-labels [README](../../../../../../../../dump_ancestors/argmatch-labels/README.md)
