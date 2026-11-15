# Documented config keys: Caddy ingress controller + Caddy

Documented config keys for `github.com/caddyserver/ingress` (the controller) and the
Caddy JSON config it generates. They are meant to be compared with keys that static
analysis finds in the code. The format follows
`../../prometheus/documentedkeys/prometheus_config_keys.txt`: one key per line,
`<dotted key>\t<documented type>`, with list indices left out (see
`prom-test-infra/prombench/config_keys_notes.md`).

## What "the config" is
The controller embeds Caddy (`caddy.Load`, `internal/controller/controller.go:276`).
It never reads a Caddy JSON file, and nothing passes raw Caddy JSON through. A user
writes Kubernetes objects:
- **ConfigMap** (global options): string values decoded into `ConfigMapOptions` with
  mapstructure (`pkg/store/configmap_parser.go`). The Helm chart writes
  `ingressController.config.*` into it (`templates/configmap.yaml`).
- **CLI flags** (`cmd/caddy/flag.go`), set by the chart's `templates/deployment.yaml`.
- **Ingress resources**: `spec.rules[].host`, `paths[].path/pathType/backend.service`,
  `spec.tls[]`, the `kubernetes.io/ingress.class` annotation or `spec.ingressClassName`,
  and `caddy.ingress.kubernetes.io/*` annotations. TLS Secrets are written to a cert
  folder.

Plugins in `internal/caddy/{global,ingress}` turn these into a `converter.Config`
(`pkg/converter/config.go`). That struct is marshalled to Caddy JSON and loaded.
`controller_to_caddy.tsv` records which input sets which Caddy key. The controller
therefore covers only a small, fixed part of Caddy's JSON. Caddy's admin endpoint
stays at its default (`admin: {}`, so localhost:2019 inside the pod), and it could load
any JSON the linked modules accept. That is a side door, not a documented input.

## Files
| File | What |
|---|---|
| `list_caddy_keys.py` | Fetches the docs API into `raw/` and writes the `caddy_keys_*` lists |
| `caddy_keys_full.txt` | Every documented key of the **standard** Caddy modules, reachable from the config root (1,380) |
| `caddy_keys_full_jsonpaths.txt` | Same, with the module-choice segments (`x=y`) removed and duplicates merged: the literal JSON paths (1,275) |
| `caddy_keys_linked.txt` | Subset of `caddy_keys_full.txt` whose modules are all linked into the controller binary (1,022). **POST-SKILL RUN NOTE**: This is the file paramtrack ended up using |
| `linked_modules.txt` | Those modules (93 of 136), from `go list -deps ./cmd/caddy` plus each module's package in the API |
| `caddy_keys_controller.txt` | Caddy keys the controller can actually produce (34), from `controller_to_caddy.tsv` |
| `controller_keys.py` | Writes the `controller_*` files and `caddy_keys_controller.txt` |
| `controller_documented_keys.txt` | The controller's documented inputs: `configmap.<key>` (10) and `flag.<name>` (6) |
| `controller_helm_values.txt` | All Helm chart values (52), from the chart README table plus `values.yaml`. Most only template k8s objects |
| `controller_code_keys.txt` | Inputs as the code defines them (regex over the Go source, 28), for contrast. **Not documentation** |
| `controller_to_caddy.tsv` | Hand-written map: controller input -> Caddy key -> code location (read from faf444a) |
| `check_against_source.py` | Lists documented struct fields with no matching `json:"..."` tag in a given Caddy tree (a version-drift check) |
| `raw/` | Cached API responses (fetched 2026-11-06) |

## Sources
- Controller: `/home/emily/projects/config_tracing/caddyserver-ingress` at `faf444a`. Only
  git-tracked files were used: `README.md`, `charts/caddy-ingress-controller/{README.md,
  values.yaml,templates/*.yaml}`, and the Go files cited in `controller_to_caddy.tsv`.
- Caddy docs: the JSON API behind https://caddyserver.com/docs/json/ and
  `/docs/modules/`, found in the site's `docs-api.js`, `json-docs.js` and
  `module-docs.js` (site build `v=6b06285`):
  - `GET /api/modules`: every module ID, with its repo and Go package.
  - `GET /api/docs/config/`: the root `caddy.Config` structure.
  - `GET /api/docs/module/<id>`: one module's structure, once per standard module (136).

  138 requests in total, fetched 2026-11-06. The structures are generated from the Go
  struct docs. Nested modules are not expanded in them, so the script expands them.
- **Version caveat:** the site documents current Caddy and doesn't say which version.
  The controller builds against Caddy **v2.11.4** (`go.mod`, released 2026-06-01). The
  `caddy` checkout is at `6116e4f9` (2025-04-16, older). `check_against_source.py` found
  14 of 572 documented (struct, key) pairs with no tag in v2.11.4, and 16 in the
  checkout. Most of those are false positives from embedded structs. Real differences
  include `CombinedCAPool.sources`, `ProxyFromURL.url` and
  `MultiRegexpFilter.operations`, which are missing from the checkout. Compare against
  the source version you analyze.
- Other options considered: there is no public generator for Caddy's JSON docs. The
  site's backend is not in the `caddyserver/website` repo, which only has the JS that
  renders the API. Rendering pages or scraping HTML would add nothing, because the API
  is the machine-readable form of the same data. The chart README is helm-docs output,
  but stale, so `values.yaml` is read as well.

## Rerun
```sh
cd /home/emily/projects/config_tracing/golang.org-x-tools/gopls/internal/cmd/conftamer/querygraph/paramtrack/modules/caddy/documentedkeys
timeout 600 python3 list_caddy_keys.py --ingress-dir /home/emily/projects/config_tracing/caddyserver-ingress   # add --refetch to re-download raw/
timeout 180 uv run --with pyyaml python controller_keys.py --ingress-dir /home/emily/projects/config_tracing/caddyserver-ingress
python3 check_against_source.py ~/go/pkg/mod/github.com/caddyserver/caddy/v2@v2.11.4
```
With a filled `raw/`, nothing is fetched. `--ingress-dir` runs `go list -deps` (needs
the module cache).

