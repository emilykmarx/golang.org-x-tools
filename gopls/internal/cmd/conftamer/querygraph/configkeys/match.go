package main

// Match a concrete config key to the path(s) through the Unmarshaler Subgraph that produce it

import (
	"fmt"
	"maps"
	"slices"
	"strings"

	graph "github.com/emilykmarx/dominikbraun-graph"
	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
	dlvgraph "golang.org/x/tools/gopls/internal/cmd/conftamer/dlv/graph"
)

type outEdge struct {
	target   ct.CTypeHash
	segments []segment // possible key segments for the edge (one per AST path)
}

type segment = ct.KeySegment

type finder struct {
	g      ct.CTypeGraph
	adj    map[ct.CTypeHash][]outEdge
	roots  []ct.CTypeHash
	format ct.Format
	// Per node, from its extra edges (gopls conftamer's -extra, e.g. Caddy's modules, Prometheus' SD configs): the
	// keys of the fields that hold modules (their keys come only from the extra edges), and the selector keys (see
	// ct.KeySegment.Selector)
	module_fields map[ct.CTypeHash]map[string]bool
	selectors     map[ct.CTypeHash][]string
	// Keys (relative to the node) of modules whose type is scalar-like (e.g. Caddy's match.host, caddyhttp.MatchHost, a
	// []string) => the kind of their values: keys of the node's section, like its scalar fields
	scalar_modules map[ct.CTypeHash]map[string]ct.ValueKind
	// Likewise for modules whose type is a leaf map (see leafMap; e.g. Caddy's match.header, caddyhttp.MatchHeader):
	// their map keys (<key>.*) are keys of the node's section, as a map field's are
	map_modules map[ct.CTypeHash]map[string]ct.ValueKind

	acc           *acc   // results for the key being matched
	module_prefix string // cut from node names (set by find and enumerate)
}

// Accumulated paths for one key (concrete, or a full key from the graph)
type acc struct {
	full map[string]*pathInfo // path string => info
	// Number of times a full or dynamic path was recorded (including duplicates): whether a search below found
	// anything
	n_full int
	// Paths (with customSuffix) ending at a dynamic custom unmarshaler, which decides the keys below it: not full
	// paths (the key isn't a full key), but more than a partial path says
	dynamic map[string]bool
	partial map[string]bool // path string (with matched suffix) => true
}

// What a full path says about its key
type pathInfo struct {
	section       map[string]bool       // keys in the section containing the key
	section_names map[string]bool       // the section's name, <key prefix> @ <node> (one, normally)
	kinds         map[ct.ValueKind]bool // kind of the Go value the key's values are stored as ("" if unknown)
}

// A section (TERMINOLOGY.md): its name, <key prefix> @ <node> (the node holding the keys, with the module prefix
// cut, and the key that leads to it), and its keys
type section struct {
	name string
	keys []string
}

func newAcc() *acc {
	return &acc{full: map[string]*pathInfo{}, dynamic: map[string]bool{}, partial: map[string]bool{}}
}

// A path ending at a dynamic custom unmarshaler (see acc.dynamic)
func (a *acc) recordDynamic(path_str string) {
	a.dynamic[path_str] = true
	a.n_full++
}

func (a *acc) recordFull(path_str string, sec section, kind ct.ValueKind) {
	info, ok := a.full[path_str]
	if !ok {
		info = &pathInfo{section: map[string]bool{}, section_names: map[string]bool{}, kinds: map[ct.ValueKind]bool{}}
		a.full[path_str] = info
	}
	a.n_full++
	for _, k := range sec.keys {
		info.section[k] = true
	}
	info.section_names[sec.name] = true
	info.kinds[kind] = true
}

// Add b's paths to a
func (a *acc) merge(b *acc) {
	for path_str, info := range b.full {
		for name := range info.section_names {
			for k := range info.kinds {
				a.recordFull(path_str, section{name: name, keys: slices.Collect(maps.Keys(info.section))}, k)
			}
		}
	}
	for p := range b.dynamic {
		a.dynamic[p] = true
	}
	for p := range b.partial {
		a.partial[p] = true
	}
}

