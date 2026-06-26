// Copyright 2019 The Go Authors. All rights reserved.
// Use of this source code is governed by a BSD-style
// license that can be found in the LICENSE file.

package golang

// This file defines the 'references' query based on a serializable
// index constructed during type checking, thus avoiding the need to
// type-check packages at search time.
//
// See the ./xrefs/ subpackage for the index construction and lookup.
//
// This implementation does not intermingle objects from distinct
// calls to TypeCheck.

import (
	"context"
	"fmt"
	"go/ast"
	"go/types"
	"sort"
	"strings"
	"sync"

	"golang.org/x/sync/errgroup"
	"golang.org/x/tools/go/ast/edge"
	"golang.org/x/tools/go/ast/inspector"
	"golang.org/x/tools/go/types/objectpath"
	"golang.org/x/tools/go/types/typeutil"
	"golang.org/x/tools/gopls/internal/cache"
	"golang.org/x/tools/gopls/internal/cache/metadata"
	"golang.org/x/tools/gopls/internal/cache/methodsets"
	"golang.org/x/tools/gopls/internal/cache/parsego"
	"golang.org/x/tools/gopls/internal/file"
	"golang.org/x/tools/gopls/internal/protocol"
	"golang.org/x/tools/gopls/internal/util/cursorutil"
	"golang.org/x/tools/gopls/internal/util/safetoken"

	"golang.org/x/tools/internal/event"
)

// References returns a list of all references (sorted with
// definitions before uses) to the object denoted by the identifier at
// the given file/position, searching the entire workspace.
func References(ctx context.Context, snapshot *cache.Snapshot, fh file.Handle, rng protocol.Range, includeDeclaration bool) ([]protocol.Location, error) {
	references, _, err := references(ctx, snapshot, fh, rng, includeDeclaration)
	if err != nil {
		return nil, err
	}
	locations := make([]protocol.Location, len(references))
	for i, ref := range references {
		locations[i] = ref.location
	}
	return locations, nil
}

// returned []Locations is the references, []TypeInfo is the parent types (each reference may have 0,1,or multiple)
func ReferencesMoreInfo(ctx context.Context, snapshot *cache.Snapshot, fh file.Handle, rng protocol.Range, includeDeclaration bool) ([]protocol.Location, []TypeInfo, error) {
	references, parent_types, err := references(ctx, snapshot, fh, rng, includeDeclaration)
	if err != nil {
		return nil, nil, err
	}
	locations := make([]protocol.Location, len(references))
	for i, ref := range references {
		locations[i] = ref.location
	}
	return locations, parent_types, nil
}

// A reference describes an identifier that refers to the same
// object as the subject of a References query.
type reference struct {
	isDeclaration bool
	location      protocol.Location
	pkgPath       PackagePath // of declaring package (same for all elements of the slice)
}

// references returns a list of all references (sorted with
// definitions before uses) to the object denoted by the identifier at
// the given file/position, searching the entire workspace.
func references(ctx context.Context, snapshot *cache.Snapshot, f file.Handle, rng protocol.Range, includeDeclaration bool) ([]reference, []TypeInfo, error) {
	ctx, done := event.Start(ctx, "golang.references")
	defer done()

	// Is the cursor within the package name declaration?
	_, inPackageName, err := parsePackageNameDecl(ctx, snapshot, f, rng)
	if err != nil {
		return nil, nil, err
	}

	var refs []reference
	var parent_types []TypeInfo
	if inPackageName {
		refs, err = packageReferences(ctx, snapshot, f.URI())
	} else {
		refs, parent_types, err = ordinaryReferences(ctx, snapshot, f.URI(), rng)
	}
	if err != nil {
		return nil, nil, err
	}

	sort.Slice(refs, func(i, j int) bool {
		x, y := refs[i], refs[j]
		if x.isDeclaration != y.isDeclaration {
			return x.isDeclaration // decls < refs
		}
		return protocol.CompareLocation(x.location, y.location) < 0
	})

	// De-duplicate by location, and optionally remove declarations.
	out := refs[:0]
	for _, ref := range refs {
		if !includeDeclaration && ref.isDeclaration {
			continue
		}
		if len(out) == 0 || out[len(out)-1].location != ref.location {
			out = append(out, ref)
		}
	}
	refs = out

	return refs, parent_types, nil
}

