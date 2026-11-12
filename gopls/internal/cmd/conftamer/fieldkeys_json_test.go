package conftamer

import (
	"encoding/json"
	"reflect"
	"slices"
	"testing"
)

// Test types for getJSONFieldKey, covering encoding/json's per-field rules
type jsonInner struct {
	InnerA string `json:"inner_a"`
	InnerB string
}

type jsonInner2 struct {
	Deep string `json:"deep"`
}

type jsonUnexportedInner struct {
	Promoted string `json:"promoted"`
}

type JSONNamed string

type jsonStrictInner struct {
	X string `json:"x"`
}

type jsonAll struct {
	Tagged       string `json:"tagged"`
	TaggedOmit   string `json:"tagged_omit,omitempty"`
	OnlyOptions  string `json:",omitempty"`
	Untagged     string
	Dash         string `json:"-"`
	DashComma    string `json:"-,"`
	InvalidName  string `json:"bad\\name"`
	Quoted       int    `json:"quoted,string"`
	YAMLOnly     string `yaml:"yaml_only"`
	unexported   string //nolint:unused
	jsonInner           // untagged embedded struct: inlined
	*jsonInner2         // untagged embedded pointer to struct: inlined
	jsonUnexportedInner // unexported embedded struct: its exported fields are promoted
	JSONNamed           // embedded non-struct (exported): keyed by its name
	Strict jsonStrictInner `json:"strict"`
	Tagged2 struct {
		Y string `json:"y"`
	} `json:"tagged2"`
	jsonStrictInnerAlias `json:"embedded_tagged"` // tagged embedded struct: not inlined
}

type jsonStrictInnerAlias struct {
	Z string `json:"z"`
}

// rawFieldOf builds the RawField getJSONFieldKey sees, from reflect (as StructRawField does from go/types)
func rawFieldOf(sf reflect.StructField) RawField {
	t := sf.Type
	if t.Kind() == reflect.Pointer {
		t = t.Elem()
	}
	return RawField{Name: sf.Name, Tag: string(sf.Tag), Embedded: sf.Anonymous, IsStruct: t.Kind() == reflect.Struct}
}

// keysOf: the top-level JSON keys getJSONFieldKey gives a struct type, expanding inlined embedded structs. Untagged
// fields count: encoding/json keys them (FieldKey.Untagged is only our exception).
func keysOf(t *testing.T, typ reflect.Type) []string {
	var keys []string
	for i := range typ.NumField() {
		sf := typ.Field(i)
		k, err := getJSONFieldKey(rawFieldOf(sf))
		if err != nil {
			t.Fatalf("%v: %v", sf.Name, err)
		}
		switch {
		case k.Skip:
		case k.Inline:
			ft := sf.Type
			if ft.Kind() == reflect.Pointer {
				ft = ft.Elem()
			}
			keys = append(keys, keysOf(t, ft)...)
		default:
			keys = append(keys, k.Key)
		}
	}
	slices.Sort(keys)
	return keys
}

// fill sets every settable field (recursively, allocating pointers) to a non-zero value, so json.Marshal writes it
func fill(v reflect.Value) {
	switch v.Kind() {
	case reflect.Pointer:
		if v.IsNil() && v.CanSet() {
			v.Set(reflect.New(v.Type().Elem()))
		}
		if !v.IsNil() {
			fill(v.Elem())
		}
	case reflect.Struct:
		for i := range v.NumField() {
			f := v.Field(i)
			if f.CanSet() || v.Type().Field(i).Anonymous {
				fill(f)
			}
		}
	case reflect.String:
		if v.CanSet() {
			v.SetString("x")
		}
	case reflect.Int:
		if v.CanSet() {
			v.SetInt(1)
		}
	}
}

func TestGetJSONFieldKeyMatchesEncodingJSON(t *testing.T) {
	var v jsonAll
	fill(reflect.ValueOf(&v).Elem())
	v.jsonInner2 = &jsonInner2{Deep: "x"} // (an unexported embedded pointer isn't settable by fill)
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatal(err)
	}
	var want []string
	for k := range m {
		want = append(want, k)
	}
	slices.Sort(want)

	got := keysOf(t, reflect.TypeOf(v))
	if !slices.Equal(got, want) {
		t.Errorf("keys from getJSONFieldKey:\n  %v\nencoding/json wrote:\n  %v\n(json: %s)", got, want, b)
	}
}

func TestGetJSONFieldKeyUntagged(t *testing.T) {
	for _, tc := range []struct {
		tag          string
		untagged     bool
		key          string
		skip, inline bool
	}{
		{tag: `json:"a"`, key: "a"},
		{tag: `json:",omitempty"`, key: "F"},
		{tag: ``, untagged: true, key: "F"},
		{tag: `yaml:"a"`, untagged: true, key: "F"},
		{tag: `json:"-"`, skip: true},
		{tag: `json:"-,"`, key: "-"},
	} {
		k, err := getJSONFieldKey(RawField{Name: "F", Tag: tc.tag})
		if err != nil {
			t.Fatal(err)
		}
		if k.Untagged != tc.untagged || k.Key != tc.key || k.Skip != tc.skip || k.Inline != tc.inline {
			t.Errorf("tag %q: got %+v", tc.tag, k)
		}
	}
}
