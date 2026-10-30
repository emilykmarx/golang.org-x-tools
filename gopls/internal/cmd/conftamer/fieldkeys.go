package conftamer

import (
	"fmt"
	"go/types"
	"reflect"
	"strings"
	"unicode"
	"unicode/utf8"
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

// RawField for a serialized graph node's field
func NodeRawField(n CTypeNode, field string) RawField {
	return RawField{Name: field, Tag: n.Tags[field], Embedded: n.EmbeddedFields[field], IsMap: n.MapFields[field]}
}