// packageReferences returns a list of references to the package
// declaration of the specified name and uri by searching among the
// import declarations of all packages that directly import the target
// package.
func packageReferences(ctx context.Context, snapshot *cache.Snapshot, uri protocol.DocumentURI) ([]reference, error) {
	metas, err := snapshot.MetadataForFile(ctx, uri, false)
	if err != nil {
		return nil, err
	}
	if len(metas) == 0 {
		return nil, fmt.Errorf("found no package containing %s", uri)
	}

	var refs []reference

	// Find external references to the package declaration
	// from each direct import of the package.
	//
	// The narrowest package is the most broadly imported,
	// so we choose it for the external references.
	//
	// But if the file ends with _test.go then we need to
	// find the package it is testing; there's no direct way
	// to do that, so pick a file from the same package that
	// doesn't end in _test.go and start over.
	narrowest := metas[0]
	if narrowest.ForTest != "" && strings.HasSuffix(string(uri), "_test.go") {
		for _, f := range narrowest.CompiledGoFiles {
			if !strings.HasSuffix(string(f), "_test.go") {
				return packageReferences(ctx, snapshot, f)
			}
		}
		// This package has no non-test files.
		// Skip the search for external references.
		// (Conceivably one could blank-import an empty package, but why?)
	} else {
		rdeps, err := snapshot.ReverseDependencies(ctx, narrowest.ID, false) // direct
		if err != nil {
			return nil, err
		}

		// Restrict search to workspace packages.
		workspace, err := snapshot.WorkspaceMetadata(ctx)
		if err != nil {
			return nil, err
		}
		workspaceMap := make(map[PackageID]*metadata.Package, len(workspace))
		for _, mp := range workspace {
			workspaceMap[mp.ID] = mp
		}

		for _, rdep := range rdeps {
			if _, ok := workspaceMap[rdep.ID]; !ok {
				continue
			}
			for _, uri := range rdep.CompiledGoFiles {
				fh, err := snapshot.ReadFile(ctx, uri)
				if err != nil {
					return nil, err
				}
				f, err := snapshot.ParseGo(ctx, fh, parsego.Header)
				if err != nil {
					return nil, err
				}
				for _, imp := range f.File.Imports {
					if rdep.DepsByImpPath[metadata.UnquoteImportPath(imp)] == narrowest.ID {
						refs = append(refs, reference{
							isDeclaration: false,
							location:      mustLocation(f, imp),
							pkgPath:       narrowest.PkgPath,
						})
					}
				}
			}
		}
	}

	// Find internal "references" to the package from
	// of each package declaration in the target package itself.
	//
	// The widest package (possibly a test variant) has the
	// greatest number of files and thus we choose it for the
	// "internal" references.
	widest := metas[len(metas)-1] // may include _test.go files
	for _, uri := range widest.CompiledGoFiles {
		fh, err := snapshot.ReadFile(ctx, uri)
		if err != nil {
			return nil, err
		}
		f, err := snapshot.ParseGo(ctx, fh, parsego.Header)
		if err != nil {
			return nil, err
		}
		// golang/go#66250: don't crash if the package file lacks a name.
		if f.File.Name.Pos().IsValid() {
			refs = append(refs, reference{
				isDeclaration: true, // (one of many)
				location:      mustLocation(f, f.File.Name),
				pkgPath:       widest.PkgPath,
			})
		}
	}

	return refs, nil
}

