package main

import (
	"flag"
	"fmt"
	"log"
	"log/slog"
	"os"
	"time"

	"github.com/dominikbraun/graph"
	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
)

// Get all paths containing the start_type_name, or shortest path from start_type_name to end_type_name.
// Serialize and write to .gv the resulting subgraph.
func main() {
	var infile, outfile, start_type_name, end_type_name string
	flag.StringVar(&infile, "infile", "", "File containing serialized full graph")
	flag.StringVar(&outfile, "outfile", "", "Filename prefix for subgraphs (serialized will be <outfile>.txt, graphviz will be <outfile>.gv)")
	flag.StringVar(&start_type_name, "start-type", "", "Query start type (mandatory)")
	flag.StringVar(&end_type_name, "end-type", "", "Query end type (optional - "+
		"if passed, will find shortest path from start to end, rather than all paths containing start)")
	flag.Parse()
	if infile == "" || outfile == "" || start_type_name == "" {
		flag.Usage()
		log.Fatalf("Missing mandatory argument")
	}

	g, m := ct.Deserialize(infile)
	full_g := ct.CTypes{Graph: g, List: m.List}

	start_hash, ok := full_g.GetHash(ct.FullTypeName(start_type_name))
	if !ok {
		panic(fmt.Errorf("type %v not found", start_type_name))
	}
	end_hash := ct.CTypeHash("")
	if end_type_name != "" {
		end_hash, ok = full_g.GetHash(ct.FullTypeName(end_type_name))
		if !ok {
			panic(fmt.Errorf("type %v not found", end_type_name))
		}
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
	var end *ct.CTypeHash
	if end_type_name != "" {
		end = &end_hash
		graph.Logf(g.Log(), slog.LevelInfo, "Querying for path %v => %v", start_type_name, end_type_name)
	} else {
		graph.Logf(g.Log(), slog.LevelInfo, "Querying for paths containing %v", start_type_name)
	}
	sub_g := graph.Query(g, start_hash, end)
	graph.Logf(g.Log(), slog.LevelInfo, "Query time: %v", time.Since(start))

	// reuse list (will be superset of nodes actually in subgraph)
	sub_ctypes := ct.CTypes{Graph: sub_g, List: m.List}
	sub_ctypes.Serialize(outfile+".txt", "", true)
}
