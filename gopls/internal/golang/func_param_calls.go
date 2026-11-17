package golang

import (
	"context"
	"fmt"
	"go/ast"
	"go/token"
	"go/types"

	"golang.org/x/tools/go/ast/edge"
	"golang.org/x/tools/go/ast/inspector"
	"golang.org/x/tools/go/types/typeutil"
	"golang.org/x/tools/gopls/internal/cache"
	"golang.org/x/tools/gopls/internal/protocol"
)

// A use of a parameter in the body of the function that declares it: a call of a function-typed parameter, or (with a
// decode func) a call passing the parameter to the decode func
type FuncParamCall struct {
	// Type of the first argument, if the use is a call (e.g. *T for unmarshal((*plain)(c)) or unmarshal(&v)); with a
	// decode func, of the argument after the parameter (e.g. *T for json.Unmarshal(b, &v))
	Arg types.Type
	// The parameter is used other than as the callee of a call with one argument
	// (e.g. passed to another function, which may call it with anything)
	Dynamic bool
	Loc     protocol.Location
}

// For the method `method_name` of the type named by the identifier at `loc` (looked up in *T's method set, so it may
// be promoted from an embedded field and declared in another package), find the uses of its parameter `param_idx`
// in its body - e.g. the calls UnmarshalYAML makes to its `unmarshal` func parameter, and their argument types.
// With decode_func (a function's package path and name, e.g. "encoding/json.Unmarshal"), find instead the calls
// passing the parameter as the first of two arguments to that function - e.g. UnmarshalJSON's json.Unmarshal(b, &v) -
// and the second argument's type; other uses of the parameter (e.g. b[0], string(b)) only read the data, so they're
// left out.
// Without decode_func, two uses are followed (up to maxParamDepth levels) instead of being reported as they are:
//   - the parameter passed to a function known statically: that function's uses of its parameter (e.g. Grafana's
//     values.StringValue.UnmarshalYAML passes unmarshal to getInterpolated, which calls unmarshal(&s) with a string)
//   - a call decoding into another type with its own method_name: that method's uses (e.g. Alertmanager's
//     SecretURL.UnmarshalYAML calls unmarshal((*URL)(s)), and URL's UnmarshalYAML decodes a string). (A
//     `type plain T` has no methods, so its decode is reported as it is.)
// Returns no calls if the type has no such method, or the method has no body or doesn't name the parameter.
func FuncParamCalls(ctx context.Context, snapshot *cache.Snapshot, loc protocol.Location, method_name string, param_idx int, decode_func string) ([]FuncParamCall, error) {
	pkg, _, cur, err := locToCursor(ctx, snapshot, loc)
	if err != nil {
		return nil, err
	}
	id, ok := cur.Node().(*ast.Ident)
	if !ok {
		return nil, fmt.Errorf("FuncParamCalls: no identifier at %v", loc)
	}
	tn, ok := pkg.TypesInfo().ObjectOf(id).(*types.TypeName)
	if !ok {
		return nil, fmt.Errorf("FuncParamCalls: %v at %v is not a type name", id.Name, loc)
	}
	fn := lookupMethod(tn.Type(), method_name)
	if fn == nil {
		return nil, nil
	}
	return funcParamUses(ctx, snapshot, pkg.FileSet(), fn, method_name, param_idx, decode_func, 0)
}

// How many levels of calls funcParamUses follows the parameter through (see FuncParamCalls)
const maxParamDepth = 3

// The method method_name in *T's method set, or nil
func lookupMethod(t types.Type, method_name string) *types.Func {
	if _, is_ptr := t.Underlying().(*types.Pointer); !is_ptr {
		t = types.NewPointer(t)
	}
	sel := types.NewMethodSet(t).Lookup(nil, method_name)
	if sel == nil {
		return nil
	}
	fn, _ := sel.Obj().(*types.Func)
	return fn
}