// ordinaryReferences computes references for all ordinary objects (not package declarations).
func ordinaryReferences(ctx context.Context, snapshot *cache.Snapshot, uri protocol.DocumentURI, rng protocol.Range) ([]reference, []TypeInfo, error) {
	// Strategy: use the reference information computed by the
	// type checker to find the declaration. First type-check this
	// package to find the declaration, then type check the
	// declaring package (which may be different), plus variants,
	// to find local (in-package) references.
	// Global references are satisfied by the index.

	// Strictly speaking, a wider package could provide a different
	// declaration (e.g. because the _test.go files can change the
	// meaning of a field or method selection), but the narrower
	// package reports the more broadly referenced object.
	pkg, pgf, err := NarrowestPackageForFile(ctx, snapshot, uri)
	if err != nil {
		return nil, nil, err
	}

	// Find the selected object (declaration or reference).
	// For struct{T}, we choose the field (Def) over the type (Use).
	start, end, err := pgf.RangePos(rng)
	if err != nil {
		return nil, nil, err
	}
	cur, _ := pgf.Cursor().FindByPos(start, end) // can't fail

	candidates, err := objectsAt(pkg.TypesInfo(), cur)
	if err != nil {
		return nil, nil, err
	}
	// Pick first object arbitrarily.
	// The case variables of a type switch have different
	// types but that difference is immaterial here.
	obj := candidates[0].obj

	// nil, error, error.Error, iota, or other built-in?
	if isBuiltin(obj) {
		return nil, nil, fmt.Errorf("references to builtin %q are not supported", obj.Name())
	}

	// Find metadata of all packages containing the object's defining file.
	// This may include the query pkg, and possibly other variants.
	declPosn := safetoken.StartPosition(pkg.FileSet(), obj.Pos())
	declURI := protocol.URIFromPath(declPosn.Filename)
	variants, err := snapshot.MetadataForFile(ctx, declURI, false)
	if err != nil {
		return nil, nil, err
	}
	if len(variants) == 0 {
		return nil, nil, fmt.Errorf("no packages for file %q", declURI) // can't happen
	}
	// (variants must include ITVs for reverse dependency computation below.)

	// Is object exported?
	// If so, compute scope and targets of the global search.
	var (
		globalScope   = make(map[PackageID]*metadata.Package) // (excludes ITVs)
		globalTargets map[PackagePath]map[objectpath.Path]unit
		expansions    = make(map[PackageID]unit) // packages that caused search expansion
	)
	// TODO(adonovan): what about generic functions? Need to consider both
	// uninstantiated and instantiated. The latter have no objectpath. Use Origin?
	if path, err := objectpath.For(obj); err == nil && obj.Exported() {
		pkgPath := variants[0].PkgPath // (all variants have same package path)
		globalTargets = map[PackagePath]map[objectpath.Path]unit{
			pkgPath: {path: {}}, // primary target
		}

		// Compute set of (non-ITV) workspace packages.
		// We restrict references to this subset.
		workspace, err := snapshot.WorkspaceMetadata(ctx)
		if err != nil {
			return nil, nil, err
		}
		workspaceMap := make(map[PackageID]*metadata.Package, len(workspace))
		workspaceIDs := make([]PackageID, 0, len(workspace))
		for _, mp := range workspace {
			workspaceMap[mp.ID] = mp
			workspaceIDs = append(workspaceIDs, mp.ID)
		}

		// addRdeps expands the global scope to include the
		// reverse dependencies of the specified package.
		addRdeps := func(id PackageID, transitive bool) error {
			rdeps, err := snapshot.ReverseDependencies(ctx, id, transitive)
			if err != nil {
				return err
			}
			for rdepID, rdep := range rdeps {
				// Skip non-workspace packages.
				//
				// This means we also skip any expansion of the
				// search that might be caused by a non-workspace
				// package, possibly causing us to miss references
				// to the expanded target set from workspace packages.
				//
				// TODO(adonovan): don't skip those expansions.
				// The challenge is how to so without type-checking
				// a lot of non-workspace packages not covered by
				// the initial workspace load.
				if _, ok := workspaceMap[rdepID]; !ok {
					continue
				}

				globalScope[rdepID] = rdep
			}
			return nil
		}

		// How far need we search?
		// For package-level objects, we need only search the direct importers.
		// For fields and methods, we must search transitively.
		transitive := obj.Pkg().Scope().Lookup(obj.Name()) != obj

		// The scope is the union of rdeps of each variant.
		// (Each set is disjoint so there's no benefit to
		// combining the metadata graph traversals.)
		for _, mp := range variants {
			if err := addRdeps(mp.ID, transitive); err != nil {
				return nil, nil, err
			}
		}

		// Is object a method?
		//
		// If so, expand the search so that the targets include
		// all methods that correspond to it through interface
		// satisfaction, and the scope includes the rdeps of
		// the package that declares each corresponding type.
		//
		// 'expansions' records the packages that declared
		// such types.
		if recv := effectiveReceiver(obj); recv != nil {
			if err := expandMethodSearch(ctx, snapshot, workspaceIDs, obj.(*types.Func), recv, addRdeps, globalTargets, expansions); err != nil {
				return nil, nil, err
			}
		}
	}

	// The search functions will call report(loc) for each hit,
	// passing a corresponding parent type if there is one
	// (a location may have multiple, in which case report() is called for each).
	// report() records the loc whether a parent_type is passed or not.
	var (
		refsMu       sync.Mutex
		refs         []reference
		parent_types []TypeInfo
	)
	report := func(loc protocol.Location, parent_type *TypeInfo, isDecl bool) {
		// record loc
		ref := reference{
			isDeclaration: isDecl,
			location:      loc,
			pkgPath:       pkg.Metadata().PkgPath,
		}
		refsMu.Lock()
		refs = append(refs, ref)

		// record parent type if any
		if parent_type != nil {
			parent_types = append(parent_types, *parent_type)
		}
		refsMu.Unlock()
	}

	// Loop over the variants of the declaring package,
	// and perform both the local (in-package) and global
	// (cross-package) searches, in parallel.
	//
	// TODO(adonovan): opt: support LSP reference streaming. See:
	// - https://github.com/microsoft/vscode-languageserver-node/pull/164
	// - https://github.com/microsoft/language-server-protocol/pull/182
	//
	// Careful: this goroutine must not return before group.Wait.
	var group errgroup.Group

	// Compute local references for each variant.
	// The target objects are identified by (URI, offset).
	for _, mp := range variants {
		// We want the ordinary importable package,
		// plus any test-augmented variants, since
		// declarations in _test.go files may change
		// the reference of a selection, or even a
		// field into a method or vice versa.
		//
		// But we don't need intermediate test variants,
		// as their local references will be covered
		// already by other variants.
		if mp.IsIntermediateTestVariant() {
			continue
		}
		mp := mp
		group.Go(func() error {
			// TODO(adonovan): opt: batch these TypeChecks.
			pkgs, err := snapshot.TypeCheck(ctx, mp.ID)
			if err != nil {
				return err
			}
			pkg := pkgs[0]

			// Find the declaration of the corresponding
			// object in this package based on (URI, offset).
			pgf, err := pkg.File(declURI)
			if err != nil {
				return err
			}
			pos, err := safetoken.Pos(pgf.Tok, declPosn.Offset)
			if err != nil {
				return err
			}
			cur, _ := pgf.Cursor().FindByPos(pos, pos) // can't fail

			objects, err := objectsAt(pkg.TypesInfo(), cur)
			if err != nil {
				return err // unreachable? (probably caught earlier)
			}

			// Report the locations of the declaration(s).
			// TODO(adonovan): what about for corresponding methods? Add tests.
			for _, o := range objects {
				// This is the declaration, so no parent type
				report(mustLocation(pgf, o.cur.Node()), nil, true)
			}

			// Convert objects list to targets set.
			targets := make(map[types.Object]bool)
			for _, o := range objects {
				targets[o.obj] = true
			}

			return localReferences(pkg, targets, true, report)
		})
	}

	// Also compute local references within packages that declare
	// corresponding methods (see above), which expand the global search.
	// The target objects are identified by (PkgPath, objectpath).
	for id := range expansions {
		group.Go(func() error {
			// TODO(adonovan): opt: batch these TypeChecks.
			pkgs, err := snapshot.TypeCheck(ctx, id)
			if err != nil {
				return err
			}
			pkg := pkgs[0]

			targets := make(map[types.Object]bool)
			for objpath := range globalTargets[pkg.Metadata().PkgPath] {
				obj, err := objectpath.Object(pkg.Types(), objpath)
				if err != nil {
					// No such object, because it was
					// declared only in the test variant.
					continue
				}
				targets[obj] = true
			}

			// Don't include corresponding types or methods
			// since expansions did that already, and we don't
			// want (e.g.) concrete -> interface -> concrete.
			const correspond = false
			return localReferences(pkg, targets, correspond, report)
		})
	}

	// Compute global references for selected reverse dependencies,
	// i.e. references in a package other than the declaring one
	group.Go(func() error {
		var globalIDs []PackageID
		for id := range globalScope {
			globalIDs = append(globalIDs, id)
		}
		indexes, err := snapshot.References(ctx, globalIDs...)
		if err != nil {
			return err
		}
		for _, index := range indexes {
			for _, loc := range index.Lookup(globalTargets) {
				ref_pkg, ref_pgf, ref_cursor, err := locToCursor(ctx, snapshot, loc)
				if err != nil {
					return err
				}
				parent_types, err := parentTypes(ref_pkg, ref_pgf, *ref_cursor)
				if err != nil {
					return err
				}
				// Report the hit even if no parent types
				if len(parent_types) == 0 {
					report(loc, nil, false)
				}
				// Report each parent type
				for _, parent_type := range parent_types {
					report(loc, &parent_type, false)
				}
			}
		}
		return nil
	})

	if err := group.Wait(); err != nil {
		return nil, nil, err
	}
	return refs, parent_types, nil
}

