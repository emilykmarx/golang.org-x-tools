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
func (c *CTypes) PrettyPrint(cutprefix string) error {
	all_nodes := []TestNode{}
	err := graph.DFSAllStartingNodes(c.Graph, func(n CTypeHash) bool {
		// TODO (minor) if multiple names, print all
		short_name, _ := strings.CutPrefix(string(n), cutprefix)
		fmt.Printf("%v\n", short_name)
		node, err := c.Graph.Vertex(n)
		CheckErr(err)

		all_nodes = append(all_nodes, TestNode{
			ID:           short_name,
			Stored_down:  node.Stored_down,
			Stored_up:    node.Stored_up,
			Stored_final: node.Stored_final,
		})
		return false // continue
	}, graph.UpdatePathVertices[CTypeHash, CTypeNode]{}, true, true, graph.Forwards)

	CheckErr(err)

	marshaled, err := json.Marshal(all_nodes)
	CheckErr(err)

	// Need to write in this fancy way for test to be able to unmarshal it
	var buf bytes.Buffer
	buf.Write(marshaled)
	outfile := "stored.log"
	f, err := os.Create(outfile)
	CheckErr(err)
	defer f.Close()
	_, err = io.Copy(f, &buf)
	CheckErr(err)

	return nil
}

// For convenience, remove the package name
func shortHash(full FullTypeName) string {
	hash_parts := strings.Split(string(full), ".")
	return hash_parts[len(hash_parts)-1]
}

func PrintStored(k Stored) {
	fmt.Printf("%v via %v\n", k.FieldInfo.Tag, k.FieldInfo.Field)
}

func PrintStoredX(m map[Stored]struct{}) {
	for k := range m {
		PrintStored(k)
	}
}
