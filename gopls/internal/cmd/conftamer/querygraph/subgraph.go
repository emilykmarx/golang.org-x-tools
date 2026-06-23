package main

import (
	"flag"
	"log"

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

	sub_g := graph.Query(g, hash)

	// reuse list (will be superset of nodes actually in subgraph)
	sub_ctypes := ct.CTypes{Graph: sub_g, List: m.List}
	sub_ctypes.Serialize(outfile+".txt", "", true)
}
