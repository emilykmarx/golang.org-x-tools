package cmd

import (
	"context"
	"flag"
	"fmt"
	"go/types"
	"log/slog"
	"os"
	"path/filepath"
	"slices"

	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
	"golang.org/x/tools/gopls/internal/golang"
	"golang.org/x/tools/gopls/internal/protocol"
	"golang.org/x/tools/gopls/internal/server"
	"golang.org/x/tools/internal/tool"
)

// conftamer implements the conftamer verb for gopls
type conftamer struct {
	app           *Application
	ctx           context.Context
	cli           *client
	local_server  *server.Server
	ctypes        *ct.CTypes
	log           *slog.Logger
	UnmarshalDefn string `flag:"u,unmarshal_defn" help:"Location of the unmarshal interface definition"`
	ModulePrefix  string `flag:"m,module_prefix" help:"Prefix of module path (used to pretty-print)"`
}

const (
	DEFAULT_UNMARSHAL_DEFN = "/home/emily/go/pkg/mod/gopkg.in/yaml.v2@v2.4.0/yaml.go:33:3"
)

func (c *conftamer) Name() string      { return "conftamer" }
func (c *conftamer) Parent() string    { return c.app.Name() }
func (c *conftamer) Usage() string     { return "[conftamer-flags]" }
func (c *conftamer) ShortHelp() string { return "Finds the CTypes graph" }
func (c *conftamer) DetailedHelp(f *flag.FlagSet) {
	fmt.Fprint(f.Output(), `
	conftamer-flags:`)
	printFlagDefaults(f)
	fmt.Fprintf(f.Output(), `
	Default: %[1]v
`, DEFAULT_UNMARSHAL_DEFN) // unsure how to put this in the struct tag for the flag
}

// Type names that implement the UnmarshalYAML interface
func (c *conftamer) unmarshalImpls() ([]golang.Implementer, error) {
	// TODO find definition of UnmarshalYAML properly (and other unmarshal pkgs)
	other_unmarshal_pkgs := []string{"gopkg.in/yaml.v3", "sigs.k8s.io/yaml/goyaml.v2"}
	p, err := locStrToImplParams(c.ctx, c.UnmarshalDefn, c.cli)
	ct.CheckErr(err)

	implementations, err := c.local_server.ImplementationMoreInfo(c.ctx, p)
	ct.CheckErr(err)

	// Also returns the other two interface definitions from the other two yaml packages in prometheus - ignore
	implementations = slices.DeleteFunc(implementations, func(impl golang.Implementer) bool {
		return slices.Contains(other_unmarshal_pkgs, string(impl.PkgPath))
	})

	return implementations, nil
}

// Type definition of type that implements method at location `method_name_loc`
func (c *conftamer) implementingTypeDefinition(method_name_loc protocol.Location) ([]protocol.Location, *types.TypeName, error) {
	// 1. method name location => type name location
	// TODO proper way of doing this with AST (this assumes single space between type name and method name) - effectiveReceiver?
	type_name_loc := method_name_loc
	type_name_loc.Range.Start.Character = method_name_loc.Range.Start.Character - 2
	type_name_loc.Range.End.Character = type_name_loc.Range.Start.Character

	// 2. type name location => type definition
	p := protocol.DefinitionParams{
		TextDocumentPositionParams: protocol.LocationTextDocumentPositionParams(type_name_loc),
	}
	defn_locs, defn_obj, err := c.local_server.DefinitionMoreInfo(c.ctx, &p)
	ct.CheckErr(err)

	if len(defn_locs) == 0 {
		err := fmt.Errorf("%v: no definition location (not an identifier?)", type_name_loc)
		ct.CheckErr(err)
	}
	if defn_obj == nil {
		err := fmt.Errorf("%v: no object at locs %v", type_name_loc, defn_locs)
		ct.CheckErr(err)
	}
	type_info, ok := (*defn_obj).(*types.TypeName)
	if !ok {
		err := fmt.Errorf("obj %+v is not a type", *defn_obj)
		ct.CheckErr(err)
	}

	return defn_locs, type_info, nil
}