func locToCursor(ctx context.Context, snapshot *cache.Snapshot, loc protocol.Location) (*cache.Package, *parsego.File, *inspector.Cursor, error) {
	pkg, pgf, err := NarrowestPackageForFile(ctx, snapshot, loc.URI)
	if err != nil {
		return nil, nil, nil, err
	}

	start, end, err := pgf.RangePos(loc.Range)
	if err != nil {
		return nil, nil, nil, err
	}
	cur, _ := pgf.Cursor().FindByPos(start, end) // can't fail
	return pkg, pgf, &cur, nil
}

// expandMethodSearch expands the scope and targets of a global search
// for an exported method to include all methods in the workspace
// that correspond to it through interface satisfaction.
//
// Each package that declares a corresponding type is added to
// expansions so that we can also find local references to the type
// within the package, which of course requires type checking.
//
// The scope is expanded by a sequence of calls (not concurrent) to addRdeps.
//
// recv is the method's effective receiver type, for method-set computations.
func expandMethodSearch(ctx context.Context, snapshot *cache.Snapshot, workspaceIDs []PackageID, method *types.Func, recv types.Type, addRdeps func(id PackageID, transitive bool) error, targets map[PackagePath]map[objectpath.Path]unit, expansions map[PackageID]unit) error {
	// Compute the method-set fingerprint used as a key to the global search.
	key, hasMethods := methodsets.KeyOf(recv)
	if !hasMethods {
		// The query object was method T.m, but methodset(T)={}:
		// this indicates that ill-typed T has conflicting fields and methods.
		// Rather than bug-report (#67978), treat the empty method set at face value.
		return nil
	}
	// Search the methodset index of each package in the workspace.
	indexes, err := snapshot.MethodSets(ctx, workspaceIDs...)
	if err != nil {
		return err
	}
	var mu sync.Mutex // guards addRdeps, targets, expansions
	var group errgroup.Group
	for i, index := range indexes {
		group.Go(func() error {
			// Consult index for matching (super/sub) methods.
			const want = methodsets.Supertype | methodsets.Subtype
			results := index.Search(key, want, method)
			if len(results) == 0 {
				return nil
			}

			// We have discovered one or more corresponding types.
			id := workspaceIDs[i]

			mu.Lock()
			defer mu.Unlock()

			// Expand global search scope to include rdeps of this pkg.
			if err := addRdeps(id, true); err != nil {
				return err
			}

			// Mark this package so that we search within it for
			// local references to the additional types/methods.
			expansions[id] = unit{}

			// Add each corresponding method the to set of global search targets.
			for _, res := range results {
				methodPkg := PackagePath(res.PkgPath)
				opaths, ok := targets[methodPkg]
				if !ok {
					opaths = make(map[objectpath.Path]unit)
					targets[methodPkg] = opaths
				}
				opaths[res.ObjectPath] = unit{}
			}
			return nil
		})
	}
	return group.Wait()
}

