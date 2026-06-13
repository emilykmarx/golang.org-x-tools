package graph

import (
	"github.com/dominikbraun/graph"
	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
)

type ASTPath []string
type CTypesPath []graph.Edge[ct.CTypeNode]

func edgeASTPaths(edge graph.Edge[ct.CTypeNode]) []ASTPath {
	ast_paths := []ASTPath{}
	// Edge data marshals annoyingly by default
	if edge.Properties.Data != nil {
		ast_paths_raw := edge.Properties.Data.([]interface{})
		for _, ast_path_raw := range ast_paths_raw { // range over [][]string
			ast_path := ASTPath{}
			for _, ast_edge_raw := range ast_path_raw.([]interface{}) { // range over []string
				ast_edge := ast_edge_raw.(string)
				ast_path = append(ast_path, ast_edge)
			}
			ast_paths = append(ast_paths, ast_path)
		}
	}
	return ast_paths
}

// Find all CTypes paths from start_hash to a leaf,
// and all AST paths corresponding to each.
// (An edge can have multiple AST paths - get all combos of AST paths across all edges)
// Assumes g has been marshaled (which changes the type of the edge data).
// For each path, also return corresponding AST path(s).
func CTypePathsToLeaves(g ct.CTypeGraph, start_hash ct.CTypeHash) ([]CTypesPath, [][]ASTPath) {
	all_ctypes_paths := []CTypesPath{}
	all_ast_paths := [][]ASTPath{}

	_, leaves, err := graph.RootsLeaves(g)
	ct.CheckErr(err)
	for _, leaf := range leaves {
		// PERF: Recomputes the adjacency map on every call to AllPathsBetween.
		paths, err := graph.AllPathsBetween(g, start_hash, leaf)
		ct.CheckErr(err)
		for _, path := range paths {
			ctype_path := CTypesPath{}
			prev_edge_ast_paths := []ASTPath{} // as of previous edge

			for i := range path[:len(path)-1] {
				edge, err := g.Edge(path[i], path[i+1])
				ct.CheckErr(err)
				ctype_path = append(ctype_path, edge)
				edge_ast_paths := edgeASTPaths(edge)
				new_ast_paths := []ASTPath{}

				if i > 0 {
					for _, cur_edge_ast_path := range edge_ast_paths {
						for _, prev_edge_ast_path := range prev_edge_ast_paths {
							// Append to all AST paths of previous edge
							new_ast_path := append(prev_edge_ast_path, cur_edge_ast_path...)
							new_ast_paths = append(new_ast_paths, new_ast_path)
						}
					}
				} else {
					new_ast_paths = edge_ast_paths
				}
				prev_edge_ast_paths = new_ast_paths
			}

			// Done with edges on path
			all_ctypes_paths = append(all_ctypes_paths, ctype_path)
			all_ast_paths = append(all_ast_paths, prev_edge_ast_paths)
		}
	}

	return all_ctypes_paths, all_ast_paths
}
