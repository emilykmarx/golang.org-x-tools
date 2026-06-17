package main

import (
	"flag"
	"fmt"
	"log"
	"slices"
	"strings"

	"github.com/emilykmarx/conftamer/contexttrack"
	"github.com/go-delve/delve/service/api"
	"github.com/go-delve/delve/service/rpc2"
	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
)

func main() {
	var dlv_port int
	var test_pkg, test_name, graph_file, module_prefix string
	flag.IntVar(&dlv_port, "dlv-port", 4040, "Listening port for dlv")
	flag.StringVar(&module_prefix, "module-prefix", "", "module as in go.mod")
	flag.StringVar(&test_pkg, "test-pkg", "", "Package of test to run, relative to module with leading slash "+
		" (optional - defaults to all packages in module)")
	flag.StringVar(&test_name, "test-name", "", "Name of test to run (optional - defaults to all tests in package)")
	flag.StringVar(&graph_file, "graph-file", "", "File containing CTypes graph")
	flag.Parse()

	if module_prefix == "" || graph_file == "" {
		flag.Usage()
		log.Fatalf("Missing mandatory argument")
	}

	_, ok := strings.CutPrefix(test_pkg, "/")
	if test_pkg != "" && !ok {
		log.Fatalf("test package missing leading /")
	}

	Run(dlv_port, module_prefix, test_pkg, test_name, graph_file)
}

// Info passed to the dlv client for each package
type ClientInfo struct {
	ctypes     ct.CTypes
	pkg        string
	pkg_ctypes []ct.CTypeNode
}

func Run(dlv_port int, module_prefix string, test_pkg string, test_name string, graph_file string) {
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
		client_info := ClientInfo{ctypes: ctypes, pkg: pkg, pkg_ctypes: pkg_ctypes}
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
	loadcfg = api.LoadConfig{FollowPointers: true, MaxVariableRecurse: 100, MaxStringLen: 100, MaxArrayValues: 1, MaxStructFields: -1}
	scope   = api.EvalScope{GoroutineID: -1}
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

// Set breakpoints on methods of CTypes in package
func SetCTypesBreakpoints(client *rpc2.RPCClient, ctypes []ct.CTypeNode) {
	for _, node := range ctypes {
		for _, method := range node.Methods {
			method_parts := strings.Split(string(method), ".")
			method_name := method_parts[len(method_parts)-1]
			if !slices.Contains(IGNORED_METHODS, method_name) {

				// Fix format
				method_dlv := string(method)
				if stripped, ok := strings.CutPrefix(method_dlv, "(*"); ok {
					// gopls records methods with pointer receivers as (*pkg.recvr).method, but dlv wants pkg.(*recvr).method
					method_parts = strings.Split(stripped, ".") // pkg parts (may have "."), recvr), method
					pkg_parts := method_parts[:len(method_parts)-2]
					method_parts_final := make([]string, len(pkg_parts))
					copy(method_parts_final, pkg_parts)
					method_parts_final = append(method_parts_final, "(*")
					part1 := strings.Join(method_parts_final, ".")
					part2 := strings.Join(method_parts[len(pkg_parts):], ".")
					method_dlv = part1 + part2
				} else {
					// gopls records methods with non-pointer receivers as (pkg.recvr).method, but dlv wants pkg.recvr.method
					method_dlv = strings.ReplaceAll(method_dlv, "(", "")
					method_dlv = strings.ReplaceAll(method_dlv, ")", "")
				}

				bp := api.Breakpoint{FunctionName: method_dlv}
				_, err := client.CreateBreakpoint(&bp)
				if err != nil {
					if _, ok := strings.CutPrefix(err.Error(), "could not find function"); !ok {
						ct.CheckErr(err)
					} else {
						// Method not compiled in (or malformed method name, if there is an unknown bug above)
						// TODO any way to tell the difference?
						// (`dlv test` only compiles in methods that will be called in the tests)
					}
				}
			}
		}
	}
}

func HandleCTypesMethodEntry(client *rpc2.RPCClient, args ClientInfo, method string) {
	fmt.Printf("ENTER CTYPES METHOD %v\n", method)

	MethodParams(client, args, method)
}

func RunDlvClient(dlv_endpoint string, info any) error {
	fmt.Printf("Connecting to dlv on %v\n", dlv_endpoint)

	args := info.(ClientInfo)

	client := rpc2.NewClient(dlv_endpoint)
	// XXX also set bps on messages (currently prom uses forked k8s client that logs them), and method exits
	// XXX log info similarly to conftamer repo - may need a lock around the data used to hold the info (for parallel tests)?
	SetCTypesBreakpoints(client, args.pkg_ctypes)

	state := <-client.Continue()

	for ; !state.Exited; state = <-client.Continue() {
		if state.Err != nil {
			log.Fatalf("Error in debugger state: %v\n", state.Err)
		}

		for _, thread := range state.Threads {
			hit_bp := thread.Breakpoint
			if hit_bp != nil {
				HandleCTypesMethodEntry(client, args, hit_bp.FunctionName)
			}
		}
	}

	fmt.Printf("Target exited with status %v\n", state.ExitStatus)

	client.Detach(false)
	return nil
}
