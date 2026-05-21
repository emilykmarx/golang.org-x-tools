package main

import (
	"fmt"
	"log"

	"gopkg.in/yaml.v2"
)

/* Type enclosing a CType, but not via struct field ("alias" is not actually the right name for this).
 * Leaf has fields. */

type AliasRoot struct {
	Alias_field Alias
}

type RealRoot struct {
	Real_field Real
}

type Real struct {
	A_field string
}

type Alias Real

func (c *AliasRoot) UnmarshalYAML(unmarshal func(interface{}) error) error {
	*c = AliasRoot{}
	type plain AliasRoot
	return unmarshal((*plain)(c))
}

// Here for convenience for now
func main() {
	real_root := RealRoot{Real_field: Real{A_field: "paramval"}} // file has `real_field` key
	//alias_root := AliasRoot{Alias_field: Alias{A_field: "paramval"}} // file has `alias_field` key

	unmarshaled, err := yaml.Marshal(real_root)
	if err != nil {
		log.Fatalf("error marshaling: %v", err)
	}

	fmt.Printf("**MARSHALED**\n\n %+v\n", string(unmarshaled))
}