// A key in a node's section, from one of its fields
type sectionField struct {
	key         string // with MAP_KEY for any key of a map field or inline map
	kind        ct.ValueKind
	scalar_edge bool // the field has an edge out to a scalar-like type
	map_node    bool // the field has an edge out to a leaf map node (see leafMap): its values are keys here
}

// Whether the node is a map whose values are scalars, so no edge goes on from its map keys
// (e.g. config.ProxyHeader, url.Values, or labels.Labels, whose custom unmarshaler decodes a map)
func (f *finder) leafMap(node ct.CTypeHash) bool {
	n, err := f.g.Vertex(node)
	ct.CheckErr(err)
	if !n.IsMap {
		return false
	}
	for _, e := range f.adj[node] {
		for _, seg := range e.segments {
			if strings.Split(seg.Pattern, ".")[0] == MAP_KEY {
				return false
			}
		}
	}
	return true
}

// Keys in the section of `node` at `prefix` that are reachable without following any edge out of `node`:
// the node's key fields that have no edge out - except edges to scalar-like types (e.g. model.Duration), whose
// values are scalars, and to leaf map nodes (see leafMap), whose values are in the section of the struct holding the
// map, as for a map field with no node - so they're keys of this section.
// So fields of inlined structs, yaml:"-" fields, and fields whose type is another node in the graph are excluded.
// Fields that aren't keys (see fieldKey) are skipped. MAP_KEY stands for any key of a map field or inline map.
func (f *finder) sectionFields(node ct.CTypeHash, prefix string) []sectionField {
	n, err := f.g.Vertex(node)
	ct.CheckErr(err)

	// Field keys with an edge out, by what the edge's target is
	scalar_edge, other_edge := map[string]bool{}, map[string]bool{}
	map_edge := map[string]ct.ValueKind{} // => kind of the map's values
	for _, e := range f.adj[node] {
		target, err := f.g.Vertex(e.target)
		ct.CheckErr(err)
		for _, seg := range e.segments {
			edge_key := strings.Split(seg.Pattern, ".")[0]
			switch {
			case target.ScalarLike:
				scalar_edge[edge_key] = true
			case f.leafMap(e.target):
				map_edge[edge_key] = target.Kind
			default:
				other_edge[edge_key] = true
			}
		}
	}

	fields := []sectionField{}
	add := func(edge_key string, key string, field string) {
		if other_edge[edge_key] || f.module_fields[node][edge_key] {
			return // (a module field's keys come from its extra edges)
		}
		sf := sectionField{key: key, kind: ct.NodeFieldKind(n, field), scalar_edge: scalar_edge[edge_key]}
		if kind, ok := map_edge[edge_key]; ok {
			sf.map_node, sf.kind = true, kind
			if !strings.HasSuffix(key, "."+MAP_KEY) {
				sf.key = ct.JoinKey(key, MAP_KEY) // e.g. labels.Labels, a struct decoded as a map
			}
		}
		fields = append(fields, sf)
	}
	for _, field := range ct.KeyFieldNames(n) {
		field_key := f.fieldKey(n, field, false)
		switch {
		case !field_key.IsKey():
			// unexported, untagged (our exception), or yaml:"-" (only populated via an edge, by a custom unmarshaler)
		case field_key.Inline:
			if field_key.InlineMap {
				add(MAP_KEY, ct.JoinKey(prefix, MAP_KEY), field) // inline map with non-CType values
			} // inline structs are only reachable via an edge
		case ct.NodeRawField(n, field).IsMap:
			add(field_key.Key, ct.JoinKey(prefix, field_key.Key, MAP_KEY), field)
		default:
			add(field_key.Key, ct.JoinKey(prefix, field_key.Key), field)
		}
	}
	// Selectors: keys whose value picks a module (e.g. a route's handle.handler), so strings
	for _, sel := range f.selectors[node] {
		fields = append(fields, sectionField{key: ct.JoinKey(prefix, sel), kind: ct.KindString})
	}
	// Modules whose type is scalar-like: keys here, as scalar fields are
	for _, k := range slices.Sorted(maps.Keys(f.scalar_modules[node])) {
		fields = append(fields, sectionField{key: ct.JoinKey(prefix, k), kind: f.scalar_modules[node][k], scalar_edge: true})
	}
	// Modules whose type is a leaf map: their map keys are keys here (emitted at the map's node, via the edge)
	for _, k := range slices.Sorted(maps.Keys(f.map_modules[node])) {
		fields = append(fields, sectionField{key: ct.JoinKey(prefix, k, MAP_KEY), kind: f.map_modules[node][k], map_node: true})
	}
	return fields
}

