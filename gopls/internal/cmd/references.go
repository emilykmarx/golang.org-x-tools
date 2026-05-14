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

// references implements the references verb for gopls
type references struct {
	IncludeDeclaration bool `flag:"d,declaration" help:"include the declaration of the specified identifier in the results"`

	app *Application
}

func (r *references) Name() string      { return "references" }
func (r *references) Parent() string    { return r.app.Name() }
func (r *references) Usage() string     { return "[references-flags] <position>" }
func (r *references) ShortHelp() string { return "display selected identifier's references" }
func (r *references) DetailedHelp(f *flag.FlagSet) {
	fmt.Fprint(f.Output(), `
Example:

	$ # 1-indexed location (:line:column or :#offset) of the target identifier
	$ gopls references helper/helper.go:8:6
	$ gopls references helper/helper.go:#53

references-flags:
`)
	printFlagDefaults(f)
}

func locStrToRefParams(ctx context.Context, locstr string, cli *client, includeDecl bool) (*protocol.ReferenceParams, error) {
	from := parseSpan(locstr)
	file, err := cli.openFile(ctx, from.URI())
	if err != nil {
		return nil, err
	}

	loc, err := file.spanLocation(from)
	if err != nil {
		return nil, err
	}

	p := protocol.ReferenceParams{
		Context: protocol.ReferenceContext{
			IncludeDeclaration: includeDecl,
		},
		TextDocumentPositionParams: protocol.LocationTextDocumentPositionParams(loc),
	}

	return &p, nil
}

func (r *references) Run(ctx context.Context, args ...string) error {
	if len(args) != 1 {
		return tool.CommandLineErrorf("references expects 1 argument (position)")
	}

	cli, _, err := r.app.connect(ctx)
	if err != nil {
		return err
	}
	defer cli.terminate(ctx)

	p, err := locStrToRefParams(ctx, args[0], cli, r.IncludeDeclaration)
	if err != nil {
		return err
	}

	locations, err := cli.server.References(ctx, p)
	if err != nil {
		return err
	}
	spans, err := locsToSpans(ctx, cli, locations)
	if err != nil {
		return err
	}

	for _, s := range spans {
		fmt.Println(s)
	}
	return nil
}