// localReferences traverses syntax and reports each reference to one
// of the target objects, or (if correspond is set) an object that
// corresponds to one of them via interface satisfaction.
func localReferences(pkg *cache.Package, targets map[types.Object]bool, correspond bool, report func(loc protocol.Location, parent_type *TypeInfo, isDecl bool)) error {
	// If we're searching for references to a method optionally
	// broaden the search to include references to corresponding
	// methods of mutually assignable receiver types.
	// (We use a slice, but objectsAt never returns >1 methods.)
	var methodRecvs []types.Type
	var methodName string // name of an arbitrary target, iff a method
	if correspond {
		for obj := range targets {
			if t := effectiveReceiver(obj); t != nil {
				methodRecvs = append(methodRecvs, t)
				methodName = obj.Name()
			}
		}
	}

	var msets typeutil.MethodSetCache

	// matches reports whether obj either is or corresponds to a target.
	// (Correspondence is defined as usual for interface methods: super/subtype.)
	matches := func(obj types.Object) bool {
		if containsOrigin(targets, obj) {
			return true
		}
		if methodRecvs != nil && obj.Name() == methodName {
			if orecv := effectiveReceiver(obj); orecv != nil {
				for _, mrecv := range methodRecvs {
					if implements(&msets, orecv, mrecv) ||
						implements(&msets, mrecv, orecv) {
						return true
					}
				}
			}
		}
		return false
	}

	// Collect target names for fast pre-filtering:
	// skip identifiers whose name can't match any target.
	targetNames := make(map[string]struct{}, len(targets))
	for obj := range targets {
		targetNames[obj.Name()] = struct{}{}
	}

	// Scan through syntax looking for uses of one of the target objects.
	for _, pgf := range pkg.CompiledGoFiles() {
		for curId := range pgf.Cursor().Preorder((*ast.Ident)(nil)) {
			id := curId.Node().(*ast.Ident)
			if _, ok := targetNames[id.Name]; !ok {
				continue
			}
			if obj, ok := pkg.TypesInfo().Uses[id]; ok && matches(obj) {
				// Found a use
				parent_types, err := parentTypes(pkg, pgf, curId)
				if err != nil {
					return err
				}
				loc := mustLocation(pgf, id)
				// Report the hit even if no parent types
				if len(parent_types) == 0 {
					report(loc, nil, false)
				}
				// Report each parent type
				for _, parent_type := range parent_types {
					report(loc, &parent_type, false)
				}
			}
		}
	}
	return nil
}

