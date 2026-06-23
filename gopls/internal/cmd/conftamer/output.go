package conftamer

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/dominikbraun/graph"
	"github.com/dominikbraun/graph/draw"
)

/* Utilities for printing and parsing the output of the CTypes finder */

// When adding a field, remember to populate it while outputting - and upate marshal/unmarshal support if needed
type TestNode struct {
	ID           string
	Stored_down  map[Stored]struct{}
	Stored_up    map[Stored]struct{}
	Stored_final map[Stored]struct{}
}

// Make the test logs easily checkable

// Used to unmarshal/marshal maps with key of type Stored
func (a Stored) MarshalText() (text []byte, err error) {
	return []byte(fmt.Sprintf("%v %v", a.FieldInfo.Field, a.FieldInfo.Tag)), nil
}
func (a *Stored) UnmarshalText(text []byte) error {
	parts := strings.Fields(string(text))
	if len(parts) == 2 {
		a.FieldInfo.Field = parts[0]
		a.FieldInfo.Tag = parts[1]
	} else if len(parts) == 1 {
		// if field key is "", parts is just the tag
		a.FieldInfo.Tag = parts[0]
	} else {
		a.FieldInfo = FieldInfo{} // make empty ones marshal correctly
	}
	return nil
}

type MarshalableNode struct {
	Names   []FullTypeName
	Methods []FullTypeName
	Tags    map[string]string // Field name => tag (populated if struct)
}

// If n is a struct, get its field tags
func getTags(n *CTypeNode) map[string]string {
	if n.TypeInfo == nil {
		// E.g. if marshaling a subgraph created by querying - tags are already populated
		return n.Tags
	}

	struct_info := IsStruct(n.TypeInfo)
	if struct_info == nil {
		// not a struct
		return nil
	}
	tags := make(map[string]string)

	for i := range struct_info.NumFields() {
		tags[struct_info.Field(i).Name()] = struct_info.Tag(i)
	}

	return tags
}

func (n *CTypeNode) MarshalJSON() ([]byte, error) {
	m := MarshalableNode{Names: n.Names, Methods: n.Methods, Tags: getTags(n)}
	return json.Marshal(m)

	// marshal without error to empty string (probably bc interesting fields aren't exported): types.Type, *types.Named, types.Named
	// marshal without error, but error on unmarshal: CTypeNode
}
func (n *CTypeNode) UnmarshalJSON(b []byte) error {
	m := MarshalableNode{}
	if err := json.Unmarshal(b, &m); err != nil {
		return err
	}
	n.Names = m.Names
	n.Methods = m.Methods
	n.Tags = m.Tags

	return nil
}

// Marshalable representation of CTypeGraph - also more easily comparable
type Marshalable struct {
	Edges    []graph.Edge[CTypeHash]
	Vertices []CTypeNode
	List     CTypeList
}

// Cut prefix from both vertex names and edge hashes.
// Vertices are marshaled as in MarshalJSON() override above.
// Edges are marshaled with default MarshalJSON, which includes src/target and edge data.
func Marshal(g CTypeGraph, l CTypeList, cutprefix string) ([]byte, Marshalable) {
	// Edges
	all := Marshalable{}
	edges, err := g.Edges()
	CheckErr(err)
	short_edges := []graph.Edge[CTypeHash]{}
	for _, edge := range edges {
		short_src, _ := strings.CutPrefix(string(edge.Source), cutprefix)
		short_target, _ := strings.CutPrefix(string(edge.Target), cutprefix)
		edge.Source = CTypeHash(short_src)
		edge.Target = CTypeHash(short_target)
		short_edges = append(short_edges, edge)
	}
	all.Edges = short_edges

	// Vertices
	vertices, err := g.Vertices()
	CheckErr(err)
	short_vertices := []CTypeNode{}
	for _, node := range vertices {
		short_names := []FullTypeName{}
		for _, name := range node.Names {
			short_name, _ := strings.CutPrefix(string(name), cutprefix)
			short_names = append(short_names, FullTypeName(short_name))
		}
		node.Names = short_names
		short_vertices = append(short_vertices, node)
	}
	all.Vertices = short_vertices

	// List
	short_list := make(CTypeList)
	for k, v := range l {
		short_k, _ := strings.CutPrefix(string(k), cutprefix)
		short_v, _ := strings.CutPrefix(string(v), cutprefix)
		short_list[FullTypeName(short_k)] = CTypeHash(short_v)
	}
	all.List = short_list

	marshaled, err := json.Marshal(all)
	CheckErr(err)
	return marshaled, all
}

