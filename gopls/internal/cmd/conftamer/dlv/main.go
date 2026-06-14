package main

import (
	"flag"
	"fmt"
	"log"
	"reflect"
	"slices"
	"strings"

	"github.com/dominikbraun/graph"
	"github.com/emilykmarx/conftamer/contexttrack"
	"github.com/go-delve/delve/service/api"
	"github.com/go-delve/delve/service/rpc2"
	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
	dlvgraph "golang.org/x/tools/gopls/internal/cmd/conftamer/dlv/graph"
	"golang.org/x/tools/gopls/internal/golang"
)

func main() {
	var dlv_port int
	var test_pkg, test_name, graph_file, module_prefix string
	flag.IntVar(&dlv_port, "dlv-port", 4040, "Listening port for dlv")
	flag.StringVar(&test_pkg, "test-pkg", "", "Package of test to run")
	flag.StringVar(&test_name, "test-name", "", "Name of test to run")
	flag.StringVar(&module_prefix, "module-prefix", "", "module as in go.mod")
	flag.StringVar(&graph_file, "graph-file", "", "File containing CTypes graph")
	flag.Parse()

	Run(dlv_port, test_pkg, test_name, graph_file)
}

func Run(dlv_port int, test_pkg string, test_name string, graph_file string) {
	// 1. Load the CTypes graph
	graph_file = "testdata/graph.text"
	g, m := ct.Deserialize(graph_file)
	ctypes := ct.CTypes{Graph: g, List: m.List}
	// 2. Connect to dlv server and run test
	if err := contexttrack.Run(dlv_port, test_pkg, test_name, ctypes, RunDlvClient); err != nil {
		panic(err)
	}
}

var (
	loadcfg = api.LoadConfig{FollowPointers: true, MaxVariableRecurse: 100, MaxStringLen: 100, MaxArrayValues: 100, MaxStructFields: -1}
	scope   = api.EvalScope{GoroutineID: -1}
)

func RunDlvClient(dlv_endpoint string, ctypes_all any) error {
	fmt.Printf("Connecting to dlv on %v\n", dlv_endpoint)

	ctypes := ctypes_all.(ct.CTypes)

	client := rpc2.NewClient(dlv_endpoint)
	// Set breakpoints on methods of CTypes in module
	// XXX also set bps on messages

	// XXX get these from graph
	// XXX get recvr hash - (need to lookup recvr CType - for now assume its node hash is its name)
	module_name := "golang.org/x/tools/gopls/internal/cmd/conftamer/dlv"
	recvr_type := "ParentFirst"
	method_name := module_name + "." + recvr_type + ".Method"
	recvr_name := "c"
	bp := api.Breakpoint{FunctionName: method_name}

	_, err := client.CreateBreakpoint(&bp)
	ct.CheckErr(err)

	state := <-client.Continue()

	for ; !state.Exited; state = <-client.Continue() {
		if state.Err != nil {
			log.Fatalf("Error in debugger state: %v\n", state.Err)
		}

		// XXX check which bp was hit
		// TODO proper handling for incomplete loads - see ClientHowTo.md
		recvr_var, err := client.EvalVariable(scope, recvr_name, loadcfg)
		ct.CheckErr(err)

		recvr_hash := ct.CTypeHash(recvr_type)
		params := []CTypeParam{}

		// 1. Get key prefixes for all paths from a root to the receiver
		key_prefixes := []string{}
		ctype_paths, ast_paths := dlvgraph.CTypePathsToOrFrom(ctypes.Graph, recvr_hash, graph.Backwards)
		for i, ctype_path := range ctype_paths {
			for _, ast_path := range ast_paths[i] {
				key := ""
				CTypePathToParams(ctype_path, ast_path, 0, api.Variable{}, &params, &key)
				key_prefixes = append(key_prefixes, key)
			}
			// If receiver is root, this will do nothing which is correct
		}

		// 2. Get key postfixes and values for all paths from the receiver to a leaf
		ctype_paths, ast_paths = dlvgraph.CTypePathsToOrFrom(ctypes.Graph, recvr_hash, graph.Forwards)
		for i, ctype_path := range ctype_paths {
			for _, ast_path := range ast_paths[i] {
				key := ""
				// populates params with keys and values
				CTypePathToParams(ctype_path, ast_path, 0, *recvr_var, &params, &key)
			}
			if len(ast_paths[i]) == 0 {
				// Receiver is leaf
				key := ""
				CTypePathToParams(ctype_path, dlvgraph.ASTPath{}, 0, *recvr_var, &params, &key)
			}
		}

		// 3. Final keys: prepend all prefixes to all postfixes
		// (if a key appears in multiple sections of file, the corresponding type has multiple paths to it in the graph)
		final_params := []CTypeParam{}
		for _, param := range params {
			for _, key_prefix := range key_prefixes {
				final_param := param
				final_param.Key = key_prefix + "." + param.Key
				fmt.Printf("FINAL PARAM: KEY %v, VAL %+v\n", final_param.Key, final_param.Value)
				final_params = append(final_params, final_param)
			}
		}
	}

	fmt.Printf("Target exited with status %v\n", state.ExitStatus)

	client.Detach(false)
	return nil
}

// The key and value of a config param that a CType has access to,
// via copy or alias.
type CTypeParam struct {
	Key   string
	Value api.Variable
}

