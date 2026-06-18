package main

import (
	"flag"
	"fmt"
	"log"
	"strings"

	"github.com/emilykmarx/conftamer/contexttrack"
	"github.com/go-delve/delve/service/api"
	"github.com/go-delve/delve/service/rpc2"
	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
	modulemsginfo "k8s.io/client-go/testing"
)

func main() {
	var dlv_port int
	var msg_send_funcs arrayFlags
	var test_pkg, test_name, graph_file, module_prefix string
	flag.IntVar(&dlv_port, "dlv-port", 4040, "Listening port for dlv")
	flag.StringVar(&module_prefix, "module-prefix", "", "module as in go.mod")
	flag.StringVar(&test_pkg, "test-pkg", "", "Package of test to run, relative to module with leading slash "+
		" (optional - defaults to all packages in module)")
	flag.StringVar(&test_name, "test-name", "", "Name of test to run (optional - defaults to all tests in package)")
	flag.StringVar(&graph_file, "graph-file", "", "File containing CTypes graph")
	flag.Var(&msg_send_funcs, "send-funcs", "Functions that send messages (format: --send-funcs='f1' --send-funcs='f2'")
	flag.Parse()

	if module_prefix == "" || graph_file == "" || len(msg_send_funcs) == 0 {
		flag.Usage()
		log.Fatalf("Missing mandatory argument")
	}

	_, ok := strings.CutPrefix(test_pkg, "/")
	if test_pkg != "" && !ok {
		log.Fatalf("test package missing leading /")
	}

	Run(dlv_port, module_prefix, test_pkg, test_name, graph_file, msg_send_funcs)
}

// Allow array CLI arg
type arrayFlags []string

func (i *arrayFlags) String() string {
	return fmt.Sprintf("%v", *i)
}
func (i *arrayFlags) Set(value string) error {
	*i = append(*i, value)
	return nil
}

// Info passed to the dlv client for each package
type ClientInfo struct {
	ctypes         ct.CTypes
	pkg            string
	pkg_ctypes     []ct.CTypeNode
	msg_send_funcs []string
}

func Run(dlv_port int, module_prefix string, test_pkg string, test_name string, graph_file string, msg_send_funcs []string) {
	// 1. Load the CTypes graph
	g, m := ct.Deserialize(graph_file)
	ctypes := ct.CTypes{Graph: g, List: m.List}

	// 2. Get packages that have CTypes
	per_pkg_ctypes := GetCTypesPackages(g, test_pkg)
	if len(per_pkg_ctypes) == 0 {
		panic("no ctypes found - bad package name?")
	}

	// 3. For each package:
	// Connect to dlv server and run tests
	// (dlv can only run tests in one package at a time)
	for pkg, pkg_ctypes := range per_pkg_ctypes {
		client_info := ClientInfo{ctypes: ctypes, pkg: pkg, pkg_ctypes: pkg_ctypes, msg_send_funcs: msg_send_funcs}
		if err := contexttrack.Run(dlv_port, module_prefix+pkg, test_name, client_info, RunDlvClient); err != nil {
			if _, ok := err.(*contexttrack.ErrNoTests); ok {
				if test_pkg != "" {
					// Presumably user thought package had tests
					panic(err)
				} else {
					// No tests in this package
				}
			} else {
				panic(err)
			}
		}
	}
}

var (
	// don't set breakpoints on these
	IGNORED_METHODS = []string{
		"String",
		"UnmarshalYAML",
	}
)

// Get module's CTypes, organized by package (avoid iterating over entire graph for each package)
// package format: "/<path after module prefix>"
func GetCTypesPackages(g ct.CTypeGraph, test_pkg string) map[string][]ct.CTypeNode {
	ctypes := make(map[string][]ct.CTypeNode)

	nodes, err := g.Vertices()
	ct.CheckErr(err)
	for _, node := range nodes {
		_, contains_prefix := ct.IsModuleNode(ct.CTypeNodeHash(node), "/", g)
		if contains_prefix {
			// See comment in AddCType - if a node has multiple names, we may be missing some methods
			// Should check that - for now, assume all methods are in same package
			pkg := strings.Split(string(node.Names[0]), ".")[0] // assumes package name minus module prefix contains no "."
			if pkg == "/storage_test" || pkg == "/promql_test" {
				// TODO (minor) gopls skips the last directory in the path for these packages
				// (both in recording the type name and methods)
				// e.g. records github.com/prometheus/prometheus/storage_test, not github.com/prometheus/prometheus/storage/storage_test
				// They're the only package names with "_", maybe that confuses it??
				continue
			}
			if test_pkg == "" || pkg == test_pkg {
				ctypes[pkg] = append(ctypes[pkg], node)
			}
		}
	}

	return ctypes
}

// Set breakpoints on message send functions
func SetMessageSendBreakpoints(client *rpc2.RPCClient, send_funcs []string) {
	for _, f := range send_funcs {
		bp := api.Breakpoint{FunctionName: f}
		_, err := client.CreateBreakpoint(&bp)
		ct.CheckErr(err)
	}
}

/*
- Params can affect 3 decisions we can track (CF):
  - Msg send
  - Function call
  - Goroutine spawn (currently don't track)

- Params can be copied from method's CType to msg send via function calls (DF)

Thus:
  - Msg is CF-tainted by all in-scope params, across all goroutines
    -- TODO: should limit to the goroutines in the sending one's spawn tree - need to track spawning for that
  - Check DF from the CType method that sent the msg

Thus:
- BP on msg send - on hit:
  - Check stack of all goroutines for all CTypes methods (CF)
  - Check stack of sending goroutine for most recent CTypes method (DF)
*/
func HandleMessageSend(client *rpc2.RPCClient, args ClientInfo, bp *api.Breakpoint, goroutine int64) {
	msgID, err := modulemsginfo.GetMessageInfo(client, bp, goroutine)
	ct.CheckErr(err)
	// LEFT OFF the last "Thus" above
	fmt.Printf("MSG SEND %v\n", msgID)
}

func RunDlvClient(dlv_endpoint string, info any) error {
	fmt.Printf("Connecting to dlv on %v\n", dlv_endpoint)

	args := info.(ClientInfo)

	client := rpc2.NewClient(dlv_endpoint)
	// XXX log info similarly to conftamer repo - may need a lock around the data used to hold the info (for parallel tests)?
	SetMessageSendBreakpoints(client, args.msg_send_funcs)

	state := <-client.Continue()

	for ; !state.Exited; state = <-client.Continue() {
		if state.Err != nil {
			log.Fatalf("Error in debugger state: %v\n", state.Err)
		}

		for _, thread := range state.Threads {
			hit_bp := thread.Breakpoint
			if hit_bp != nil {
				HandleMessageSend(client, args, hit_bp, thread.GoroutineID)
			}
		}
	}

	fmt.Printf("Target exited with status %v\n", state.ExitStatus)

	client.Detach(false)
	return nil
}
