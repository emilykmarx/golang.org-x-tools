package main

// Enumerate all keys the Unmarshaler Subgraph allows, as full keys (MAP_KEY for any one segment), with their paths.
// Follows the same rules as dfs (match.go), so a concrete key's full path is also a path of the full key it instantiates.
// Replaces dlv --dump-params (ASTPathToParams, removed).

import (
	"fmt"
	"log"
	"slices"
	"strings"

	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
)

// Stop enumerating if there are more paths than this (the graph's paths can grow exponentially)
const MAX_ENUM_PATHS = 10_000_000

type enumerator struct {
	*finder
	module_prefix string
	keys          map[string]*acc // full key => paths
	n_paths       int             // number of nodes visited, across all paths
}

// Full keys for all keys reachable from the finder's roots
func (f *finder) enumerate(module_prefix string) map[string]*acc {
	f.module_prefix = module_prefix
	e := &enumerator{finder: f, module_prefix: module_prefix, keys: map[string]*acc{}}
	for _, root := range f.roots {
		e.visit(root, "", []ct.CTypeHash{root}, false, lastConsume{}, map[ct.CTypeHash]bool{root: true})
	}
	return e.keys
}

func (e *enumerator) emit(key string, path_str string, sec section, kind ct.ValueKind) {
	a, ok := e.keys[key]
	if !ok {
		a = newAcc()
		e.keys[key] = a
	}
	a.recordFull(path_str, sec, kind)
}

// Visit `node`, at key `prefix` (consumed by the path so far). See dfs for via_inline and last.
func (e *enumerator) visit(node ct.CTypeHash, prefix string, path []ct.CTypeHash, via_inline bool, last lastConsume, on_path map[ct.CTypeHash]bool) {
	if e.n_paths++; e.n_paths > MAX_ENUM_PATHS {
		log.Fatalf("More than %v paths: the graph's paths are too many to enumerate (at %v)", MAX_ENUM_PATHS, e.pathString(path, e.module_prefix))
	}
	n, err := e.g.Vertex(node)
	ct.CheckErr(err)
	path_str := e.pathString(path, e.module_prefix)

	if n.ScalarLike {
		// Config values of this type are scalars: the key is a leaf (dfs: an edge consumed the rest of the key)
		if prefix != "" {
			e.emit(prefix, path_str, e.consumedSection(last, prefix, node), n.Kind)
		}
		return
	}

	// Keys of the node's own fields (dfs: the rest of the key is a field, or a map field's key + a map key).
	// Map fields whose values are scalar-like nodes, and fields of leaf map types, are reached via their edges instead.
	fields := e.sectionFields(node, prefix)
	sec := section{name: e.sectionName(node, prefix)}
	for _, field := range fields {
		sec.keys = append(sec.keys, field.key)
	}
	slices.Sort(sec.keys)
	at_node := map[string]bool{}
	for _, field := range fields {
		if field.map_node || (field.scalar_edge && strings.HasSuffix(field.key, MAP_KEY)) {
			continue // emitted at the map's node, via the edge
		}
		e.emit(field.key, path_str, sec, field.kind)
		at_node[field.key] = true
	}

	// Keys of a map with non-CType values (dfs: mapKeyMatch)
	has_map_edge := false
	for _, out := range e.adj[node] {
		for _, seg := range out.segments {
			has_map_edge = has_map_edge || strings.Split(seg.Pattern, ".")[0] == MAP_KEY
		}
	}
	// (Not for a node whose keys come from extra edges, e.g. Caddy's MatchNot: their module names are its map keys)
	if n.IsMap && !has_map_edge && prefix != "" && e.noModules(node) {
		// In the section of the struct holding the map (see sectionFields)
		map_key := ct.JoinKey(prefix, MAP_KEY)
		e.emit(map_key, path_str, e.consumedSection(last, map_key, node), n.Kind)
	}

	// Edges
	for _, out := range e.adj[node] {
		if on_path[out.target] {
			continue // cycle
		}
		target, err := e.g.Vertex(out.target)
		ct.CheckErr(err)
		for _, seg := range out.segments {
			pattern := seg.Pattern
			if seg.Dash {
				if !n.DynamicUnmarshaler() {
					continue // nothing populates the yaml:"-" field from the file
				}
				pattern = ct.JoinKey(pattern, MAP_KEY) // key chosen by the custom unmarshaler
			}
			next_prefix := ct.JoinKey(prefix, pattern)
			if target.ScalarLike && (pattern == "" || seg.Dash || at_node[next_prefix]) {
				// dfs never reaches a scalar-like node without consuming the rest of the key (yaml:"-" edges leave at
				// least one segment), and matches fields of the node before following their edges
				continue
			}
			next_last := last
			if pattern != "" {
				next_last = lastConsume{src: node, prefix: prefix, pattern: pattern}
			}
			on_path[out.target] = true
			e.visit(out.target, next_prefix, append(slices.Clone(path), out.target), seg.Inline, next_last, on_path)
			delete(on_path, out.target)
		}
	}

	// (A non-struct with a dynamic custom unmarshaler that the library will call decides the keys below it: none are
	// full keys. dfs records a concrete key that reaches one as dynamic: see acc.dynamic.)
}

// Whether concrete key `key` is an instance of full key `pattern` (same number of segments; MAP_KEY matches any segment)
func instanceOf(key, pattern string) bool {
	rest, ok := matchSegment(pattern, key)
	return ok && rest == "" && strings.Count(key, ".") == strings.Count(pattern, ".")
}

// Each key as the module spells it: a key that's an instance of no full key as written, but is of some under
// case-folding (as encoding/json matches keys to fields: an exact match first, then bytes.EqualFold), takes those
// full keys' spelling for their named segments (MAP_KEY segments keep the key's own); unchanged if there's none, or
// the full keys disagree.
func canonicalKeys(keys []string, patterns map[string]*acc) map[string]string {
	canon := map[string]string{}
	for _, key := range keys {
		if _, done := canon[key]; done {
			continue
		}
		canon[key] = key
		exact := false
		spellings := map[string]bool{}
		parts := strings.Split(key, ".")
		for pattern := range patterns {
			if instanceOf(key, pattern) {
				exact = true
				break
			}
			p_parts := strings.Split(pattern, ".")
			if len(p_parts) != len(parts) {
				continue
			}
			spelled := slices.Clone(parts)
			ok := true
			for i, p := range p_parts {
				if p != MAP_KEY {
					if !strings.EqualFold(p, parts[i]) {
						ok = false
						break
					}
					spelled[i] = p
				}
			}
			if ok {
				spellings[strings.Join(spelled, ".")] = true
			}
		}
		if !exact && len(spellings) == 1 {
			for s := range spellings {
				canon[key] = s
			}
		}
	}
	return canon
}

// Path strings of a key's full paths, as nodes (without customSuffix)
func pathNodes(path_str string) []string {
	path_str, _, _ = strings.Cut(path_str, " (")
	return strings.Split(path_str, " => ")
}

// Keep only the paths that go through `through`; returns false if none
func (a *acc) filterThrough(through string) bool {
	for p := range a.full {
		if !slices.Contains(pathNodes(p), through) {
			delete(a.full, p)
		}
	}
	for p := range a.partial {
		if !slices.Contains(pathNodes(p), through) {
			delete(a.partial, p)
		}
	}
	return len(a.full) > 0 || len(a.partial) > 0
}

func (a *acc) String() string {
	return fmt.Sprintf("full: %v, partial: %v", len(a.full), len(a.partial))
}
