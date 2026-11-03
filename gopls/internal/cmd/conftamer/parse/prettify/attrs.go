package main

import (
	"encoding/csv"
	"flag"
	"log"
	"maps"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"slices"
	"strconv"
	"strings"

	graph "github.com/emilykmarx/dominikbraun-graph"
	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
	"golang.org/x/tools/gopls/internal/cmd/conftamer/parse"
)

/* Adds attributes useful for visualizing, given a .text file from gopls.
 * Useful to play around without rerunning gopls. */

func main() {
	var module_prefix, infile, outfile, node_attrs, keep_nodes_with, exclude_nodes string
	var split_roots bool
	var misc_max_nodes int
	flag.StringVar(&module_prefix, "module-prefix", "", "module as in go.mod")
	flag.StringVar(&infile, "infile", "", "Serialized graph from gopls (.text)")
	flag.StringVar(&outfile, "outfile", "", "File for output .gv")
	flag.BoolVar(&split_roots, "split-roots", false, "Also split the graph into connected components and per-root subgraphs (see README.md)")
	flag.IntVar(&misc_max_nodes, "misc-max-nodes", 10, "With -split-roots: components and per-root subgraphs with <= this many nodes go in misc graphs (see README.md)")
	flag.StringVar(&node_attrs, "node-attrs", "", "Optional csv of extra node attributes: a `node` column (named as in --infile), and one column per attribute (see README.md)")
	flag.StringVar(&keep_nodes_with, "keep-nodes-with", "", "Optional, with --node-attrs: keep only nodes whose value for this --node-attrs column is set and not 0 or false; other nodes are contracted away (see README.md)")
	flag.StringVar(&exclude_nodes, "exclude-nodes", "", "Optional regex: don't graph nodes whose name matches it, or nodes only reachable through them (see README.md)")
	flag.Parse()
	if keep_nodes_with != "" && node_attrs == "" {
		log.Fatalf("--keep-nodes-with needs --node-attrs")
	}

	in, m := ct.Deserialize(infile)
	ctypes := ct.CTypes{Graph: in, List: m.List}
	nodes, err := in.Vertices()
	ct.CheckErr(err)

	extra := map[string]map[string]string{}
	if node_attrs != "" {
		extra = readNodeAttrs(node_attrs)
	}

	p := parse.Parser{ParserConfig: parse.ParserConfig{ModulePrefix: module_prefix}}
	n_extra := 0
	for _, node := range nodes {
		hash := string(ct.CTypeNodeHash(node))
		pkg := parse.Pkg(string(hash))
		attrs := map[string]string{
			"pkg":   pkg,
			"label": p.ShortLabel(hash, pkg),
			// else gephi only shows this in Data Lab, not Overview
			"type": hash,
		}
		if node_extra, ok := extra[hash]; ok {
			n_extra++
			for k, v := range node_extra {
				attrs[k] = v
			}
		}
		in.UpdateVertex(ct.CTypeNodeHash(node), node, nil, graph.VertexAttributes(attrs))
	}

	if node_attrs != "" {
		log.Printf("--node-attrs: attributes for %v of %v nodes (%v rows)", n_extra, len(nodes), len(extra))
	}
	if exclude_nodes != "" {
		excludeNodes(in, regexp.MustCompile(exclude_nodes))
	}
	if keep_nodes_with != "" {
		keepNodesWith(in, extra, keep_nodes_with)
	}
	ctypes.Serialize(outfile, module_prefix, true)

	if split_roots {
		splitRoots(in, outfile, misc_max_nodes)
	}

	// Lay out the .gv (same path Serialize uses) - after splitRoots, which reads it
	runDot(strings.TrimSuffix(outfile, filepath.Ext(outfile)) + ".gv")
}

// Remove the nodes whose name matches re, and the nodes that were reachable from a root but are no longer once those are
// removed (e.g. a type used only by the matching nodes; a type also used elsewhere stays).
func excludeNodes(in ct.CTypeGraph, re *regexp.Regexp) {
	adj, err := in.AdjacencyMap()
	ct.CheckErr(err)
	has_in := map[ct.CTypeHash]bool{}
	for _, targets := range adj {
		for t := range targets {
			has_in[t] = true
		}
	}
	reachable := func(skip map[ct.CTypeHash]bool) map[ct.CTypeHash]bool {
		seen := map[ct.CTypeHash]bool{}
		var visit func(h ct.CTypeHash)
		visit = func(h ct.CTypeHash) {
			if seen[h] || skip[h] {
				return
			}
			seen[h] = true
			for t := range adj[h] {
				visit(t)
			}
		}
		for h := range adj {
			if !has_in[h] {
				visit(h) // a root of the original graph
			}
		}
		return seen
	}
	matched := map[ct.CTypeHash]bool{}
	for h := range adj {
		matched[h] = re.MatchString(string(h))
	}
	before, after := reachable(nil), reachable(matched)
	remove := map[ct.CTypeHash]bool{}
	for h := range adj {
		remove[h] = matched[h] || (before[h] && !after[h])
	}
	n_removed := 0
	for h := range adj {
		if !remove[h] {
			continue
		}
		for t := range adj[h] {
			ct.CheckErr(in.RemoveEdge(h, t))
		}
		for src := range adj {
			if _, ok := adj[src][h]; ok && !remove[src] {
				ct.CheckErr(in.RemoveEdge(src, h))
			}
		}
		n_removed++
	}
	for h := range adj {
		if remove[h] {
			ct.CheckErr(in.RemoveVertex(h))
		}
	}
	n_matched := 0
	for _, m := range matched {
		if m {
			n_matched++
		}
	}
	log.Printf("--exclude-nodes %v: removed %v nodes (%v matching, %v only reachable through them)", re, n_removed,
		n_matched, n_removed-n_matched)
}

