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

type CTypeGraph graph.Graph[FullTypeName, CTypeNode]
type FullTypeName string

type FieldInfo struct {
	// Field name in source code
	Field string
	// Inferred key in config file, for the section or parameter corresponding to field
	Tag string
}

type CTypeNode struct {
	// The rest of the info about the type
	TypeInfo types.TypeName

	// Location(s) of the type name somewhere in the code (not necessarily in the declaration) -
	// since that is what the gopls CLI functions we use require.
	// Format is same as expected by the gopls CLI (human-readable, can be anywhere within the identifier).
	// If there are multiple, may only need one?
	Loc []string

	// Parameters this CType can access, and via which fields
	Stored_down  map[Stored]struct{} // becomes irrelevant once entire push down pass is done
	Stored_up    map[Stored]struct{}
	Stored_final map[Stored]struct{}

	// Field type => how this CType can access that type via its own fields
	// (For fields that are also CTypes, there is one map entry for each edge to that type)
	Children map[FullTypeName][]FieldInfo // Can be edge attr (unless leaf) - slice bc can hv multiple fields of same type

	Visited bool
}

// Accumulating info about a param a node has access to
type Stored struct {
	Path      string // can't put slice in map => separate by ,
	FieldInfo FieldInfo
}

func CTypeNodeHash(n CTypeNode) FullTypeName {
	// Node is uniquely identified by package-qualified type name
	return TypeName(&n.TypeInfo)
}

func NewCTypeGraph() CTypeGraph {
	g := graph.New(CTypeNodeHash, graph.Directed())
	return g
}

// Given the object defined at the location, record its info.
func AddCType(obj *types.TypeName, defn_locs []string, g CTypeGraph) (*CTypeNode, error) {
	new_ctype := CTypeNode{TypeInfo: *obj, Loc: defn_locs, Children: make(map[FullTypeName][]FieldInfo)}
	err := g.AddVertex(new_ctype, func(vp *graph.VertexProperties) {})

	if err != nil {
		if errors.Is(err, graph.ErrVertexAlreadyExists) {
			// ok, but return the existing one (so visited is correct)
			existing, err := g.Vertex(CTypeNodeHash(new_ctype))
			if err != nil {
				// shouldn't happen
				return nil, err
			}
			return &existing, nil
		} else {
			return nil, err
		}
	}

	return &new_ctype, err
}

// Add edge from struct field CType (child) to enclosing struct (parent).
// For parent, get info on child's field (name and corresponding yaml key).
func AddCTypeEdge(g CTypeGraph, parent CTypeNode, child CTypeNode) error {
	struct_info := IsStruct(&parent.TypeInfo)
	if struct_info == nil {
		return fmt.Errorf("AddCTypeEdge currently only supported for adding edge from struct to field")
	}
	parent_type := CTypeNodeHash(parent)

	// For any fields of child's type , get key info
	for i := range struct_info.NumFields() {
		field_type_str := struct_info.Field(i).Type().String() // already fully-qualified
		// Field could be a copy, pointer, slice, slice of pointers - any others?
		// Likely better to get this from AST - revisit we need it (for proper param and field keys)
		field_type := FullTypeName(strings.Trim(field_type_str, "*[]"))

		if field_type == CTypeNodeHash(child) {
			// 1. Add edge - if already existed, done (don't append to parent.Children again)
			err := g.AddEdge(parent_type, field_type)
			if err != nil {
				if errors.Is(err, graph.ErrEdgeAlreadyExists) {
					continue
				} else {
					return err
				}
			}
			// 2. Get field's param key and field key
			param_key := FieldToParamKey(struct_info.Field(i), struct_info.Tag(i))
			field_key := struct_info.Field(i).Name()
			field_info := FieldInfo{Field: field_key, Tag: param_key}
			parent.Children[field_type] = append(parent.Children[field_type], field_info)
		}

		err := g.UpdateVertex(parent_type, parent, func(vp *graph.VertexProperties) {})
		if err != nil {
			return err
		}
	}
	return nil
}

// Length of path
func pathLen(path string) int {
	path_parts := strings.Split(path, ",")
	return len(path_parts)
}

// Index of n in path
func pathIdx(path string, n FullTypeName) int {
	path_parts := strings.Split(path, ",")
	return slices.Index(path_parts, string(n))
}

// Find the node preceding child in path
func pathParent(path string, child FullTypeName) FullTypeName {
	child_i := pathIdx(path, child)
	path_parts := strings.Split(path, ",")
	return FullTypeName(path_parts[child_i-1])
}

