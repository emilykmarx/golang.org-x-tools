// Copyright 2019 The Go Authors. All rights reserved.
// Use of this source code is governed by a BSD-style
// license that can be found in the LICENSE file.

package cmd

import (
	"context"
	"flag"
	"fmt"

	"golang.org/x/tools/gopls/internal/protocol"
	"golang.org/x/tools/internal/tool"
)

// implementation implements the implementation verb for gopls
type implementation struct {
	app *Application
}

func (i *implementation) Name() string      { return "implementation" }
func (i *implementation) Parent() string    { return i.app.Name() }
func (i *implementation) Usage() string     { return "<position>" }
func (i *implementation) ShortHelp() string { return "display selected identifier's implementation" }
func (i *implementation) DetailedHelp(f *flag.FlagSet) {
	fmt.Fprint(f.Output(), `
Example:

	$ # 1-indexed location (:line:column or :#offset) of the target identifier
	$ gopls implementation helper/helper.go:8:6
	$ gopls implementation helper/helper.go:#53
`)
	printFlagDefaults(f)
}

func locStrToImplParams(ctx context.Context, locstr string, cli *client) (*protocol.ImplementationParams, error) {
	from := parseSpan(locstr)
	file, err := cli.openFile(ctx, from.URI())
	if err != nil {
		return nil, err
	}

	loc, err := file.spanLocation(from)
	if err != nil {
		return nil, err
	}

	return &protocol.ImplementationParams{
		TextDocumentPositionParams: protocol.LocationTextDocumentPositionParams(loc),
	}, nil
}

func (i *implementation) Run(ctx context.Context, args ...string) error {
	if len(args) != 1 {
		return tool.CommandLineErrorf("implementation expects 1 argument (position)")
	}

	cli, _, err := i.app.connect(ctx)
	if err != nil {
		return err
	}
	defer cli.terminate(ctx)

	p, err := locStrToImplParams(ctx, args[0], cli)
	if err != nil {
		return err
	}
	implementations, err := cli.server.Implementation(ctx, p)
	if err != nil {
		return err
	}

	spans, err := locsToSpans(ctx, cli, implementations)
	if err != nil {
		return err
	}
	for _, s := range spans {
		fmt.Println(s)
	}

	return nil
}
