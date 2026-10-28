# keypaths

For each param key in a csv, find the path(s) through a serialized Unmarshaler Subgraph that produce that key.
This is roughly the inverse of `ASTPathToParams` in `dlv/params.go` (which goes path => key).

## Usage
```
go run . (--keys-csv <csv with a param_keys column> | --keys-txt <param_keys.txt>) \
  --unmarshaler-subgraph <graph serialized by Serialize, e.g. unmarshaler_subgraph.text> \
  --module-prefix <module as in go.mod, e.g. github.com/prometheus/prometheus> \
  --output <output csv>
```
Pass the keys in one of two ways:
- `--keys-csv`: the csv's `param_keys` column holds comma-separated lists of dot-separated keys.
- `--keys-txt`: a file with lines `<value>:<key>[,<key>...]`, such as the `param_keys.txt` written by `prom-test-infra/prombench/extract_param_values.py`. The keys come after the last colon on each line.

Keys are deduplicated.

## Output
One row per key, with columns `param_key,Unmarshaler,Num Unmarshalers,Unmarshaler Path`:
- `Unmarshaler`: the distinct last types on the key's paths, joined with `; `
- `Num Unmarshalers`: the number of distinct Unmarshalers. It's 0 for keys with no full path, even though `Unmarshaler` then lists the last types of the partial paths.
- `Unmarshaler Path`: all paths for the key, joined with `; `, e.g. `/config.Config => /config.ScrapeConfig`

Type names have the module prefix removed.

Rows are sorted into groups, and by key within each group:
1. Keys with no full path. Their partial paths are listed, each with a `(matched: <key prefix>)` suffix.
2. Keys whose paths end at more than one distinct Unmarshaler.
3. All other keys, which have exactly one Unmarshaler.

## How keys are matched
The search runs depth-first from every root of the graph, consuming the key one segment at a time:
- **Edge segment:** an edge's key segment is computed from its AST paths and the source node's tags, using `ct.AppendFieldTag` (the same logic as `ASTPathToParams`). An edge with no field segment, such as an inline field or a slice element, is followed without consuming any of the key.
  - **Embedded fields** (`T`, `*T`, `pkg.T`) are keyed as go-yaml keys them: by their yaml tag if they have one, so `""` for `yaml:",inline"`, and otherwise by the lowercased field name.
    - The graph's AST path for an embedded field has no `Field:` entry, because `ASTPath` only records field names and embedded fields have none.
    - So an edge is treated as an embedded field when its AST path starts `StructType.Fields`, `FieldList.List` and has no `Field:` entry. The field name is the target's type name, which must appear in the source's tags.
- **Map keys:** these are matched as go-yaml matches them.
  - A `MapType.Value` entry in an edge's AST path consumes exactly one key segment, whatever it is: the map key. For example, `scrape_configs.http_headers.X-Fuzz-Source.files` goes `HTTPClientConfig =http_headers=> Headers =(inline field, map key X-Fuzz-Source)=> Header`, then ends at field `files`.
  - An AST path with `MapType.Key` isn't followed, because the key type decodes the key's name, not a config section. In Prometheus this is `model.LabelSet => model.LabelName`.
  - A map whose value type isn't a CType has no `MapType.Value` edge. For these, the graph records which nodes are maps (`IsMap`) and which struct fields are maps (`MapFields`), in both cases after stripping pointers, slices and arrays. They are populated by `CopyMapInfo` in `conftamer/gopls.go`.
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
- **Untagged fields:** a field with no `yaml:"..."` tag uses its lowercased field name as its key segment, both for edge segments and for field matches at a node. (When finding the Unmarshaler graph we are more strict about untagged fields, i.e. we don't an add an edge A => B if B's field is untagged - otherwise there are many false positives in the Unmarshaler graph.) This is roughly what go-yaml does for untagged exported fields. Edge cases, with counts from the Prometheus graph:
  - **Non-yaml tags only:** fields with only other tags also get the lowercased name, e.g. `json:"cns"` becomes `computenodes`. Of the 683 fields with no yaml tag, 568 have some other tag. They're mostly on json/protobuf types that aren't config.
  - **Unexported fields:** these are lowercased and matched even though go-yaml ignores them. There are 20, e.g. `ionos.SDConfig.ionosEndpoint` and `xds.MonitoringAssignment_Target.state`. So a key like `...ionos_sd_configs.ionosendpoint` would match falsely, though none of the current keys do.
  - **Empty yaml name:** a yaml tag with an empty name and no `inline` flag (e.g. `yaml:",omitempty"`) gets `""` from `FieldToParamKey`, not the lowercased name go-yaml uses. So it is treated like an inline field. The graph has no such tags.
- **Cycles:** a node already on the current path is never revisited.
- **Partial path:** a partial path is recorded at a dead end, where some key remains but nothing matches. It is cut back to the node where part of the key was last consumed (to avoid outputting many partial paths found by following edges with no key segment).
  - A partial path is dropped if another partial path for the same key extends it. This happens when one branch through edges with no key segment dead-ends sooner than a sibling branch: e.g. `RemoteReadConfig => HTTPClientConfig => ProxyConfig` for an `http_headers` key, when `RemoteReadConfig => HTTPClientConfig => Headers` matched further.

## Known limitations (from the graph, not the matching)
- A custom unmarshaler is detected from the node's method list in the graph. `CopyMethods` (`conftamer/gopls.go`) fills that list from `types.Named.Methods()`, which only has declared methods. So a struct that gets `UnmarshalYAML` by embedding a type that has one isn't detected, even though go-yaml would call the promoted method.
