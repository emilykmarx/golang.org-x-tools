package main

import (
	"fmt"
	"os"
	"testing"
)

func childA() {
	fmt.Println("CHILD A")
	go func() {
		fmt.Println("GRANDCHILD")
		for {
		}
	}()
	for {
	}
}

// Query for child A's or B's ID should return parent's ID/stack
// Query for grandchild's ID should return child A's ID/stack and parent's ID/stack
func TestGSpawn(t *testing.T) {
	if os.Getenv("IGNOREME") == "IGNOREME" {
		IgnoreMe()
	}
	fmt.Println("PARENT")
	go childA()

	go func() {
		fmt.Println("CHILD B")
		for {
		}
	}()
	for {
	}
}

func IgnoreMe() {} // So dlv doesn't complain about setting message send breakpoints
