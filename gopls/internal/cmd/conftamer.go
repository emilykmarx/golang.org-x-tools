package cmd

import (
	"context"
	"flag"
	"fmt"
	"go/types"
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
func (c *conftamer) unmarshalImpls(ctx context.Context, cli *client, local_server *server.Server) ([]golang.Implementer, error) {
	// TODO find definition of UnmarshalYAML properly (and other unmarshal pkgs)
	other_unmarshal_pkgs := []string{"gopkg.in/yaml.v3", "sigs.k8s.io/yaml/goyaml.v2"}
	p, err := locStrToImplParams(ctx, c.UnmarshalDefn, cli)
	if err != nil {
		return nil, err
	}
	implementations, err := local_server.ImplementationMoreInfo(ctx, p)
	if err != nil {
		return nil, err
	}
	// Also returns the other two interface definitions from the other two yaml packages in prometheus - ignore
	implementations = slices.DeleteFunc(implementations, func(impl golang.Implementer) bool {
		return slices.Contains(other_unmarshal_pkgs, string(impl.PkgPath))
	})

	return implementations, nil
}

// Type definition of type that implements method at location `method_name_loc`
func implementingTypeDefinition(ctx context.Context, cli *client, local_server *server.Server,
	method_name_loc protocol.Location) ([]protocol.Location, *types.TypeName, error) {
	// 1. method name location => type name location
	// TODO proper way of doing this with AST (this assumes single space between type name and method name) - effectiveReceiver?
	type_name_loc := method_name_loc
	type_name_loc.Range.Start.Character = method_name_loc.Range.Start.Character - 2
	type_name_loc.Range.End.Character = type_name_loc.Range.Start.Character

	// 2. type name location => type definition
	p := protocol.DefinitionParams{
		TextDocumentPositionParams: protocol.LocationTextDocumentPositionParams(type_name_loc),
	}
	defn_locs, defn_obj, err := local_server.DefinitionMoreInfo(ctx, &p)
	if err != nil {
		return nil, nil, err
	}

	if len(defn_locs) == 0 {
		return nil, nil, fmt.Errorf("%v: no definition location (not an identifier?)", type_name_loc)
	}
	if defn_obj == nil {
		return defn_locs, nil, fmt.Errorf("%v: no object at locs %v", type_name_loc, defn_locs)
	}
	type_info, ok := (*defn_obj).(*types.TypeName)
	if !ok {
		return nil, nil, fmt.Errorf("obj %+v is not a type", *defn_obj)
	}

	return defn_locs, type_info, nil
}

// Get types that enclose this CType (which is defined at defn_locs)
func getParentCTypes(ctx context.Context, cli *client, local_server *server.Server, defn_locs []string) ([]golang.Implementer, error) {
	parent_ctypes := []golang.Implementer{}

	for _, defn_loc := range defn_locs {
		p, err := locStrToRefParams(ctx, defn_loc, cli, false)
		if err != nil {
			return nil, fmt.Errorf("locStrToRefParams: %v", err.Error())
		}

		// Check CType's references for other types
		_, enclosing_types, err := local_server.ReferencesMoreInfo(ctx, p)
		if err != nil {
			return nil, fmt.Errorf("ReferencesMoreInfo: %v", err.Error())
		}
		parent_ctypes = append(parent_ctypes, enclosing_types...)
	}

	return parent_ctypes, nil
}

// Get types enclosed in this CType (which is defined at defn_locs)
func getChildCTypes(ctx context.Context, cli *client, local_server *server.Server, defn_locs []string) ([]golang.Implementer, error) {
	child_ctypes := []golang.Implementer{}

	for _, defn_loc := range defn_locs {
		p, _, err := locStrToDefnParams(ctx, defn_loc, cli)
		if err != nil {
			return nil, fmt.Errorf("locStrToDefnParams: %v", err.Error())
		}

		// Check CType's type definition for other types
		enclosed_types, err := local_server.EnclosedTypes(ctx, p)
		if err != nil {
			return nil, fmt.Errorf("EnclosedTypes: %v", err.Error())
		}
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
func addReachableCTypes(ctx context.Context, cli *client, local_server *server.Server,
	obj *types.TypeName, defn_locs []string, g ct.CTypeGraph, list *ct.CTypeList, neigh_name *ct.FullTypeName, neigh_age NeighAge, neigh_reason ct.NeighReason) error {

	cur_name := ct.TypeName(obj)

	// 1. Add the CType to the graph, combining with existing node if not via struct field.
	existed, err := ct.AddCType(obj, g, list, neigh_name, neigh_reason)
	if err != nil {
		return fmt.Errorf("AddCType: %v", err.Error())
	}

	// 2. Add edge to neighbor we found obj via, if via struct field -
	// even if already added the node for obj (need edge for all of obj's neighbors)
	if neigh_name != nil && neigh_reason == ct.StructField {
		neigh_hash, ok := ct.Hash(*neigh_name, list)
		if !ok {
			return fmt.Errorf("neighbor %v doesn't exist", neigh_hash)
		}
		own_hash, ok := ct.Hash(cur_name, list)
		if !ok {
			return fmt.Errorf("cur node %v doesn't exist", own_hash)
		}
		parent_hash := neigh_hash
		child_name := cur_name
		if neigh_age == NeighIsChild {
			parent_hash = own_hash
			child_name = *neigh_name
		}
		// Need parent's type info and child's type name =>
		// pass HASH of parent and NAME of child
		ct.AddCTypeEdge(g, list, parent_hash, child_name)
	}

	// Stop recursing if had already added this node.
	if existed == ct.TypeNameExists {
		return nil
	}

	fmt.Printf("\nADD CTYPE %v\n", cur_name)

	// 3. Find new neighbors (direct parents and children), and nodes reachable from them -
	// i.e. find enclosing (parent) CTypes, and enclosed (child) CTypes, then recurse on them
	parents, err := getParentCTypes(ctx, cli, local_server, defn_locs)
	if err != nil {
		return fmt.Errorf("getParentStructCTypes: %v", err.Error())
	}
	children, err := getChildCTypes(ctx, cli, local_server, defn_locs)
	if err != nil {
		return fmt.Errorf("getChildFieldCTypes: %v", err.Error())
	}
	fmt.Printf("PARENTS: %+v\n", parents)
	fmt.Printf("CHILDREN: %+v\n", children)

	for parent_or_child, new_neighbors := range [][]golang.Implementer{parents, children} {
		for _, new := range new_neighbors {
			new_defn_locs, err := locsToSpans(ctx, cli, []protocol.Location{new.Loc})
			if err != nil {
				return fmt.Errorf("locsToSpans: %v", err.Error())
			}
			// cur is now neigh => if found a parent, pass child as relation
			neigh_age := NeighIsChild
			if parent_or_child == 1 {
				neigh_age = NeighIsParent
			}
			neigh_reason := ct.NotStructField
			if new.IsStructField {
				neigh_reason = ct.StructField
			}
			err = addReachableCTypes(ctx, cli, local_server, new.TypeInfo, new_defn_locs, g, list, &cur_name, neigh_age, neigh_reason)
			if err != nil {
				return fmt.Errorf("addReachableCTypes: %v", err.Error())
			}
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
	ctypes_graph, ctypes_list := ct.NewCTypeGraph()

	// 1. Find types that contain config file contents,
	// i.e. those that implement UnmarshalYAML
	// TODO also find all types passed as 2nd arg to yaml.Unmarshal - for any that don't impl Unmarshal, record their params

	local_server := cli.server.(*server.Server)
	unmarshalImpls, err := c.unmarshalImpls(ctx, cli, local_server)
	if err != nil {
		return fmt.Errorf("unmarshalImpls: %v", err.Error())
	}

	// 2. Find all CTypes reachable from the unmarshaling types
	for _, unmarshalImpl := range unmarshalImpls {
		defn_locs, defn_obj, err := implementingTypeDefinition(ctx, cli, local_server, unmarshalImpl.Loc)
		if err != nil {
			return fmt.Errorf("implementingTypeDefinition for %v: %v", unmarshalImpl.Loc, err.Error())
		}
		nice_defn_locs, err := locsToSpans(ctx, cli, defn_locs)
		if err != nil {
			return fmt.Errorf("locsToSpans: %v", err.Error())
		}

		err = addReachableCTypes(ctx, cli, local_server, defn_obj, nice_defn_locs, ctypes_graph, &ctypes_list, nil, 0, 0)
		if err != nil {
			return fmt.Errorf("addReachableCTypes: %v", err.Error())
		}
	}

	// 3. Find param keys and corresponding source code expressions
	err = ct.GetCTypeParams(ctypes_graph)
	if err != nil {
		return err
	}

	err = ct.PrettyPrint(ctypes_graph, c.ModulePrefix)
	if err != nil {
		return err
	}

	return nil
}
