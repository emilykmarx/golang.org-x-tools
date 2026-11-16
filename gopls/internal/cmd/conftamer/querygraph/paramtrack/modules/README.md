# Basic info
- `GO_MODULE`, `TITLE`, `MODULE_DIR`: name and source code location
  - The source code should be checked out to the same tag the testbed will run
- `FORMAT`: yaml or json
- `GOLITS_PACKAGE`, `GOLITS_TAGS`: entrypoint package and build tags
- `ROOTS`: One entry per config source of interest
  - Key: name of the source (often just `config` if the module only has one of interest)
  - Values: `root`, `docs`, `ignore`, `transformed` (below)

# Configuration

## Scope (ignored keys)
- `root`: The Go type the workflow will treat as the root of the config types and keys
  - E.g. in Prometheus, `/config.Config` holds the documented config keys, but the Unmarshaler graph has a few other roots
  - E.g. in the Caddy ingress controller, the user writes the controller's config into Kubernetes objects, which the controller then converts to Caddy JSON. This populates only the portion of Caddy's keys that the controller supports, and transforms the keys in the Kubernetes objects to Caddy's documented keys.
    - To avoid adding special support for the Kubernetes objects, the controller uses Caddy's config type as the root (`--root github.com/caddyserver/caddy/v2.Config`)
- `ignore` (optional): A list of keys the testbed is unlikely to be able to use
  - E.g. in Prometheus, we choose to ignore many of the service discovery keys
  - Generated for each module on an ad hoc basis
  - Interaction with `argmatch`: The /concreteconfig skill does not typically set these (since they are not "target" - see [TERMINOLOGY](../configkeys/stats/TERMINOLOGY.md)), so `argmatch` will typically not search them.
    - But, the skill may e.g. if it finds that it must in order for the config to be valid. In this case, `argmatch` still searches for the corresponding value if it is a searchable kind, and the final step before the statistics still outputs those matches.
- `transformed` (optional): A list of keys whose values are likely to have different contents in memory than in the config file
  - E.g. Prometheus parses `*.proxy_url` and `*.api_server` URLs into scheme, host, etc, which are stored as separate fields in `config.URL` - see [modules/prometheus/transformed.txt](modules/prometheus/transformed.txt)
  - Generated for each module on an ad hoc basis
  - Interaction with `argmatch`: Still searches for these keys' values, in case we're wrong.
- `META_TYPES` (optional, default {}): Go type => reason, for types whose keys the graph finds because of how the
  module structures its types, but which aren't config (e.g. Kubernetes' metav1.TypeMeta: apiVersion, kind). A full
  key is a meta key if every Unmarshaler Path to it passes through one (stats.meta_keys); meta keys are out of scope
  like --ignore's (no target keys, no statistics), except in stats.py's Q4, which counts them separately

## Custom unmarshaling
- `CustomKeys(module_dir)`: the keys custom unmarshalers pick (resolved in the graph by extra_graph's edges), for
  stats.py's tables of them: .names (graph type => the key it's under), .parents (the keys they're under), .of(fk)
  (the picked key a full key is under, or None)
-  `UNTAGGED_KEYS` (optional, default False): lift our exception for untagged fields (gopls conftamer -untagged-keys,
  configkeys --untagged-keys; see ct.UntaggedAreKeys)
- `extra_graph(module_dir, out_path)` (optional): write gopls conftamer's -extra file (roots and edges the source code
  doesn't show: the keys a custom unmarshaler or run-time decoding picks, e.g. Prometheus' SD configs, Caddy's
  modules); paramtrack's step 1 runs it into its directory

## Config documentation
- `docs`: A list of keys generated from the module's documentation using the `/documentedkeys` skill.
- `docs_kind(type)`: the kind of Go value a documented type means (string, number, bool, map)
- `graph_docs_reason(key, in_graph)`: why a key is only in the graph (in_graph) or only in the docs
- `docs_entry(key, type)` (optional): stats.read_docs' hook for the docs list's lines: the (key, type) to use (type
  "map" for maps), or None for a line that isn't a key

## The concrete config
- `UNSET_REASONS`: why the `/concreteconfig` skill left target keys unset: (reason, fnmatch patterns)

# Messages
- `ARGMATCH_ARGS` (optional): Build args for argmatch.
- `CHAIN_FUNCTIONS` (optional, default (GO_MODULE + "/",)): prefixes of the function names that count as the module's
  own on message chains (configkeys/stats/messages/chains.py: chain groups, similarity, origins), e.g. Caddy's, whose
  code runs the controller's binary but isn't under its Go module
