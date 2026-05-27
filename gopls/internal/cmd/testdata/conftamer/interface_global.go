package pkg_b

import "example.com/pkg_a"

type GlobalImplementer struct {
	Impl_field string
}

func (i *GlobalImplementer) Method() {}

// A global reference (i.e. parent of an already-found type)
type GlobalReference struct {
	Implementer pkg_a.Implementer
}
