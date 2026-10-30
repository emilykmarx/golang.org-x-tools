package conftamer

import (
	"fmt"
	"go/types"
	"reflect"
	"slices"
	"strings"
	"unicode"
	"unicode/utf8"

	"golang.org/x/tools/gopls/internal/golang"
)

/* How config file keys map to struct fields, following the library that unmarshals the given format */

// Config file format, which determines the unmarshaling library whose behavior we follow
type Format string

const (
	YAML Format = "yaml" // gopkg.in/yaml.v2
	JSON Format = "json" // encoding/json (not yet supported)
)

func ParseFormat(s string) (Format, error) {
	switch Format(s) {
	case YAML:
		return YAML, nil
	case JSON:
		return "", fmt.Errorf("format %q not yet supported", s)
	default:
		return "", fmt.Errorf("unknown format %q (want %q or %q)", s, YAML, JSON)
	}
}

// What we need to know about a struct field to find its key -
// available both from go/types (when building the graph) and from a serialized graph node
type RawField struct {
	Name     string // Go field name
	Tag      string // whole struct tag
	Embedded bool
	IsMap    bool // after stripping pointers/slices/arrays (see IsMap)
}

// How a struct field maps to config file keys
type FieldKey struct {
	Key string // the field's key; "" if Inline or Skip
	// The library never populates the field from the file (e.g. unexported, or yaml:"-").
	// (A custom unmarshaler on the struct might populate it anyway - see Dash.)
	Skip   bool
	Dash   bool // yaml:"-" (also Skip)
	Inline bool // the field's keys are the parent's keys (inline struct, or with InlineMap, any key)
	// InlineMap: an inline map, which collects all keys of the parent that aren't other fields
	InlineMap bool
	// The field has no tag for this format. The library would still key it by its lowercased name, but we
	// deliberately treat it as not a key (Untagged is set; Skip is not, so callers apply this exception):
	// if the document has no key for a field, go-yaml silently leaves the field as it was - so we expect following
	// go-yaml strictly for untagged exported fields would be more likely to cause false positives (i.e. outputting
	// keys that the developer intends to hold data not directly from the config file) than false negatives (i.e.
	// failing to output keys that the developer didn't tag, but that are directly populated from the config file).
	Untagged bool
}

// Whether the field's key can appear in the config file, applying our untagged exception
func (k FieldKey) IsKey() bool {
	return !k.Skip && !k.Untagged
}

// Find how a struct field maps to config file keys.
// For YAML, ported from the per-field part of getStructInfo in gopkg.in/yaml.v2 v2.4.0 (yaml.go;
// Apache-2.0/MIT, Copyright (c) 2011-2019 Canonical Ltd), from reflect to plain field info.
// Struct-level checks (duplicate keys, multiple inline maps) aren't done.
func GetFieldKey(f RawField, format Format) (FieldKey, error) {
	if format != YAML {
		return FieldKey{}, fmt.Errorf("format %q not yet supported", format)
	}
	r, _ := utf8.DecodeRuneInString(f.Name)
	if !unicode.IsUpper(r) && !f.Embedded {
		return FieldKey{Skip: true}, nil // Private field
	}

	tag, has_tag := reflect.StructTag(f.Tag).Lookup("yaml")
	if tag == "" && strings.Index(f.Tag, ":") < 0 {
		// Legacy: whole tag is the yaml tag, if it has no "key:" parts
		tag = f.Tag
		has_tag = tag != ""
	}
	if tag == "-" {
		return FieldKey{Skip: true, Dash: true}, nil
	}

	key := FieldKey{Untagged: !has_tag}
	fields := strings.Split(tag, ",")
	if len(fields) > 1 {
		for _, flag := range fields[1:] {
			switch flag {
			case "omitempty", "flow":
			case "inline":
				key.Inline = true
			default:
				return FieldKey{}, fmt.Errorf("unsupported flag %q in tag %q of field %v", flag, tag, f.Name)
			}
		}
		tag = fields[0]
	}

	if key.Inline {
		// go-yaml allows inline structs and maps with string keys (anything else is an error)
		key.InlineMap = f.IsMap
		return key, nil
	}

	if tag != "" {
		key.Key = tag
	} else {
		key.Key = strings.ToLower(f.Name)
	}
	return key, nil
}

