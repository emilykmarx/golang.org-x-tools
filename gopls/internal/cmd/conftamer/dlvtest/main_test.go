package main

import (
	"fmt"
	"testing"
)

type ParentFirst struct {
}

// Fake module (with unit tests and CType definitions all in one package)
func Test_main(t *testing.T) {
	ctype := ParentFirst{}
	ctype.Method()
}

func (c *ParentFirst) Method() {
	fmt.Println("method")
}
