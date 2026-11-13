package golang

import (
	"context"
	"fmt"
	"go/ast"
	"go/types"
	"strings"

	"golang.org/x/tools/gopls/internal/cache"
	"golang.org/x/tools/gopls/internal/cache/metadata"
)

// Look up types by fully-qualified name (<package path>.<type name>, e.g. github.com/caddyserver/caddy/v2.Config) in
// any package the snapshots know, dependencies included (unlike TaggedTypes, which searches only the workspace).
// Each found type's TypeSource is source. Returns an error for a name that isn't found.
func NamedTypes(ctx context.Context, snapshots []*cache.Snapshot, names []string, source TypeSource) ([]TypeInfo, error) {
	found := []TypeInfo{}
	for _, name := range names {
		i := strings.LastIndex(name, ".")
		if i <= 0 {
			return nil, fmt.Errorf("NamedTypes: %q isn't <package path>.<type name>", name)
		}
		pkg_path, type_name := name[:i], name[i+1:]
		var info *TypeInfo
		for _, snapshot := range snapshots {
			var err error
			if info, err = namedType(ctx, snapshot, pkg_path, type_name); err != nil {
				return nil, err
			}
			if info != nil {
				break
			}
		}
		if info == nil {
			return nil, fmt.Errorf("NamedTypes: type %v not found", name)
		}
		info.TypeSource = source
		found = append(found, *info)
	}
	return found, nil
}

// The type type_name of the package with path pkg_path in the snapshot (its non-test variant), or nil
func namedType(ctx context.Context, snapshot *cache.Snapshot, pkg_path, type_name string) (*TypeInfo, error) {
	g, err := snapshot.LoadMetadataGraph(ctx)
	if err != nil {
		return nil, err
	}
	var id metadata.PackageID
	for _, mp := range g.Packages {
		if string(mp.PkgPath) == pkg_path && mp.ForTest == "" {
			id = mp.ID
			break
		}
	}
	if id == "" {
		return nil, nil
	}
	pkgs, err := snapshot.TypeCheck(ctx, id)
	if err != nil {
		return nil, err
	}
	pkg := pkgs[0]
	tn, ok := pkg.Types().Scope().Lookup(type_name).(*types.TypeName)
	if !ok {
		return nil, nil
	}
	pgf, err := pkg.FileEnclosing(tn.Pos())
	if err != nil {
		return nil, err
	}
	// The type's TypeSpec, for its location (as enclosingType gives it: the start of the name)
	for _, decl := range pgf.File.Decls {
		gd, ok := decl.(*ast.GenDecl)
		if !ok {
			continue
		}
		for _, spec := range gd.Specs {
			if ts, ok := spec.(*ast.TypeSpec); ok && ts.Name.Pos() == tn.Pos() {
				loc := mustLocation(pgf, ts)
				loc.Range.End = loc.Range.Start
				return &TypeInfo{Loc: loc, TypeInfo: tn}, nil
			}
		}
	}
	return nil, fmt.Errorf("namedType: declaration of %v.%v not found", pkg_path, type_name)
}
