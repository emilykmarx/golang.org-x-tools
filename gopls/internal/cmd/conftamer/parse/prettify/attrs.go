package main

import (
	"flag"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"

	graph "github.com/emilykmarx/dominikbraun-graph"
	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
	"golang.org/x/tools/gopls/internal/cmd/conftamer/parse"
)

/* Adds attributes useful for visualizing, given a .text file from gopls.
 * Useful to play around without rerunning gopls. */

func main() {
	var module_prefix, infile, outfile string
	var split_roots bool
	var misc_max_nodes int
	flag.StringVar(&module_prefix, "module-prefix", "", "module as in go.mod")
	flag.StringVar(&infile, "infile", "", "Serialized graph from gopls (.text)")
	flag.StringVar(&outfile, "outfile", "", "File for output .gv")
	flag.BoolVar(&split_roots, "split-roots", false, "Also split the graph into connected components and per-root subgraphs (see README.md)")
	flag.IntVar(&misc_max_nodes, "misc-max-nodes", 10, "With -split-roots: components and per-root subgraphs with <= this many nodes go in misc graphs (see README.md)")
	flag.Parse()

	in, m := ct.Deserialize(infile)
	ctypes := ct.CTypes{Graph: in, List: m.List}
	nodes, err := in.Vertices()
	ct.CheckErr(err)

	p := parse.Parser{ParserConfig: parse.ParserConfig{ModulePrefix: module_prefix}}
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

	if split_roots {
		splitRoots(in, outfile, misc_max_nodes)
	}

	// Lay out the .gv (same path Serialize uses) - after splitRoots, which reads it
	runDot(strings.TrimSuffix(outfile, filepath.Ext(outfile)) + ".gv")
}

// Split the .gv (same path Serialize uses) with split_roots.py, then lay out the per-root subgraphs.
// See README.md for the output files.
func splitRoots(in ct.CTypeGraph, outfile string, misc_max_nodes int) {
	stem := strings.TrimSuffix(outfile, filepath.Ext(outfile))
	roots, _, err := graph.RootsLeaves(in)
	ct.CheckErr(err)
	var root_lines []string
	for _, r := range roots {
		root_lines = append(root_lines, string(r))
	}
	_, this_file, _, _ := runtime.Caller(0)
	script := filepath.Join(filepath.Dir(this_file), "split_roots.py")
	cmd := exec.Command("python3", script, stem+".gv", stem+"_roots", stem, strconv.Itoa(misc_max_nodes))
	cmd.Stdin = strings.NewReader(strings.Join(root_lines, "\n"))
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	ct.CheckErr(cmd.Run())

	per_root, err := filepath.Glob(filepath.Join(stem+"_roots", "comp*", "*.gv"))
	ct.CheckErr(err)
	for _, gv := range per_root {
		// Per-component misc graphs get no layout, like <stem>_misc_<n>nodes.gv
		if !strings.HasPrefix(filepath.Base(gv), "misc_") {
			runDot(gv)
		}
	}
}

// Writes <gv>.dot, then deletes gv
func runDot(gv string) {
	cmd := exec.Command("dot", "-Tdot", "-O", gv)
	cmd.Stderr = os.Stderr
	ct.CheckErr(cmd.Run())
	ct.CheckErr(os.Remove(gv))
}
