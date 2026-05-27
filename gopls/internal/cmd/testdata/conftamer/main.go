package main

import (
	"fmt"
	"log"

	"gopkg.in/yaml.v2"
)

/* CTypes graph with multiple leaves, multiple paths to each leaf.
A type in the middle of the graph implements UnmarshalYAML.
Leaf has no fields.
XXX struct tags, fields that are ptr/[] */

type Root struct {
	A_field A
	X_field X
}
type A struct {
	B_field B
}
type X struct {
	B_field B
}
type B struct {
	C_field C
	D_field D
}
type C string
type D string

func (c *A) UnmarshalYAML(unmarshal func(interface{}) error) error {
	*c = A{}
	type plain A
	return unmarshal((*plain)(c))
}

// Here for convenience for now
func main() {
	b := B{C_field: "val", D_field: "val"}
	a := Root{A_field: A{B_field: b}, X_field: X{B_field: b}}

	unmarshaled, err := yaml.Marshal(a)
	if err != nil {
		log.Fatalf("error marshaling: %v", err)
	}

	fmt.Printf("**MARSHALED**\n\n %+v\n", string(unmarshaled))
}
