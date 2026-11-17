//go:build ignore

// The Go types Grafana decodes a provisioned datasource's jsonData and a contact point's settings into, read off its
// code, for module.py's extra_graph (the generated part; the hand-picked part is by_key.py). Run from the gopls
// module (it needs only golang.org/x/tools), with Grafana's checkout as the argument:
//
//	go run <this file> <grafana checkout>
//
// Prints JSON: {"datasources": {<plugin id>: [<type>, ...]}, "contact_points": {<integration type>: <target>}}, types
// fully qualified (<package path>.<name>). A contact point's target is {"type": <type>}, or, if the type isn't
// package-level (declared in the function, or anonymous), {"fields": {<json key>: {"type": <type>} or {"kind": <kind>}}}:
// its fields, a named struct field by type and the rest by kind (string, bool, number, map).
//   - datasources: the core plugins' ids and services, from coreplugin.ProvideCoreRegistry's map (id constant =>
//     asBackendPlugin(<service>)); for each, the named types its service's package (and the packages below it) decode
//     a JSONData field into with encoding/json.Unmarshal(<...>.JSONData, &v). External plugins (e.g. elasticsearch)
//     and plugins that read jsonData by key (e.g. prometheus) have none.
//   - contact points: from github.com/grafana/alerting/notify's parseNotifier, which switches on the integration type
//     (schema.<X>Type) and calls <package>.NewConfig: the type NewConfig decodes the settings into
//     (json.Unmarshal(<settings>, &v)), often <package>.Config, sometimes a struct declared in NewConfig. Type ids are
//     lowercased: Grafana looks them up case-insensitively (e.g. LINE), and the provisioning docs write them lowercase.
package main

import (
	"encoding/json"
	"fmt"
	"go/ast"
	"go/constant"
	"go/types"
	"log"
	"os"
	"reflect"
	"sort"
	"strings"

	"golang.org/x/tools/go/packages"
)

const (
	corePlugins = "github.com/grafana/grafana/pkg/services/pluginsintegration/coreplugin"
	notify      = "github.com/grafana/alerting/notify"
	mode        = packages.NeedName | packages.NeedFiles | packages.NeedSyntax | packages.NeedTypes | packages.NeedTypesInfo
)

func load(dir string, patterns ...string) []*packages.Package {
	pkgs, err := packages.Load(&packages.Config{Dir: dir, Mode: mode}, patterns...)
	if err != nil {
		log.Fatal(err)
	}
	for _, p := range pkgs {
		for _, e := range p.Errors {
			log.Printf("warning: %v: %v", p.PkgPath, e)
		}
	}
	return pkgs
}

// The function declaration named name in pkg
func funcDecl(pkg *packages.Package, name string) *ast.FuncDecl {
	for _, f := range pkg.Syntax {
		for _, d := range f.Decls {
			if fd, ok := d.(*ast.FuncDecl); ok && fd.Name.Name == name {
				return fd
			}
		}
	}
	log.Fatalf("no func %v in %v", name, pkg.PkgPath)
	return nil
}

func stringConst(info *types.Info, e ast.Expr) (string, bool) {
	if tv, ok := info.Types[e]; ok && tv.Value != nil && tv.Value.Kind() == constant.String {
		return constant.StringVal(tv.Value), true
	}
	return "", false
}

func deref(t types.Type) types.Type {
	if p, ok := t.(*types.Pointer); ok {
		return p.Elem()
	}
	return t
}

// Plugin id => the package path of its service, from ProvideCoreRegistry's map literal
func coreServices(dir string) map[string]string {
	pkg := load(dir, corePlugins)[0]
	out := map[string]string{}
	ast.Inspect(funcDecl(pkg, "ProvideCoreRegistry"), func(n ast.Node) bool {
		kv, ok := n.(*ast.KeyValueExpr)
		if !ok {
			return true
		}
		id, ok := stringConst(pkg.TypesInfo, kv.Key)
		call, is_call := kv.Value.(*ast.CallExpr)
		if !ok || !is_call || len(call.Args) != 1 {
			return true
		}
		if named, ok := deref(pkg.TypesInfo.TypeOf(call.Args[0])).(*types.Named); ok && named.Obj().Pkg() != nil {
			out[id] = named.Obj().Pkg().Path()
		}
		return true
	})
	return out
}

// Package path => the named types its code decodes a JSONData field into (json.Unmarshal(<x>.JSONData, &v))
func jsonDataTypes(pkgs []*packages.Package) map[string][]string {
	out := map[string][]string{}
	for _, pkg := range pkgs {
		for _, f := range pkg.Syntax {
			ast.Inspect(f, func(n ast.Node) bool {
				call, ok := n.(*ast.CallExpr)
				if !ok || len(call.Args) != 2 {
					return true
				}
				fn, ok := typeutilCallee(pkg.TypesInfo, call).(*types.Func)
				if !ok || fn.Pkg() == nil || fn.Pkg().Path() != "encoding/json" || fn.Name() != "Unmarshal" {
					return true
				}
				if sel, ok := ast.Unparen(call.Args[0]).(*ast.SelectorExpr); !ok || sel.Sel.Name != "JSONData" {
					return true
				}
				named, ok := deref(pkg.TypesInfo.TypeOf(call.Args[1])).(*types.Named)
				if !ok || named.Obj().Pkg() == nil {
					return true
				}
				if _, is_struct := named.Underlying().(*types.Struct); is_struct {
					t := named.Obj().Pkg().Path() + "." + named.Obj().Name()
					out[pkg.PkgPath] = append(out[pkg.PkgPath], t)
				}
				return true
			})
		}
	}
	return out
}

