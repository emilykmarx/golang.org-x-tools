package main

import (
	"reflect"
	"slices"
	"strings"
	"testing"

	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
	"golang.org/x/tools/gopls/internal/cmd/conftamer/dlv/graph"
)

// Tests for main.go

func Test_ASTPaths(t *testing.T) {
	graph_file := "testdata/graph.text"
	recvr_type := "T1"
	recvr_hash := ct.CTypeHash(recvr_type)

	expected_edges_T1 := []graph.ASTPath{
		graph.ASTPath{"StructType.Fields", "Field:F1", "FieldList.List", "Field.Type"},
		graph.ASTPath{"StructType.Fields", "Field:F2", "FieldList.List", "Field.Type", "ArrayType.Elt"},
	}

	expected_edges_T2 := []graph.ASTPath{
		graph.ASTPath{"StructType.Fields", "Field:F3", "FieldList.List", "Field.Type"},
		graph.ASTPath{"StructType.Fields", "Field:F4", "FieldList.List", "Field.Type", "ArrayType.Elt"},
	}
	expected_edges := []graph.ASTPath{}
	// 4 paths for the path T1 => T2 => T3: Each of T1 fields + each of T2 fields
	for _, edge1 := range expected_edges_T1 {
		for _, edge2 := range expected_edges_T2 {
			expected_edges = append(expected_edges, append(edge1, edge2...))
		}
	}
	expected_ast_paths := [][]graph.ASTPath{expected_edges}

	g, m := ct.Deserialize(graph_file)
	ctypes := ct.CTypes{Graph: g, List: m.List}
	_, ast_paths := graph.CTypePathsToLeaves(ctypes.Graph, recvr_hash)

	sort := func(a graph.ASTPath, b graph.ASTPath) int {
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
}
