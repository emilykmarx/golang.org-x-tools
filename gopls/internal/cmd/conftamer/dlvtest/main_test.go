package main

import (
	"fmt"
	"testing"
)

type ParentFirst struct {
	ChildField1 []ChildSecond
	NonCType    int
}
type ChildSecond struct {
	Val int
}

// Fake module (with unit tests and CType definitions all in one package)
func Test_Field_Slice(t *testing.T) {
	ctype := ParentFirst{ChildField1: []ChildSecond{{Val: 1}, {Val: 2}}, NonCType: 5}
	ctype.Method()
}

func (c ParentFirst) Method() {
	fmt.Printf("RECVR VALUE IN METHOD: %+v\n", c)
}