// The section of `node` at `prefix`: its keys (see sectionFields), plus `key` (already matched by the path)
func (f *finder) sectionKeys(node ct.CTypeHash, prefix string, key string) section {
	keys := []string{key}
	for _, field := range f.sectionFields(node, prefix) {
		keys = append(keys, field.key)
	}
	slices.Sort(keys)
	return section{name: f.sectionName(node, prefix), keys: slices.Compact(keys)}
}

func (f *finder) sectionName(node ct.CTypeHash, prefix string) string {
	short, _ := strings.CutPrefix(string(node), f.module_prefix)
	return prefix + " @ " + short
}

// Section containing `key`, when the last edge followed consumed the rest of it, or ended at a leaf map node whose
// map key is the rest of it: the section of the edge's source (the struct holding the field, or the map field).
// If no edge consumed part of the key, just the key's `<prefix>.*`, named for `node`, where the key ends.
func (f *finder) consumedSection(last lastConsume, key string, node ct.CTypeHash) section {
	if last.src == "" {
		parts := strings.Split(key, ".")
		prefix := ct.JoinKey(parts[:len(parts)-1]...)
		return section{name: f.sectionName(node, prefix), keys: []string{ct.JoinKey(prefix, MAP_KEY)}}
	}
	return f.sectionKeys(last.src, last.prefix, key)
}

// How a node's field maps to config file keys, following the unmarshaling library for f.format (see ct.NodeFieldKey)
func (f *finder) fieldKey(n ct.CTypeNode, field string, embedded bool) ct.FieldKey {
	return ct.NodeFieldKey(n, field, embedded, f.format)
}

// Appended to paths ending at a node because of its dynamic custom unmarshaler (acc.dynamic)
func (f *finder) customSuffix() string {
	if f.format == ct.JSON {
		return " (custom UnmarshalJSON)"
	}
	return " (custom UnmarshalYAML)"
}

// Matches any one key segment (see ct.MAP_KEY)
const MAP_KEY = ct.MAP_KEY

// Key segment(s) for an edge, one per AST path that can produce a key (see ct.ASTPathKeySegment)
func (f *finder) edgeSegments(src, target ct.CTypeNode, edge graph.Edge[ct.CTypeHash]) []segment {
	segs := []segment{}
	for _, ast_path := range dlvgraph.EdgeASTPaths(edge.Properties) {
		if seg, ok := ct.ASTPathKeySegment(src, target, ast_path, f.format); ok {
			segs = append(segs, seg)
		}
	}
	slices.SortFunc(segs, func(a, b segment) int { return strings.Compare(fmt.Sprint(a), fmt.Sprint(b)) })
	return slices.Compact(segs)
}

func newFinder(g ct.CTypeGraph, format ct.Format) *finder {
	f := &finder{g: g, format: format}
	roots, _, err := graph.RootsLeaves(g)
	ct.CheckErr(err)
	slices.Sort(roots)

	adj_map, err := g.AdjacencyMap()
	ct.CheckErr(err)
	adj := map[ct.CTypeHash][]outEdge{}
	for src, targets := range adj_map {
		src_node, err := g.Vertex(src)
		ct.CheckErr(err)
		for target, edge := range targets {
			target_node, err := g.Vertex(target)
			ct.CheckErr(err)
			adj[src] = append(adj[src], outEdge{target: target, segments: f.edgeSegments(src_node, target_node, edge)})
		}
		slices.SortFunc(adj[src], func(a, b outEdge) int { return strings.Compare(string(a.target), string(b.target)) })
	}
	f.adj, f.roots = adj, roots
	f.useExtraEdges()
	return f
}

