package main

import (
	"flag"

	graph "github.com/emilykmarx/dominikbraun-graph"
	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
	"golang.org/x/tools/gopls/internal/cmd/conftamer/parse"
)

/* Adds attributes useful for visualizing, given a .text file from gopls.
 * Useful to play around without rerunning gopls. */

func main() {
	var module_prefix, infile, outfile string
	flag.StringVar(&module_prefix, "module-prefix", "", "module as in go.mod")
	flag.StringVar(&infile, "infile", "", "Serialized graph from gopls (.text)")
	flag.StringVar(&outfile, "outfile", "", "File for output .gv")
	flag.Parse()

	in, m := ct.Deserialize(infile)
	ctypes := ct.CTypes{Graph: in, List: m.List}
	nodes, err := in.Vertices()
	ct.CheckErr(err)

	p := parse.Parser{Module_prefix: module_prefix}
	for _, node := range nodes {
		hash := string(ct.CTypeNodeHash(node))
		pkg := parse.Pkg(string(hash))
		attrs := map[string]string{
			"pkg":   pkg,
			"label": p.ShortLabel(hash, pkg),
			// else gephi only shows this in Data Lab, not Overview
			"type": hash,
		}
		in.UpdateVertex(ct.CTypeNodeHash(node), node, nil, graph.VertexAttributes(attrs))
	}

	ctypes.Serialize(outfile, module_prefix, true)
}