## Conventions and judgment calls
- **Leaves only**, as in the Prometheus list. Keys that only group others (structs,
  lists of structs) are not listed. Exceptions: a struct or module with no documented
  fields is listed with type `{}`, so that choosing it is visible (e.g.
  `listener_wrappers.wrapper=tls`).
- **Lists are invisible**: `routes.handle.handler=reverse_proxy.upstreams.dial` is a
  key inside lists of routes, handlers and upstreams.
- **Maps with user-chosen keys**: `*` if the values have keys of their own
  (`apps.http.servers.*.listen`, `logging.logs.*.level`). Otherwise the map is a leaf
  with type `map[K]V` (`static_response.headers`).
- **Module maps** (the module name is the JSON key, e.g. `apps`, route `match`,
  `tls.certificates`): the module name is an ordinary path segment, which is also the
  literal JSON (`apps.tls...`, `routes.match.host`).
- **Inline modules** (the module is picked by a key inside the object, e.g.
  `"handler": "reverse_proxy"`): one key per module, with a segment
  `<inline_key>=<module_name>`, plus one key for the selector itself:
  - `...routes.handle.handler` has type `module_id:http.handlers`.
  - `...routes.handle.handler=reverse_proxy.upstreams.dial` has type `string`.

  Dropping the `x=y` segments gives the literal JSON path. That is
  `caddy_keys_full_jsonpaths.txt`, where different modules' same-named fields merge.
  Keeping the module in the key matters because code-side tools see each module's Go
  struct separately.
- **Only standard modules** (repo `github.com/caddyserver/caddy`). The site also
  documents about 440 third-party modules (layer4, DNS providers, ...), which the
  controller doesn't link. A module field with no standard implementation is listed
  as its selector with `:no-standard-modules`, e.g.
  `issuers.module=acme.challenges.dns.provider.name`.
- **Shared blocks are expanded at every place**, as in the Prometheus list. For
  example, all handlers appear under `servers.*.routes`, `servers.*.errors.routes` and
  `servers.*.named_routes.*`, and all CA pool sources appear under reverse_proxy TLS
  and under `tls_connection_policies.client_authentication`.
- **Recursion is cut**, and the cut point is listed with type `recursive:...`:
  - A struct type that is already an ancestor, e.g. `handler=subroute.routes` has type
    `recursive:caddyhttp.Route`.
  - A module namespace nested under a module of the same namespace, e.g. an encoder's
    `wrap`, a selection policy's `fallback` or `lazy.ca`. Without this the encoders
    alone gave 600+ combinations.
- **Types** are the API's: `string`, `bool`, `int`, `float`, `[]T`, `map[K]V`,
  `kind:NamedType` for named scalars (`int:v2.Duration` is `caddy.Duration`, which also
  accepts strings like `"5m"`), `module_id:<namespace>`, and `any` for untyped values.
  They are not normalized.
- **Controller subset (`caddy_keys_controller.txt`)**: every Caddy key that some code
  path in `internal/caddy/**` and `pkg/converter` can set, whether from an input or as
  a constant. This came from reading the code, since no doc describes it. The script
  checks each key against `caddy_keys_full.txt`. Four entries the controller sets as a
  whole (e.g. `handler=metrics`, `routes`) are grouping keys, so they are added with
  their struct type.
- **Controller documented keys**: ConfigMap keys come from the chart README table,
  `values.yaml` and the README prose (`onDemandAsk` appears only there). Flags are the
  ones the chart's deployment template passes. Annotations: **none are documented**
  anywhere in the repo.

## Known gaps and doc quirks (left as-is)
- **Undocumented controller inputs**, found in code (`controller_code_keys.txt`):
  - All 9 `caddy.ingress.kubernetes.io/*` annotations: `rewrite-to`,
    `rewrite-strip-prefix`, `disable-ssl-redirect`, `backend-protocol`,
    `insecure-skip-verify`, `permanent-redirect`, `permanent-redirect-code`,
    `temporal-redirect` and `trusted-proxies`.
  - `kubernetes.io/ingress.class`.
  - ConfigMap `ocspCheckInterval`.
  - Flag `-plugins-order`.
- **`caddy.storage.secret_store`** is the controller's own Caddy module
  (`pkg/storage`). It isn't on caddyserver.com. Its keys `namespace` and `leaseId` are
  in the controller subset typed `string:controller-module-undocumented`. In Go,
  `SecretStorage` has no json tags, so these names match `Namespace` and `LeaseID`
  only through encoding/json's case-insensitive matching.
- **`pkg/proxy`** defines a second `caddy.listeners.proxy_protocol` module but is never
  imported. The `proxyProtocol` option uses Caddy's own module.
- **Fields with custom JSON are under-documented**, e.g. `match.not` and
  `tls_connection_policies.match.not` are `{}`, and `ca.provider=combined.sources` is
  typed `[][]uint` (raw JSON bytes of modules).
- **9 standard modules are unreachable from the root**, so their keys aren't listed:
  `admin.api.*` (4), `caddy.filesystems`, `caddy.logging.cores.mock`,
  `caddy.network_proxy.{none,url}` and `tls.ech.publishers.dns`. Some are used by
  fields the site doesn't document. For example, the reverse_proxy HTTP transport
  has no `network_proxy` key in the API response, though the modules exist.
- The site documents Caddy's current version, not v2.11.4 or `6116e4f9`. See
  "Version caveat" above.