// Get types that enclose this CType (which is defined at defn_locs)
func (c *conftamer) getParentCTypes(defn_locs []string) ([]golang.Implementer, error) {
	parent_ctypes := []golang.Implementer{}

	for _, defn_loc := range defn_locs {
		p, err := locStrToRefParams(c.ctx, defn_loc, c.cli, false)
		ct.CheckErr(err)

		// Check CType's references for other types
		_, enclosing_types, err := c.local_server.ReferencesMoreInfo(c.ctx, p)
		ct.CheckErr(err)

		parent_ctypes = append(parent_ctypes, enclosing_types...)
	}

	return parent_ctypes, nil
}

// Get types enclosed in this CType (which is defined at defn_locs)
func (c *conftamer) getChildCTypes(defn_locs []string) ([]golang.Implementer, error) {
	child_ctypes := []golang.Implementer{}

	for _, defn_loc := range defn_locs {
		p, _, err := locStrToDefnParams(c.ctx, defn_loc, c.cli)
		ct.CheckErr(err)

		// Check CType's type definition for other types
		enclosed_types, err := c.local_server.EnclosedTypes(c.ctx, p)
		ct.CheckErr(err)
		child_ctypes = append(child_ctypes, enclosed_types...)
	}

	return child_ctypes, nil
}

type NeighAge int

const (
	NeighIsParent NeighAge = iota
	NeighIsChild
)

// Add all CTypes reachable from this one, stopping on reaching one we've already found
// neigh_* is info about the neighbor we found this obj via (if any)
// defn_locs is of the obj
func (c *conftamer) addReachableCTypes(obj *types.TypeName, defn_locs []string, neigh_name *ct.FullTypeName, neigh_age NeighAge, neigh_reason ct.NeighReason) error {
	// Ignore types not declared in package scope
	if obj.Parent() == nil || obj.Parent().Parent() != types.Universe {
		// e.g. function-local types, or `error`
		// Can cause TypeName to segfault - don't call it here
		ct.Logf(c.log, slog.LevelInfo, "Ignoring non-package-scope type %v", ct.TypeNameSafe(obj))
		return nil
	}
	cur_name := ct.TypeName(obj)

	// 1. Add the CType to the graph, combining with existing node if not via struct field.
	ct.Logf(c.log, slog.LevelDebug, "\nADD CTYPE %v\n", cur_name)

	existed, err := c.ctypes.AddCType(obj, neigh_name, neigh_reason)
	ct.CheckErr(err)

	// 2. Add edge to neighbor we found obj via, if we didn't combine it with the neighbor -
	// even if we had already added the node for obj (need edge for all of obj's neighbors)
	if neigh_name != nil {
		neigh_hash, ok := c.ctypes.GetHash(*neigh_name)
		if !ok {
			err := fmt.Errorf("neighbor %v doesn't exist", neigh_hash)
			ct.CheckErr(err)
		}
		own_hash, ok := c.ctypes.GetHash(cur_name)
		if !ok {
			err = fmt.Errorf("cur node %v doesn't exist", own_hash)
			ct.CheckErr(err)
		}
		if neigh_hash != own_hash {
			// didn't combine
			parent_hash := neigh_hash
			child_name := cur_name
			if neigh_age == NeighIsChild {
				parent_hash = own_hash
				child_name = *neigh_name
			}
			// Need parent's type info and child's type name =>
			// pass HASH of parent and NAME of child
			err = c.ctypes.AddCTypeEdge(parent_hash, child_name)
			ct.CheckErr(err)
		}
	}

	// Stop recursing if had already added this node.
	if existed == ct.TypeNameExists {
		return nil
	}

	// 3. Find new neighbors (direct parents and children), and nodes reachable from them -
	// i.e. find enclosing (parent) CTypes, and enclosed (child) CTypes, then recurse on them
	parents, err := c.getParentCTypes(defn_locs)
	ct.CheckErr(err)

	children, err := c.getChildCTypes(defn_locs)
	ct.CheckErr(err)

	ct.Logf(c.log, slog.LevelDebug, "PARENTS: %+v\n", parents)
	ct.Logf(c.log, slog.LevelDebug, "CHILDREN: %+v\n", children)

	for parent_or_child, new_neighbors := range [][]golang.Implementer{parents, children} {
		for _, new := range new_neighbors {
			new_defn_locs, err := locsToSpans(c.ctx, c.cli, []protocol.Location{new.Loc})
			ct.CheckErr(err)

			// cur is now neigh => if found a parent, pass child as relation
			neigh_age := NeighIsChild
			if parent_or_child == 1 {
				neigh_age = NeighIsParent
			}
			neigh_reason := ct.NotStructField
			if new.IsStructField {
				neigh_reason = ct.StructField
			}
			err = c.addReachableCTypes(new.TypeInfo, new_defn_locs, &cur_name, neigh_age, neigh_reason)
			ct.CheckErr(err)
		}
	}

	return nil
}