func pushDown(g CTypeGraph) error {
	// parent and child are copies - return the new PARENT
	// Initialize roots (receive nothing pushed down)
	initializeRoots := func(parent CTypeNode, child CTypeNode) CTypeNode {
		root := parent.Stored_down == nil
		if root {
			// Just need one entry with empty field/tag to append child info to
			parent.Stored_down = make(map[Stored]struct{})
			stored := Stored{Path: string(CTypeNodeHash(parent))} // field info will be filled in later
			parent.Stored_down[stored] = struct{}{}
		}
		return parent
	}

	// parent and child are copies - return the new CHILD
	pushDown := func(parent CTypeNode, child CTypeNode) CTypeNode {
		child_fields := parent.Children[CTypeNodeHash(child)]

		if child.Stored_down == nil {
			child.Stored_down = make(map[Stored]struct{})
		}

		// PARENT: Append child field info and child type to all own stored, add to child's stored
		for parent_stored := range parent.Stored_down {
			parent_stored.Path = fmt.Sprintf("%v,%v", parent_stored.Path, CTypeNodeHash(child))
			for _, child_field := range child_fields {
				parent_stored.FieldInfo.Field = fmt.Sprintf("%v.%v", parent_stored.FieldInfo.Field, child_field.Field)
				parent_stored.FieldInfo.Tag = fmt.Sprintf("%v.%v", parent_stored.FieldInfo.Tag, child_field.Tag)
				parent_stored.FieldInfo.Tag, _ = strings.CutPrefix(parent_stored.FieldInfo.Tag, ".")
				// XXX handle arrays - codegen needs to loop over them
				child.Stored_down[parent_stored] = struct{}{}
			}
		}

		return child
	}

	update_vertices := graph.UpdatePathVertices[CTypeNode]{
		UpdateChild:  &pushDown,
		UpdateParent: &initializeRoots,
		UpdateFirst:  graph.Parent,
	}

	err := graph.DFSAllStartingNodes(g, func(ctype_name FullTypeName) bool { return false }, update_vertices, true, false, graph.Forwards)
	if err != nil {
		// TODO unsure what to do about cycles
		if errors.Is(err, graph.ErrCycleFound) {
		}
		return err
	}

	return nil
}

// Remove keys corresponding to nodes before n in path (if any).
// Don't do this in-place in Stored_up, since parent needs this info
func updateFieldKeys(n CTypeNode, stored Stored) Stored {
	// XXX if node is inline, no corresponding key. Any other tag options to handle?
	path_i := pathIdx(stored.Path, CTypeNodeHash(n))
	if path_i == pathLen(stored.Path)-1 {
		stored.FieldInfo.Field = "" // leaf
	} else {
		key_parts := strings.Split(stored.FieldInfo.Field, ".")
		key_parts = key_parts[1:] // part[0] is "" due to leading "."
		key := strings.Join(key_parts[path_i:], ".")
		key = "." + key
		stored.FieldInfo.Field = key
	}

	return stored
}

func pushUp(g CTypeGraph) error {
	// parent and child are copies - return the new CHILD
	// Initialize leaves (receive nothing pushed up)
	initializeLeaves := func(parent CTypeNode, child CTypeNode) CTypeNode {
		leaf := child.Stored_up == nil
		if leaf {
			child.Stored_up = child.Stored_down // all the info has been pushed down to leaves
			child.Stored_final = make(map[Stored]struct{})
			for stored := range child.Stored_up {
				child.Stored_final[updateFieldKeys(child, stored)] = struct{}{}
			}
		}

		return child
	}

	// parent and child are copies - return the new PARENT
	pushUp := func(parent CTypeNode, child CTypeNode) CTypeNode {

		if parent.Stored_up == nil {
			parent.Stored_up = make(map[Stored]struct{})
			// Could clear out parent.Stored_down at this point too (all the relevant info is coming up from the leaves now)
		}
		if parent.Stored_final == nil {
			parent.Stored_final = make(map[Stored]struct{})
		}

		for child_stored := range child.Stored_up {
			// CHILD: Send all stored only if parent is parent in corresponding path
			if pathParent(child_stored.Path, CTypeNodeHash(child)) == CTypeNodeHash(parent) {
				// PARENT: Remove field keys corresponding to types before me in path, add to stored_final
				// (separate from stored_up since parent needs the removed keys)
				parent.Stored_up[child_stored] = struct{}{}
				parent.Stored_final[updateFieldKeys(parent, child_stored)] = struct{}{}
			}
		}

		return parent
	}

	update_vertices := graph.UpdatePathVertices[CTypeNode]{
		UpdateChild:  &initializeLeaves,
		UpdateParent: &pushUp,
		UpdateFirst:  graph.Child,
	}

	err := graph.DFSAllStartingNodes(g, func(ctype_name FullTypeName) bool { return false }, update_vertices, true, false, graph.Backwards)
	if err != nil {
		return err
	}

	return nil
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