// child_cursor is an identifier in a reference.
// If reference is part of a function argument, find the returned types, if any
func argToRetType(pkg *cache.Package, pgf *parsego.File, child_cursor inspector.Cursor) ([]TypeInfo, error) {
	found_param := false
	retvals := []TypeInfo{}
	var retvals_cursor *inspector.Cursor

	// Check if child_cursor is in function arg
	for parent_cursor := range child_cursor.Enclosing() {
		if parent_cursor.ParentEdgeKind() == edge.FuncType_Params {
			// Path to function indicates child_cursor was in a param
			found_param = true
		}
		if _, ok := parent_cursor.Node().(*ast.FuncType); ok {
			// Found a function decl => check if child_cursor was part of an arg, or some other part (e.g. ret)
			if found_param {
				found_ret := false
				for func_child := range parent_cursor.Children() {
					if func_child.ParentEdgeKind() == edge.FuncType_Results {
						found_ret = true
					}
				}
				if found_ret {
					c := parent_cursor.ChildAt(edge.FuncType_Results, -1)
					retvals_cursor = &c
				}
			}
		}
	}

	if retvals_cursor == nil {
		// child_cursor is not in function arg
		return nil, nil
	}

	// get return type(s)
	filter := []ast.Node{(*ast.Ident)(nil)}

	retvals_cursor.Inspect(filter, func(child_cursor inspector.Cursor) bool {
		ret_node := child_cursor.Node()

		// identifier - check if it's of a type name
		ret_typeinfo, err := cursorToTypeInfo(ret_node, *retvals_cursor, pkg)
		if err != nil {
			// Not a type name
			// TODO (conftamer) (minor) Some of these errors may be actual errors
			return true
		}

		ret_type := ret_typeinfo.Type()
		if _, ok := ret_type.(*types.Basic); ok {
			// ignore built-in types
			return true
		}
		ret_type_loc := mustLocation(pgf, ret_node)
		retvals = append(retvals, TypeInfo{Loc: ret_type_loc, TypeInfo: ret_typeinfo,
			ASTPath: nil, TypeSource: ArgToRet}) // no AST path from an argument to its ret

		return false
	})

	return retvals, nil
}

