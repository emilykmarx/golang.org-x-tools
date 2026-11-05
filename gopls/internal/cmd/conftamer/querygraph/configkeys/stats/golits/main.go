// Command golits prints the distinct string literals in the non-test Go files compiled into a program: the given
// package and everything it imports, the standard library included, as `go list -deps` lists them for the build tags
// (-tags; default Prometheus'). Interpreted ("...") and raw (`...`) strings are both decoded (strconv.Unquote), and comments are skipped
// (go/scanner). Each literal is printed once, as a JSON string on its own line, since literals can contain newlines.
// See STATISTICS.md, "Caveats", for what this doesn't find.
//
// Usage: golits -dir <module checkout> [-tags <build tags>] [package, default ./cmd/prometheus]
package main

import (
	"encoding/json"
	"flag"
	"go/scanner"
	"go/token"
	"log"
	"os"
	"path/filepath"
	"strconv"

	"golang.org/x/tools/go/packages"
)

func main() {
	dir := flag.String("dir", ".", "Module checkout to load the package from")
	// Prometheus' build tags (.promu.yml), which change which files are compiled
	tags := flag.String("tags", "netgo,builtinassets,stringlabels", "Build tags, comma-separated")
	flag.Parse()
	pattern := "./cmd/prometheus"
	if flag.NArg() > 0 {
		pattern = flag.Arg(0)
	}

	cfg := &packages.Config{Dir: *dir, Mode: packages.NeedName | packages.NeedFiles | packages.NeedImports | packages.NeedDeps,
		BuildFlags: []string{"-tags=" + *tags}}
	roots, err := packages.Load(cfg, pattern)
	if err != nil {
		log.Fatal(err)
	}
	if packages.PrintErrors(roots) > 0 {
		os.Exit(1)
	}

	lits := map[string]bool{}
	n_files := 0
	packages.Visit(roots, nil, func(p *packages.Package) {
		for _, f := range p.GoFiles { // compiled non-test files (cgo files are in GoFiles too)
			src, err := os.ReadFile(f)
			if err != nil {
				log.Fatal(err)
			}
			n_files++
			fset := token.NewFileSet()
			var s scanner.Scanner
			s.Init(fset.AddFile(filepath.Base(f), -1, len(src)), src, nil, 0) // mode 0: comments skipped
			for {
				_, tok, lit := s.Scan()
				if tok == token.EOF {
					break
				}
				if tok == token.STRING {
					if v, err := strconv.Unquote(lit); err == nil {
						lits[v] = true
					}
				}
			}
		}
	})

	enc := json.NewEncoder(os.Stdout)
	enc.SetEscapeHTML(false)
	for v := range lits {
		if err := enc.Encode(v); err != nil {
			log.Fatal(err)
		}
	}
	log.Printf("%v distinct string literals in %v files", len(lits), n_files)
}
