package cmd

import (
	"context"
	"flag"
	"fmt"
	"slices"

	"golang.org/x/tools/gopls/internal/golang"
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

func (c *conftamer) Run(ctx context.Context, args ...string) error {
	if len(args) != 0 {
		return tool.CommandLineErrorf("conftamer expects no arguments")
	}

	cli, _, err := c.app.connect(ctx)
	if err != nil {
		return err
	}
	defer cli.terminate(ctx)

	// 1. Find types that contain config file contents
	// i.e. those that implement UnmarshalYAML
	// TODO find definition of UnmarshalYAML properly (and other unmarshal pkgs)
	unmarshal_defn := "/home/emily/go/pkg/mod/gopkg.in/yaml.v2@v2.4.0/yaml.go:33:3"
	other_unmarshal_pkgs := []string{"gopkg.in/yaml.v3", "sigs.k8s.io/yaml/goyaml.v2"}

	local_server := cli.server.(*server.Server)

	p, err := locStrToImplParams(ctx, unmarshal_defn, cli)
	if err != nil {
		return err
	}
	implementations, err := local_server.ImplementationMoreInfo(ctx, p)
	if err != nil {
		return err
	}
	// Also returns the other two interface definitions from the other two yaml packages in prometheus - ignore
	implementations = slices.DeleteFunc(implementations, func(impl golang.Implementer) bool { return slices.Contains(other_unmarshal_pkgs, string(impl.PkgPath)) })

	for _, impl := range implementations {
		fmt.Printf("%+v\n", impl)
	}

	// 2. Find params in those types
	// using struct tags

	// 3. Go up the tree of types

	return nil
}