// The uses of function fn's parameter param_idx in its body (see FuncParamCalls); fset positions fn
func funcParamUses(ctx context.Context, snapshot *cache.Snapshot, fset *token.FileSet, fn *types.Func, method_name string, param_idx int, decode_func string, depth int) ([]FuncParamCall, error) {
	// Find the declaration (by file and line, since the object may come from another package's export data)
	pos := fset.Position(fn.Pos())
	if !pos.IsValid() {
		return nil, fmt.Errorf("FuncParamCalls: no position for %v", fn.FullName())
	}
	decl_pkg, decl_pgf, err := NarrowestPackageForFile(ctx, snapshot, protocol.URIFromPath(pos.Filename))
	if err != nil {
		return nil, err
	}
	sig, _ := fn.Type().(*types.Signature)
	var decl *ast.FuncDecl
	for _, d := range decl_pgf.File.Decls {
		if fd, ok := d.(*ast.FuncDecl); ok && (fd.Recv != nil) == (sig != nil && sig.Recv() != nil) &&
			fd.Name.Name == fn.Name() && decl_pkg.FileSet().Position(fd.Name.Pos()).Line == pos.Line {
			decl = fd
			break
		}
	}
	if decl == nil {
		return nil, fmt.Errorf("FuncParamCalls: declaration of %v not found at %v", fn.FullName(), pos)
	}
	if decl.Body == nil {
		return nil, nil
	}

	// The parameter's object
	var param types.Object
	i := 0
	for _, field := range decl.Type.Params.List {
		for _, name := range field.Names {
			if i == param_idx {
				param = decl_pkg.TypesInfo().Defs[name]
			}
			i++
		}
	}
	if param == nil {
		return nil, nil // unnamed or "_"
	}
	// The receiver's type, if a method (a decode into it isn't followed: that's recursion, not delegation)
	recv := ""
	if sig != nil && sig.Recv() != nil {
		recv = types.TypeString(derefType(sig.Recv().Type()), nil)
	}

	// Its uses in the body
	info := decl_pkg.TypesInfo()
	body_cur, ok := decl_pgf.Cursor().FindNode(decl.Body)
	if !ok {
		return nil, fmt.Errorf("FuncParamCalls: body of %v not found", fn.FullName())
	}
	calls := []FuncParamCall{}
	body_cur.Inspect([]ast.Node{(*ast.Ident)(nil)}, func(c inspector.Cursor) bool {
		use := c.Node().(*ast.Ident)
		if info.Uses[use] != param {
			return true
		}
		call := FuncParamCall{Loc: mustLocation(decl_pgf, use)}
		if decode_func != "" {
			if kind, idx := c.ParentEdge(); kind == edge.CallExpr_Args && idx == 0 {
				call_expr := c.Parent().Node().(*ast.CallExpr)
				if fn, ok := typeutil.Callee(info, call_expr).(*types.Func); ok && fn.Pkg() != nil &&
					fn.Pkg().Path()+"."+fn.Name() == decode_func && len(call_expr.Args) == 2 {
					call.Arg = info.TypeOf(call_expr.Args[1])
					calls = append(calls, call)
				}
			}
			return true
		}
		kind, idx := c.ParentEdge()
		switch {
		case kind == edge.CallExpr_Fun:
			if call_expr := c.Parent().Node().(*ast.CallExpr); len(call_expr.Args) == 1 {
				call.Arg = info.TypeOf(call_expr.Args[0])
				// A decode into another type with its own method: what that method decodes
				if named, ok := derefType(call.Arg).(*types.Named); ok && call.Arg != nil && depth < maxParamDepth &&
					types.TypeString(named, nil) != recv {
					if m := lookupMethod(named, method_name); m != nil {
						if sub, err := funcParamUses(ctx, snapshot, decl_pkg.FileSet(), m, method_name, 0, "", depth+1); err == nil && len(sub) > 0 {
							calls = append(calls, sub...)
							return true
						}
					}
				}
			}
		case kind == edge.CallExpr_Args && depth < maxParamDepth:
			// Passed to a function known statically: that function's uses of its parameter
			if callee := typeutil.StaticCallee(info, c.Parent().Node().(*ast.CallExpr)); callee != nil {
				if sub, err := funcParamUses(ctx, snapshot, decl_pkg.FileSet(), callee, method_name, idx, "", depth+1); err == nil && len(sub) > 0 {
					calls = append(calls, sub...)
					return true
				}
			}
		}
		call.Dynamic = call.Arg == nil
		calls = append(calls, call)
		return true
	})
	return calls, nil
}

// t, or what it points to
func derefType(t types.Type) types.Type {
	if t == nil {
		return nil
	}
	if p, ok := t.Underlying().(*types.Pointer); ok {
		return p.Elem()
	}
	return t
}
