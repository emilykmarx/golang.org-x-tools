package golang

import (
	"context"
	"fmt"
	"go/ast"
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
	recv := tn.Type()
	if _, is_ptr := recv.Underlying().(*types.Pointer); !is_ptr {
		recv = types.NewPointer(recv)
	}
	sel := types.NewMethodSet(recv).Lookup(nil, method_name)
	if sel == nil {
		return nil, nil
	}
	fn, ok := sel.Obj().(*types.Func)
	if !ok {
		return nil, nil
	}

	// Find the method's declaration (by file and line, since the object may come from another package's export data)
	pos := pkg.FileSet().Position(fn.Pos())
	if !pos.IsValid() {
		return nil, fmt.Errorf("FuncParamCalls: no position for %v", fn.FullName())
	}
	decl_pkg, decl_pgf, err := NarrowestPackageForFile(ctx, snapshot, protocol.URIFromPath(pos.Filename))
	if err != nil {
		return nil, err
	}
	var decl *ast.FuncDecl
	for _, d := range decl_pgf.File.Decls {
		if fd, ok := d.(*ast.FuncDecl); ok && fd.Recv != nil && fd.Name.Name == method_name &&
			decl_pkg.FileSet().Position(fd.Name.Pos()).Line == pos.Line {
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
		if c.ParentEdgeKind() == edge.CallExpr_Fun {
			if call_expr := c.Parent().Node().(*ast.CallExpr); len(call_expr.Args) == 1 {
				call.Arg = info.TypeOf(call_expr.Args[0])
			}
		}
		call.Dynamic = call.Arg == nil
		calls = append(calls, call)
		return true
	})
	return calls, nil
}