// For nodes with extra edges: record their module fields and selectors, and drop the module fields' other edges
// (e.g. Caddy's ModuleMap fields' edges to caddy.ModuleMap, a map of json.RawMessage, which would give `apps.*`):
// a module field's keys are the modules its extra edges name
func (f *finder) useExtraEdges() {
	f.module_fields, f.selectors = map[ct.CTypeHash]map[string]bool{}, map[ct.CTypeHash][]string{}
	f.scalar_modules, f.map_modules = map[ct.CTypeHash]map[string]ct.ValueKind{}, map[ct.CTypeHash]map[string]ct.ValueKind{}
	for src, outs := range f.adj {
		go_fields := map[string]bool{} // Go names of the module fields
		for _, e := range outs {
			for _, seg := range e.segments {
				if !seg.Extra {
					continue
				}
				if f.module_fields[src] == nil {
					f.module_fields[src] = map[string]bool{}
				}
				f.module_fields[src][seg.Field] = true
				go_fields[seg.GoField] = true

				if seg.Selector != "" && !slices.Contains(f.selectors[src], seg.Selector) {
					f.selectors[src] = append(f.selectors[src], seg.Selector)
				}
			}
		}
		slices.Sort(f.selectors[src])
		if f.module_fields[src] == nil {
			continue
		}
		kept := []outEdge{}
		for _, e := range outs {
			e.segments = slices.DeleteFunc(slices.Clone(e.segments), func(seg segment) bool {
				return !seg.Extra && seg.GoField != "" && go_fields[seg.GoField]
			})
			if len(e.segments) > 0 {
				kept = append(kept, e)
			}
		}
		f.adj[src] = kept
	}
	// Modules whose type is scalar-like or a leaf map (once every node's module fields are known: a map whose keys come
	// from extra edges, e.g. Caddy's MatchNot, isn't a leaf map here)
	for src, outs := range f.adj {
		for _, e := range outs {
			target, err := f.g.Vertex(e.target)
			ct.CheckErr(err)
			for _, seg := range e.segments {
				if !seg.Extra {
					continue
				}
				switch {
				case target.ScalarLike:
					if f.scalar_modules[src] == nil {
						f.scalar_modules[src] = map[string]ct.ValueKind{}
					}
					f.scalar_modules[src][seg.Pattern] = target.Kind
				case f.leafMap(e.target) && f.noModules(e.target):
					if f.map_modules[src] == nil {
						f.map_modules[src] = map[string]ct.ValueKind{}
					}
					f.map_modules[src][seg.Pattern] = target.Kind
				}
			}
		}
	}
}

// Field key => the inline keys of its objects (from the selectors: e.g. handle.handler => handle: [handler]), for
// reading JSON documents (see readDocument)
func (f *finder) inlineKeys() map[string][]string {
	keys := map[string][]string{}
	for _, sels := range f.selectors {
		for _, sel := range sels {
			parts := strings.Split(sel, ".")
			field, k := parts[len(parts)-2], parts[len(parts)-1]
			if !slices.Contains(keys[field], k) {
				keys[field] = append(keys[field], k)
			}
		}
	}
	return keys
}

// Whether node has no module fields (keys from extra edges)
func (f *finder) noModules(node ct.CTypeHash) bool {
	return len(f.module_fields[node]) == 0
}

func (f *finder) pathString(path []ct.CTypeHash, module_prefix string) string {
	names := []string{}
	for _, h := range path {
		short, _ := strings.CutPrefix(string(h), module_prefix)
		names = append(names, short)
	}
	return strings.Join(names, " => ")
}