// RawField for field i of a struct, from go/types
func StructRawField(st *types.Struct, i int) RawField {
	v := st.Field(i)
	return RawField{Name: v.Name(), Tag: st.Tag(i), Embedded: v.Embedded(), IsMap: IsMap(v.Type())}
}

// RawField for a graph node's field (of the decoded struct, if a custom unmarshaler decodes into another struct)
func NodeRawField(n CTypeNode, field string) RawField {
	if n.DecodedFields != nil {
		d := n.DecodedFields[field]
		return RawField{Name: field, Tag: d.Tag, Embedded: d.Embedded, IsMap: d.IsMap}
	}
	return RawField{Name: field, Tag: n.Tags[field], Embedded: n.EmbeddedFields[field], IsMap: n.MapFields[field]}
}

// Whether go-yaml v2 calls the type's own UnmarshalYAML instead of applying its default rules:
// whether *T implements gopkg.in/yaml.v2's Unmarshaler, i.e. has UnmarshalYAML(func(interface{}) error) error,
// possibly promoted from an embedded field. (go-yaml checks this on every value it decodes into, before anything else.)
func ImplementsYAMLUnmarshaler(typ types.Type) bool {
	sig := lookupMethod(typ, "UnmarshalYAML")
	if sig == nil || sig.Params().Len() != 1 || !returnsOnlyError(sig) {
		return false
	}
	unmarshal, ok := sig.Params().At(0).Type().Underlying().(*types.Signature)
	if !ok || unmarshal.Params().Len() != 1 || !returnsOnlyError(unmarshal) {
		return false
	}
	iface, ok := unmarshal.Params().At(0).Type().Underlying().(*types.Interface)
	return ok && iface.Empty()
}

// Whether *T implements encoding.TextUnmarshaler (UnmarshalText([]byte) error), possibly promoted.
// go-yaml v2 uses this for scalar values (after checking for Unmarshaler).
func ImplementsTextUnmarshaler(typ types.Type) bool {
	sig := lookupMethod(typ, "UnmarshalText")
	if sig == nil || sig.Params().Len() != 1 || !returnsOnlyError(sig) {
		return false
	}
	slice, ok := sig.Params().At(0).Type().Underlying().(*types.Slice)
	if !ok {
		return false
	}
	elem, ok := slice.Elem().Underlying().(*types.Basic)
	return ok && elem.Kind() == types.Byte
}

// Signature of the exported method `name` in *T's method set (or T's, if T is already a pointer), or nil.
// Interfaces have none: decoding into an interface field doesn't call its dynamic type's methods.
func lookupMethod(typ types.Type, name string) *types.Signature {
	if _, ok := typ.Underlying().(*types.Interface); ok {
		return nil
	}
	if _, ok := typ.Underlying().(*types.Pointer); !ok {
		typ = types.NewPointer(typ)
	}
	sel := types.NewMethodSet(typ).Lookup(nil, name)
	if sel == nil {
		return nil
	}
	fn, ok := sel.Obj().(*types.Func)
	if !ok {
		return nil
	}
	return fn.Signature()
}

func returnsOnlyError(sig *types.Signature) bool {
	return sig.Results().Len() == 1 && types.Identical(sig.Results().At(0).Type(), types.Universe.Lookup("error").Type())
}

// What a custom unmarshaler decodes the config value into (see ClassifyUnmarshaler)
type UnmarshalKind string

