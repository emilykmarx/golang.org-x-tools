package cmd

import (
	"context"
	"flag"
	"fmt"

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
	fmt.Println("help i'm trapped in a computer")
	if local_server, ok := cli.server.(*server.Server); ok {
		local_server.ImplementationMore()
	}

	return nil
}
