package main

// For each param key in a csv, find the path(s) through the Unmarshaler Subgraph that produce it
// (roughly the inverse of ASTPathToParams in dlv/params.go)

import (
	"encoding/csv"
	"flag"
	"fmt"
	"log"
	"os"
	"slices"
	"strconv"
	"strings"

	graph "github.com/emilykmarx/dominikbraun-graph"
	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
	dlvgraph "golang.org/x/tools/gopls/internal/cmd/conftamer/dlv/graph"
	"golang.org/x/tools/gopls/internal/golang"
)

type outEdge struct {
	target   ct.CTypeHash
	segments []segment // possible key segments for the edge (one per AST path)
}

type segment struct {
	pattern string // dot-separated, possibly with MAP_KEY parts; "" if no key consumed
	inline  bool   // via an inline field (go-yaml won't call the target's custom unmarshaler)
	dash    bool   // via a yaml:"-" field (go-yaml ignores it; only a custom unmarshaler of the source can populate it)
}

type finder struct {
	g      ct.CTypeGraph
	adj    map[ct.CTypeHash][]outEdge
	roots  []ct.CTypeHash
	format ct.Format

	// per-key results
	full    map[string]bool // path string => true
	n_full  int             // number of times a full path was recorded (including duplicates)
	partial map[string]bool // path string (with matched suffix) => true
	section map[string]bool // keys in the sections containing the key, across full paths
}

func (f *finder) recordFull(path_str string, section_keys []string) {
	f.full[path_str] = true
	f.n_full++
	for _, k := range section_keys {
		f.section[k] = true
	}
}

// Join key parts with ".", skipping empty ones
func joinKey(parts ...string) string {
	return strings.Join(slices.DeleteFunc(slices.Clone(parts), func(p string) bool { return p == "" }), ".")
}

// Keys in the section of `node` at `prefix` that are reachable without following any edge out of `node`:
// the key itself (already matched by the path), plus the node's fields that have no edge out.
// So fields of inlined structs, yaml:"-" fields, and fields whose type is a node in the graph are excluded.
// Fields that aren't keys (see fieldKey) are skipped. MAP_KEY stands for any key of an inline map.
func (f *finder) sectionKeys(node ct.CTypeHash, prefix string, key string) []string {
	keys := []string{key}
	n, err := f.g.Vertex(node)
	ct.CheckErr(err)

	// Field keys with an edge out
	edge_keys := map[string]bool{}
	for _, e := range f.adj[node] {
		for _, seg := range e.segments {
			edge_keys[strings.Split(seg.pattern, ".")[0]] = true
		}
	}

	for field := range n.Tags {
		field_key := f.fieldKey(n, field, false)
		switch {
		case !field_key.IsKey():
			// unexported, untagged (our exception), or yaml:"-" (only populated via an edge, by a custom unmarshaler)
		case field_key.Inline:
			if field_key.InlineMap && !edge_keys[MAP_KEY] {
				keys = append(keys, joinKey(prefix, MAP_KEY)) // inline map with non-CType values
			} // inline structs are only reachable via an edge
		default:
			if !edge_keys[field_key.Key] {
				keys = append(keys, joinKey(prefix, field_key.Key))
			}
		}
	}
	slices.Sort(keys)
	return slices.Compact(keys)
}

// Whether the node has a custom UnmarshalYAML (go-yaml calls it instead of applying its default rules)
func customUnmarshaler(n ct.CTypeNode) bool {
	return slices.ContainsFunc(n.Methods, func(m ct.FullTypeName) bool { return strings.HasSuffix(string(m), ".UnmarshalYAML") })
}

// How a node's field maps to config file keys, following the unmarshaling library for f.format (see ct.GetFieldKey).
// Callers apply our untagged-field exception via FieldKey.IsKey.
// embedded: the field is known to be embedded (for graphs serialized before EmbeddedFields was recorded).
func (f *finder) fieldKey(n ct.CTypeNode, field string, embedded bool) ct.FieldKey {
	raw := ct.NodeRawField(n, field)
	raw.Embedded = raw.Embedded || embedded
	key, err := ct.GetFieldKey(raw, f.format)
	if err != nil {
		log.Printf("Warning: treating field as not a key: %v (in %v)", err, n.Names)
		return ct.FieldKey{Skip: true}
	}
	return key
}

// Whether the field can be followed to a child: a key, or yaml:"-" (populated by a custom unmarshaler, if any)
func followable(key ct.FieldKey) bool {
	return key.IsKey() || key.Dash
}

