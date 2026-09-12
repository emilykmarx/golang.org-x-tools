package graph

import (
	"fmt"
	"strings"

	graph "github.com/emilykmarx/dominikbraun-graph"
	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
)

func printEdgePath(p []graph.Edge[ct.CTypeNode]) {
	s := ""
	for _, edge := range p {
		s += fmt.Sprintf("%v => %v\n", edge.Source.Names, edge.Target.Names)
	}
	fmt.Println(strings.Trim(s, "\n"))
}

// If edge has no AST paths, return an array with len 1 (containing an empty array)
func EdgeASTPaths(edgeProperties graph.EdgeProperties) []ct.ASTPath {
	ast_paths := []ct.ASTPath{}
	// Edge data marshals annoyingly by default
	if edgeProperties.Data != nil {
		ast_paths_raw := edgeProperties.Data.([]interface{})
		for _, ast_path_raw := range ast_paths_raw { // range over [][]string
			ast_path := ct.ASTPath{}
			if ast_path_raw != nil {
				for _, ast_edge_raw := range ast_path_raw.([]interface{}) { // range over []string
					ast_edge := ast_edge_raw.(string)
					ast_path = append(ast_path, ast_edge)
				}
				ast_paths = append(ast_paths, ast_path)
			}
		}
	}
	if len(ast_paths) == 0 {
		ast_paths = append(ast_paths, ct.ASTPath{})
	}
	return ast_paths
}

// Given a CType path, find all possible AST paths on it indexed by edge
func CTypePathASTPaths(ctype_path []graph.Edge[ct.CTypeNode]) [][]ct.ASTPath {
	// If CType path has no AST paths, return an array with len 1 (containing an empty array)
	all_ast_paths := [][]ct.ASTPath{} // each element: a possible path (indexed by edge) - len = # ctype edges thus far
	for ctype_edge_i, edge := range ctype_path {
		edge_ast_paths := EdgeASTPaths(edge.Properties)
		new_ast_paths := [][]ct.ASTPath{} // each element is an array with one AST path per edge thus far

		if ctype_edge_i == 0 {
			for _, cur_edge_ast_path := range edge_ast_paths {
				new_ast_paths = append(new_ast_paths, []ct.ASTPath{cur_edge_ast_path})
			}
		} else {
			for _, cur_edge_ast_path := range edge_ast_paths {
				for _, prev_edge_ast_path := range all_ast_paths {
					// Append to all AST paths of previous edge
					new_ast_path := append(prev_edge_ast_path, cur_edge_ast_path)
					new_ast_paths = append(new_ast_paths, new_ast_path)
				}
			}
		}

		all_ast_paths = new_ast_paths
	}

	if len(all_ast_paths) == 0 {
		panic(fmt.Errorf("bug in CTypePathASTPaths: no AST paths"))
	}
	// Verify indices are 1:1 (each should be indexed by edge)
	for _, ast_path := range all_ast_paths {
		if len(ast_path) != len(ctype_path) {
			panic(fmt.Errorf("bug in CTypePathASTPaths: %v vs %+v", ast_path, ctype_path))
		}
	}

	return all_ast_paths
}

func hashPathToEdgePath(g ct.CTypeGraph, hash_path []ct.CTypeHash) []graph.Edge[ct.CTypeNode] {
	edge_path := []graph.Edge[ct.CTypeNode]{}
	if len(hash_path) == 1 {
		// hash is root/leaf
		node, err := g.Vertex(hash_path[0])
		ct.CheckErr(err)
		edge := graph.Edge[ct.CTypeNode]{Source: node, Target: node}
		edge_path = append(edge_path, edge)
		return edge_path
	}

	for i := range hash_path[:len(hash_path)-1] {
		edge, err := g.Edge(hash_path[i], hash_path[i+1])
		ct.CheckErr(err)
		edge_path = append(edge_path, edge)
	}

	return edge_path
}

// Find CTypes paths from a root to hash (`Backwards`), or from hash to a leaf,
// and AST paths corresponding to each, indexed by edge.
// (An edge can have multiple AST paths - get all combos of AST paths across all edges).
// Assumes g has been marshaled (which changes the type of the edge data).
// If hash is a root(Backwards)/leaf(Forwards), make a fake path with a self-edge
func CTypePathsToOrFrom(g ct.CTypeGraph, hash ct.CTypeHash, opts graph.DFSOpts[ct.CTypeHash, ct.CTypeNode]) ([][]graph.Edge[ct.CTypeNode], [][][]ct.ASTPath) {
	all_ctypes_paths := [][]graph.Edge[ct.CTypeNode]{}
	all_ast_paths := [][][]ct.ASTPath{}

	roots, leaves, err := graph.RootsLeaves(g)
	ct.CheckErr(err)
	others := roots
	if opts.Direction == graph.Forwards {
		others = leaves
	}

	/*
		if !opts.All_paths {
			panic("!all_paths not supported in CTypePathsToOrFrom")
		}
	*/

	// PERF if no paths are possible (to root, or from leaf), no need to call graph lib
	for _, other := range others {
		// PERF: Recomputes the adjacency map on every call to AllPathsBetween.
		var all_paths [][]ct.CTypeHash
		var err error
		if opts.Direction == graph.Forwards {
			all_paths, err = graph.AllPathsBetween(g, hash, other)
		} else {
			all_paths, err = graph.AllPathsBetween(g, other, hash)
		}

		// if unreachable, returns nil
		ct.CheckErr(err)

		for _, hash_path := range all_paths {
			edge_path := hashPathToEdgePath(g, hash_path)
			ast_paths := CTypePathASTPaths(edge_path)

			all_ctypes_paths = append(all_ctypes_paths, edge_path)
			all_ast_paths = append(all_ast_paths, ast_paths)
		}
	}

	return all_ctypes_paths, all_ast_paths
}