const (
	// The receiver's own type (e.g. `type plain T; unmarshal((*plain)(c))`): the type's fields/tags apply as usual
	UnmarshalPlain UnmarshalKind = "plain"
	// A scalar (basic type, slice of scalars, or TextUnmarshaler): the type is a leaf, with no keys below it
	UnmarshalScalar UnmarshalKind = "scalar"
	// A map: any key
	UnmarshalMap UnmarshalKind = "map"
	// Another struct: its fields (DecodedFields) are the keys, rather than the type's own
	UnmarshalStruct UnmarshalKind = "struct"
	// Unknown (e.g. interface{}, or the unmarshal func is passed to another function):
	// assume the type's fields/tags apply, and that it may populate yaml:"-" fields under keys it picks
	UnmarshalDynamic UnmarshalKind = "dynamic"
)

// A field of a struct that a custom unmarshaler decodes into (serializable subset of RawField)
type DecodedField struct {
	Tag      string    `json:",omitempty"`
	Embedded bool      `json:",omitempty"`
	IsMap    bool      `json:",omitempty"`
	Kind     ValueKind `json:",omitempty"`
}

// Classify what the node's custom UnmarshalYAML decodes into, from the uses of its unmarshal func parameter
// (see golang.FuncParamCalls), and set UnmarshalKinds, DecodedFields, IsMap, ScalarLike and Kind accordingly.
// With no uses found, the unmarshaler is treated as dynamic.
// TODO(CT) This only records what type the value is decoded into, not what the unmarshaler then does with it. It could
// be extended to detect unmarshalers that change the value before storing it - e.g. relabel.Action's UnmarshalYAML
// (prometheus/model/relabel/relabel.go) stores strings.ToLower(s), so `action: Replace` is in memory as "replace" -
// so that callers searching memory for config values know to look for the changed value.
func ClassifyUnmarshaler(node *CTypeNode, calls []golang.FuncParamCall) {
	kinds := map[UnmarshalKind]bool{}
	decoded := map[string]DecodedField{}
	map_kinds := map[ValueKind]bool{}
	for _, call := range calls {
		kind := UnmarshalDynamic
		if !call.Dynamic {
			arg := call.Arg
			if ptr, ok := arg.Underlying().(*types.Pointer); ok {
				arg = ptr.Elem()
			}
			switch u := arg.Underlying().(type) {
			case *types.Interface:
				kind = UnmarshalDynamic
			case *types.Map:
				kind = UnmarshalMap
				map_kinds[GetValueKind(u)] = true
			default:
				// Compare printed types, not types.Identical: the method's package may be type-checked separately from
				// the node's, so the same named types are different objects
				if IsScalarType(arg) {
					kind = UnmarshalScalar // including `type plain T` for a T with a basic underlying type
				} else if types.TypeString(u, nil) == types.TypeString(node.TypeInfo.Underlying(), nil) {
					kind = UnmarshalPlain
				} else if st, ok := u.(*types.Struct); ok && !ImplementsYAMLUnmarshaler(arg) {
					kind = UnmarshalStruct
					for i := range st.NumFields() {
						raw := StructRawField(st, i)
						decoded[raw.Name] = DecodedField{Tag: raw.Tag, Embedded: raw.Embedded, IsMap: raw.IsMap,
							Kind: GetValueKind(st.Field(i).Type())}
					}
				}
			}
		}
		kinds[kind] = true
	}
	if len(kinds) == 0 {
		kinds[UnmarshalDynamic] = true
	}

	node.UnmarshalKinds = nil
	for _, k := range []UnmarshalKind{UnmarshalPlain, UnmarshalScalar, UnmarshalMap, UnmarshalStruct, UnmarshalDynamic} {
		if kinds[k] {
			node.UnmarshalKinds = append(node.UnmarshalKinds, k)
		}
	}
	only := func(k UnmarshalKind) bool { return len(kinds) == 1 && kinds[k] }
	node.ScalarLike = only(UnmarshalScalar)
	if only(UnmarshalMap) {
		node.IsMap = true // decoded as a map: any key
		if len(map_kinds) == 1 {
			for k := range map_kinds {
				node.Kind = k // the map's values (e.g. labels.Labels stores strings)
			}
		}
	}
	// Decoded struct fields are the keys, unless the type's own fields may also apply
	if kinds[UnmarshalStruct] && !kinds[UnmarshalPlain] && !kinds[UnmarshalDynamic] {
		node.DecodedFields = decoded
	}
}

