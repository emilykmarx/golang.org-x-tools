package main

import (
	"flag"
	"fmt"
	"log"

	"github.com/emilykmarx/conftamer/contexttrack"
	"github.com/go-delve/delve/service/api"
	"github.com/go-delve/delve/service/rpc2"
	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
)

func main() {
	var dlv_port int
	var test_pkg, test_name, graph_file string
	flag.IntVar(&dlv_port, "dlv-port", 4040, "Listening port for dlv")
	flag.StringVar(&test_pkg, "test-pkg", "", "Package of test to run")
	flag.StringVar(&test_name, "test-name", "", "Name of test to run")
	flag.StringVar(&graph_file, "graph-file", "", "File containing CTypes graph")
	flag.Parse()

	Run(dlv_port, test_pkg, test_name, graph_file)
}

func Run(dlv_port int, test_pkg string, test_name string, graph_file string) {
	// 1. Load the CTypes graph
	graph_file = "testdata/graph.text"
	g, _ := ct.Deserialize(graph_file)

	// 2. Connect to dlv server and run test
	if err := contexttrack.Run(dlv_port, test_pkg, test_name, g, RunDlvClient); err != nil {
		panic(err)
	}
}

func RunDlvClient(dlv_endpoint string, g any) error {
	fmt.Printf("Connecting to dlv on %v\n", dlv_endpoint)

	graph := g.(ct.CTypeGraph)
	order, err := graph.Order()
	if err != nil {
		panic(err)
	}
	fmt.Printf("GRAPH SZ: %v\n", order) // test

	client := rpc2.NewClient(dlv_endpoint)
	// Set breakpoints on CTypes methods
	// XXX also set bps on messages
	// WORKS
	bp := api.Breakpoint{FunctionName: "golang.org/x/tools/gopls/internal/cmd/conftamer/dlvtest.(*ParentFirst).Method"}
	// LEFT OFF hv graph from integration_test - how to write unit test that calls methods from that module?

	if _, err := client.CreateBreakpoint(&bp); err != nil {
		log.Panicf("method bp: %v", err)
	}

	state := <-client.Continue()

	for ; !state.Exited; state = <-client.Continue() {
		if state.Err != nil {
			log.Fatalf("Error in debugger state: %v\n", state.Err)
		}
		scope := api.EvalScope{GoroutineID: -1}
		// XXX handle pointer recvrs
		recvr_var, err := client.EvalVariable(scope, "*c", api.LoadConfig{})
		if err != nil {
			log.Panicf("eval recvr: %v", err)
		}
		fmt.Printf("RECVR VAR %+v\n", *recvr_var)
	}

	fmt.Printf("Target exited with status %v\n", state.ExitStatus)

	client.Detach(false) // Also kills server, despite function doc
	return nil
}
