package conftamer

import (
	"errors"
	"fmt"
	"go/types"
	"slices"
	"strings"

	"github.com/dominikbraun/graph"
)

/*
 * Data structures holding CTypes.
 */

type CTypeGraph graph.Graph[CTypeHash, CTypeNode]

// Makes lookup less confusing
type CTypeHash FullTypeName

// Indicates what the string is
type FullTypeName string

// CType name => hash of node it's part of.
// Needed because a type can have multiple names (via `type X Y`), but a node can only have one hash
// (and having multiple nodes corresponding to the same type is a hassle)
type CTypeList map[FullTypeName]CTypeHash

// Edges are annotated with []FieldInfo indicating how the parent can access the child via the parent's fields
// slice bc can hv multiple fields of same type
type FieldInfo struct {
	// Field name in source code
	Field string
	// Inferred key in config file, for the section or parameter corresponding to field
	Tag string
}

// NOTE to look up a type name in the CTypeGraph, first find its hash in the CTypeList
func Hash(type_name FullTypeName, list *CTypeList) (CTypeHash, bool) {
	hash, ok := (*list)[type_name]
	return hash, ok
}

type CTypeNode struct {
	// Package-qualified type name. Alphabetically ascending. Possibly multiple due to `type X Y`
	Names []FullTypeName

	// Info about the type
	TypeInfo types.Type

	// Parameters this CType can access, and via which fields
	Stored_down  map[Stored]struct{} // becomes irrelevant once entire push down pass is done
	Stored_up    map[Stored]struct{}
	Stored_final map[Stored]struct{}
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

func NewCTypeGraph() (CTypeGraph, CTypeList) {
	g := graph.New(CTypeNodeHash, graph.Directed())
	return g, make(CTypeList)
}

type TypeNameExistence int

const (
	TypeNameExists TypeNameExistence = iota
	TypeNameNotExists
)

// If obj is already part of some node, combine that node and neighbor node.
// Else, add obj to neighbor node.
// Update the list accordingly.
// Return whether obj already existed in some node.
func combineTypes(obj *types.TypeName, g CTypeGraph, list *CTypeList, neigh_name FullTypeName) (TypeNameExistence, error) {
	new_name := TypeName(obj)

	existing_hash, exists := Hash(new_name, list)
	existed := TypeNameNotExists
	if exists {
		existed = TypeNameExists
	}

	neigh_hash, ok := Hash(neigh_name, list)
	if !ok {
		return existed, fmt.Errorf("combineTypes called via neighbor %v that doesn't exist", neigh_name)
	}
	new_node, err := g.Vertex(neigh_hash)
	if err != nil {
		return existed, err
	}
	if existing_hash == neigh_hash {
		// Already in the same node as neighbor
		return existed, nil
	}

	// Get new names list: new name, names in neighbor node, plus names in the existing node if applicable
	new_node.Names = append(new_node.Names, new_name)

	if existed == TypeNameExists {
		// Move over names and edges from existing node
		existing_node, err := g.Vertex(existing_hash)
		if err != nil {
			return existed, err
		}
		new_node.Names = append(new_node.Names, existing_node.Names...)

		out_edges, in_edges, err := g.RemoveVertexWithEdges(existing_hash)
		if err != nil {
			return existed, err
		}
		err = g.AddEdges(neigh_hash, out_edges, in_edges)
		if err != nil {
			return existed, err
		}
	}

	// Keep names sorted
	slices.Sort(new_node.Names)

	// Update neighbor node with new names
	// (Stored* shouldn't be populated yet, and TypeInfo should stay the same - just need to update names)
	err = g.UpdateVertex(neigh_hash, new_node, func(vp *graph.VertexProperties) {})
	if err != nil {
		return existed, err
	}

	new_hash := CTypeNodeHash(new_node)

	// 3. Update list
	(*list)[TypeName(obj)] = new_hash
	(*list)[neigh_name] = new_hash // hash may have changed
	fmt.Printf("COMBINED %v + %v: %+v\n", TypeName(obj), neigh_name, new_node)

	return existed, nil
}

type NeighReason int

const (
	StructField NeighReason = iota
	NotStructField
)

// Given the object defined at the location, record its info.
// If not struct field, combine with the corresponding existing node if any.
// Return whether existed
func AddCType(obj *types.TypeName, g CTypeGraph, list *CTypeList, neigh_name *FullTypeName, neigh_reason NeighReason) (TypeNameExistence, error) {
	if neigh_reason == NotStructField {
		// combineTypes will combine with neighbor
		return combineTypes(obj, g, list, *neigh_name)
	}

	_, exists := (*list)[TypeName(obj)]
	if exists {
		return TypeNameExists, nil
	}

	// Make new node
	new_ctype := CTypeNode{TypeInfo: obj.Type(), Names: []FullTypeName{TypeName(obj)}}
	err := g.AddVertex(new_ctype, func(vp *graph.VertexProperties) {})
	fmt.Printf("NEW NODE for %v\n", TypeName(obj))

	if err != nil {
		// Shouldn't have existed - checked that above
		return TypeNameExists, err
	}

	// Add to list
	(*list)[TypeName(obj)] = CTypeNodeHash(new_ctype)
	return TypeNameNotExists, err
}

// If n is a struct, get info on all its fields (even if some aren't CTypes) -
// indexed by field type so we can add it to the corresponding edges (if leaf, will ignore types)
func getFieldInfo(n CTypeNode) map[FullTypeName][]FieldInfo {
	struct_info := IsStruct(n.TypeInfo)
	if struct_info == nil {
		// not a struct
		return nil
	}
	fields := make(map[FullTypeName][]FieldInfo)

	for i := range struct_info.NumFields() {
		field_type_str := struct_info.Field(i).Type().String() // already fully-qualified
		// Field could be a copy, pointer, slice, slice of pointers - any others?
		// Likely better to get this from AST (probably when finding enclosed types) - revisit when we need it (for proper param and field keys)
		// typeToObjects might be helpful?
		field_type := FullTypeName(strings.Trim(field_type_str, "*[]"))

		param_key := FieldToParamKey(struct_info.Field(i), struct_info.Tag(i))
		field_key := struct_info.Field(i).Name()
		field_info := FieldInfo{Field: field_key, Tag: param_key}
		fields[field_type] = append(fields[field_type], field_info)
	}

	return fields
}

// Add edge from enclosing CType (parent) to enclosed CType (child).
// Annotate edge with info on field(s) parent type uses to access child type name.
func AddCTypeEdge(g CTypeGraph, list *CTypeList, parent_hash CTypeHash, child_name FullTypeName) error {
	parent_node, err := g.Vertex(parent_hash)
	if err != nil {
		return err
	}
	all_fields := getFieldInfo(parent_node)
	child_field, ok := all_fields[child_name]
	if !ok {
		return fmt.Errorf("AddCTypeEdge - parent %v has no field info for child %v\n", parent_hash, child_name)
	}

	child_hash, ok := Hash(child_name, list)
	if !ok {
		return fmt.Errorf("AddCTypeEdge - child %v does not exist\n", child_name)
	}

	err = g.AddEdge(parent_hash, child_hash, graph.EdgeData(child_field))
	if err != nil {
		if !errors.Is(err, graph.ErrEdgeAlreadyExists) {
			return err
		} else {
			return nil
		}
	}
	fmt.Printf("ADDED EDGE %v => %v\n", parent_hash, child_hash)

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

// parent and child are copies - return the new CHILD
var pushDownFunc = func(g graph.Graph[CTypeHash, CTypeNode], parent CTypeNode, child CTypeNode) CTypeNode {
	child_fields := []FieldInfo{}
	from_leaf := CTypeNodeHash(parent) == CTypeNodeHash(child)

	if from_leaf {
		// Called from leaf => get field info
		fields := getFieldInfo(child)
		for _, info := range fields {
			// ignore types
			child_fields = append(child_fields, info...)
		}
		if len(fields) == 0 {
			// Nothing to append
			return child
		}
	} else {
		edge, err := g.Edge(CTypeNodeHash(parent), CTypeNodeHash(child))
		if err != nil {
			panic(err)
		}

		child_fields = edge.Properties.Data.([]FieldInfo)
	}

	if child.Stored_down == nil {
		child.Stored_down = make(map[Stored]struct{})
	}

	// PARENT: Append child field info and child type to all own stored, add to child's stored
	fmt.Printf("\nstored_down %v => %v:\n", parent.Names, child.Names)
	// If from leaf: replace the old values
	leaf_stored_down := make(map[Stored]struct{})

	for parent_stored := range parent.Stored_down {
		if !from_leaf {
			parent_stored.Path = fmt.Sprintf("%v,%v", parent_stored.Path, CTypeNodeHash(child))
		}
		for _, child_field := range child_fields {
			parent_stored.FieldInfo.Field = fmt.Sprintf("%v.%v", parent_stored.FieldInfo.Field, child_field.Field)
			parent_stored.FieldInfo.Tag = fmt.Sprintf("%v.%v", parent_stored.FieldInfo.Tag, child_field.Tag)
			parent_stored.FieldInfo.Tag, _ = strings.CutPrefix(parent_stored.FieldInfo.Tag, ".")
			// XXX handle arrays - codegen needs to loop over them
			if !from_leaf {
				child.Stored_down[parent_stored] = struct{}{}
				PrintStored(parent_stored)
			} else {
				leaf_stored_down[parent_stored] = struct{}{}
			}
		}
	}
	if from_leaf {
		child.Stored_down = make(map[Stored]struct{})
		for stored := range leaf_stored_down {
			PrintStored(stored)
			child.Stored_down[stored] = struct{}{}
		}
	}

	return child
}

func pushDown(g CTypeGraph) error {
	// parent and child are copies - return the new PARENT
	// Initialize roots (receive nothing pushed down)
	initializeRoots := func(g graph.Graph[CTypeHash, CTypeNode], parent CTypeNode, child CTypeNode) CTypeNode {
		root := parent.Stored_down == nil
		if root {
			// Just need one entry with empty field/tag to append child info to
			parent.Stored_down = make(map[Stored]struct{})
			stored := Stored{Path: string(CTypeNodeHash(parent))} // field info will be filled in later
			parent.Stored_down[stored] = struct{}{}
		}
		return parent
	}

	update_vertices := graph.UpdatePathVertices[CTypeHash, CTypeNode]{
		UpdateChild:  &pushDownFunc,
		UpdateParent: &initializeRoots,
		UpdateFirst:  graph.Parent,
	}

	return graph.DFSAllStartingNodes(g, func(CTypeHash) bool { return false }, update_vertices, true, false, graph.Forwards)
}

// Remove keys corresponding to nodes before n in path (if any).
// Don't do this in-place in Stored_up, since parent needs this info
func updateFieldKeys(n CTypeNode, stored Stored) Stored {
	// XXX if node is inline, no corresponding key. Any other tag options to handle?
	path_i := pathIdx(stored.Path, CTypeNodeHash(n))
	key_parts := strings.Split(stored.FieldInfo.Field, ".")
	key_parts = key_parts[1:] // part[0] is "" due to leading "."
	leaf := path_i == pathLen(stored.Path)-1
	if leaf && len(key_parts) < pathLen(stored.Path) {
		// Should only happen if leaf has no field
		stored.FieldInfo.Field = ""
	} else {
		key := strings.Join(key_parts[path_i:], ".")
		key = "." + key
		stored.FieldInfo.Field = key
	}

	return stored
}

func pushUp(g CTypeGraph) error {
	// parent and child are copies - return the new CHILD
	// Initialize leaves (receive nothing pushed up)
	initializeLeaves := func(g graph.Graph[CTypeHash, CTypeNode], parent CTypeNode, child CTypeNode) CTypeNode {
		leaf := child.Stored_up == nil
		if leaf {
			// Append own field keys, if any (for non-leaves, happens when adding edge)
			child = pushDownFunc(g, child, child)
			child.Stored_up = child.Stored_down // all the info has been pushed down to leaves
			child.Stored_final = make(map[Stored]struct{})
			for stored := range child.Stored_up {
				child.Stored_final[updateFieldKeys(child, stored)] = struct{}{}
			}
			fmt.Printf("\nleaf stored_up:\n")
			PrintStoredX(child.Stored_up)
			fmt.Printf("\nleaf stored_final:\n")
			PrintStoredX(child.Stored_final)
		}

		return child
	}

	// parent and child are copies - return the new PARENT
	pushUp := func(g graph.Graph[CTypeHash, CTypeNode], parent CTypeNode, child CTypeNode) CTypeNode {

		if parent.Stored_up == nil {
			parent.Stored_up = make(map[Stored]struct{})
			// Could clear out parent.Stored_down at this point too (all the relevant info is coming up from the leaves now)
		}
		if parent.Stored_final == nil {
			parent.Stored_final = make(map[Stored]struct{})
		}

		fmt.Printf("\npush up %v => %v:\n", parent.Names, child.Names)
		for child_stored := range child.Stored_up {
			// CHILD: Send all stored only if parent is parent in corresponding path
			// TODO (minor): See "ideally" comment in TestConftamerAlias - sometimes we'd want to push additional tags up
			if pathParent(child_stored.Path, CTypeNodeHash(child)) == CTypeNodeHash(parent) {
				// PARENT: Remove field keys corresponding to types before me in path, add to stored_final
				// (separate from stored_up since parent needs the removed keys)
				parent.Stored_up[child_stored] = struct{}{}
				fmt.Printf("stored_up: ")
				PrintStored(child_stored)
				parent.Stored_final[updateFieldKeys(parent, child_stored)] = struct{}{}
				fmt.Printf("stored_final: ")
				PrintStored(updateFieldKeys(parent, child_stored))
			}
		}

		return parent
	}

	update_vertices := graph.UpdatePathVertices[CTypeHash, CTypeNode]{
		UpdateChild:  &initializeLeaves,
		UpdateParent: &pushUp,
		UpdateFirst:  graph.Child,
	}

	return graph.DFSAllStartingNodes(g, func(CTypeHash) bool { return false }, update_vertices, true, false, graph.Backwards)
}

// For each CType:
// Get the full names of the parameters it can access (prefixing with <section.subsection...>),
// and via which expression(s) (e.g. A.B.C for CType A, B.C for CType B)
func GetCTypeParams(g CTypeGraph) error {
	// 1. PUSH DOWN: Accumulate full param keys and field keys at leaves
	err := pushDown(g)
	if err != nil {
		return err
	}

	// 2. PUSH UP: Push full param keys and field keys all the way up
	// Also clip irrelevant parts of field keys (roots need all, leaves need none)
	err = pushUp(g)
	if err != nil {
		return err
	}
	return nil
}
