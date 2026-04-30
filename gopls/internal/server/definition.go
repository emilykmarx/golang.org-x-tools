// Copyright 2019 The Go Authors. All rights reserved.
// Use of this source code is governed by a BSD-style
// license that can be found in the LICENSE file.

package server

import (
	"context"
	"fmt"
	"go/types"

	"golang.org/x/tools/gopls/internal/file"
	"golang.org/x/tools/gopls/internal/goasm"
	"golang.org/x/tools/gopls/internal/golang"
	"golang.org/x/tools/gopls/internal/label"
	"golang.org/x/tools/gopls/internal/protocol"
	"golang.org/x/tools/gopls/internal/telemetry"
	"golang.org/x/tools/gopls/internal/template"
	"golang.org/x/tools/internal/event"
)

func (s *Server) Definition(ctx context.Context, params *protocol.DefinitionParams) (_ []protocol.Location, rerr error) {
	recordLatency := telemetry.StartLatencyTimer("definition")
	defer func() {
		recordLatency(ctx, rerr)
	}()

	ctx, done := event.Start(ctx, "server.Definition", label.URI.Of(params.TextDocument.URI))
	defer done()

	// TODO(rfindley): definition requests should be multiplexed across all views.
	fh, snapshot, release, err := s.session.FileOf(ctx, params.TextDocument.URI)
	if err != nil {
		return nil, err
	}
	defer release()
	switch kind := snapshot.FileKind(fh); kind {
	case file.Tmpl:
		return template.Definition(snapshot, fh, params.Range)
	case file.Go:
		return golang.Definition(ctx, snapshot, fh, params.Range)
	case file.Asm:
		return goasm.Definition(ctx, snapshot, fh, params.Range)
	default:
		return nil, fmt.Errorf("can't find definitions for file type %s", kind)
	}
}

func (s *Server) DefinitionMoreInfo(ctx context.Context, params *protocol.DefinitionParams) (_ []protocol.Location, _ *types.Object, rerr error) {
	recordLatency := telemetry.StartLatencyTimer("definition")
	defer func() {
		recordLatency(ctx, rerr)
	}()

	ctx, done := event.Start(ctx, "server.Definition", label.URI.Of(params.TextDocument.URI))
	defer done()

	// TODO(rfindley): definition requests should be multiplexed across all views.
	fh, snapshot, release, err := s.session.FileOf(ctx, params.TextDocument.URI)
	if err != nil {
		return nil, nil, err
	}
	defer release()
	switch kind := snapshot.FileKind(fh); kind {
	case file.Tmpl:
		locs, err := template.Definition(snapshot, fh, params.Range)
		return locs, nil, err
	case file.Go:
		return golang.DefinitionMoreInfo(ctx, snapshot, fh, params.Range)
	case file.Asm:
		locs, err := goasm.Definition(ctx, snapshot, fh, params.Range)
		return locs, nil, err
	default:
		return nil, nil, fmt.Errorf("can't find definitions for file type %s", kind)
	}
}
func (s *Server) TypeDefinition(ctx context.Context, params *protocol.TypeDefinitionParams) ([]protocol.Location, error) {
	ctx, done := event.Start(ctx, "server.TypeDefinition", label.URI.Of(params.TextDocument.URI))
	defer done()

	// TODO(rfindley): type definition requests should be multiplexed across all views.
	fh, snapshot, release, err := s.session.FileOf(ctx, params.TextDocument.URI)
	if err != nil {
		return nil, err
	}

	defer release()
	switch kind := snapshot.FileKind(fh); kind {
	case file.Go:
		return golang.TypeDefinition(ctx, snapshot, fh, params.Range)
	default:
		return nil, fmt.Errorf("can't find type definitions for file type %s", kind)
	}
}
