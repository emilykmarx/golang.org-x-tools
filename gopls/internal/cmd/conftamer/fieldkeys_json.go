// Parts of this file are copied or ported from Go's encoding/json (go1.26.2: encode.go, tags.go):
//
// Copyright 2010 The Go Authors. All rights reserved.
// Use of this source code is governed by a BSD-style
// license that can be found in the LICENSE file.

package conftamer

import (
	"reflect"
	"strings"
	"unicode"
	"unicode/utf8"
)

/* How config file keys map to struct fields for JSON, following encoding/json */

// Find how a struct field maps to JSON keys: ported from the per-field part of typeFields in encoding/json
// (encode.go), from reflect to plain field info. Like the YAML port, struct-level rules aren't applied (here: which of
// several fields with the same name wins, by depth and tagging). encoding/json matches keys to these names
// case-insensitively when decoding; Key is the name as encoding/json gives it.
func getJSONFieldKey(f RawField) (FieldKey, error) {
	r, _ := utf8.DecodeRuneInString(f.Name)
	exported := unicode.IsUpper(r)
	if f.Embedded {
		if !exported && !f.IsStruct {
			// Ignore embedded fields of unexported non-struct types.
			return FieldKey{Skip: true}, nil
		}
		// Do not ignore embedded fields of unexported struct types
		// since they may have exported fields.
	} else if !exported {
		// Ignore unexported non-embedded fields.
		return FieldKey{Skip: true}, nil
	}
	tag, has_tag := reflect.StructTag(f.Tag).Lookup("json")
	if tag == "-" {
		return FieldKey{Skip: true, Dash: true}, nil
	}
	name, _ := parseTag(tag) // (options: omitempty, omitzero and string don't change the key)
	if !isValidTag(name) {
		name = ""
	}

	// An untagged embedded struct: its fields are the parent's (typeFields explores it at the next level)
	if name == "" && f.Embedded && f.IsStruct {
		return FieldKey{Inline: true}, nil
	}
	// (Our exception for fields with no json tag at all: see FieldKey.Untagged)
	key := FieldKey{Untagged: !has_tag}
	if name == "" {
		name = f.Name
	}
	key.Key = name
	return key, nil
}

// tagOptions is the string following a comma in a struct field's "json"
// tag, or the empty string. It does not include the leading comma.
type tagOptions string

// parseTag splits a struct field's json tag into its name and
// comma-separated options.
func parseTag(tag string) (string, tagOptions) {
	tag, opt, _ := strings.Cut(tag, ",")
	return tag, tagOptions(opt)
}

// Contains reports whether a comma-separated list of options
// contains a particular substr flag. substr must be surrounded by a
// string boundary or commas.
func (o tagOptions) Contains(optionName string) bool {
	if len(o) == 0 {
		return false
	}
	s := string(o)
	for s != "" {
		var name string
		name, s, _ = strings.Cut(s, ",")
		if name == optionName {
			return true
		}
	}
	return false
}

func isValidTag(s string) bool {
	if s == "" {
		return false
	}
	for _, c := range s {
		switch {
		case strings.ContainsRune("!#$%&()*+-./:;<=>?@[]^_{|}~ ", c):
			// Backslash and quote chars are reserved, but
			// otherwise any punctuation chars are allowed
			// in a tag name.
		case !unicode.IsLetter(c) && !unicode.IsDigit(c):
			return false
		}
	}
	return true
}