// child_cursor is an identifier in a reference.
// Find parent types that have "access" to the child type - i.e.
// parent encloses child in its type definition, or child is an argument to a function that returns parent
func parentTypes(pkg *cache.Package, pgf *parsego.File, child_cursor inspector.Cursor) ([]TypeInfo, error) {
	parents, err := argToRetType(pkg, pgf, child_cursor)
	if err != nil {
		return nil, err
	}

	enclosing, err := enclosingType(pkg, pgf, child_cursor)
	if err != nil {
		return nil, err
	}
	if enclosing != nil {
		parents = append(parents, *enclosing)
	}

	return parents, nil
}

// child_cursor is an identifier in a reference.
// If reference is part of a type declaration, find the type being declared.
// (If part of a nested type decl, find the innermost one)
// (e.g. a struct containing a field of child_cursor's type)
func enclosingType(pkg *cache.Package, pgf *parsego.File, child_cursor inspector.Cursor) (*TypeInfo, error) {
	for parent_cursor := range child_cursor.Enclosing((*ast.TypeSpec)(nil)) {
		// Found parent type => get its info
		parent_node := parent_cursor.Node().(*ast.TypeSpec)

		parent_typeinfo, err := cursorToTypeInfo(parent_node.Name, parent_cursor, pkg)
		if err != nil {
			return nil, err
		}

		parent_type_loc := mustLocation(pgf, parent_node)
		// TypeSpec location start is struct name and end is the closing brace => move end back to start so it's within the name
		parent_type_loc.Range.End = parent_type_loc.Range.Start
		edges := ASTPath(child_cursor, parent_cursor)

		return &TypeInfo{Loc: parent_type_loc, TypeInfo: parent_typeinfo,
			ASTPath: edges, TypeSource: Enclosing}, nil
	}

	return nil, nil // no enclosing type
}

// Assuming target is in subtree and is the identifier of a type, find its type info.
// (If we run into edge cases where this doesn't work, look more closely at TypeDefinition())
func cursorToTypeInfo(target_node ast.Node, subtree inspector.Cursor, pkg *cache.Package) (*types.TypeName, error) {
	target_cursor, ok := subtree.FindNode(target_node) // convert to cursor
	if !ok {
		return nil, fmt.Errorf("cursorToTypeInfo - couldn't find target cursor")
	}
	objs, err := objectsAt(pkg.TypesInfo(), target_cursor)
	if err != nil {
		return nil, fmt.Errorf("cursorToTypeInfo - err getting object: %v", err.Error())
	}
	if len(objs) != 1 {
		// only happens in switch
		return nil, fmt.Errorf("cursorToTypeInfo - %v objects, expected one", len(objs))
	}
	typeinfo, ok := objs[0].obj.(*types.TypeName)
	if !ok {
		return nil, fmt.Errorf("cursorToTypeInfo - obj %+v is not a type", objs[0].obj)
	}
	return typeinfo, nil
}

// effectiveReceiver returns the effective receiver type for method-set
// comparisons for obj, if it is a method, or nil otherwise.
func effectiveReceiver(obj types.Object) types.Type {
	if fn, ok := obj.(*types.Func); ok {
		if recv := fn.Signature().Recv(); recv != nil {
			return methodsets.EnsurePointer(recv.Type())
		}
	}
	return nil
}

type objectAt struct {
	obj types.Object     // symbol
	cur inspector.Cursor // associated syntax (Ident | ImportSpec)
}

