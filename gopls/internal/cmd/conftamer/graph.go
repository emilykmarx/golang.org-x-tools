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
	TypeInfo    types.TypeName
	Stored_down map[Stored]struct{} // becomes irrelevant once entire push down pass is done
	Stored_up   map[Stored]struct{}
	// Field type => how this CType can access that type via its own fields
	// (For fields that are also CTypes, there is one map entry for each edge to that type)
	Children map[FullTypeName][]FieldInfo // Can be edge attr (unless leaf) - slice bc can hv multiple fields of same type
}

// Accumulating info about a param a node has access to
type Stored struct {
	Path      string // can't put slice in map => separate by ,
	FieldInfo FieldInfo
}

func CTypeNodeHash(n CTypeNode) FullTypeName {
	// Node is uniquely identified by package-qualified type name
	return typeName(&n.TypeInfo)
}

func NewCTypeGraph() CTypeGraph {
	g := graph.New(CTypeNodeHash, graph.Directed())
	return g
}

// Given the object defined at the location, record its info.
func AddCType(defn_locs []string, obj *types.Object, g CTypeGraph) error {
	type_info, ok := (*obj).(*types.TypeName)
	if !ok {
		return fmt.Errorf("obj %+v is not a type", *obj)
	}

	ctype := CTypeNode{TypeInfo: *type_info, Children: make(map[FullTypeName][]FieldInfo)}
	err := g.AddVertex(ctype, func(vp *graph.VertexProperties) {})
	// Tolerate dups for now: If type T embeds implementer U, unmarshalImpls() returns both,
	// but implementingTypeDefinition() finds U only (see comment in ImplementationMoreInfo())
	// Currently we're not using anything besides Loc in unmarshalImpl,
	// so maybe should revert to the original implementation verb that only returns and dedups by loc
	if err != nil && !errors.Is(err, graph.ErrVertexAlreadyExists) {
		return err
	}
	return nil
}

/*
	For struct CTypes:

- Get info on fields (name and corresponding yaml key)
- For any fields that are CTypes, add an edge
*/
func FindCTypeEdges(g CTypeGraph) error {
	m, err := g.AdjacencyMap()
	if err != nil {
		return err
	}

	for parent_ctype_name := range m {
		parent_ctype, err := g.Vertex(parent_ctype_name)
		if err != nil {
			return err
		}
		if struct_info := IsStruct(&parent_ctype.TypeInfo); struct_info != nil {
			for i := range struct_info.NumFields() {
				field_type_str := struct_info.Field(i).Type().String() // already fully-qualified
				// Field could be a copy, pointer, slice, slice of pointers - any others?
				field_type := FullTypeName(strings.Trim(field_type_str, "*[]"))

				// 1. Get field's param key and field key
				param_key := FieldToParamKey(struct_info.Field(i), struct_info.Tag(i))
				field_key := struct_info.Field(i).Name()
				field_info := FieldInfo{Field: field_key, Tag: param_key}
				parent_ctype.Children[field_type] = append(parent_ctype.Children[field_type], field_info)

				// 2. Add edge if field type is also CType
				if field, err := g.Vertex(field_type); err == nil {
					err := g.AddEdge(parent_ctype_name, CTypeNodeHash(field))
					if err != nil && !errors.Is(err, graph.ErrEdgeAlreadyExists) {
						return err
					}
				}
			}

			err := g.UpdateVertex(parent_ctype_name, parent_ctype, func(vp *graph.VertexProperties) {})
			if err != nil {
				return err
			}
		}
	}
	// LEFT OFF if type T contains iface, add edge from T to all the iface implementers (do this after write code that uses the graph)
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

// For each CType:
// Get the full names of the parameters it can access (prefixing with <section.subsection...>),
// and via which expression(s) (e.g. A.B.C for CType A, B.C for CType B)
func GetCTypeParams(g CTypeGraph) error {
	// 1. PUSH DOWN: Accumulate full param keys and field keys at leaves

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
	}

	err := graph.DFSAllStartingNodes(g, func(ctype_name FullTypeName) bool { return false }, update_vertices, true, false, false) // forwards
	if err != nil {
		// TODO unsure what to do about cycles
		if errors.Is(err, graph.ErrCycleFound) {
		}
		return err
	}

	// LEFT OFF do 2 and 3
	// 2. PUSH UP: Push full param keys and field keys all the way up, truncating field keys (roots need all, leaves need none)
	// 3. Clip irrelevant parts of field keys, output result
	return nil
}
