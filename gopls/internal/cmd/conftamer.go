package cmd

import (
	"context"
	"flag"
	"fmt"
	"go/types"
	"slices"

	"github.com/dominikbraun/graph"
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

// Find struct types that have a field of type <CType> (for the CType defined at defn_locs)
func getParentStructCTypes(ctx context.Context, cli *client, local_server *server.Server, defn_locs []string) ([]ct.CTypeNode, error) {
	parent_ctypes := []ct.CTypeNode{}

	for _, defn_loc := range defn_locs {
		p, err := locStrToRefParams(ctx, defn_loc, cli, false)
		if err != nil {
			return nil, fmt.Errorf("locStrToRefParams: %v", err.Error())
		}

		_, struct_ctypes, err := local_server.ReferencesMoreInfo(ctx, p)
		if err != nil {
			return nil, fmt.Errorf("ReferencesMoreInfo: %v", err.Error())
		}
		new_parent_ctypes, err := cTypeLocsToSpans(ctx, cli, struct_ctypes)
		if err != nil {
			return nil, fmt.Errorf("cTypeLocsToSpans: %v", err.Error())
		}
		parent_ctypes = append(parent_ctypes, new_parent_ctypes...)
	}

	return parent_ctypes, nil
}

// If ctype is a struct, find its field types.
func getChildFieldCTypes(ctx context.Context, cli *client, local_server *server.Server, defn_locs []string, ctype *ct.CTypeNode) ([]ct.CTypeNode, error) {
	// Check if ctype is a struct
	parent_struct := ct.IsStruct(&ctype.TypeInfo)
	if parent_struct == nil {
		return nil, nil
	}

	child_ctypes := []ct.CTypeNode{}

	for _, defn_loc := range defn_locs {
		p, _, err := locStrToDefnParams(ctx, defn_loc, cli)
		if err != nil {
			return nil, fmt.Errorf("locStrToDefnParams: %v", err.Error())
		}

		field_ctypes, err := local_server.StructFieldTypeObjs(ctx, p)
		if err != nil {
			return nil, fmt.Errorf("StructFieldTypeObjs: %v", err.Error())
		}
		new_child_ctypes, err := cTypeLocsToSpans(ctx, cli, field_ctypes)
		if err != nil {
			return nil, fmt.Errorf("cTypeLocsToSpans: %v", err.Error())
		}
		child_ctypes = append(child_ctypes, new_child_ctypes...)
	}

	return child_ctypes, nil
}

// reformat the locations and create the corresponding nodes
func cTypeLocsToSpans(ctx context.Context, cli *client, ctypes_raw []golang.Implementer) ([]ct.CTypeNode, error) {
	ctypes := []ct.CTypeNode{}
	for _, new_ctype := range ctypes_raw {
		new_defn_locs, err := locsToSpans(ctx, cli, []protocol.Location{new_ctype.Loc})
		if err != nil {
			return nil, fmt.Errorf("locsToSpans: %v", err.Error())
		}

		ctypes = append(ctypes, ct.CTypeNode{TypeInfo: *new_ctype.TypeInfo, Loc: new_defn_locs})
	}

	return ctypes, nil
}

// Add all CTypes reachable from this one, stopping on reaching one we've already found
func addReachableCTypes(ctx context.Context, cli *client, local_server *server.Server, obj *types.TypeName, defn_locs []string, g ct.CTypeGraph) error {
	// 1. Add the CType to the graph, stopping if we've already visited it
	// (will already exist since we added it to add an edge from its child)

	new_ctype, err := ct.AddCType(obj, defn_locs, g)
	if err != nil {
		return fmt.Errorf("AddCType: %v", err.Error())
	}
	if new_ctype.Visited {
		return nil
	}
	visited := new_ctype
	visited.Visited = true
	err = g.UpdateVertex(ct.CTypeNodeHash(*new_ctype), *visited, func(vp *graph.VertexProperties) {})
	if err != nil {
		return err
	}

	// 2. Find direct parents and children, and nodes reachable from them

	// 2a. Find structs that have this CType as field (new parents),
	// and fields of this CType if it's a struct (new children)
	parents, err := getParentStructCTypes(ctx, cli, local_server, defn_locs)
	if err != nil {
		return fmt.Errorf("getParentStructCTypes: %v", err.Error())
	}
	children, err := getChildFieldCTypes(ctx, cli, local_server, defn_locs, new_ctype)
	if err != nil {
		return fmt.Errorf("getChildFieldCTypes: %v", err.Error())
	}

	for i, neighbors := range [][]ct.CTypeNode{parents, children} {
		for _, neigh := range neighbors {
			// Add neighbor node but don't mark it visited yet
			neigh_ctype, err := ct.AddCType(&neigh.TypeInfo, neigh.Loc, g)
			if err != nil {
				return fmt.Errorf("AddCType neighbor: %v", err.Error())
			}
			// Add edge to neighbor
			parent := neigh_ctype
			child := new_ctype
			if i == 1 { // just found a child
				parent = new_ctype
				child = neigh_ctype
			}
			ct.AddCTypeEdge(g, *parent, *child)

			err = addReachableCTypes(ctx, cli, local_server, &neigh.TypeInfo, neigh.Loc, g)
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
	ctypes_graph := ct.NewCTypeGraph()

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

		err = addReachableCTypes(ctx, cli, local_server, defn_obj, nice_defn_locs, ctypes_graph)
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
