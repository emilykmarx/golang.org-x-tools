package conftamer

import (
	"log"
	"slices"
	"strings"

	"golang.org/x/tools/gopls/internal/golang"
)

/* Config file key segments for edges of the CTypes graph, following the unmarshaling library for a format.
Shared by querygraph/keypaths (key => path) and dlv's ASTPathToParams (path => key). */

// Stands for any one key segment: a map key, or the key a custom unmarshaler picks for a yaml:"-" field
const MAP_KEY = "*"

// The config file key segment for one AST path of an edge (from a parent CType to a child)
type KeySegment struct {
	// Dot-separated, possibly with MAP_KEY parts; "" if no key is consumed (e.g. inline field, slice element)
	Pattern string
	Inline  bool // via an inline field (go-yaml won't call the child's custom unmarshaler)
	// Via a yaml:"-" field: the library ignores it, so only a custom unmarshaler of the parent can populate it,
	// under a key it chooses (callers decide whether to treat that as a MAP_KEY segment)
	Dash bool
}

// Join key parts with ".", skipping empty ones
func JoinKey(parts ...string) string {
	return strings.Join(slices.DeleteFunc(slices.Clone(parts), func(p string) bool { return p == "" }), ".")
}

// How a node's field maps to config file keys (see GetFieldKey). Callers apply our untagged-field exception via
// FieldKey.IsKey. embedded: the field is known to be embedded (for graphs serialized before EmbeddedFields existed).
// If the field's tag is invalid for the library, warns and treats it as not a key.
func NodeFieldKey(n CTypeNode, field string, embedded bool, format Format) FieldKey {
	raw := NodeRawField(n, field)
	raw.Embedded = raw.Embedded || embedded
	key, err := GetFieldKey(raw, format)
	if err != nil {
		log.Printf("Warning: treating field as not a key: %v (in %v)", err, n.Names)
		return FieldKey{Skip: true}
	}
	return key
}

// Whether an edge can be followed through the field: it's a key, or yaml:"-" (populated by a custom unmarshaler, if any)
func Followable(key FieldKey) bool {
	return key.IsKey() || key.Dash
}

// Key segment for one AST path of an edge src => target, with fields keyed by the unmarshaling library (see
// NodeFieldKey) and map values keyed by MAP_KEY.
// ok is false if the path can't produce a key: it goes through a field that isn't Followable (e.g. unexported, or
// untagged per our exception), or to a map's key type (which decodes the key's name, not a section).
func ASTPathKeySegment(src, target CTypeNode, ast_path ASTPath, format Format) (seg KeySegment, ok bool) {
	if slices.Contains(ast_path, "MapType.Key") {
		return seg, false
	}
	add_field := func(key FieldKey) bool {
		if !Followable(key) {
			return false
		}
		seg.Pattern = JoinKey(seg.Pattern, key.Key)
		seg.Inline = seg.Inline || key.Inline
		seg.Dash = seg.Dash || key.Dash
		return true
	}
	if field, ok := embeddedField(src, target, ast_path); ok {
		if !add_field(NodeFieldKey(src, field, true, format)) {
			return seg, false
		}
	}
	for _, ast_edge := range ast_path {
		if field, ok := strings.CutPrefix(ast_edge, golang.FIELD_NAME_PREFIX); ok {
			if !add_field(NodeFieldKey(src, field, false, format)) {
				return seg, false
			}
		} else if ast_edge == "MapType.Value" {
			seg.Pattern = JoinKey(seg.Pattern, MAP_KEY)
		}
	}
	return seg, true
}

// Name of the embedded field the AST path goes through, if any.
// ASTPath records no Field: edge for embedded fields (they have no names), so identify them as
// a field of the source struct with no Field: edge, and take the field name from the target's type name.
func embeddedField(src, target CTypeNode, ast_path ASTPath) (string, bool) {
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
