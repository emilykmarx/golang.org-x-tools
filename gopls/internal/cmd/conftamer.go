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
	TODOflag bool `flag:"d,declaration" help:"include the declaration of the specified identifier in the results"`
	app      *Application
}

func (c *conftamer) Name() string      { return "conftamer" }
func (c *conftamer) Parent() string    { return c.app.Name() }
func (c *conftamer) Usage() string     { return "[conftamer-flags] <TODO>" }
func (c *conftamer) ShortHelp() string { return "TODO short help" }
func (c *conftamer) DetailedHelp(f *flag.FlagSet) {
	fmt.Fprint(f.Output(), `
	TODO
conftamer-flags:
`)
	printFlagDefaults(f)
}

// Type names that implement the UnmarshalYAML interface
func unmarshalImpls(ctx context.Context, cli *client, local_server *server.Server) ([]golang.Implementer, error) {
	// TODO find definition of UnmarshalYAML properly (and other unmarshal pkgs)
	unmarshal_defn := "/home/emily/go/pkg/mod/gopkg.in/yaml.v2@v2.4.0/yaml.go:33:3"
	other_unmarshal_pkgs := []string{"gopkg.in/yaml.v3", "sigs.k8s.io/yaml/goyaml.v2"}
	p, err := locStrToImplParams(ctx, unmarshal_defn, cli)
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
	method_name_loc protocol.Location) ([]protocol.Location, *types.Object, error) {
	// 1. method name location => type name location
	// TODO proper way of doing this with AST (this assumes single space between type name and method name)
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

	return defn_locs, defn_obj, nil
}

func (c *conftamer) Run(ctx context.Context, args ...string) error {
	if len(args) != 0 {
		return tool.CommandLineErrorf("conftamer expects no arguments")
	}

	cli, _, err := c.app.connect(ctx)
	if err != nil {
		return err
	}
	defer cli.terminate(ctx)
	ctypes_graph := ct.NewCTypeGraph()

	// 1. Find types that contain config file contents
	// i.e. those that implement UnmarshalYAML
	// TODO also find all types passed as 2nd arg to yaml.Unmarshal - for any that don't impl Unmarshal, record their params

	local_server := cli.server.(*server.Server)
	unmarshalImpls, err := unmarshalImpls(ctx, cli, local_server)
	if err != nil {
		return err
	}

	// 2. Find params in unmarshaling types
	for _, unmarshalImpl := range unmarshalImpls {
		defn_locs, defn_obj, err := implementingTypeDefinition(ctx, cli, local_server, unmarshalImpl.Loc)
		if err != nil {
			return err
		}
		nice_defn_locs, err := locsToSpans(ctx, cli, defn_locs)
		if err != nil {
			return err
		}

		err = ct.AddCType(nice_defn_locs, defn_obj, ctypes_graph)
		if err != nil {
			return err
		}
	}

	// 3. Find edges between unmarshaling types
	err = ct.FindCTypeEdges(ctypes_graph)
	if err != nil {
		return err
	}

	// 4. Find param key prefixes
	err = ct.FindParamKeys(ctypes_graph)
	if err != nil {
		return err
	}

	// TODO (minor): get this automatically from go.mod (or at least take it as CLI argument)
	module_name := "github.com/prometheus/prometheus"
	ct.PrettyPrint(ctypes_graph, false, module_name+"/")

	return nil
}