// Name of the embedded field the AST path goes through, if any.
// ASTPath records no Field: edge for embedded fields (they have no names), so identify them as
// a field of the source struct with no Field: edge, and take the field name from the target's type name.
func embeddedField(src, target ct.CTypeNode, ast_path ct.ASTPath) (string, bool) {
	if len(ast_path) < 2 || ast_path[0] != "StructType.Fields" || ast_path[1] != "FieldList.List" {
		return "", false
	}
	for _, ast_edge := range ast_path {
		if strings.HasPrefix(ast_edge, golang.FIELD_NAME_PREFIX) {
			return "", false
		}
	}
	for _, name := range target.Names {
		short := string(name)
		short = short[strings.LastIndex(short, ".")+1:]
		short, _, _ = strings.Cut(short, "[") // type args
		if _, ok := src.Tags[short]; ok {
			return short, true
		}
	}
	log.Printf("Warning: embedded field from %v to %v not found in tags %v", src.Names, target.Names, src.Tags)
	return "", false
}

// Appended to paths ending at a node because of its custom unmarshaler
const CUSTOM_SUFFIX = " (custom UnmarshalYAML)"

// Matches any one key segment (a map key)
const MAP_KEY = "*"

// Key segment(s) for an edge, one per AST path, with fields keyed as the unmarshaling library does (see fieldKey).
// An AST path through a field that isn't a key (e.g. unexported, or untagged per our exception) gives no segment.
// Map values are keyed by any one segment (the map key), represented as MAP_KEY.
// Edges to map key types aren't followed (the key type decodes the key's name, not a section) => no segment.
func (f *finder) edgeSegments(src, target ct.CTypeNode, edge graph.Edge[ct.CTypeHash]) []segment {
	segs := []segment{}
ast_paths:
	for _, ast_path := range dlvgraph.EdgeASTPaths(edge.Properties) {
		if slices.Contains(ast_path, "MapType.Key") {
			continue
		}
		seg := segment{}
		add_field := func(key ct.FieldKey) bool {
			if !followable(key) {
				return false
			}
			seg.pattern = joinKey(seg.pattern, key.Key)
			seg.inline = seg.inline || key.Inline
			seg.dash = seg.dash || key.Dash
			return true
		}
		if field, ok := embeddedField(src, target, ast_path); ok {
			if !add_field(f.fieldKey(src, field, true)) {
				continue
			}
		}
		for _, ast_edge := range ast_path {
			if field, ok := strings.CutPrefix(ast_edge, golang.FIELD_NAME_PREFIX); ok {
				if !add_field(f.fieldKey(src, field, false)) {
					continue ast_paths
				}
			} else if ast_edge == "MapType.Value" {
				seg.pattern = joinKey(seg.pattern, MAP_KEY)
			}
		}
		segs = append(segs, seg)
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
	return f
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
	if remaining == "" {
		// An edge consumed the rest: the key is in the section of the edge's source (or in a map, if it ended with a map key)
		var section []string
		if pattern_parts := strings.Split(last.pattern, "."); pattern_parts[len(pattern_parts)-1] == MAP_KEY {
			section = []string{joinKey(last.prefix, joinKey(pattern_parts[:len(pattern_parts)-1]...), MAP_KEY)}
		} else {
			section = f.sectionKeys(last.src, last.prefix, key)
		}
		f.recordFull(f.pathString(path, module_prefix), section)
		return
	}
	n_full_before := f.n_full

	// Remaining key is a field of this node => path ends here, without following any edges out:
	// - even if the field also has an edge out
	//   (overapproximation: a type with access to one key has access to all keys in its section)
	// - even if an inlined struct has a field with the same key (yaml panics on such duplicates)
	n, err := f.g.Vertex(node)
	ct.CheckErr(err)
	for field := range n.Tags {
		if field_key := f.fieldKey(n, field, false); field_key.IsKey() && !field_key.Inline && field_key.Key == remaining {
			f.recordFull(f.pathString(path, module_prefix), f.sectionKeys(node, prefix, key))
			return
		}
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
		f.dfs(e.target, key, next, append(slices.Clone(path), e.target), next_consumed_len, seg.inline, next_last, on_path, module_prefix)
		delete(on_path, e.target)
	}

	// Follow edges whose segment is a prefix of the remaining key
	for _, e := range f.adj[node] {
		if on_path[e.target] {
			continue // cycle
		}
		for _, seg := range e.segments {
			if seg.dash {
				continue // handled below
			}
			next, next_consumed_len := remaining, consumed_len
			if seg.pattern == "" {
				// no key consumed
			} else if rest, ok := matchSegment(seg.pattern, remaining); ok {
				next, next_consumed_len = rest, len(path)+1
				consumed = true
			} else {
				continue
			}
			follow(e, seg, next, next_consumed_len, seg.pattern)
		}
	}

	// No field or key-consuming edge matched => remaining key may be a map key of a map whose value type isn't a CType
	// (so no edge with MapType.Value): a map node + one segment, or a map field's key + one segment
	if !consumed {
		if map_section, ok := f.mapKeyMatch(n, remaining); ok {
			f.recordFull(f.pathString(path, module_prefix), []string{joinKey(prefix, map_section)})
			return
		}
	}

	// Nothing found yet (including via inline fields) => try yaml:"-" fields, if this node has a custom unmarshaler
	// (else nothing populates them). The custom unmarshaler chooses the field's key, so match any one segment -
	// but not the last one, since param keys are leaves and the field's type is a CType.
	if f.n_full == n_full_before && customUnmarshaler(n) {
		if _, rest, found := strings.Cut(remaining, "."); found {
			for _, e := range f.adj[node] {
				if on_path[e.target] {
					continue
				}
				for _, seg := range e.segments {
					if seg.dash {
						follow(e, seg, rest, len(path)+1, MAP_KEY)
					}
				}
			}
		}
	}

	// Nothing below matched, but this is a non-struct with a custom unmarshaler that go-yaml will call =>
	// default go-yaml rules (tags etc) don't say how its keys map to types, so attribute the rest of the key to it.
	// (Structs with custom unmarshalers are assumed to use the `type plain T` pattern, so their tags still apply.)
	if f.n_full == n_full_before && customUnmarshaler(n) && n.Tags == nil && !via_inline && remaining != key {
		// The custom unmarshaler decides the section's keys
		f.recordFull(f.pathString(path, module_prefix)+CUSTOM_SUFFIX, []string{joinKey(prefix, MAP_KEY)})
		return
	}

	// Dead end with some of the key matched => partial path, up to where the key was last consumed
	if !matched && remaining != key {
		path = path[:consumed_len]
		matched_prefix := strings.TrimSuffix(strings.TrimSuffix(key, remaining), ".")
		f.partial[fmt.Sprintf("%v (matched: %v)", f.pathString(path, module_prefix), matched_prefix)] = true
	}
}

// Whether `remaining` is a map key of the node (if a map), or a map field's key followed by a map key.
// If so, also return the map's section key relative to the node (MAP_KEY, or <field key>.MAP_KEY).
func (f *finder) mapKeyMatch(n ct.CTypeNode, remaining string) (string, bool) {
	field_key, map_key, found := strings.Cut(remaining, ".")
	if !found {
		// One segment: map key of the node itself, or of an inline map field
		if n.IsMap {
			return MAP_KEY, true
		}
		for field := range n.MapFields {
			if field_key := f.fieldKey(n, field, false); field_key.IsKey() && field_key.InlineMap {
				return MAP_KEY, true
			}
		}
		return "", false
	}
	if strings.Contains(map_key, ".") {
		return "", false
	}
	for field := range n.MapFields {
		if k := f.fieldKey(n, field, false); k.IsKey() && !k.Inline && k.Key == field_key {
			return joinKey(field_key, MAP_KEY), true
		}
	}
	return "", false
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

type result struct {
	key          string
	unmarshalers []string
	paths        []string
	full         bool
	section_keys []string // keys in the sections containing the key (full paths only)
}

func (f *finder) find(key, module_prefix string) result {
	f.full = map[string]bool{}
	f.partial = map[string]bool{}
	f.section = map[string]bool{}
	for _, root := range f.roots {
		f.dfs(root, key, key, []ct.CTypeHash{root}, 1, false, lastConsume{}, map[ct.CTypeHash]bool{root: true}, module_prefix)
	}

	// Drop partial paths that another partial path extends (found by a sibling branch that dead-ended sooner)
	for p := range f.partial {
		p_nodes, _, _ := strings.Cut(p, " (matched: ")
		for q := range f.partial {
			if strings.HasPrefix(q, p_nodes+" => ") {
				delete(f.partial, p)
				break
			}
		}
	}

	res := result{key: key, full: len(f.full) > 0}
	paths := f.partial
	if res.full {
		paths = f.full
	}
	for p := range paths {
		res.paths = append(res.paths, p)
		last, _, _ := strings.Cut(p, " (")
		if i := strings.LastIndex(last, " => "); i != -1 {
			last = last[i+len(" => "):]
		}
		res.unmarshalers = append(res.unmarshalers, last)
	}
	if res.full {
		for k := range f.section {
			res.section_keys = append(res.section_keys, k)
		}
		slices.Sort(res.section_keys)
	}
	slices.Sort(res.paths)
	slices.Sort(res.unmarshalers)
	res.unmarshalers = slices.Compact(res.unmarshalers)
	return res
}

// Read keys from a file with lines "<value>:<key>[,<key>...]" (as written by extract_param_values.py)
func readKeysTxt(keys_txt string) []string {
	contents, err := os.ReadFile(keys_txt)
	ct.CheckErr(err)
	keys := []string{}
	for _, line := range strings.Split(string(contents), "\n") {
		i := strings.LastIndex(line, ":")
		if i == -1 {
			continue
		}
		for _, key := range strings.Split(line[i+1:], ",") {
			if key = strings.TrimSpace(key); key != "" {
				keys = append(keys, key)
			}
		}
	}
	slices.Sort(keys)
	return slices.Compact(keys)
}

func readKeys(keys_csv string) []string {
	fd, err := os.Open(keys_csv)
	ct.CheckErr(err)
	defer fd.Close()
	records, err := csv.NewReader(fd).ReadAll()
	ct.CheckErr(err)

	col := slices.Index(records[0], "param_keys")
	if col == -1 {
		log.Fatalf("No param_keys column in %v", keys_csv)
	}
	keys := []string{}
	for _, record := range records[1:] {
		for _, key := range strings.Split(record[col], ",") {
			if key = strings.TrimSpace(key); key != "" {
				keys = append(keys, key)
			}
		}
	}
	slices.Sort(keys)
	return slices.Compact(keys)
}

func main() {
	var keys_csv, keys_txt, unmarshaler_subgraph, module_prefix, output, root, format_arg string
	flag.StringVar(&keys_csv, "keys-csv", "", "csv with a param_keys column (or pass --keys-txt)")
	flag.StringVar(&keys_txt, "keys-txt", "", `file with lines "<value>:<key>[,<key>...]", e.g. param_keys.txt from extract_param_values.py (or pass --keys-csv)`)
	flag.StringVar(&unmarshaler_subgraph, "unmarshaler-subgraph", "", "File containing serialized unmarshaler subgraph")
	flag.StringVar(&module_prefix, "module-prefix", "", "module as in go.mod")
	flag.StringVar(&output, "output", "", "Output csv")
	flag.StringVar(&format_arg, "format", string(ct.YAML), "Config file format: yaml (follow gopkg.in/yaml.v2) or json (follow encoding/json; not yet supported)")
	flag.StringVar(&root, "root", "", "Node to search from, as named in the serialized graph (e.g. /config.Config) - optional, default all roots")
	flag.Parse()
	if (keys_csv == "") == (keys_txt == "") || unmarshaler_subgraph == "" || module_prefix == "" || output == "" {
		flag.Usage()
		log.Fatalf("Missing mandatory argument")
	}

	format, err := ct.ParseFormat(format_arg)
	if err != nil {
		log.Fatalf("--format: %v", err)
	}

	g, _ := ct.Deserialize(unmarshaler_subgraph)
	f := newFinder(g, format)
	if root != "" {
		if _, err := g.Vertex(ct.CTypeHash(root)); err != nil {
			log.Fatalf("--root %v not in graph: %v", root, err)
		}
		if !slices.Contains(f.roots, ct.CTypeHash(root)) {
			log.Printf("Warning: --root %v is not a root of the graph (it has in-edges)", root)
		}
		f.roots = []ct.CTypeHash{ct.CTypeHash(root)}
	}

	// Groups: 0 = no full path, 1 = multiple unmarshalers, 2 = one unmarshaler
	groups := make([][]result, 3)
	var keys []string
	if keys_csv != "" {
		keys = readKeys(keys_csv)
	} else {
		keys = readKeysTxt(keys_txt)
	}
	for _, key := range keys {
		res := f.find(key, module_prefix)
		switch {
		case !res.full:
			groups[0] = append(groups[0], res)
		case len(res.unmarshalers) > 1:
			groups[1] = append(groups[1], res)
		default:
			groups[2] = append(groups[2], res)
		}
	}
	fmt.Printf("no path: %v, multiple unmarshalers: %v, one unmarshaler: %v\n", len(groups[0]), len(groups[1]), len(groups[2]))

	fd, err := os.Create(output)
	ct.CheckErr(err)
	defer fd.Close()
	w := csv.NewWriter(fd)
	ct.CheckErr(w.Write([]string{"param_key", "Unmarshaler", "Num Unmarshalers", "Unmarshaler Path", "section keys"}))
	for _, group := range groups {
		for _, res := range group {
			// Unmarshalers of partial paths don't count
			num_unmarshalers := 0
			if res.full {
				num_unmarshalers = len(res.unmarshalers)
			}
			ct.CheckErr(w.Write([]string{res.key, strings.Join(res.unmarshalers, "; "), strconv.Itoa(num_unmarshalers), strings.Join(res.paths, "; "), strings.Join(res.section_keys, "; ")}))
		}
	}
	w.Flush()
	ct.CheckErr(w.Error())
}
