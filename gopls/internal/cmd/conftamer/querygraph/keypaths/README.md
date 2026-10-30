# keypaths

For each param key in a csv, find the path(s) through a serialized Unmarshaler Subgraph that produce that key.
This is roughly the inverse of `ASTPathToParams` in `dlv/params.go` (which goes path => key). Both build each edge's key segment with `ct.ASTPathKeySegment` (`conftamer/keysegments.go`), so they follow the same rules.

## Usage
```
go run . (--keys-csv <csv with a param_keys column> | --keys-txt <param_keys.txt>) \
  --unmarshaler-subgraph <graph serialized by Serialize, e.g. unmarshaler_subgraph.text> \
  --module-prefix <module as in go.mod, e.g. github.com/prometheus/prometheus> \
  --output <output csv> \
  [--format yaml|json] \
  [--root <node to search from, e.g. /config.Config>]
```
`--format` is the config file's format, which determines whose unmarshaling behavior is followed: `yaml` follows `gopkg.in/yaml.v2` (the default), and `json` will follow `encoding/json` (not yet supported). Generate the graph with the same format (`gopls conftamer -format`).
`--root` names the node as it appears in the serialized graph, i.e. with the module prefix cut. By default the search starts from every root of the graph. Other roots can be types for other files in the same format, e.g. promtool's test files, and they can add spurious matches.
Pass the keys in one of two ways:
- `--keys-csv`: the csv's `param_keys` column holds comma-separated lists of dot-separated keys.
- `--keys-txt`: a file with lines `<value>:<key>[,<key>...]`, such as the `param_keys.txt` written by `prom-test-infra/prombench/extract_param_values.py`. The keys come after the last colon on each line.

Keys are deduplicated.

## Output
One row per key, with columns `param_key,Unmarshaler,Num Unmarshalers,Unmarshaler Path,section keys`:
- `Unmarshaler`: the distinct last types on the key's paths, joined with `; `
- `Num Unmarshalers`: the number of distinct Unmarshalers. It's 0 for keys with no full path, even though `Unmarshaler` then lists the last types of the partial paths.
- `Unmarshaler Path`: all paths for the key, joined with `; `, e.g. `/config.Config => /config.ScrapeConfig`
- `section keys`: the keys in the section directly containing the key that can be reached without following any edge beyond the key's own paths in `Unmarshaler Path`. Joined with `; `.
  - It's the union over the key's full paths, and empty for keys with no full path.
  - It always includes the key itself, plus the Unmarshaler's fields that have no outgoing edge in the graph.
  - Following go-yaml, unexported fields are skipped.
  - Everything that needs an edge is excluded:
    - fields whose type is a node in the graph, e.g. `scrape_configs.params` (`url.Values`) and `scrape_configs.scrape_interval` (`model.Duration`)
    - fields of inlined structs, e.g. `HTTPClientConfig`'s `scrape_configs.bearer_token_file`
    - `yaml:"-"` fields
  - For example, `scrape_configs.scrape_failure_log_file` gets `scrape_configs.job_name` but not `scrape_configs.params`.
  - `*` stands for any map key.
  - Keys whose path ends in a map key, or in a custom unmarshaler absorbing the rest of the key, get just the map's or type's `<prefix>.*`, e.g. `remote_write.headers.*` or `global.external_labels.*`.
  - If an edge used up the rest of the key, the section is that edge's source. If the edge ended with a map key, it's the map's `.*` instead, e.g. `scrape_configs.proxy_connect_header.*`.
  - Where several Unmarshalers are possible, their sections are combined. For example, `scrape_configs.kubernetes_sd_configs.role` gets the combined sections of all 6 SD configs that have a `role` field.

Type names have the module prefix removed.

Rows are sorted into groups, and by key within each group:
1. Keys with no full path. Their partial paths are listed, each with a `(matched: <key prefix>)` suffix.
2. Keys whose paths end at more than one distinct Unmarshaler.
3. All other keys, which have exactly one Unmarshaler.

## How keys are matched
The search runs depth-first from every root of the graph, consuming the key one segment at a time:
- **Field keys:** every decision about a struct field's key goes through `ct.GetFieldKey` (`conftamer/fieldkeys.go`), which `gopls conftamer` also uses when building the graph. For YAML it's a port of the per-field part of go-yaml v2.4.0's `getStructInfo`, applied to the field's name, tag, and whether it's embedded or a map:
  - Unexported fields are skipped, unless they're embedded.
  - The key is the `yaml` tag's name, or the lowercased field name if the tag has no name. A tag with no `key:"..."` parts is used whole, as a yaml tag (go-yaml's legacy behavior).
  - `yaml:"-"` fields are skipped by go-yaml. See "Custom unmarshalers" below for when they're followed anyway.
  - `,inline` fields contribute no key segment of their own. An inline map collects all other keys.
  - Fields with no tag for the format are excluded (see "Untagged fields").
