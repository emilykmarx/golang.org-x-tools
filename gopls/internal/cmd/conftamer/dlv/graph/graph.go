package graph

import (
	"errors"
	"fmt"
	"reflect"

	"github.com/dominikbraun/graph"
	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
)

type ASTPath []string
type CTypesPath []graph.Edge[ct.CTypeNode]

func (p CTypesPath) String() string {
	s := ""
	for _, edge := range p {
		s += fmt.Sprintf("%v => %v\n", edge.Source.Names, edge.Target.Names)
	}
	return s
}

func EdgeASTPaths(edgeProperties graph.EdgeProperties) []ASTPath {
	ast_paths := []ASTPath{}
	// Edge data marshals annoyingly by default
	if edgeProperties.Data != nil {
		ast_paths_raw := edgeProperties.Data.([]interface{})
		for _, ast_path_raw := range ast_paths_raw { // range over [][]string
			ast_path := ASTPath{}
			if ast_path_raw != nil {
				for _, ast_edge_raw := range ast_path_raw.([]interface{}) { // range over []string
					ast_edge := ast_edge_raw.(string)
					ast_path = append(ast_path, ast_edge)
				}
				ast_paths = append(ast_paths, ast_path)
			}
		}
	}
	return ast_paths
}

// Find corresponding CType edge based on index in concatenated AST path (`want`)
func AstIdxToEdge(ctypes_path CTypesPath, ast_path ASTPath, want int) graph.Edge[ct.CTypeNode] {
	cur := 0 // idx in concatenated AST path
	for _, edge := range ctypes_path {
		edge_ast_paths := EdgeASTPaths(edge.Properties)
		if len(edge_ast_paths) == 0 {
			// nothing to do (and don't want to panic below)
			continue
		}

		found := false
		// Find which of the edge's ast paths this ast_path took
		for _, edge_ast_path := range edge_ast_paths {
			edge_end := cur + len(edge_ast_path) // idx in concatenated AST path
			part := ast_path[cur:edge_end]       // part of concatenated ast path corresponding to this edge

			if reflect.DeepEqual(part, edge_ast_path) {
				if want < edge_end { // edge_end is exclusive
					// idx we're looking for is in this edge
					return edge
				}

				// Eat the AST edges we took on this AST edge, check the next CType edge
				cur += len(edge_ast_path)
				found = true
				break
			}
		}

		if !found {
			panic(fmt.Errorf("Failed to find corresponding ast_path for idx %v on %+v", want, ctypes_path))
		}
	}

	panic(fmt.Errorf("Failed to find corresponding ast_path for %v on %+v", ast_path, ctypes_path))
}

// Find CTypes paths from a root to hash (`Backwards`), or from hash to a leaf,
// and AST paths corresponding to each.
// (An edge can have multiple AST paths - get all combos of AST paths across all edges).
// Assumes g has been marshaled (which changes the type of the edge data).
// If hash is a root(Backwards)/leaf(Forwards), make a fake path with a self-edge
func CTypePathsToOrFrom(g ct.CTypeGraph, hash ct.CTypeHash, opts graph.DFSOpts[ct.CTypeHash, ct.CTypeNode]) ([]CTypesPath, [][]ASTPath) {
	all_ctypes_paths := []CTypesPath{}
	all_ast_paths := [][]ASTPath{}

	roots, leaves, err := graph.RootsLeaves(g)
	ct.CheckErr(err)
	others := roots
	if opts.Direction == graph.Forwards {
		others = leaves
	}

	for _, other := range others {
		// PERF: Recomputes the adjacency map on every call to AllPathsBetween.
		var paths [][]ct.CTypeHash
		var shortest_path []ct.CTypeHash
		var err error
		if opts.Direction == graph.Forwards {
			if opts.All_paths {
				paths, err = graph.AllPathsBetween(g, hash, other)
			} else {
				shortest_path, err = graph.ShortestPath(g, hash, other)
			}
		} else {
			if opts.All_paths {
				paths, err = graph.AllPathsBetween(g, other, hash)
			} else {
				shortest_path, err = graph.ShortestPath(g, other, hash)
			}
		}

		if opts.All_paths {
			// if unreachable, returns nil
			ct.CheckErr(err)
		} else {
			// if unreachable, returns err (but should ignore)
			if !errors.Is(err, graph.ErrTargetNotReachable) {
				ct.CheckErr(err)
			}
			paths[0] = shortest_path
		}

		for _, path := range paths {
			ctype_path := CTypesPath{}
			prev_edge_ast_paths := []ASTPath{} // as of previous edge
			if len(path) == 1 {
				// hash is root/leaf
				node, err := g.Vertex(hash)
				ct.CheckErr(err)
				edge := graph.Edge[ct.CTypeNode]{Source: node, Target: node}
				ctype_path = append(ctype_path, edge)
			}

			for i := range path[:len(path)-1] {
				edge, err := g.Edge(path[i], path[i+1])
				ct.CheckErr(err)
				ctype_path = append(ctype_path, edge)
				edge_ast_paths := EdgeASTPaths(edge.Properties)
				if len(edge_ast_paths) == 0 {
					// nothing to do (and don't want to reset new_ast_paths)
					continue
				}
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