// objectsAt returns the non-empty list of objects referenced (defined
// or used) at or near the position specified by cur. It returns an
// error if none were found.
//
// The implementation may look "nearby", for example at x when given
// a cursor to an expression "*x", i.e. the cursor was at the star.
//
// The result may contain more than one element because all case
// variables of a type switch appear to be declared at the same
// identifier.
//
// Each object is paired with a cursor for the syntax node that was
// treated as an identifier, which is not always an ast.Ident.
func objectsAt(info *types.Info, cur inspector.Cursor) ([]objectAt, error) {

	// Within an ImportSpec, return the PkgName
	// and its .Name (if explicit) or the spec if not.
	if cur.ParentEdgeKind() == edge.ImportSpec_Path {
		cur = cur.Parent() // ImportSpec
		spec := cur.Node().(*ast.ImportSpec)
		pkgname := info.PkgNameOf(spec)
		if pkgname == nil {
			return nil, fmt.Errorf("%w for import %s", errNoObjectFound, metadata.UnquoteImportPath(spec))
		}
		if spec.Name != nil { // explicit?
			cur = cur.ChildAt(edge.ImportSpec_Name, -1) // Ident
		}
		return []objectAt{{pkgname, cur}}, nil
	}

	// If the selection is the * of *T or *ptr,
	// nudge it to the T or ptr operand in the hope
	// that it is an Ident. This makes (e.g.) Hover and
	// Definition more flexible w.r.t selections:
	if is[*ast.StarExpr](cur.Node()) {
		cur, _ = cur.FirstChild() // can't fail
	}

	id, ok := cur.Node().(*ast.Ident)
	if !ok {
		return nil, ErrNoIdentFound
	}

	// If id is a reference to a special var v in
	//  switch v := expr.(type) { case T: use(v); ... }
	// then return all the implicit case vars.
	objects, _ := typeSwitchVars(info, cur)
	if len(objects) > 0 {
		return objects, nil
	}

	// All other identifiers.
	// For struct{T}, we prefer the defined field Var over the used TypeName.
	obj := info.ObjectOf(id)
	if obj == nil {
		return nil, fmt.Errorf("%w for %q", errNoObjectFound, id.Name)
	}
	return []objectAt{{obj, cur}}, nil
}

// typeSwitchVars returns information about type switch local variables.
//
// Given the cursor for an identifier that refers to a variable v
// declared by a type switch of this form:
//
//	switch v := expr.(type) {
//	case T: use(v)
//	...
//	}
//
// it returns:
//
//   - the (possibly empty) list of variables implicitly declared for
//     each case type; and
//
//   - the identifier's effective type, which is either the case type
//     (for an occurrence in a case), or the type of 'expr' for the
//     occurrence in "switch v".
//
// The identifier may be v in "switch v", or a use of it in one of the
// cases. typeSwitchVars returns zero for all other identifiers.
func typeSwitchVars(info *types.Info, curIdent inspector.Cursor) ([]objectAt, types.Type) {
	sw, curSwitch := cursorutil.FirstEnclosing[*ast.TypeSwitchStmt](curIdent)
	if sw == nil {
		return nil, nil
	}
	assign, ok := sw.Assign.(*ast.AssignStmt)
	if !(ok &&
		len(assign.Lhs) == 1 &&
		is[*ast.Ident](assign.Lhs[0]) &&
		len(assign.Rhs) == 1 &&
		is[*ast.TypeAssertExpr](assign.Rhs[0])) {
		return nil, nil
	}
	// Have: switch v := expr.(type)

	id := curIdent.Node().(*ast.Ident)

	match := false

	// Is selected ident "switch v" var?
	// Since it has no object, use the type of 'expr'.
	var t types.Type
	if id == assign.Lhs[0] {
		match = true
		t = info.TypeOf(assign.Rhs[0].(*ast.TypeAssertExpr).X) // may be nil
	}

	// Gather the switch's implicit variables.
	var objects []objectAt
	for curCase := range curSwitch.ChildAt(edge.TypeSwitchStmt_Body, -1).Children() {
		clause := curCase.Node().(*ast.CaseClause)
		v, ok := info.Implicits[clause]
		if ok {
			if v == info.Uses[id] {
				// Selected ident is one of the case vars.
				t = v.Type()
				match = true
			}
			objects = append(objects, objectAt{v, curIdent})
		}
	}

	if !match {
		// Type switch is unrelated to ident.
		return nil, nil
	}

	// Note: match does not imply t != nil,
	// as type information may be incomplete.

	return objects, t
}

// mustLocation reports the location interval a syntax node,
// which must belong to m.File.
//
// Safe for use only by references and implementations.
func mustLocation(pgf *parsego.File, n ast.Node) protocol.Location {
	loc, err := pgf.NodeLocation(n)
	if err != nil {
		panic(err) // can't happen in references or implementations
	}
	return loc
}