- **Edge segment:** an edge's key segment is built from its AST paths by `ct.ASTPathKeySegment`, keying each field on the path with `ct.GetFieldKey`. An AST path through a field that isn't a key gives no segment, so that path isn't followed. An edge with no field segment, such as an inline field or a slice element, is followed without consuming any of the key.
  - **Embedded fields** (`T`, `*T`, `pkg.T`) are keyed like any other field: by their yaml tag if they have one, so `""` for `yaml:",inline"`. Untagged embedded fields are excluded (see "Untagged fields"). go-yaml only inlines an embedded field with `,inline`, unlike encoding/json.
    - The graph's AST path for an embedded field has no `Field:` entry, because `ASTPath` only records field names and embedded fields have none.
    - So an edge is treated as an embedded field when its AST path starts `StructType.Fields`, `FieldList.List` and has no `Field:` entry. The field name is the target's type name, which must appear in the source's tags. (Graphs now also record each node's `EmbeddedFields`.)
- **Map keys:** these are matched as go-yaml matches them.
  - A `MapType.Value` entry in an edge's AST path consumes exactly one key segment, whatever it is: the map key. For example, `scrape_configs.http_headers.X-Fuzz-Source.files` goes `HTTPClientConfig =http_headers=> Headers =(inline field, map key X-Fuzz-Source)=> Header`, then ends at field `files`.
  - An AST path with `MapType.Key` isn't followed, because the key type decodes the key's name, not a config section. In Prometheus this is `model.LabelSet => model.LabelName`.
  - A map whose value type isn't a CType has no `MapType.Value` edge. For these, the graph records which nodes are maps (`IsMap`) and which struct fields are maps (`MapFields`), in both cases after stripping pointers, slices and arrays. They are populated by `CopyKeyInfo` in `conftamer/gopls.go`.
    - If no field and no key-consuming edge matches at a node, one remaining segment matches as a map key when the node is a map or has an inline map field. A map field's key followed by one segment also matches. The path ends at that node.
    - For example, `scrape_configs.params.fuzz_job` ends at `net/url.Values`. `remote_write.headers.X-SINK-SOURCE` ends at `/config.RemoteWriteConfig`, the struct holding the `map[string]string` field, consistent with the "section containing that key" overapproximation above.
    - Only one level of map is handled: `map[string]map[string]string` matches one segment, not two.
- **Custom unmarshalers:** go-yaml calls a type's `UnmarshalYAML` (if its method list has one) instead of applying its default rules. It doesn't do this when the type is reached through an inline field: the inlined struct's fields are decoded into the parent's mapping.
  - Checked with a test program against `gopkg.in/yaml.v2` v2.4.0:
    - **Named inline field** (`In T yaml:",inline"`): `T.UnmarshalYAML` isn't called.
    - **Slice element:** it is called.
    - **`yaml:"-"` field:** ignored, even when the YAML has that key.
    - **Embedded inline field** (`T yaml:",inline"`): the method is promoted, so the outer type is the one that has `UnmarshalYAML`.
  - Two rules follow from this:
    - **`yaml:"-"` edges.** Default go-yaml never populates a `yaml:"-"` field, so the edge is only followed when the source node has a custom unmarshaler. That unmarshaler chooses the field's key, so the edge matches any one key segment.
      - It's only tried when nothing else under the node matched, including through inline fields.
      - It never matches the last segment, since param keys are scalar leaves.
      - For example, `scrape_configs.kubernetes_sd_configs.api_server` goes `ScrapeConfig =(yaml:"-", any segment: kubernetes_sd_configs)=> discovery.Configs => discovery.Config => kubernetes.SDConfig`, then ends at field `api_server`. Without a custom unmarshaler on the source, `yaml:"-"` edges aren't followed at all.
    - **Non-struct types with a custom unmarshaler** (e.g. `labels.Labels`, whose `UnmarshalYAML` decodes a `map[string]string`): tags say nothing about how such a type's keys map to types. So if nothing under the node matched, the rest of the key is attributed to the node.
      - Only when the node wasn't reached through an inline field, and only when some of the key was already consumed.
      - These paths get a ` (custom UnmarshalYAML)` suffix, e.g. `global.external_labels.cluster`: `/config.Config => /config.GlobalConfig => /model/labels.Labels (custom UnmarshalYAML)`.
      - Structs with a custom unmarshaler are assumed to use the usual `type plain T; unmarshal((*plain)(c))` pattern, so their tags still describe their keys. Otherwise every SD config, all of which have custom unmarshalers, would claim any key below `*_sd_configs`.
  - The `yaml:"-"` wildcard doesn't say *which* type the custom unmarshaler picks for the segment. So every type reachable through it that has a matching field is returned. For example, `scrape_configs.kubernetes_sd_configs.role` gets 6 Unmarshalers: the SD configs with a `role` field, i.e. hetzner, kubernetes, moby, openstack, scaleway and triton. Keys like `kubernetes_sd_configs.tls_config.ca_file` get one path per SD config, though these all end at the same type, `TLSConfig`. Tying the segment to the right SD type would need Prometheus-specific knowledge (the key is each config's `Name()` + `_sd_configs`).
