package main

import (
	"reflect"
	"slices"
	"strings"
	"testing"

	"github.com/dominikbraun/graph"
	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
	dlvgraph "golang.org/x/tools/gopls/internal/cmd/conftamer/dlv/graph"
)

// Tests for main.go

func Test_ASTPaths(t *testing.T) {
	graph_file := "testdata/graph.text"
	recvr_type := "T1"
	recvr_hash := ct.CTypeHash(recvr_type)

	expected_edges_T1 := []dlvgraph.ASTPath{
		dlvgraph.ASTPath{"StructType.Fields", "Field:F1", "FieldList.List", "Field.Type"},
		dlvgraph.ASTPath{"StructType.Fields", "Field:F2", "FieldList.List", "Field.Type", "ArrayType.Elt"},
	}

	expected_edges_T2 := []dlvgraph.ASTPath{
		dlvgraph.ASTPath{"StructType.Fields", "Field:F3", "FieldList.List", "Field.Type"},
		dlvgraph.ASTPath{"StructType.Fields", "Field:F4", "FieldList.List", "Field.Type", "ArrayType.Elt"},
	}
	expected_edges := []dlvgraph.ASTPath{}
	// 4 paths for the path T1 => T2 => T3: Each of T1 fields + each of T2 fields
	for _, edge1 := range expected_edges_T1 {
		for _, edge2 := range expected_edges_T2 {
			expected_edges = append(expected_edges, append(edge1, edge2...))
		}
	}
	expected_ast_paths := [][]dlvgraph.ASTPath{expected_edges}

	g, m := ct.Deserialize(graph_file)
	ctypes := ct.CTypes{Graph: g, List: m.List}
	ctype_paths, ast_paths := dlvgraph.CTypePathsToOrFrom(ctypes.Graph, recvr_hash, graph.Forwards)

	// TEST CTypePathsToOrFrom()
	sort := func(a dlvgraph.ASTPath, b dlvgraph.ASTPath) int {
		// Sort by T1 field, then T2 field
		cmp := strings.Compare(a[1], b[1])
		if cmp != 0 {
			return cmp
		}
		cmp = strings.Compare(a[len(a)-1], b[len(b)-1])
		return cmp
	}
	slices.SortFunc(expected_ast_paths[0], sort)
	slices.SortFunc(ast_paths[0], sort)

	if !reflect.DeepEqual(ast_paths, expected_ast_paths) {
		t.Fatalf("AST paths:\nExpected \n%v\nActual \n%v", expected_ast_paths, ast_paths)
	}

	// TEST AstIdxToEdge()
	for i, ctype_path := range ctype_paths {
		for which_ast, ast_path := range ast_paths[i] {
			for idx := range ast_path {
				expected_edge_src := "T1"
				// Paths starting with F1 get sorted first
				if which_ast < 2 {
					// Path starts with F1
					if idx >= len(expected_edges_T1[0]) {
						expected_edge_src = "T2"
					}
				} else {
					// Path starts with F2
					if idx >= len(expected_edges_T1[1]) {
						expected_edge_src = "T2"
					}

				}
				edge := dlvgraph.AstIdxToEdge(ctype_path, ast_path, idx)
				if edge.Source.Names[0] != ct.FullTypeName(expected_edge_src) {
					t.Fatalf("AST path %v - expected edge %v for idx %v, got %v", ast_path, expected_edge_src, idx, edge.Source.Names[0])
				}
			}
		}
	}
}
