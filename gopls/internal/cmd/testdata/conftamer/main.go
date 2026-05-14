package main

import (
	"fmt"
	"log"

	"gopkg.in/yaml.v2"
)

/* CTypes graph with multiple leaves, multiple paths to each leaf */

type Root struct {
	A A
	X X
}
type A struct {
	B B
}
type X struct {
	B B
}
type B struct {
	C C
	D D
}
type C string
type D string

func (c *Root) UnmarshalYAML(unmarshal func(interface{}) error) error {
	*c = Root{}
	type plain Root
	return unmarshal((*plain)(c))
}
func (c *A) UnmarshalYAML(unmarshal func(interface{}) error) error {
	*c = A{}
	type plain A
	return unmarshal((*plain)(c))
}
func (c *X) UnmarshalYAML(unmarshal func(interface{}) error) error {
	*c = X{}
	type plain X
	return unmarshal((*plain)(c))
}
func (c *B) UnmarshalYAML(unmarshal func(interface{}) error) error {
	*c = B{}
	type plain B
	return unmarshal((*plain)(c))
}
func (c *C) UnmarshalYAML(unmarshal func(interface{}) error) error {
	return unmarshal((*string)(c))
}
func (c *D) UnmarshalYAML(unmarshal func(interface{}) error) error {
	return unmarshal((*string)(c))
}

// Here for convenience for now
func main() {
	b := B{C: "val", D: "val"}
	a := Root{A: A{B: b}, X: X{B: b}}

	unmarshaled, err := yaml.Marshal(a)
	if err != nil {
		log.Fatalf("error marshaling: %v", err)
	}

	fmt.Printf("**MARSHALED**\n\n %+v\n", string(unmarshaled))
}
