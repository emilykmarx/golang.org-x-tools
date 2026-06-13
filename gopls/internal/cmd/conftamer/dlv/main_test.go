package main

import (
	"fmt"
	"testing"
)

// Fake module (with unit tests and CType definitions all in one package)

// CType edge with field and array AST edges.
// Non-CType field (child that should be ignored).
// Struct tag.
type ParentFirst struct {
	ChildField1 []ChildSecond `yaml:"renamed_key"`
	NonCType    int
}
type ChildSecond struct {
	Val int
}

// Multiple AST paths on single CType edge, on consecutive CType edges.
// (combinatorial AST paths)
type T1 struct {
	F1 T2
	F2 []T2
}

type T2 struct {
	F3 T3
	F4 []T3
}

type T3 struct {
	Val int
}

func Test_Field_Slice(t *testing.T) {
	ctype := ParentFirst{ChildField1: []ChildSecond{{Val: -1}, {Val: -2}}, NonCType: -5}
	ctype.Method()
}

func (c ParentFirst) Method() {
	fmt.Printf("RECVR VALUE IN METHOD: %+v\n", c)
}

func Test_Multiple_AST_Paths(t *testing.T) {
	t2 := T2{F3: T3{Val: 1}, F4: []T3{{Val: 2}, {Val: 3}}}
	t2_2 := T2{F3: T3{Val: 4}, F4: []T3{{Val: 5}, {Val: 6}}}
	t2_3 := T2{F3: T3{Val: 7}, F4: []T3{{Val: 8}, {Val: 9}}}
	ctype := T1{F1: t2, F2: []T2{t2_2, t2_3}}
	ctype.Method()
}

func (c T1) Method() {
	fmt.Printf("RECVR VALUE IN METHOD: %+v\n", c)
}
