# Caddy ingress controller

Everything paramtrack's workflow needs that is specific to Caddy's ingress controller (`github.com/caddyserver/ingress`) is here or linked from here.

## Files here
- [module.py](module.py): the settings paramtrack and stats.py read, and the hooks they call. [../\_\_init\_\_.py](../__init__.py) describes each one.
- [registry.go](registry.go): run by `module.py`'s `extra_graph` (below), not built with gopls (`//go:build ignore`).

## Decisions
- **Version:** Caddy v2.11.4, the version the controller's `go.mod` pins, at controller commit faf444a.
- **The config:** the Caddy JSON the controller generates from its Kubernetes inputs, which `caddy.Load` decodes into `caddy.Config` with encoding/json. The controller's own inputs (ConfigMap, flags, Ingress resources and annotations) aren't analyzed.
- **Checkout and root:** the graph is built from the controller's checkout (`MODULE_DIR`), so it covers exactly the modules the controller's binary links, Caddy's (from the Go module cache) and the controller's own (`pkg/storage`'s `secret_store`, `pkg/proxy`'s `proxy_protocol`). `caddy.Config` isn't in the controller's packages, so `extra_graph` adds it as a root.
- **Modules:** Caddy decodes its modules at run time from `json.RawMessage` fields tagged `caddy:"namespace=... [inline_key=...]"`, so the type definitions don't link them. `extra_graph` adds those links as edges: it runs `registry.go`, which uses Caddy's own module registry (`caddy.Modules()`) and reflection over the binary's packages. Their keys are the module name (a module map, e.g. `apps.http`) or `<inline key>=<name>` (e.g. `handle.handler=reverse_proxy`); TERMINOLOGY.md ("Full key", "Section") has the format and the sections.
- **Selector keys** (e.g. `handle.handler`) are full keys.
- **Untagged fields are keys** (`UNTAGGED_KEYS`), as encoding/json keys them: `secret_store`'s fields have no json tags.

## Documented keys
`DOCS` is `caddy_keys_linked.txt` in [documentedkeys/](documentedkeys/), from a background agent run (2026-11-06) whose brief later became the documentedkeys skill. Its README there gives the sources (caddyserver.com's JSON docs API, cached in `raw/`) and how to rerun `list_caddy_keys.py`.

## Changes to shared programs made for Caddy
- JSON configs (encoding/json) in `gopls conftamer` and configkeys (commit 1f33eee58).
- `gopls conftamer -extra` (roots and edges the source doesn't show) and `-untagged-keys`; Prometheus's SD configs use `-extra` too.
- configkeys: keys of module fields (including selector keys, and modules whose type is scalar-like or a map), `k=v` segments inserted when reading JSON documents, keys respelled as the code spells them (encoding/json matches keys ignoring case), and the `section` column.

## Not done yet
- The testbed: prombench, with Caddy's ingress controller in place of nginx.
- stats.py: its output still speaks of service discovery (`sd_configs.csv`, the `sd` column), and reading the docs list needs hooks for its `map[...]` and `{}` types (a `{}` entry is a module choice with no fields of its own, e.g. `listener_wrappers.wrapper=tls`, not a key).
- The concreteconfig skill's `MODULE.md` for this module.
