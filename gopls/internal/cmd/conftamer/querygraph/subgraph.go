package main

import (
	"flag"
	"log"
	"log/slog"
	"os"
	"time"

	"github.com/dominikbraun/graph"
	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
)

// Get all paths containing the type_name.
// Serialize and write to .gv the resulting subgraph.
func main() {
	var infile, outfile, type_name string
	flag.StringVar(&infile, "infile", "", "File containing serialized full graph")
	flag.StringVar(&outfile, "outfile", "", "Filename prefix for subgraphs (serialized will be <outfile>.txt, graphviz will be <outfile>.gv)")
	flag.StringVar(&type_name, "type", "", "Query type")
	flag.Parse()
	if infile == "" || outfile == "" || type_name == "" {
		flag.Usage()
		log.Fatalf("Missing mandatory argument")
	}

	g, m := ct.Deserialize(infile)
	full_g := ct.CTypes{Graph: g, List: m.List}
	hash, ok := full_g.GetHash(ct.FullTypeName(type_name))
	if !ok {
		panic("type not found")
	}

	// needed for logging in graph lib
	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelDebug,
		ReplaceAttr: func(groups []string, a slog.Attr) slog.Attr {
			if a.Key == "msg" {
				return a
			} else {
				return slog.Attr{}
			}
		}}))

	g.SetLog(log)

	start := time.Now()
	graph.Logf(g.Log(), slog.LevelInfo, "Querying for paths containing %v", type_name)
	sub_g := graph.Query(g, hash)
	graph.Logf(g.Log(), slog.LevelInfo, "Query time: %v", time.Since(start))

	// reuse list (will be superset of nodes actually in subgraph)
	sub_ctypes := ct.CTypes{Graph: sub_g, List: m.List}
	sub_ctypes.Serialize(outfile+".txt", "", true)
}