- **Path end:** a path ends at a node when the key is fully consumed, or when the remaining key is one of the node's fields.
  - If the remaining key is a field that also has an edge out, the edge is not followed and the path ends at the node containing the field. For example, `scrape_configs.relabel_configs.action` ends at `/model/relabel.Config`, not `/model/relabel.Action`. This reflects the overapproximation that if a type has access to one key, it also has access to all other keys in the section containing that key.
  - Once a path ends, no further edges are followed from that node, including edges with no key segment.
    - [The following doesn't matter] So if a node and a struct inlined into it both have a field with the remaining key, only the outer node is returned: the outer field shadows the inlined one.
      - That situation shouldn't arise in a config that loads. go-yaml (`gopkg.in/yaml.v2` v2.4.0, which Prometheus uses, and `gopkg.in/yaml.v3` v3.0.1) panics with `Duplicated key '<key>' in struct <type>` when unmarshaling into such a struct. This happens whichever order the fields are declared in.
      - This was checked by reading `getStructInfo` in `yaml.go` and running a small test program.
- **Untagged fields:** go-yaml keys an untagged exported field by its lowercased name. Ignoring untagged fields is a deliberate exception to go-yaml, both when building the Unmarshaler Subgraph (`ignoreCType` in `conftamer.go` adds no edge A => B through an untagged field) and in keypaths.
  - The reason: if the document has no key for a field, go-yaml silently leaves the field as it was. So we expect following go-yaml strictly for untagged exported fields would be more likely to cause false positives (i.e. outputting keys that the developer intends to hold data not directly from the config file) than false negatives (i.e. failing to output keys that the developer didn't tag, but that are directly populated from the config file).
  - Checked against `gopkg.in/yaml.v2` v2.4.0: a field with no key in the document keeps its previous value with no error, even under `UnmarshalStrict`, which Prometheus uses. Strict mode only rejects document keys that match no field.
  - Example: `TSDBConfig.OutOfOrderTimeWindow` is untagged. go-yaml would accept `storage.tsdb.outofordertimewindow`, but Prometheus means it to hold data derived from the tagged `out_of_order_time_window`.
  - Implemented by `ct.GetFieldKey` reporting `Untagged`, which `ct.FieldKey.IsKey` turns into "not a key". "Untagged" means no tag for the chosen format, so a field with only `json:"cns"` is untagged for YAML. In the graph this removes many json/protobuf API types' edges. The struct-tags strategy (`TaggedTypes`) uses the same rule to pick starting types: a struct counts as tagged only if one of its fields is a key for the format. So those types aren't in a YAML graph at all. In Prometheus the roots go from 54 to 8, all of them YAML config types: `config.Config`, `config.ScrapeConfigs`, `config.ExternalFilesConfig`, rule files, promtool test files, and `xds.HTTPConfig`.
  - Untagged embedded fields are excluded too. In Prometheus this drops `config.URL => net/url.URL` and `relabel.Regexp => grafana/regexp.Regexp`, while the `yaml:",inline"` embeds (`HTTPClientConfig`/`OAuth2 => ProxyConfig`, `xds.HTTPConfig => HTTPClientConfig`) are kept. For JSON, untagged embedded structs should be kept, because encoding/json promotes their fields.
  - A known cost: `targetgroup.Group`'s fields are untagged, so `scrape_configs.static_configs.targets` now ends at `/discovery.Configs (custom UnmarshalYAML)`. `Group.UnmarshalYAML` actually decodes `targets` and `labels` through an anonymous struct that *is* tagged. Handling that needs analysis of what each custom unmarshaler decodes.
- **Cycles:** a node already on the current path is never revisited.
- **Partial path:** a partial path is recorded at a dead end, where some key remains but nothing matches. It is cut back to the node where part of the key was last consumed (to avoid outputting many partial paths found by following edges with no key segment).
  - A partial path is dropped if another partial path for the same key extends it. This happens when one branch through edges with no key segment dead-ends sooner than a sibling branch: e.g. `RemoteReadConfig => HTTPClientConfig => ProxyConfig` for an `http_headers` key, when `RemoteReadConfig => HTTPClientConfig => Headers` matched further.

## Known limitations (from the graph, not the matching)
- A custom unmarshaler is detected from the node's method list in the graph. `CopyMethods` (`conftamer/gopls.go`) fills that list from `types.Named.Methods()`, which only has declared methods. So a struct that gets `UnmarshalYAML` by embedding a type that has one isn't detected, even though go-yaml would call the promoted method.
