# configkeys

Finds every config key in the two input files:
- An Unmarshaler Subgraph
  - Key = pattern the graph allows, e.g.:
    - `global.external_labels.*`
    - `scrape_configs.*.role`
    - `global.scrape_interval`
- A concrete config file (or a keys file)
  - Key = concrete key, e.g.:
    - `global.external_labels.cluster`
    - `scrape_configs.kubernetes_sd_configs.role`
    - `global.scrape_interval`

For each key, find other keys in the same section.

For each concrete key, output the corresponding values.

Each edge's key segment comes from `ct.ASTPathKeySegment` (`conftamer/keysegments.go`), which the DOT edge labels also use, so they follow the same rules.

This replaces `keypaths`, `prom-test-infra/prombench/extract_param_values.py`, and `dlv --dump-params` (`ASTPathToParams`) and `querygraph/dumpparams`, which have been removed along with the rest of `dlv`'s message-send tracker (all in git history).

## Usage
```
go run . --unmarshaler-subgraph <graph serialized by Serialize, e.g. unmarshaler_subgraph.text> \
  --module-prefix <module as in go.mod, e.g. github.com/prometheus/prometheus> \
  --output <output csv> \
  [--config <config file> [--txt-dir <dir> [--strings-only]] | --keys-txt <param_keys.txt> | --keys-csv <csv>] \
  [--format yaml|json] [--root <node>] [--through <node>] [--enumerate=false]
```
For example, for Prometheus:
```
go run . --config .../prom-test-infra/prombench/fuzzed_prometheus.yaml --txt-dir .../prom-test-infra/prombench --strings-only \
  --unmarshaler-subgraph <dir>/unmarshaler_subgraph.text --module-prefix github.com/prometheus/prometheus \
  --root /config.Config --output keys.csv
```
- `--format` is the config file's format, which determines whose unmarshaling behavior is followed: `yaml` follows `gopkg.in/yaml.v2` (the default), and `json` will follow `encoding/json` (not yet supported). Generate the graph with the same format (`gopls conftamer -format`).
- `--root` names the node as it appears in the serialized graph, i.e. with the module prefix cut. By default matching and enumeration start from every root of the graph. Other roots can be types for other files in the same format, e.g. promtool's test files, and they can add spurious matches and patterns.
- `--through` keeps only keys with a path through the given node, and only those paths: the keys accessible from a (possibly non-root) type, e.g. `github.com/prometheus/common/config.TLSConfig`.
- Concrete keys come from one of:
  - `--config`: the config file, parsed with the format's own parser (`gopkg.in/yaml.v3` nodes, or `encoding/json` tokens). Keys are the dot-separated map keys down to each scalar; list indices aren't part of keys. YAML aliases are followed (each use is a separate occurrence) and merge keys (`<<`) add the merged mapping's keys. Empty values are skipped.
  - `--keys-txt`: lines `<value>:<key>[,<key>...]`, e.g. `param_keys.txt`. The keys come after the last colon on each line. No values.
  - `--keys-csv`: a csv whose `param_keys` column holds comma-separated keys. No values.
- `--txt-dir` (with `--config`) writes, as `extract_param_values.py` did:
  - `param_values.txt`: each distinct value once, in order of first appearance
  - `param_keys.txt`: `<value>:<key>[,<key>...]`, every distinct key at which the value appears

  Values are written as they appear in the file (after unquoting, e.g. `true`, `5s`), with newlines (e.g. in inline PEM blocks) as a literal `\n`. Unlike the script, `no_proxy` values aren't split on commas: the value is stored whole.
- `--strings-only` (with `--txt-dir`) writes only values whose key's Go values are stored as strings or byte arrays/slices, or structs, since memory is searched for strings, including inside structs (e.g. `config.URL`, `relabel.Regexp`, `relabel.Action`, `config.Secret`). This excludes numbers (including `model.Duration`, `config.TLSVersion`, `units.Base2Bytes`) and bools. It's by the key's Go type, not the YAML value: go-yaml stores an unquoted `true` in a string field as `"true"`. If the key's kind is unknown (no full path, or decided by a dynamic custom unmarshaler) or an interface, it falls back to whether the parser resolves the value to a string. This replaces the script's YAML-tag check and hard-coded Prometheus duration regex.

