package conftamer

import (
	"cmp"
	"errors"
	"fmt"
	"go/types"
	"log/slog"
	"reflect"
	"slices"
	"strings"
	"time"

	"github.com/dominikbraun/graph"
	"golang.org/x/tools/gopls/internal/golang"
	"golang.org/x/tools/gopls/internal/telemetry"
)

/*
 * Data structures holding CTypes.
 */

type CTypes struct {
	Graph   CTypeGraph
	List    CTypeList
	Latency map[string]telemetry.LatencyTotal // operation => timing
}

type CTypeGraph graph.Graph[CTypeHash, CTypeNode]

// Makes lookup less confusing
type CTypeHash FullTypeName

// Indicates what the string is
type FullTypeName string

// CType name => hash of node it's part of.
// Needed because a type can have multiple names (via `type X Y`), but a node can only have one hash
// (and having multiple nodes corresponding to the same type is a hassle)
type CTypeList map[FullTypeName]CTypeHash

type FieldInfo struct {
	// Field name in source code
	Field string
	// Inferred key in config file, for the section or parameter corresponding to field
	Tag string
}

var UNKNOWNFIELD = FieldInfo{Field: "<unknown>", Tag: "<unknown>"}

// NOTE to look up a type name in the CTypeGraph, first find its hash in the CTypeList
func (c *CTypes) GetHash(type_name FullTypeName) (CTypeHash, bool) {
	hash, ok := c.List[type_name]
	return hash, ok
}

func (c *CTypes) SetHash(type_name FullTypeName, hash CTypeHash) {
	c.List[type_name] = hash
}

type CTypeNode struct {
	// Package-qualified type name. Alphabetically ascending. Possibly multiple due to `type X Y`
	Names []FullTypeName

	// Info about the type
	TypeInfo types.Type

	// (these are also in TypeInfo, but copied here to make [un]marshaling easier)
	Methods []FullTypeName
	Tags    map[string]string // Field name => tag (populated if struct)

	// Parameters this CType can access, and via which fields
	Stored_down  map[Stored]struct{} // becomes irrelevant once entire push down pass is done
	Stored_up    map[Stored]struct{}
	Stored_final map[Stored]struct{}

	Indent int
}

func NodeSort(a, b CTypeNode) int {
	return cmp.Compare(string(CTypeNodeHash(a)), string(CTypeNodeHash(b)))
}
func NodeEqual(a, b CTypeNode) bool {
	return CTypeNodeHash(a) == CTypeNodeHash(b)
}

// Accumulating info about a param a node has access to
type Stored struct {
	Path      string // can't put slice in map => separate by ,
	FieldInfo FieldInfo
}

func CTypeNodeHash(n CTypeNode) CTypeHash {
	// Node is uniquely identified by first name
	return CTypeHash(n.Names[0])
}

func New(log *slog.Logger) *CTypes {
	g := graph.New(CTypeNodeHash, log, graph.Directed())
	return &CTypes{Graph: g, List: make(CTypeList), Latency: make(map[string]telemetry.LatencyTotal)}
}

type TypeNameExistence int

const (
	TypeNameExists TypeNameExistence = iota
	TypeNameNotExists
)

// If obj and neighbor aren't same underlying type, return.
// If obj is already part of some node, combine that node and neighbor node.
// Else, add obj to neighbor node.
// Update the list accordingly.
// Return whether obj already existed in some node, and whether combined (or were already combined).
func (c *CTypes) combineTypes(obj *types.TypeName, neigh_name FullTypeName) (TypeNameExistence, error, bool) {
	// 1. Check whether to combine
	combined := false
	new_name := TypeName(obj)

	existing_hash, exists := c.GetHash(new_name)
	existed := TypeNameNotExists
	if exists {
		existed = TypeNameExists
	}

	neigh_hash, ok := c.GetHash(neigh_name)
	if !ok {
		err := fmt.Errorf("combineTypes called via neighbor %v that doesn't exist", neigh_name)
		CheckErr(err)
	}
	new_node, err := c.Graph.Vertex(neigh_hash)
	CheckErr(err)

	if existing_hash == neigh_hash {
		// Already in the same node as neighbor
		return existed, nil, combined
	}

	if new_node.TypeInfo.Underlying() != obj.Type().Underlying() {
		// Shouldn't happen if they have no AST edges?
		// TODO happens occasionally in Prometheus even when types look the same at a glance - e.g.
		// promql.Vector and promql.vectorByReverseValueHeap
		graph.Logf(c.Graph.Log(), slog.LevelError, "combineTypes called on different types: %v and %v (types %+v and %+v)",
			new_name, new_node.Names, new_node.TypeInfo.Underlying(), obj.Type().Underlying())
		return existed, nil, combined
	}

	// 2. Combine

	combined = true
	graph.Logf(c.Graph.Log(), slog.LevelDebug, "COMBINING new %v (hash if any: %v) + neigh %v (hash: %v)", new_name, existing_hash, neigh_name, neigh_hash)
	graph.Logf(c.Graph.Log(), slog.LevelDebug, "%v node BEFORE combine: %+v", neigh_hash, new_node)

	if existed == TypeNameExists {
		// Move over names from existing node (including new name)
		existing_node, err := c.Graph.Vertex(existing_hash)
		CheckErr(err)

		graph.Logf(c.Graph.Log(), slog.LevelDebug, "%v node BEFORE combine: %+v", existing_hash, existing_node)

		new_node.Names = append(new_node.Names, existing_node.Names...)
	} else {
		new_node.Names = append(new_node.Names, new_name)
	}

	// Keep names sorted
	slices.Sort(new_node.Names)
	new_hash := CTypeNodeHash(new_node) // may still be neigh_hash, or not

	graph.Logf(c.Graph.Log(), slog.LevelDebug, "%v node AFTER combine: %+v", new_hash, new_node)

	// Update neighbor node with new names, and existing edges if any
	// (Stored* shouldn't be populated yet, and TypeInfo should stay the same - just need to update names)
	start := time.Now()
	if existed == TypeNameExists {
		// Combine with existing
		c.Graph.UpdateVertex(neigh_hash, new_node, &existing_hash, func(vp *graph.VertexProperties) {})
	} else {
		c.Graph.UpdateVertex(neigh_hash, new_node, nil, func(vp *graph.VertexProperties) {})
	}
	telemetry.RecordLatency(c.Latency, "UpdateVertex", time.Since(start))

	// 3. Update list for all possibly moved names
	// i.e. new obj, names in neighbor node (since its hash may have changed), and names in existing node (since they were moved)
	for _, new_name := range new_node.Names {
		c.SetHash(new_name, new_hash)
	}

	return existed, nil, combined
}

