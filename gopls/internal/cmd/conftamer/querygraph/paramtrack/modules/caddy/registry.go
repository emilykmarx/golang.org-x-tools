//go:build ignore

// Prints, as JSON, Caddy's module registry as linked into a binary, and the struct fields that hold modules: what
// module.py turns into gopls conftamer's -extra roots and edges. module.py runs it in a temporary module with blank
// imports of the binary's packages (IMPORTS below is replaced), so that their init functions register their modules:
// - modules: each registered module's ID and Go type (caddy.Modules, caddy.GetModule, and the type New returns);
// - fields: each struct field with a `caddy:"namespace=... [inline_key=...]"` tag (Caddy's convention for fields
//   holding modules as json.RawMessage, decoded at run time by caddy.Context.LoadModule), in caddy.Config and in the
//   types reachable from it or from any module.
package main

import (
	"encoding/json"
	"os"
	"reflect"
	"strings"

	"github.com/caddyserver/caddy/v2"
	// IMPORTS
)

type module struct {
	ID   string `json:"id"`
	Type string `json:"type"`
}

type field struct {
	Parent    string `json:"parent"`
	Field     string `json:"field"`
	Namespace string `json:"namespace"`
	InlineKey string `json:"inline_key"`
}

func typeName(t reflect.Type) string {
	return t.PkgPath() + "." + t.Name()
}

func main() {
	out := struct {
		Modules []module `json:"modules"`
		Fields  []field  `json:"fields"`
	}{}
	var starts []reflect.Type
	for _, id := range caddy.Modules() {
		info, err := caddy.GetModule(id)
		if err != nil {
			panic(err)
		}
		t := reflect.TypeOf(info.New())
		for t.Kind() == reflect.Pointer {
			t = t.Elem()
		}
		out.Modules = append(out.Modules, module{ID: id, Type: typeName(t)})
		starts = append(starts, t)
	}
	starts = append(starts, reflect.TypeOf(caddy.Config{}))

	// The struct types reachable from the starts, and their module fields
	seen := map[reflect.Type]bool{}
	var walk func(t reflect.Type)
	walk = func(t reflect.Type) {
		for {
			switch t.Kind() {
			case reflect.Pointer, reflect.Slice, reflect.Array:
				t = t.Elem()
				continue
			case reflect.Map:
				walk(t.Key())
				t = t.Elem()
				continue
			}
			break
		}
		if t.Kind() != reflect.Struct || seen[t] {
			return
		}
		seen[t] = true
		for i := range t.NumField() {
			sf := t.Field(i)
			if tag, ok := sf.Tag.Lookup("caddy"); ok && t.Name() != "" {
				f := field{Parent: typeName(t), Field: sf.Name}
				for _, part := range strings.Fields(tag) {
					k, v, _ := strings.Cut(part, "=")
					switch k {
					case "namespace":
						f.Namespace = v
					case "inline_key":
						f.InlineKey = v
					}
				}
				out.Fields = append(out.Fields, f)
			}
			walk(sf.Type)
		}
	}
	for _, t := range starts {
		walk(t)
	}

	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(out); err != nil {
		panic(err)
	}
}