// Marshal graph and write result to file which can be deserialized - module prefix is cut.
// If draw_dot, also write DOT file which can be drawn - module prefix is not cut.
// DOT filename is <filename prefix>.gv
func (c *CTypes) Serialize(filename string, cutprefix string, draw_dot bool) {
	marshaled, _ := Marshal(c.Graph, c.List, cutprefix)
	WriteTestFile(marshaled, filename)

	if draw_dot {
		// TODO (minor) - would be nice to cut the module prefix
		parts := strings.Split(filename, ".")
		if len(parts) == 1 {
			panic("filename should have a .")
		}
		gv_filename := strings.Join(parts[:len(parts)-1], ".") + ".gv"
		gv, err := os.Create(gv_filename)
		CheckErr(err)
		err = draw.DOT(c.Graph, gv)
		CheckErr(err)
	}
}

func Deserialize(filename string) (CTypeGraph, Marshalable) {
	fd, err := os.Open(filename)
	CheckErr(err)
	defer fd.Close()
	marshaled, err := io.ReadAll(fd)
	CheckErr(err)
	return Unmarshal(marshaled)
}

func Unmarshal(marshaled []byte) (CTypeGraph, Marshalable) {
	all := Marshalable{}
	err := json.Unmarshal(marshaled, &all)
	CheckErr(err)

	g := graph.New(CTypeNodeHash, nil, graph.Directed())

	// Vertices
	for _, n := range all.Vertices {
		err = g.AddVertex(n, func(vp *graph.VertexProperties) {})
		if !errors.Is(graph.ErrVertexAlreadyExists, err) {
			CheckErr(err)
		}
	}
	// Edges
	for _, e := range all.Edges {
		err = g.AddEdge(graph.CopyEdge(e)) // preserve properties
		if !errors.Is(graph.ErrEdgeAlreadyExists, err) {
			CheckErr(err)
		}
	}

	return g, all
}

// Whether any of the names start with prefix.
// Return all its shortened names concatenated.
func IsModuleNode(hash CTypeHash, cutprefix string, g CTypeGraph) (string, bool) {
	node, err := g.Vertex(hash)
	CheckErr(err)

	contains_prefix := false
	names := ""
	for i, name := range node.Names {
		short_name, contains := strings.CutPrefix(string(name), cutprefix)
		if contains {
			contains_prefix = true
		}
		if i > 0 {
			names += ", "
		}
		names += short_name
	}

	return names, contains_prefix
}

// Write stored up/down/final for testing.
// Prints depth-first starting from each root.
// If only_prefix: Only print nodes where any of the names start with prefix.
// If all_paths: Print each node every time it's reachable on any path.
// Else: Print each node once per root it's reachable from.
func (c *CTypes) PrettyPrint(cutprefix string, only_prefix bool, all_paths bool) error {
	all_nodes := []TestNode{}
	recordIndent := func(g graph.Graph[CTypeHash, CTypeNode], parent CTypeNode, child CTypeNode) CTypeNode {
		// Record child's indent as parent + 1
		child.Indent = parent.Indent + 1
		return child
	}

	visit := func(hash CTypeHash, _ CTypeHash) graph.VisitRet {
		names, contains_prefix := IsModuleNode(hash, cutprefix, c.Graph)

		node, err := c.Graph.Vertex(hash)
		CheckErr(err)

		for range node.Indent {
			fmt.Printf(" ")
		}
		if !only_prefix || contains_prefix {
			fmt.Printf("%v\n", names)
		}

		short_hash, _ := strings.CutPrefix(string(hash), cutprefix)
		all_nodes = append(all_nodes, TestNode{
			ID:           short_hash,
			Stored_down:  node.Stored_down,
			Stored_up:    node.Stored_up,
			Stored_final: node.Stored_final,
		})
		return graph.KeepVisiting
	}

	opts := graph.DFSOpts[CTypeHash, CTypeNode]{Visit: &visit, Update_vertices: graph.UpdatePathVertices[CTypeHash, CTypeNode]{UpdateChild: &recordIndent},
		All_paths: all_paths, Direction: graph.Forwards}
	err := graph.DFSAllStartingNodes(c.Graph, opts)

	CheckErr(err)

	marshaled, err := json.Marshal(all_nodes)
	CheckErr(err)
	WriteTestFile(marshaled, "stored.log")

	return nil
}

// Need to write in this fancy way for test to be able to unmarshal it
func WriteTestFile(marshaled []byte, filename string) {
	var buf bytes.Buffer
	buf.Write(marshaled)
	f, err := os.Create(filename)
	CheckErr(err)
	defer f.Close()
	_, err = io.Copy(f, &buf)
	CheckErr(err)
}

func PrintStored(k Stored) {
	fmt.Printf("%v via %v\n", k.FieldInfo.Tag, k.FieldInfo.Field)
}

func PrintStoredX(m map[Stored]struct{}) {
	for k := range m {
		PrintStored(k)
	}
}