type NeighReason int

const (
	StructField NeighReason = iota
	NotStructField
)

type NeighAge int

const (
	NeighIsParent NeighAge = iota
	NeighIsChild
)

// Info about the neighbor CType from which we found a new CType
type NeighInfo struct {
	Name     FullTypeName
	Age      NeighAge
	Ast_path []string
}

// Given the object defined at the location, record its info.
// If not struct field, combine with the corresponding existing node if any.
// Return whether existed
func (c *CTypes) AddCType(typ golang.TypeInfo, neigh_info *NeighInfo) (TypeNameExistence, error) {
	if typ.TypeSource != golang.Implementer {
		combine := len(neigh_info.Ast_path) == 0 || slices.Compare(neigh_info.Ast_path, []string{"SelectorExpr.Sel"}) == 0
		if combine {
			// If no AST edges (i.e. only the TypeSpec_Type one) from enclosed/enclosing (or just one for pkg.T), `type X Y` => combine
			// (If not iface implementer, should always have a neigh_info)
			existed, err, combined := c.combineTypes(typ.TypeInfo, neigh_info.Name)
			CheckErr(err)
			if combined {
				// If combineTypes combined nodes, it already updated the list => check the value of existed it returned
				return existed, nil
			}
		}
	}

	_, exists := c.GetHash(TypeName(typ.TypeInfo))
	if exists {
		return TypeNameExists, nil
	}

	// Make new node
	new_ctype := CTypeNode{TypeInfo: typ.TypeInfo.Type(), Names: []FullTypeName{TypeName(typ.TypeInfo)}}
	// TODO if we combine nodes, do we need to add the methods of the new type?
	CopyMethods(&new_ctype)
	err := c.Graph.AddVertex(new_ctype, func(vp *graph.VertexProperties) {})
	// Shouldn't have existed - checked that above
	CheckErr(err)
	graph.Logf(c.Graph.Log(), slog.LevelDebug, "NEW NODE for %v", TypeName(typ.TypeInfo))

	// Add to list
	c.SetHash(TypeName(typ.TypeInfo), CTypeNodeHash(new_ctype))
	return TypeNameNotExists, nil
}

// Add edge from enclosing CType (parent) to enclosed CType (child).
// Annotate edge with info on how parent type can access child type name:
// e.g. via fields (possibly multiple), or slice indexing.
func (c *CTypes) AddCTypeEdge(parent_hash CTypeHash, child_name FullTypeName, neigh_ast_path []string) error {
	child_hash, ok := c.GetHash(child_name)
	if !ok {
		err := fmt.Errorf("AddCTypeEdge - child %v does not exist\n", child_name)
		CheckErr(err)
	}

	edge_data := [][]string{neigh_ast_path}
	err := c.Graph.AddEdge(parent_hash, child_hash, graph.EdgeData(edge_data))
	if err != nil {
		if !errors.Is(err, graph.ErrEdgeAlreadyExists) {
			CheckErr(err)
		} else {
			// existed => add path
			edge, err := c.Graph.Edge(parent_hash, child_hash)
			CheckErr(err)
			if edge.Properties.Data != nil {
				existing_edge_data := edge.Properties.Data.([][]string)
				dup := slices.ContainsFunc(existing_edge_data, func(existing_path []string) bool {
					return reflect.DeepEqual(existing_path, neigh_ast_path)
				})
				if !dup {
					edge_data = append(existing_edge_data, edge_data...)
					c.Graph.UpdateEdge(parent_hash, child_hash, graph.EdgeData(edge_data))
				}
			}
		}
	}
	graph.Logf(c.Graph.Log(), slog.LevelDebug, "ADDED EDGE %v => %v", parent_hash, child_hash)

	return nil
}

// Length of path
func pathLen(path string) int {
	path_parts := strings.Split(path, ",")
	return len(path_parts)
}

// Index of n in path
func pathIdx(path string, n CTypeHash) int {
	path_parts := strings.Split(path, ",")
	return slices.Index(path_parts, string(n))
}

// Find the node preceding child in path
func pathParent(path string, child CTypeHash) CTypeHash {
	child_i := pathIdx(path, child)
	path_parts := strings.Split(path, ",")
	return CTypeHash(path_parts[child_i-1])
}

// Panic on err (if running in dlv, will stop at a breakpoint)
func CheckErr(err error) {
	if err != nil {
		panic(err)
	}
}
