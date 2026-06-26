package main

import (
	"flag"
	"fmt"
	"log"
	"strconv"
	"strings"

	dlv "github.com/emilykmarx/conftamer/utils"
	"github.com/go-delve/delve/service/api"
	"github.com/go-delve/delve/service/rpc2"
	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
	modulemsginfo "k8s.io/client-go/testing"
)

func main() {
	var dlv_port int
	var msg_send_funcs arrayFlags
	var test_pkg, test_name, unmarshaler_subgraph, accessors, module_prefix string
	flag.IntVar(&dlv_port, "dlv-port", 4040, "Listening port for dlv")
	flag.StringVar(&module_prefix, "module-prefix", "", "module as in go.mod")
	flag.StringVar(&test_pkg, "test-pkg", "", "Package of test to run, relative to module with leading slash "+
		" (optional - defaults to all packages in module)")
	flag.StringVar(&test_name, "test-name", "", "Name of test to run (optional - defaults to all tests in package)")
	flag.StringVar(&unmarshaler_subgraph, "unmarshaler-subgraph", "", "File containing serialized unmarshaler subgraph")
	flag.StringVar(&accessors, "accessors", "", "File containing serialized Accessors graph")
	flag.Var(&msg_send_funcs, "send-funcs", "Functions that send messages (format: --send-funcs='f1' --send-funcs='f2'")
	flag.Parse()

	if module_prefix == "" || unmarshaler_subgraph == "" || accessors == "" || len(msg_send_funcs) == 0 {
		flag.Usage()
		log.Fatalf("Missing mandatory argument")
	}

	_, ok := strings.CutPrefix(test_pkg, "/")
	if test_pkg != "" && !ok {
		log.Fatalf("test package missing leading /")
	}

	Run(dlv_port, module_prefix, test_pkg, test_name, unmarshaler_subgraph, accessors, msg_send_funcs)
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
	unmarshaler_subgraph ct.CTypes
	accessors            ct.CTypes
	methods              map[string]struct{}
	pkg                  string
	msg_send_funcs       []string
}

func Run(dlv_port int, module_prefix string, test_pkg string, test_name string,
	unmarshaler_subgraph_file string, accessors_file string, msg_send_funcs []string) {

	// 1. Load the CTypes graphs
	g, m := ct.Deserialize(unmarshaler_subgraph_file)
	unmarshaler_subgraph := ct.CTypes{Graph: g, List: m.List}
	g, m = ct.Deserialize(accessors_file)
	accessors := ct.CTypes{Graph: g, List: m.List}

	// 2. Parse info from CTypes graphs
	pkgs := make(map[string]struct{})
	methods := make(map[string]struct{})
	for _, g := range []ct.CTypeGraph{unmarshaler_subgraph.Graph, accessors.Graph} {
		GetCTypesInfo(g, test_pkg, pkgs, methods)
		if len(pkgs) == 0 {
			panic("no ctypes found - bad package name?")
		}
	}

	// 3. For each package:
	// Connect to dlv server and run tests
	// (dlv can only run tests in one package at a time)
	for pkg := range pkgs {
		client_info := ClientInfo{unmarshaler_subgraph: unmarshaler_subgraph, accessors: accessors, methods: methods, pkg: pkg, msg_send_funcs: msg_send_funcs}
		if err := dlv.Run(dlv_port, module_prefix+pkg, test_name, client_info, RunDlvClient); err != nil {
			if _, ok := err.(*dlv.ErrNoTests); ok {
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

// Terminology note: "CType" = in Unmarshal Subgraph and/or Accessors
// Get all packages in the module that define CTypes
// Get all CTypes methods
// package format: "/<path after module prefix>"
func GetCTypesInfo(g ct.CTypeGraph, test_pkg string, pkgs map[string]struct{}, methods map[string]struct{}) {
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
				pkgs[pkg] = struct{}{}
			}
		}
		for _, orig_method := range node.Methods {
			method := string(orig_method)
			sanitizeMethod(&method)
			methods[method] = struct{}{}
		}
	}
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
*/

func sanitizeMethod(method *string) {
	// gopls and dlv put the * and () in different places in method names => remove them
	*method = strings.ReplaceAll(*method, "*", "")
	*method = strings.ReplaceAll(*method, "(", "")
	*method = strings.ReplaceAll(*method, ")", "")

	// if appears to be an anonymous function, switch to the function that defined it
	parts := strings.Split(*method, ".")
	maybe_anon := parts[len(parts)-1]
	if n, ok := strings.CutPrefix(maybe_anon, "func"); ok {
		if _, err := strconv.Atoi(n); err == nil {
			// ends with ".funcX", where X is a number => remove that postfix
			*method = strings.Join(parts[:len(parts)-1], ".")
		}
	}
}

func HandleMessageSend(client *rpc2.RPCClient, args ClientInfo, bp *api.Breakpoint, send_goroutine int64) {
	msgID, err := modulemsginfo.GetMessageInfo(client, bp, send_goroutine)
	ct.CheckErr(err)

	fmt.Printf("MSG SEND: %v\n", msgID)
	scope := api.EvalScope{GoroutineID: send_goroutine}
	user := api.ListGoroutinesFilter{Kind: api.GoroutineUser}
	goroutines, _, _, _, err := client.ListGoroutinesWithFilter(0, 0, []api.ListGoroutinesFilter{user}, nil, &scope)
	ct.CheckErr(err)

	send_method := "" // The most recent CTypes method in the sending goroutine's stack (if any)
	for _, goroutine := range goroutines {
		// TODO check for partially loaded, and hitting max depth (see how PrintStack() in dlv does it)
		stack, err := client.Stacktrace(goroutine.ID, 100, -1, api.StacktraceSimple, &api.LoadConfig{})
		ct.CheckErr(err)

		for _, frame := range stack {
			fn := frame.Function.Name()
			sanitizeMethod(&fn)

			if _, ok := args.methods[fn]; ok {
				// TODO ignore the same types in the Unmarshaler Subgraph that we do when finding Accessors

				// CF:
				// CTypes method in stack of any goroutine
				fmt.Printf("CF METHOD: %v\n", fn)

				if goroutine.ID == send_goroutine && send_method == "" {
					// DF:
					// Most recent CTypes method in stack of sending goroutine.
					// If sending goroutine is running an anonymous function defined in a CTypes method, count that CTypes method as the "most recent"
					// (Since that CTypes method may copy its argument into the argument to the anonymous function)
					// e.g. in discovery/kubernetes tests, sender stack is: <cache stuff>, (*Discovery).newNodeInformer.func2, (*FakeNodes).Watch, send func
					// (*Discovery).newNodeInformer copies receiver params into the arguments to (*FakeNodes).Watch
					send_method = fn
					fmt.Printf("DF METHOD: %v\n", fn)
					// For anonymous function, CType shows up in locals but not args
					fmt.Printf("LOCALS: %+v\n", frame.Locals)
					fmt.Printf("ARGS: %+v\n", frame.Arguments)
				}
			}
		}
	}

}

func RunDlvClient(dlv_endpoint string, info any) error {
	fmt.Printf("Connecting to dlv on %v\n", dlv_endpoint)

	args := info.(ClientInfo)

	client := rpc2.NewClient(dlv_endpoint)
	// XXX log info similarly to conftamer repo
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