## Output
One row per key, with columns `param_key,source,pattern,values,value kind,Unmarshaler,Num Unmarshalers,Unmarshaler Path,section keys`:
- `source`: `config` (a concrete key), `graph` (a pattern), or `both` (a pattern with no `*` that's also in the config)
- `pattern`: for concrete keys, the patterns they instantiate via the same path, joined with `; `. Every concrete key with a full path should have one; the tool warns about any that don't.
- `values`: for concrete keys from `--config`, their distinct values as a JSON array, in document order
- `value kind`: the kind of Go value the key's values are stored as (`string`, `number`, `bool`, `struct`, `interface`, `other`, or `unknown`), from the underlying type of the field or node holding them, after stripping pointers, slices/arrays and maps (whose elements and values are the leaves). `[]byte` is a string. A custom unmarshaler that decodes a map (e.g. `labels.Labels`) has its map's value kind. Joined with `; ` if the paths differ. These come from the graph's `Kind` and `FieldKinds` (and `DecodedFields`), so regenerate older graphs.
- `Unmarshaler`: the distinct last types on the key's paths, joined with `; `
- `Num Unmarshalers`: the number of distinct Unmarshalers. It's 0 for keys with no full path, even though `Unmarshaler` then lists the last types of the partial paths.
- `Unmarshaler Path`: all paths for the key, joined with `; `, e.g. `/config.Config => /config.ScrapeConfig`
- `section keys`: the keys in the section containing the key, joined with `; `.
  - **A section** is the keys of one Unmarshaler (the last type on a key's path: the struct whose field holds the value) at one place in the file, i.e. with the same **parent key** (the key minus its last segment, e.g. `alerting.alertmanagers` for `alerting.alertmanagers.timeout`). Its keys are the Unmarshaler's key fields (its decoded fields, if it has `DecodedFields`) that hold scalar values themselves, i.e. that need no edge beyond the key's own paths in `Unmarshaler Path`.
  - It always includes the key itself. It's the union over the key's full paths, and empty for keys with no full path.
  - These fields of the Unmarshaler are excluded:
    - fields whose type is another node in the graph, other than a map of scalars (see Maps below): their keys are in that node's sections, e.g. `scrape_configs.relabel_configs` (`relabel.Config`). Fields of scalar-like types have no node, so they're included, e.g. `scrape_configs.scrape_interval` (`model.Duration`). (In older graphs with scalar-like nodes, fields with an edge to one are included too.)
    - fields of inlined structs: their keys have the inlined type as Unmarshaler, so they form their own section at the same parent key, e.g. `HTTPClientConfig`'s `scrape_configs.bearer_token_file`
    - fields that aren't keys: unexported (following go-yaml), untagged (our exception), and `yaml:"-"`
  - For example, `scrape_configs.scrape_failure_log_file` gets `scrape_configs.job_name`, `scrape_configs.scrape_interval` and `scrape_configs.params.*`, but not `scrape_configs.relabel_configs.action`.
  - Another example: the section of `alerting.alertmanagers.timeout` is the 4 keys `alerting.alertmanagers.api_version`, `.path_prefix`, `.scheme` and `.timeout`, of `/config.AlertmanagerConfig`. Its other fields aren't in it:
    - `RelabelConfigs`, `AlertRelabelConfigs` and `SigV4Config` are other nodes: e.g. `alerting.alertmanagers.relabel_configs.action` is in the section of `/model/relabel.Config` at `alerting.alertmanagers.relabel_configs`.
    - `HTTPClientConfig` is inlined: `alerting.alertmanagers.bearer_token_file` is in `HTTPClientConfig`'s section at `alerting.alertmanagers`.
    - `ServiceDiscoveryConfigs` is `yaml:"-"`: its keys come from the SD configs, e.g. `alerting.alertmanagers.kubernetes_sd_configs.role`.
  - **Maps:** `*` stands for any map key.
    - A map's values (`<field key>.*`, or `<prefix>.*` for an inline map) are in the section of the struct holding the map, like its other fields; so for a map field, the parent key is the key minus its last two segments. A section is meant to group keys a user would consider closely related, so this holds whether or not the map type is its own node in the graph (it is if it's a named map type, like `type ProxyHeader map[string][]Secret`, or a type whose custom unmarshaler decodes a map, like `labels.Labels`; an unnamed `map[...]...` field type has no node).
      - E.g. `remote_read.headers.*` (an unnamed map) and its instances, like `remote_read.headers.X-Fuzz-Remote-Read`, are in `RemoteReadConfig`'s section at `remote_read`, with `remote_read.url`.
      - E.g. `scrape_configs.proxy_connect_header.*` (`config.ProxyHeader`) is in `ProxyConfig`'s section at `scrape_configs`, with `scrape_configs.proxy_url`; and `global.external_labels.*` (`labels.Labels`) is in `GlobalConfig`'s section. The key's path and Unmarshaler are still the map's node.
    - Only maps of scalars end a key. If a map's values are another node, its keys continue below, e.g. `scrape_configs.http_headers.*.values` is in the section of `config.Header`.
  - Keys whose path ends in a dynamic custom unmarshaler absorbing the rest of the key get just the type's `<prefix>.*`.
  - In older graphs with scalar-like nodes, a key whose last edge used up the rest of it is in the section of that edge's source.
  - Where several Unmarshalers are possible, their sections are combined. For example, `scrape_configs.kubernetes_sd_configs.role` gets the combined sections of all 6 SD configs that have a `role` field.

Type names have the module prefix removed.

Rows are sorted into groups, and by key within each group:
1. Keys with no full path. Their partial paths are listed, each with a `(matched: <key prefix>)` suffix.
2. Keys whose paths end at more than one distinct Unmarshaler.
3. All other keys, which have exactly one Unmarshaler.

The tool prints counts of config keys per group, the number of patterns, and the number of config keys with no pattern.

## How concrete keys are matched
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
- **Custom unmarshalers:** go-yaml calls a type's `UnmarshalYAML` instead of applying its default rules. It doesn't do this when the type is reached through an inline field: the inlined struct's fields are decoded into the parent's mapping.
  - Checked with a test program against `gopkg.in/yaml.v2` v2.4.0:
    - **Named inline field** (`In T yaml:",inline"`): `T.UnmarshalYAML` isn't called.
    - **Slice element:** it is called.
    - **`yaml:"-"` field:** ignored, even when the YAML has that key.
    - **Embedded inline field** (`T yaml:",inline"`): the method is promoted, so the outer type is the one that has `UnmarshalYAML`.
  - **Detection** (`ct.ImplementsYAMLUnmarshaler`): `*T` implements go-yaml v2's `Unmarshaler`, with promoted methods included. It's recorded on each node as `YAMLUnmarshaler`.
  - **Classification** (`golang.FuncParamCalls` + `ct.ClassifyUnmarshaler`, when the graph is built): every use of the `unmarshal` parameter in the method's body is classified by its argument type, and recorded as the node's `UnmarshalKinds`:
    - **plain**: the receiver's own type (`type plain T; unmarshal((*plain)(c))`). The type's tags apply as usual.
    - **scalar**: a basic type, a slice of scalars, or a `TextUnmarshaler`. This includes `type plain T` for a T with a basic underlying type. The type is **scalar-like** (`ScalarLike`): a leaf, with no keys below it, e.g. `model.Duration`, `config.URL`, `relabel.Regexp`, `relabel.Action`, `config.Secret`.
    - **map**: any key. The node is marked `IsMap`, e.g. `labels.Labels`, so `global.external_labels.cluster` is an ordinary map key.
    - **struct**: another struct, whose fields become the node's key fields (`DecodedFields`). For example, `targetgroup.Group` decodes an anonymous struct with `targets` and `labels`, so `scrape_configs.static_configs.targets` ends at `targetgroup.Group`.
    - **dynamic**: `interface{}`, or `unmarshal` used any other way, e.g. passed to a helper. The type's tags are assumed to apply, and the unmarshaler may also populate `yaml:"-"` fields under keys it picks. Examples: `ScrapeConfig` and `AlertmanagerConfig` (`discovery.UnmarshalYAMLWithInlineConfigs`) and `discovery.Configs`.
  - Types without a custom unmarshaler are scalar-like if they're a basic type, a slice of scalars, or a `TextUnmarshaler`, e.g. `config.ScrapeProtocol` and `model.LabelNames`.
  - **How the matching uses this:**
    - **Scalar-like nodes:** never descended into. No key matches below them, and the graph builder doesn't look for their children. In `section keys`, fields of scalar-like types count as direct keys, e.g. `scrape_configs.scrape_interval`.
    - **`yaml:"-"` edges:** followed only when the source's unmarshaler is dynamic, as a wildcard for one key segment. It's only tried when nothing else under the node matched, and never for the last segment, since param keys are scalar leaves. For example, `scrape_configs.kubernetes_sd_configs.api_server` goes `ScrapeConfig =(yaml:"-", any segment: kubernetes_sd_configs)=> discovery.Configs => discovery.Config => kubernetes.SDConfig`, then ends at field `api_server`.
    - **Dynamic non-structs:** if nothing below matches, the rest of the key is attributed to the node, with a ` (custom UnmarshalYAML)` suffix. That only applies when the node wasn't reached through an inline field, and some of the key was already consumed.
  - **Limitations:**
    - Only the type the value is decoded *into* is recorded, not what the unmarshaler then does with it. The analysis could be extended to detect unmarshalers that change the value before storing it. For example, `relabel.Action`'s `UnmarshalYAML` (`prometheus/model/relabel/relabel.go:77`) stores `strings.ToLower(s)`, so `action: Replace` is held in memory as `replace`. That matters for searching memory for config values. Not handled yet: there's a TODO on `ct.ClassifyUnmarshaler`.
    - A node with `DecodedFields` (e.g. `xds.HTTPConfig`, whose promoted `HTTPClientConfig.UnmarshalYAML` decodes `HTTPClientConfig`'s fields) has only its decoded fields' direct keys. Its real fields' edges aren't followed, and the decoded struct's field types aren't nodes in the graph.
    - The `yaml:"-"` wildcard doesn't say *which* type the custom unmarshaler picks for the segment, so every type reachable through it that has a matching field is returned. For example, `scrape_configs.kubernetes_sd_configs.role` gets 6 Unmarshalers: the SD configs with a `role` field, i.e. hetzner, kubernetes, moby, openstack, scaleway and triton. Tying the segment to the right SD type would need Prometheus-specific knowledge: the key is each config's `Name()` + `_sd_configs`.
- **Path end:** a path ends at a node when the key is fully consumed, or when the remaining key is one of the node's fields.
  - If the remaining key is a field that also has an edge out, the edge is not followed and the path ends at the node containing the field. For example, `scrape_configs.relabel_configs.action` ends at `/model/relabel.Config`, not `/model/relabel.Action`. This reflects the overapproximation that if a type has access to one key, it also has access to all other keys in the section containing that key.
  - Once a path ends, no further edges are followed from that node, including edges with no key segment.
    - [The following doesn't matter] So if a node and a struct inlined into it both have a field with the remaining key, only the outer node is returned: the outer field shadows the inlined one.
      - That situation shouldn't arise in a config that loads. go-yaml (`gopkg.in/yaml.v2` v2.4.0, which Prometheus uses, and `gopkg.in/yaml.v3` v3.0.1) panics with `Duplicated key '<key>' in struct <type>` when unmarshaling into such a struct. This happens whichever order the fields are declared in.
      - This was checked by reading `getStructInfo` in `yaml.go` and running a small test program.
- **Untagged fields:** go-yaml keys an untagged exported field by its lowercased name. Ignoring untagged fields is a deliberate exception to go-yaml, both when building the Unmarshaler Subgraph (`ignoreCType` in `conftamer.go` adds no edge A => B through an untagged field) and here.
  - The reason: if the document has no key for a field, go-yaml silently leaves the field as it was. So we expect following go-yaml strictly for untagged exported fields would be more likely to cause false positives (i.e. outputting keys that the developer intends to hold data not directly from the config file) than false negatives (i.e. failing to output keys that the developer didn't tag, but that are directly populated from the config file).
  - Checked against `gopkg.in/yaml.v2` v2.4.0: a field with no key in the document keeps its previous value with no error, even under `UnmarshalStrict`, which Prometheus uses. Strict mode only rejects document keys that match no field.
  - Example: `TSDBConfig.OutOfOrderTimeWindow` is untagged. go-yaml would accept `storage.tsdb.outofordertimewindow`, but Prometheus means it to hold data derived from the tagged `out_of_order_time_window`.
  - Implemented by `ct.GetFieldKey` reporting `Untagged`, which `ct.FieldKey.IsKey` turns into "not a key". "Untagged" means no tag for the chosen format, so a field with only `json:"cns"` is untagged for YAML. In the graph this removes many json/protobuf API types' edges. The struct-tags strategy (`TaggedTypes`) uses the same rule to pick starting types: a struct counts as tagged only if one of its fields is a key for the format. So those types aren't in a YAML graph at all. In Prometheus the roots go from 54 to 8, all of them YAML config types: `config.Config`, `config.ScrapeConfigs`, `config.ExternalFilesConfig`, rule files, promtool test files, and `xds.HTTPConfig`.
  - Untagged embedded fields are excluded too. In Prometheus this drops `config.URL => net/url.URL` and `relabel.Regexp => grafana/regexp.Regexp`, while the `yaml:",inline"` embeds (`HTTPClientConfig`/`OAuth2 => ProxyConfig`, `xds.HTTPConfig => HTTPClientConfig`) are kept. For JSON, untagged embedded structs should be kept, because encoding/json promotes their fields.
  - `targetgroup.Group`'s own fields are untagged, but that doesn't matter: its `UnmarshalYAML` decodes a tagged anonymous struct, which becomes its key fields (see "Custom unmarshalers").
- **Cycles:** a node already on the current path is never revisited.
- **Partial path:** a partial path is recorded at a dead end, where some key remains but nothing matches. It is cut back to the node where part of the key was last consumed (to avoid outputting many partial paths found by following edges with no key segment).
  - A partial path is dropped if another partial path for the same key extends it. This happens when one branch through edges with no key segment dead-ends sooner than a sibling branch: e.g. `RemoteReadConfig => HTTPClientConfig => ProxyConfig` for an `http_headers` key, when `RemoteReadConfig => HTTPClientConfig => Headers` matched further.

## How patterns are enumerated
Depth-first from every root (or `--root`), never revisiting a node already on the current path, following the same rules as matching (`enumerate.go` mirrors `dfs` in `match.go`), so that each concrete key's full path is also a path of the pattern it instantiates:
- At each node, its `section keys` fields are patterns ending at the node: `<prefix>.<field key>`, `<prefix>.<map field key>.*`, or `<prefix>.*` for an inline map. Fields whose type is a map node, and map fields whose values are scalar-like nodes, are reached via their edge instead, as matching does.
- A map node with no key-consuming map edge (`IsMap`) gives `<prefix>.*`, in the section of the node holding the map field.
- Edges are followed with their key segment appended. `yaml:"-"` edges are followed only from dynamic unmarshalers, as a `*` segment. An edge to a scalar-like node ends a pattern there, unless the node's field already gave that key (matching stops at the field) or the edge consumes nothing.
- A dynamic non-struct (e.g. `discovery.Configs`) also gives `<prefix>.*` with the ` (custom UnmarshalYAML)` suffix, e.g. `scrape_configs.*.*`.
- So keys built at runtime appear as `*`: e.g. Prometheus `<Name()>_sd_configs` (`RegisterConfig` and `reflect.StructOf` in `discovery/registry.go`) are `scrape_configs.*`, so `scrape_configs.*.role` is one pattern for all 6 SD configs with a `role` field.

Compared with `dlv --dump-params` from `/config.Config` (`results/prom/tmp/struct_tags/dump_params_step3/all_keys.txt`, 639 keys), the 684 patterns include all of its keys, plus: keys ending at scalar-like custom types (`proxy_url`, `url`, `regex`), the `min_version` keys it missed, SD config keys such as `targets`, `labels.*` and `api_server`, `alerting.alertmanagers.relabel_configs.*`, `global.external_labels.*`, and the two `*.*` patterns of `discovery.Configs`.