func (c *conftamer) Run(ctx context.Context, args ...string) error {
	if len(args) != 0 {
		return tool.CommandLineErrorf("conftamer expects no arguments (but flags are ok)")
	}
	if c.UnmarshalDefn == "" {
		c.UnmarshalDefn = DEFAULT_UNMARSHAL_DEFN
	}

	cli, _, err := c.app.connect(ctx)
	if err != nil {
		return err
	}
	defer cli.terminate(ctx)
	c.ctx = ctx
	c.cli = cli
	c.log = slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{AddSource: true, Level: slog.LevelInfo,
		// Shorten paths
		ReplaceAttr: func(groups []string, a slog.Attr) slog.Attr {
			if a.Key == slog.SourceKey {
				source, _ := a.Value.Any().(*slog.Source)
				if source != nil {
					source.File = filepath.Base(source.File)
				}
			}
			return a
		}}))
	c.ctypes = ct.New(c.log)

	// 1. Find types that contain config file contents,
	// i.e. those that implement UnmarshalYAML
	// TODO also find all types passed as 2nd arg to yaml.Unmarshal - for any that don't impl Unmarshal, record their params

	c.local_server = cli.server.(*server.Server)
	ct.Logf(c.log, slog.LevelInfo, "Finding types implementing UnmarshalYAML")
	unmarshalImpls, err := c.unmarshalImpls()
	ct.CheckErr(err)

	// 2. Find all CTypes reachable from the unmarshaling types
	for _, unmarshalImpl := range unmarshalImpls {
		ct.Logf(c.log, slog.LevelInfo, "Finding types reachable from %v.%v", unmarshalImpl.PkgPath, unmarshalImpl.TypeName)
		defn_locs, defn_obj, err := c.implementingTypeDefinition(unmarshalImpl.Loc)
		ct.CheckErr(err)

		nice_defn_locs, err := locsToSpans(ctx, cli, defn_locs)
		ct.CheckErr(err)

		err = c.addReachableCTypes(defn_obj, nice_defn_locs, nil, 0, 0)
		ct.CheckErr(err)
	}

	n_edges, err := c.ctypes.Graph.Size()
	ct.CheckErr(err)
	n_nodes, err := c.ctypes.Graph.Order()
	ct.CheckErr(err)

	// 3. Find param keys and corresponding source code expressions
	ct.Logf(c.log, slog.LevelInfo, "Getting param keys and corresponding expressions")
	err = c.ctypes.GetCTypeParams()
	ct.CheckErr(err)

	ct.Logf(c.log, slog.LevelInfo, "Outputting CTypes - graph has %v nodes and %v edges", n_nodes, n_edges)
	err = c.ctypes.PrettyPrint(c.ModulePrefix)
	ct.CheckErr(err)

	return nil
}