// The function or method a call calls, if known statically (like typeutil.Callee, without the dependency's extras)
func typeutilCallee(info *types.Info, call *ast.CallExpr) types.Object {
	switch fun := ast.Unparen(call.Fun).(type) {
	case *ast.Ident:
		return info.Uses[fun]
	case *ast.SelectorExpr:
		if sel, ok := info.Selections[fun]; ok {
			return sel.Obj()
		}
		return info.Uses[fun.Sel]
	}
	return nil
}

// What NewConfig (fn) decodes the settings into: {"type": T} for a package-level named type, else {"fields": ...}
func newConfigTarget(dir string, fn *types.Func) map[string]any {
	pkg := load(dir, fn.Pkg().Path())[0]
	var target types.Type
	ast.Inspect(funcDecl(pkg, "NewConfig"), func(n ast.Node) bool {
		call, ok := n.(*ast.CallExpr)
		if !ok || len(call.Args) != 2 || target != nil {
			return true
		}
		if f, ok := typeutilCallee(pkg.TypesInfo, call).(*types.Func); ok && f.Pkg() != nil &&
			f.Pkg().Path() == "encoding/json" && f.Name() == "Unmarshal" {
			target = deref(pkg.TypesInfo.TypeOf(call.Args[1]))
		}
		return true
	})
	if target == nil {
		log.Printf("warning: no json.Unmarshal in %v.NewConfig", fn.Pkg().Path())
		return nil
	}
	if named, ok := target.(*types.Named); ok && named.Obj().Parent() == named.Obj().Pkg().Scope() {
		return map[string]any{"type": named.Obj().Pkg().Path() + "." + named.Obj().Name()}
	}
	st, ok := target.Underlying().(*types.Struct)
	if !ok {
		log.Printf("warning: %v.NewConfig decodes into %v", fn.Pkg().Path(), target)
		return nil
	}
	fields := map[string]any{}
	for i := range st.NumFields() {
		f := st.Field(i)
		name, _, _ := strings.Cut(reflect.StructTag(st.Tag(i)).Get("json"), ",")
		if name == "-" || f.Embedded() || !f.Exported() {
			continue
		}
		if name == "" {
			name = f.Name()
		}
		fields[name] = fieldTarget(f.Type())
	}
	return map[string]any{"fields": fields}
}

// A field of a struct declared in a function: a named struct type by name, else its kind
func fieldTarget(t types.Type) map[string]string {
	t = deref(t)
	if named, ok := t.(*types.Named); ok && named.Obj().Pkg() != nil {
		if _, is_struct := named.Underlying().(*types.Struct); is_struct {
			return map[string]string{"type": named.Obj().Pkg().Path() + "." + named.Obj().Name()}
		}
	}
	switch u := t.Underlying().(type) {
	case *types.Basic:
		switch {
		case u.Info()&types.IsBoolean != 0:
			return map[string]string{"kind": "bool"}
		case u.Info()&types.IsNumeric != 0:
			return map[string]string{"kind": "number"}
		}
	case *types.Map, *types.Interface:
		return map[string]string{"kind": "map"}
	}
	return map[string]string{"kind": "string"} // strings, json.Number, and lists of them
}

// Integration type => what its NewConfig decodes the settings into (newConfigTarget), from parseNotifier's switch
func contactPoints(dir string) map[string]any {
	pkg := load(dir, notify)[0]
	out := map[string]any{}
	ast.Inspect(funcDecl(pkg, "parseNotifier"), func(n ast.Node) bool {
		cc, ok := n.(*ast.CaseClause)
		if !ok || len(cc.List) != 1 {
			return true
		}
		id, ok := stringConst(pkg.TypesInfo, cc.List[0])
		if !ok {
			return true
		}
		for _, stmt := range cc.Body {
			ast.Inspect(stmt, func(n ast.Node) bool {
				call, ok := n.(*ast.CallExpr)
				if !ok {
					return true
				}
				fn, ok := typeutilCallee(pkg.TypesInfo, call).(*types.Func)
				if ok && fn.Name() == "NewConfig" && fn.Pkg() != nil {
					if t := newConfigTarget(dir, fn); t != nil {
						out[strings.ToLower(id)] = t
					}
				}
				return true
			})
		}
		return false
	})
	return out
}

func main() {
	if len(os.Args) != 2 {
		log.Fatal("usage: go run settings_types.go <grafana checkout>")
	}
	dir := os.Args[1]
	services := coreServices(dir)
	patterns := []string{}
	for _, p := range services {
		patterns = append(patterns, p+"/...")
	}
	sort.Strings(patterns)
	by_pkg := jsonDataTypes(load(dir, patterns...))
	datasources := map[string][]string{}
	for id, svc := range services {
		seen := map[string]bool{}
		for p, ts := range by_pkg {
			if p == svc || strings.HasPrefix(p, svc+"/") {
				for _, t := range ts {
					if !seen[t] {
						seen[t] = true
						datasources[id] = append(datasources[id], t)
					}
				}
			}
		}
		sort.Strings(datasources[id])
	}
	b, err := json.MarshalIndent(map[string]any{"datasources": datasources, "contact_points": contactPoints(dir)}, "", "  ")
	if err != nil {
		log.Fatal(err)
	}
	fmt.Println(string(b))
}