// DFS from node, with `remaining` the part of the key not yet matched.
// path[:consumed_len] is the path up to the last edge that consumed part of the key.
// via_inline: whether we reached node via an inline field (so go-yaml won't call its custom unmarshaler).
// last: the last edge that consumed part of the key (to find the section containing the key if an edge consumes the rest).
func (f *finder) dfs(node ct.CTypeHash, key, remaining string, path []ct.CTypeHash, consumed_len int, via_inline bool, last lastConsume, on_path map[ct.CTypeHash]bool, module_prefix string) {
	prefix := strings.TrimSuffix(strings.TrimSuffix(key, remaining), ".") // part of the key consumed so far
	n, err := f.g.Vertex(node)
	ct.CheckErr(err)
	if remaining == "" {
		// An edge consumed the rest: the key is in the section of the edge's source (or in a map, if it ended with a map key)
		f.acc.recordFull(f.pathString(path, module_prefix), f.consumedSection(last, key, node), n.Kind)
		return
	}
	n_full_before := f.acc.n_full

	// Remaining key is a field of this node => path ends here, without following any edges out:
	// - even if the field also has an edge out
	//   (overapproximation: a type with access to one key has access to all keys in its section)
	// - even if an inlined struct has a field with the same key (yaml panics on such duplicates)
	if n.ScalarLike {
		// Config values of this type are scalars (e.g. a custom unmarshaler decodes a string): no keys below it
		f.recordPartial(path[:consumed_len], key, remaining, module_prefix)
		return
	}
	for _, field := range ct.KeyFieldNames(n) {
		if field_key := f.fieldKey(n, field, false); field_key.IsKey() && !field_key.Inline && field_key.Key == remaining &&
			!f.module_fields[node][remaining] {
			f.acc.recordFull(f.pathString(path, module_prefix), f.sectionKeys(node, prefix, key), ct.NodeFieldKind(n, field))
			return
		}
	}
	if slices.Contains(f.selectors[node], remaining) {
		f.acc.recordFull(f.pathString(path, module_prefix), f.sectionKeys(node, prefix, key), ct.KindString)
		return
	}
	if kind, ok := f.scalar_modules[node][remaining]; ok {
		f.acc.recordFull(f.pathString(path, module_prefix), f.sectionKeys(node, prefix, key), kind)
		return
	}

	matched := false
	consumed := false // whether an edge that consumes part of the key matched

	follow := func(e outEdge, seg segment, next string, next_consumed_len int, pattern string) {
		matched = true
		next_last := last
		if pattern != "" {
			next_last = lastConsume{src: node, prefix: prefix, pattern: pattern}
		}
		on_path[e.target] = true
		f.dfs(e.target, key, next, append(slices.Clone(path), e.target), next_consumed_len, seg.Inline, next_last, on_path, module_prefix)
		delete(on_path, e.target)
	}

	// Follow edges whose segment is a prefix of the remaining key
	for _, e := range f.adj[node] {
		if on_path[e.target] {
			continue // cycle
		}
		for _, seg := range e.segments {
			if seg.Dash {
				continue // handled below
			}
			next, next_consumed_len := remaining, consumed_len
			if seg.Pattern == "" {
				// no key consumed
			} else if rest, ok := matchSegment(seg.Pattern, remaining); ok {
				next, next_consumed_len = rest, len(path)+1
				consumed = true
			} else {
				continue
			}
			follow(e, seg, next, next_consumed_len, seg.Pattern)
		}
	}

	// No field or key-consuming edge matched => remaining key may be a map key of a map whose value type isn't a CType
	// (so no edge with MapType.Value): a map node + one segment, or a map field's key + one segment
	if !consumed {
		if map_section, kind, ok := f.mapKeyMatch(node, n, remaining); ok {
			// A map's values are in the section of the struct holding the map, as for the struct's other fields:
			// this node's, for a map field or inline map, or for a map that's its own node, the node holding the
			// field that led here (as in enumerate)
			section := f.sectionKeys(node, prefix, key)
			if n.IsMap && map_section == MAP_KEY {
				section = f.consumedSection(last, key, node)
			}
			f.acc.recordFull(f.pathString(path, module_prefix), section, kind)
			return
		}
	}

	// Nothing found yet (including via inline fields) => try yaml:"-" fields, if this node has a dynamic custom
	// unmarshaler (else nothing populates them from the file). It chooses the field's key, so match any one segment -
	// but not the last one, since param keys are leaves and the field's type is a CType.
	if f.acc.n_full == n_full_before && n.DynamicUnmarshaler() {
		if _, rest, found := strings.Cut(remaining, "."); found {
			for _, e := range f.adj[node] {
				if on_path[e.target] {
					continue
				}
				for _, seg := range e.segments {
					if seg.Dash {
						follow(e, seg, rest, len(path)+1, MAP_KEY)
					}
				}
			}
		}
	}

	// Nothing below matched, but this is a non-struct with a dynamic custom unmarshaler that go-yaml will call =>
	// we don't know how its keys map to types, so attribute the rest of the key to it.
	// (Other custom unmarshalers are classified: see ct.ClassifyUnmarshaler.)
	if f.acc.n_full == n_full_before && n.DynamicUnmarshaler() && n.Tags == nil && !via_inline && remaining != key {
		// The custom unmarshaler decides the keys below (and the kind of the values): not a full key
		f.acc.recordDynamic(f.pathString(path, module_prefix) + f.customSuffix())
		return
	}

	// Dead end with some of the key matched => partial path, up to where the key was last consumed
	if !matched {
		f.recordPartial(path[:consumed_len], key, remaining, module_prefix)
	}
}