// Keep only the nodes whose node attribute `attr` is set and not 0 or false. Contract the others away: replace each
// path from a kept node through removed nodes to a kept node by one edge, whose Field attribute joins the Fields of the
// path's edges with "." (a Field with alternatives is written {a,b}), and whose `contracted` attribute lists the removed
// nodes. If the kept nodes already have a direct edge, it's left as is.
func keepNodesWith(in ct.CTypeGraph, extra map[string]map[string]string, attr string) {
	adj, err := in.AdjacencyMap()
	ct.CheckErr(err)
	keep := map[ct.CTypeHash]bool{}
	for h := range adj {
		v := extra[string(h)][attr]
		keep[h] = v != "" && v != "0" && v != "false"
	}

	field := func(e graph.Edge[ct.CTypeHash]) string {
		f := e.Properties.Attributes[string(ct.Field)]
		if strings.Contains(f, ",") {
			return "{" + f + "}"
		}
		return f
	}
	type pair struct{ src, target ct.CTypeHash }
	contracted := map[pair]map[string]bool{} // => Field labels of the paths
	via := map[pair]map[string]bool{}        // => removed nodes on the paths
	var walk func(src, cur ct.CTypeHash, fields []string, removed []string, on_path map[ct.CTypeHash]bool)
	walk = func(src, cur ct.CTypeHash, fields []string, removed []string, on_path map[ct.CTypeHash]bool) {
		for target, e := range adj[cur] {
			if on_path[target] {
				continue // cycle through removed nodes
			}
			next_fields := fields
			if f := field(e); f != "" {
				next_fields = append(slices.Clone(fields), f)
			}
			if keep[target] {
				if cur == src {
					continue // a direct edge between kept nodes: kept as is
				}
				pr := pair{src, target}
				if _, direct := adj[src][target]; direct {
					continue
				}
				if contracted[pr] == nil {
					contracted[pr], via[pr] = map[string]bool{}, map[string]bool{}
				}
				contracted[pr][strings.Join(next_fields, ".")] = true
				for _, r := range removed {
					via[pr][r] = true
				}
				continue
			}
			on_path[target] = true
			walk(src, target, next_fields, append(slices.Clone(removed), string(target)), on_path)
			delete(on_path, target)
		}
	}
	for h := range adj {
		if keep[h] {
			walk(h, h, nil, nil, map[ct.CTypeHash]bool{h: true})
		}
	}

	// Remove the other nodes and their edges, then add the contracted edges
	n_removed := 0
	for h := range adj {
		if keep[h] {
			continue
		}
		for target := range adj[h] {
			ct.CheckErr(in.RemoveEdge(h, target))
		}
		for src := range adj {
			if _, ok := adj[src][h]; ok && keep[src] {
				ct.CheckErr(in.RemoveEdge(src, h))
			}
		}
		n_removed++
	}
	for h := range adj {
		if !keep[h] {
			ct.CheckErr(in.RemoveVertex(h))
		}
	}
	for pr, labels := range contracted {
		attrs := map[string]string{
			string(ct.Field): strings.Join(slices.Sorted(maps.Keys(labels)), ","),
			"contracted":     strings.Join(slices.Sorted(maps.Keys(via[pr])), ","),
		}
		ct.CheckErr(in.AddEdge(pr.src, pr.target, graph.EdgeWeight(1), graph.EdgeAttributes(attrs)))
	}
	log.Printf("--keep-nodes-with %v: kept %v nodes, removed %v, added %v contracted edges", attr, len(adj)-n_removed,
		n_removed, len(contracted))
}

// Read a csv of node attributes: node name (column `node`) => attribute => value
func readNodeAttrs(path string) map[string]map[string]string {
	fd, err := os.Open(path)
	ct.CheckErr(err)
	defer fd.Close()
	records, err := csv.NewReader(fd).ReadAll()
	ct.CheckErr(err)
	if len(records) == 0 || records[0][0] != "node" {
		log.Fatalf("--node-attrs %v: first column must be `node`", path)
	}
	attrs := map[string]map[string]string{}
	for _, record := range records[1:] {
		attrs[record[0]] = map[string]string{}
		for i, name := range records[0][1:] {
			attrs[record[0]][name] = record[i+1]
		}
	}
	return attrs
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
