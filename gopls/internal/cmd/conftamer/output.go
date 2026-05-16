package conftamer

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/dominikbraun/graph"
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

// If !all, just print <package.type>
// Remove cutprefix from type when printing and storing test logs
// Prints depth-first starting from each root (so each CType will be printed once for every root it's reachable from)
func PrettyPrint(g CTypeGraph, cutprefix string) error {
	all_nodes := []TestNode{}
	var visit_err error
	err := graph.DFSAllStartingNodes(g, func(n FullTypeName) bool {
		short_name, _ := strings.CutPrefix(string(n), cutprefix)
		fmt.Printf("%v\n", short_name)
		node, err := g.Vertex(n)
		if err != nil {
			visit_err = err
		}

		all_nodes = append(all_nodes, TestNode{
			ID:           short_name,
			Stored_down:  node.Stored_down,
			Stored_up:    node.Stored_up,
			Stored_final: node.Stored_final,
		})
		return false // continue
	}, graph.UpdatePathVertices[CTypeNode]{}, true, true, graph.Forwards)

	if err != nil || visit_err != nil {
		return err
	}

	marshaled, err := json.Marshal(all_nodes)
	if err != nil {
		return err
	}
	// Need to write in this fancy way for test to be able to unmarshal it
	var buf bytes.Buffer
	buf.Write(marshaled)
	outfile := "stored.log"
	f, err := os.Create(outfile)
	if err != nil {
		return err
	}
	defer f.Close()
	_, err = io.Copy(f, &buf)

	if err != nil {
		return err
	}

	return nil
}

// For convenience, remove the package name
func shortHash(full FullTypeName) string {
	hash_parts := strings.Split(string(full), ".")
	return hash_parts[len(hash_parts)-1]
}