// Record a partial path (if some of the key was matched)
func (f *finder) recordPartial(path []ct.CTypeHash, key, remaining string, module_prefix string) {
	if remaining == key {
		return
	}
	matched_prefix := strings.TrimSuffix(strings.TrimSuffix(key, remaining), ".")
	f.acc.partial[fmt.Sprintf("%v (matched: %v)", f.pathString(path, module_prefix), matched_prefix)] = true
}

// Whether `remaining` is a map key of the node (if a map), or a map field's key followed by a map key.
// If so, also return the map's key pattern relative to the node (MAP_KEY, or <field key>.MAP_KEY),
// and the kind of the map's values.
func (f *finder) mapKeyMatch(node ct.CTypeHash, n ct.CTypeNode, remaining string) (string, ct.ValueKind, bool) {
	field_key, map_key, found := strings.Cut(remaining, ".")
	if !found {
		// One segment: map key of the node itself (unless its keys come from extra edges: see enumerate), or of an
		// inline map field
		if n.IsMap && f.noModules(node) {
			return MAP_KEY, n.Kind, true
		}
		for _, field := range ct.KeyFieldNames(n) {
			if field_key := f.fieldKey(n, field, false); field_key.IsKey() && field_key.InlineMap {
				return MAP_KEY, ct.NodeFieldKind(n, field), true
			}
		}
		return "", "", false
	}
	if strings.Contains(map_key, ".") {
		return "", "", false
	}
	for _, field := range ct.KeyFieldNames(n) {
		if k := f.fieldKey(n, field, false); ct.NodeRawField(n, field).IsMap && k.IsKey() && !k.Inline && k.Key == field_key &&
			!f.module_fields[node][field_key] {
			return ct.JoinKey(field_key, MAP_KEY), ct.NodeFieldKind(n, field), true
		}
	}
	return "", "", false
}

// The last edge that consumed part of the key: its source, the key consumed before it, and its segment pattern
type lastConsume struct {
	src     ct.CTypeHash
	prefix  string
	pattern string
}

// If edge segment `seg` (dot-separated, possibly with MAP_KEY parts) is a prefix of `remaining`,
// return the rest of `remaining`
func matchSegment(seg, remaining string) (string, bool) {
	seg_parts := strings.Split(seg, ".")
	rem_parts := strings.Split(remaining, ".")
	if len(seg_parts) > len(rem_parts) {
		return "", false
	}
	for i, seg_part := range seg_parts {
		if seg_part != MAP_KEY && seg_part != rem_parts[i] {
			return "", false
		}
	}
	return strings.Join(rem_parts[len(seg_parts):], "."), true
}

// Paths for a concrete config key, from all roots (full paths, or if none, the longest partial paths)
func (f *finder) find(key, module_prefix string) *acc {
	f.acc, f.module_prefix = newAcc(), module_prefix
	for _, root := range f.roots {
		f.dfs(root, key, key, []ct.CTypeHash{root}, 1, false, lastConsume{}, map[ct.CTypeHash]bool{root: true}, module_prefix)
	}

	// Drop partial paths that another partial path extends (found by a sibling branch that dead-ended sooner)
	for p := range f.acc.partial {
		p_nodes, _, _ := strings.Cut(p, " (matched: ")
		for q := range f.acc.partial {
			if strings.HasPrefix(q, p_nodes+" => ") {
				delete(f.acc.partial, p)
				break
			}
		}
	}
	return f.acc
}
