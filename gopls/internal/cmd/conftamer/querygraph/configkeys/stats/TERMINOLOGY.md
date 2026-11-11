Terms used in `paramtrack`'s data and docs.

# Keys
- **Pattern**: A key the graph allows, as `configkeys` outputs it
  - `*` represents two things:
    - map keys
    - keys a custom unmarshaler picks
      - E.g. in `scrape_configs.*.role`, the `*` is a service discovery name such as `kubernetes_sd_config` or `http_sd_config`
- **Full key**: A pattern, with custom keys resolved
    - i.e.: `*` due to custom unmarshalers are replaced with module-specific logic (`Name()` in Prometheus)
      - E.g. `scrape_configs.*.role` becomes 6 full keys, one per SD config that has a role field (e.g. `scrape_configs.kubernetes_sd_configs.role`).

# Sections
- **Section:** The full keys directly contained in one Unmarshaler at one place in the file
  - Named as `<parent key> @ <Unmarshaler>`, e.g. `alerting.alertmanagers @ /config.AlertmanagerConfig`
  - See `configkeys`' `section keys` column for detail, but note that the custom `*` resolution happens downstream of `configkeys`
  - Each full key is in exactly one section, e.g. `scrape_configs.kubernetes_sd_configs.role` is only in `scrape_configs.kubernetes_sd_configs @ /discovery/kubernetes.SDConfig`

# Properties of keys and sections
The definitions below apply to keys.
A section has the property if at least one of its keys does.

Where numbers appear under a definition, all must be satisfied.

- **Full**
  - Reachable from the workflow's `--root` (e.g. `/config.Config` in Prometheus)
- **In-scope**
  - Not left out by the module's `--ignore` file
    - Generated ad-hoc to reflect keys the testbed is unlikely able to use
  - e.g. Prometheus on Prombench ignores most service discovery configs (other than kubernetes, http, and a few others)
- **Target**
  - (1) In-scope
  - (2) Of a "kind" argmatch searches for
    - i.e. string or struct, satisfying `configkeys --strings-only`
    - These are exactly the keys argmatch will search
  - (3) Not left out by the module's `--transformed` file
    - Generated ad-hoc to reflect keys whose values are likely to have different contents in memory than in the config file
    - argmatch still searches these, in case we're wrong
    - e.g. Prometheus parses `*.proxy_url` and `*.api_server` URLs into scheme, host, etc, which are stored as separate fields
- **Concrete**
  - Set in the "concrete config", i.e. a config file runnable in the testbed

# Properties of values and sections
The definitions below apply to values.

A key has the property if all of its values do (some keys have multiple values, e.g. lists).
A section has the property if at least one of its values does.

- **Unambiguous**
  - (1) No other key in the concrete config has the same value
  - (2) The value is not a string literal in the module code