// Whether the unmarshaler is unknown, so may populate yaml:"-" fields (see UnmarshalDynamic)
func (n CTypeNode) DynamicUnmarshaler() bool {
	return n.YAMLUnmarshaler && (len(n.UnmarshalKinds) == 0 || slices.Contains(n.UnmarshalKinds, UnmarshalDynamic))
}

// Whether config values of this type are scalars (no keys below them), following go-yaml:
// a basic type, a slice/array of scalars, or an encoding.TextUnmarshaler.
// (For types with a custom UnmarshalYAML, see ClassifyUnmarshaler instead.)
func IsScalarType(typ types.Type) bool {
	switch u := typ.Underlying().(type) {
	case *types.Basic:
		return true
	case *types.Slice:
		return IsScalarType(u.Elem())
	case *types.Array:
		return IsScalarType(u.Elem())
	case *types.Pointer:
		return IsScalarType(u.Elem())
	}
	return ImplementsTextUnmarshaler(typ)
}

// Names of the fields whose keys are the node's keys: the decoded struct's fields if a custom unmarshaler decodes
// into another struct, else the node's own fields
func KeyFieldNames(n CTypeNode) []string {
	names := []string{}
	if n.DecodedFields != nil {
		for name := range n.DecodedFields {
			names = append(names, name)
		}
	} else {
		for name := range n.Tags {
			names = append(names, name)
		}
	}
	slices.Sort(names)
	return names
}

// Kind of the Go value that a config file value (a leaf of the document) is stored as, from the underlying type of
// the field or node holding it, after stripping pointers, and slices/arrays and maps (whose elements/values are the
// document's leaves). []byte is a string.
type ValueKind string

const (
	KindString    ValueKind = "string"
	KindNumber    ValueKind = "number"
	KindBool      ValueKind = "bool"
	KindStruct    ValueKind = "struct"    // e.g. config.URL, relabel.Regexp (decoded by a custom unmarshaler)
	KindInterface ValueKind = "interface" // depends on the value (go-yaml decodes into its resolved type)
	KindOther     ValueKind = "other"     // e.g. func, chan
)

func GetValueKind(typ types.Type) ValueKind {
	for {
		switch u := typ.Underlying().(type) {
		case *types.Pointer:
			typ = u.Elem()
		case *types.Slice:
			if isByte(u.Elem()) {
				return KindString
			}
			typ = u.Elem()
		case *types.Array:
			if isByte(u.Elem()) {
				return KindString
			}
			typ = u.Elem()
		case *types.Map:
			typ = u.Elem()
		case *types.Basic:
			switch {
			case u.Info()&types.IsString != 0:
				return KindString
			case u.Info()&types.IsBoolean != 0:
				return KindBool
			case u.Info()&types.IsNumeric != 0:
				return KindNumber
			}
			return KindOther
		case *types.Struct:
			return KindStruct
		case *types.Interface:
			return KindInterface
		default:
			return KindOther
		}
	}
}

func isByte(typ types.Type) bool {
	b, ok := typ.Underlying().(*types.Basic)
	return ok && b.Kind() == types.Byte
}

// Kind of the values stored in a node's field (of the decoded struct, if a custom unmarshaler decodes into another
// struct); "" if unknown (e.g. graph serialized before kinds were recorded)
func NodeFieldKind(n CTypeNode, field string) ValueKind {
	if n.DecodedFields != nil {
		return n.DecodedFields[field].Kind
	}
	return n.FieldKinds[field]
}
