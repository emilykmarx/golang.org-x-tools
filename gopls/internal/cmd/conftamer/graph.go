package conftamer

import (
	"errors"
	"fmt"
	"go/types"
	"strings"

	"github.com/dominikbraun/graph"
)

/*
 * Data structures holding CTypes.
 */

type CTypeGraph graph.Graph[string, CTypeNode]

type CTypeNode struct {
	// For each field containing a single param's value: field name => param key
	// LEFT OFF what type should this be?

	// The rest of the info about the type
	TypeInfo types.TypeName
}

func CTypeNodeHash(n CTypeNode) string {
	// Node is uniquely identified by package-qualified type name
	return typeName(&n.TypeInfo)
}

func NewCTypeGraph() CTypeGraph {
	g := graph.New(CTypeNodeHash, graph.Directed())
	return g
}

// Given the object defined at the location, record its info including which fields are affected by which params.
func AddCType(defn_locs []string, obj *types.Object, g CTypeGraph) error {
	type_info, ok := (*obj).(*types.TypeName)
	if !ok {
		return fmt.Errorf("obj %+v is not a type", *obj)
	}
	if struct_info := IsStruct(type_info); struct_info != nil {
		// LEFT OFF store each field's param key - ct.FieldToParamKey(struct_info.Field(i), struct_info.Tag(i))
	}

	ctype := CTypeNode{TypeInfo: *type_info}
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

// Add an edge between a CType P that contains another C as a struct field,
// meaning C is in P's section in the YAML.
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
				field_type := struct_info.Field(i).Type().String()
				// Field could be a copy, pointer, slice, slice of pointers - any others?
				field_type = strings.Trim(field_type, "*[]")
				if field, err := g.Vertex(field_type); err == nil {
					// Field is a CType
					err := g.AddEdge(parent_ctype_name, CTypeNodeHash(field))
					if err != nil && !errors.Is(err, graph.ErrEdgeAlreadyExists) {
						return err
					}
				}
			}
		}
	}
	// LEFT OFF if type T contains iface, add edge from T to all the iface implementers
	return nil
}

// Infer the names of the parameters contained in the CTypes (prefixing with <section.subsection...>),
// from the struct tags and the edges between CTypes
func FindParamKeys(g CTypeGraph) error {
	m, err := g.PredecessorMap()
	if err != nil {
		return err
	}

	for ctype_name, in_edges := range m {
		// A root of the graph (no incoming edges)
		if len(in_edges) == 0 {
			_, err := g.Vertex(ctype_name)
			if err != nil {
				return err
			}

			param_key := ""
			graph.DFS(g, ctype_name, func(n string) bool {
				param_key += "."
				// LEFT OFF append the param key
				return false // continue
			}, true, false)
		}

	}
	return nil
}

// If !all, just print <package.type>
// Remove cutprefix from type when printing
// Prints depth-first starting from each root (so each CType will be printed once for every root it's reachable from)
func PrettyPrint(g CTypeGraph, all bool, cutprefix string) error {
	m, err := g.PredecessorMap()
	if err != nil {
		return err
	}

	for ctype_name, in_edges := range m {
		// A root of the graph (no incoming edges)
		if len(in_edges) == 0 {
			graph.DFS(g, ctype_name, func(n string) bool {
				if !all {
					short_name, _ := strings.CutPrefix(n, cutprefix)
					fmt.Printf("%v\n", short_name)
				}
				return false // continue
			}, true, true) // all paths

			fmt.Println()
		}
	}
	return nil
}
