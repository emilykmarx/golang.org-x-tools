package pkg_a

import (
	"fmt"
	"log"

	"gopkg.in/yaml.v2"
)

/* Edge via an interface implementation. */

type InterfaceRoot struct {
	Iface_field Interface
}

type Interface interface {
	Method()
}

type Implementer struct {
	Impl_field string
}

func (i *Implementer) Method() {}

func (c *InterfaceRoot) UnmarshalYAML(unmarshal func(interface{}) error) error {
	*c = InterfaceRoot{}
	type plain InterfaceRoot
	return unmarshal((*plain)(c))
}

// Here for convenience for now
func main() {
	root := InterfaceRoot{Iface_field: &Implementer{Impl_field: "val"}}

	unmarshaled, err := yaml.Marshal(root)
	if err != nil {
		log.Fatalf("error marshaling: %v", err)
	}

	fmt.Printf("**MARSHALED**\n\n %+v\n", string(unmarshaled))
}
