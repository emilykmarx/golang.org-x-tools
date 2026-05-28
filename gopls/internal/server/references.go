// Copyright 2019 The Go Authors. All rights reserved.
// Use of this source code is governed by a BSD-style
// license that can be found in the LICENSE file.

package server

import (
	"context"

	"golang.org/x/tools/gopls/internal/file"
	"golang.org/x/tools/gopls/internal/golang"
	"golang.org/x/tools/gopls/internal/label"
	"golang.org/x/tools/gopls/internal/mod"
	"golang.org/x/tools/gopls/internal/protocol"
	"golang.org/x/tools/gopls/internal/telemetry"
	"golang.org/x/tools/gopls/internal/template"
	"golang.org/x/tools/internal/event"
)

func (s *Server) References(ctx context.Context, params *protocol.ReferenceParams) (_ []protocol.Location, rerr error) {
	recordLatency := telemetry.StartLatencyTimer("references")
	defer func() {
		recordLatency(ctx, rerr)
	}()

	ctx, done := event.Start(ctx, "server.References", label.URI.Of(params.TextDocument.URI))
	defer done()

	fh, snapshot, release, err := s.session.FileOf(ctx, params.TextDocument.URI)
	if err != nil {
		return nil, err
	}
	defer release()
	switch snapshot.FileKind(fh) {
	case file.Tmpl:
		return template.References(ctx, snapshot, fh, params)
	case file.Go:
		return golang.References(ctx, snapshot, fh, params.Range, params.Context.IncludeDeclaration)
	case file.Mod:
		return mod.References(ctx, snapshot, fh, params)
	}
	return nil, nil // empty result
}

// References, plus for any of them that represent a use as a struct field: Info on that struct
// XXX rename and fix comment on implementer
func (s *Server) ReferencesMoreInfo(ctx context.Context, params *protocol.ReferenceParams) (_ []protocol.Location, _ []golang.TypeInfo, rerr error) {
	recordLatency := telemetry.StartLatencyTimer("references")
	defer func() {
		recordLatency(ctx, rerr)
	}()

	ctx, done := event.Start(ctx, "server.References", label.URI.Of(params.TextDocument.URI))
	defer done()

	fh, snapshot, release, err := s.session.FileOf(ctx, params.TextDocument.URI)
	if err != nil {
		return nil, nil, err
	}
	defer release()
	switch snapshot.FileKind(fh) {
	case file.Tmpl:
		refs, err := template.References(ctx, snapshot, fh, params)
		return refs, nil, err
	case file.Go:
		return golang.ReferencesMoreInfo(ctx, snapshot, fh, params.Range, params.Context.IncludeDeclaration)
	case file.Mod:
		refs, err := mod.References(ctx, snapshot, fh, params)
		return refs, nil, err
	}
	return nil, nil, nil // empty result
}