// Given a CType path and optionally the variable for the CType at the beginning,
// get the corresponding parameter key(s) from CType info.
// If variable is passed, also get parameter value(s) from variable.
// Assume the default behavior of UnmarshalYAML wrt mapping file keys to types.
func CTypePathToParams(ctype_path dlvgraph.CTypesPath, ast_path dlvgraph.ASTPath, ast_path_idx int,
	cur_var api.Variable, params *[]CTypeParam, key *string) {

	get_values := cur_var.Addr != 0

	// Don't modify ast_path, since multiple elems need to recurse on it
	// ast_path_idx/cur_ast_path_idx is index in full ast_path
	cur_ast_path_idx := ast_path_idx

	if ast_path_idx < len(ast_path) {
		// 1. Eat AST edges until find child Variable(s) to recurse on, or reach end
		// Edge between CTypes isn't necessarily 1:1 with edge in path between Variable and its Children
		// (e.g. pointer Variable has a Child for its target)
		for i, ast_edge := range ast_path[ast_path_idx:] {
			cur_ast_path_idx = ast_path_idx + i

			// Case 1. Edge corresponds to one or more of the Variable's direct Children => recurse on the relevant children
			if field, ok := strings.CutPrefix(ast_edge, golang.FIELD_NAME_PREFIX); ok {
				// Children are fields
				found_field := false
				for _, child_field := range cur_var.Children {
					// Identify named fields by their field name, not their type, since e.g. if their type is []T the CType edge is to T not []T
					// (See Prometheus edge /tsdb/index.MemPostings => /storage.SeriesRef for more complex example)
					if child_field.Name == field {
						// corresponding field
						cur_var = child_field
						found_field = true
						break
					}
				}

				if !found_field && get_values {
					// Should find the field even if code doesn't explicitly set it
					panic(fmt.Errorf("Field %v not found in %+v - path %v\n", field, cur_var, ast_path))
				}

				// Append field tag to key
				edge := dlvgraph.AstIdxToEdge(ctype_path, ast_path, cur_ast_path_idx)
				key_part := FieldToParamKey(field, edge.Source.Tags[field])
				*key = fmt.Sprintf("%v.%v", *key, key_part)

				// Recurse on field
				CTypePathToParams(ctype_path, ast_path, cur_ast_path_idx+1, cur_var, params, key)
				return
			}

			switch ast_edge {
			case "ArrayType.Elt":
				// Children are array/slice elements
				for _, elem := range cur_var.Children {
					new_key := *key // don't share between children, else they will keep appending
					CTypePathToParams(ctype_path, ast_path, cur_ast_path_idx+1, elem, params, &new_key)
				}
				if !get_values {
					CTypePathToParams(ctype_path, ast_path, cur_ast_path_idx+1, cur_var, params, key)
				}
				return

			case "StarExpr.X":
				child := cur_var
				if get_values {
					// Child is target of pointer
					child = cur_var.Children[0]
				}
				CTypePathToParams(ctype_path, ast_path, cur_ast_path_idx+1, child, params, key)
				return

			// Case 2. A type of edge we know we can skip
			// Struct stuff (fields handled above)
			case "StructType.Fields":
			case "FieldList.List":
			case "Field.Type":

			// When adding a new edge type: return after recursing

			default:
				// Case 3. Not yet supported
				panic(fmt.Errorf("edge type %v not supported - path %v, cur var %+v\n", ast_edge, ast_path, cur_var))
			}
		}
	}

	if cur_ast_path_idx >= len(ast_path)-1 {
		// End of AST path

		if get_values {
			// Traversed receiver to leaf => get leaf's params
			if len(cur_var.Children) == 0 {
				// no children => a param
				*key, _ = strings.CutPrefix(*key, ".")
				*params = append(*params, CTypeParam{Key: *key, Value: cur_var})
				fmt.Printf("APPEND PARAM: KEY %v, VAL %+v\n", *key, cur_var)
			} else {
				// recurse on all children
				for _, child := range cur_var.Children {
					new_key := *key // each field will have different key
					if cur_var.Kind == reflect.Struct {
						// If leaf is struct: Append field tag to key
						leaf_node := ctype_path[len(ctype_path)-1].Target
						key_part := FieldToParamKey(child.Name, leaf_node.Tags[child.Name])
						new_key = fmt.Sprintf("%v.%v", *key, key_part)
					}
					CTypePathToParams(ctype_path, ast_path, cur_ast_path_idx, child, params, &new_key)
				}
			}
		}
	} else {
		// Traversed root to receiver => done (got key already)
	}
}

// Param key corresponding to struct field (tag key if tagged, else lowercase field name)
func FieldToParamKey(field string, tag string) string {
	param_key := ""

	if tag != "" {
		// Get tag key
		tag = strings.Split(tag, ":")[1]
		tag = strings.Trim(tag, "\"")
		tag_parts := strings.Split(tag, ",")
		param_key = tag_parts[0]
		tag_flags := tag_parts[1:]
		if slices.Contains(tag_flags, "inline") {
			// Will need this later (for getting full param name)
		}
	} else {
		// No tag => take key as lowercased field name:
		// Field could either be a key in the raw content (iff field name is uppercase, and lowercased version is in raw content),
		// or copied/otherwise derived from the raw content after unmarshaling
		param_key = strings.ToLower(field)
	}
	return param_key
}
