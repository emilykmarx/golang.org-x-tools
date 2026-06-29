package main

import (
	"errors"
	"fmt"
	"slices"
	"strings"

	"github.com/dominikbraun/graph"
	"github.com/go-delve/delve/service/api"
	"github.com/go-delve/delve/service/rpc2"
	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
	dlvgraph "golang.org/x/tools/gopls/internal/cmd/conftamer/dlv/graph"
	"golang.org/x/tools/gopls/internal/golang"
)

/* Functions for getting the parameters a CTypes method has access to */

// Get all param keys the given Unmarshaler Subgraph node has access to
func UnmarshalerIngressParams(args ClientInfo, ingress_hash ct.CTypeHash) []string {
	// 1. Get key prefixes for all paths from a root to the ingress
	// Don't include non-custom fields (not part of the key prefix)
	key_prefixes := []string{}
	opts := graph.DFSOpts[ct.CTypeHash, ct.CTypeNode]{Direction: graph.Backwards, All_paths: true}
	ctype_paths, ast_paths := dlvgraph.CTypePathsToOrFrom(args.unmarshaler_subgraph.Graph, ingress_hash, opts)

	for i, ctype_path := range ctype_paths {
		for _, ast_path := range ast_paths[i] {
			key := ""
			ASTPathToParams(ctype_path, nil, ast_path, 0, &key_prefixes, &key, false)
		}
	}

	// 2. Get key postfixes for all paths from the ingress to a leaf
	// Include non-custom fields (ingress has access to them)
	key_postfixes := []string{}
	opts.Direction = graph.Forwards
	ctype_paths, ast_paths = dlvgraph.CTypePathsToOrFrom(args.unmarshaler_subgraph.Graph, ingress_hash, opts)

	for i, ctype_path := range ctype_paths {
		for _, ast_path := range ast_paths[i] {
			key := ""
			ASTPathToParams(ctype_path, ctype_paths, ast_path, 0, &key_postfixes, &key, true)
		}
		if len(ast_paths[i]) == 0 {
			// Ingress is leaf
			key := ""
			ASTPathToParams(ctype_path, ctype_paths, nil, 0, &key_postfixes, &key, true)
		}
	}

	// 3. Final keys: prepend all prefixes to all postfixes
	// (if a key appears in multiple sections of file, the corresponding type has multiple paths to it in the graph)
	final_keys := []string{}
	for _, key_prefix := range key_prefixes {
		for _, key_postfix := range key_postfixes {
			final_key := strings.Trim(key_prefix+"."+key_postfix, ".")
			final_keys = append(final_keys, final_key)
		}
	}

	return final_keys
}

// 1. Get all Unmarshaler Subgraph nodes the receiver has an Accessors path to
func UnmarshalerIngresses(args ClientInfo, recvr_hash ct.CTypeHash) []ct.CTypeHash {
	ingresses := []ct.CTypeHash{}
	// Just get the leaf, not all paths to it (we don't need them, and has big perf impact for big graphs -
	// AllPaths is much slower than ShortestPath)
	for _, accessor_leaf := range args.accessor_leaves {
		_, err := graph.ShortestPath(args.accessors.Graph, recvr_hash, accessor_leaf)
		if err == nil {
			ingress, in_us := args.unmarshaler_subgraph.GetHash(ct.FullTypeName(accessor_leaf))
			if in_us {
				ingresses = append(ingresses, ingress)
			} else {
				// Accessor leaf is not in Unmarshaler Subgraph - rare (see CheckAccessors())
				panic(fmt.Errorf("Accessor leaf %v is not in Unmarshaler Subgraph", accessor_leaf))
			}
		} else if errors.Is(err, graph.ErrTargetNotReachable) {
			// no path to ingress - ok
		} else {
			ct.CheckErr(err)
		}
	}

	return ingresses
}

// Get the param keys the method's receiver has access to
func MethodParams(client *rpc2.RPCClient, args ClientInfo, method string) []string {
	// XXX get recvr type from dlv. If it's in the US, handle that.
	recvr_type := "/discovery/kubernetes.Discovery"
	recvr_hash := ct.CTypeHash(recvr_type)
	ingresses := UnmarshalerIngresses(args, recvr_hash)
	param_keys := []string{}

	for _, ingress_hash := range ingresses {
		param_keys = append(param_keys, UnmarshalerIngressParams(args, ingress_hash)...)
	}

	// Sort and dedup for convenience
	slices.Sort(param_keys)
	param_keys = slices.Compact(param_keys)

	fmt.Printf("METHOD %v: keys %v\n", method, param_keys)
	return param_keys
}

// The key and value of a config param that a CType has access to,
// via copy or alias.
type CTypeParam struct {
	Key   string
	Value api.Variable
}

func appendFieldTag(field string, tag string, key *string) {
	key_part := FieldToParamKey(field, tag)
	*key = fmt.Sprintf("%v.%v", *key, key_part)
	*key = strings.Trim(*key, ".")
}

// Return true if field has no AST edges out on any ctype path.
// (Unless `all_ctype_paths` not passed)
func nonCustomField(field string, ctype_edge graph.Edge[ct.CTypeNode], all_ctype_paths []dlvgraph.CTypesPath) bool {
	if all_ctype_paths == nil {
		return false
	}

	found := false
	for _, other_ctype_path := range all_ctype_paths {
		for _, other_edge := range other_ctype_path {
			if ct.NodeEqual(other_edge.Source, ctype_edge.Source) {
				// Same node on other path => see if it has out AST edges for this field
				for _, other_ast_path := range dlvgraph.EdgeASTPaths(other_edge.Properties) {
					for _, other_ast_edge := range other_ast_path {
						if other_field, ok := strings.CutPrefix(other_ast_edge, golang.FIELD_NAME_PREFIX); ok {
							if other_field == field {
								found = true
							}
						}
					}
				}
			}
		}
	}

	return !found
}

// Given an AST path and corresponding CType path,
// get the corresponding parameter key(s) from CType info.
// If `leaf_keys`: Append all of the last node's tags to the final key (won't have corresponding AST edges in given path).
// If `all_ctype_paths`: Also include non-custom fields.
// Assume the default behavior of UnmarshalYAML wrt mapping file keys to types.
func ASTPathToParams(ctype_path dlvgraph.CTypesPath, all_ctype_paths []dlvgraph.CTypesPath, ast_path dlvgraph.ASTPath, ast_path_idx int,
	keys *[]string, key *string, leaf_keys bool) {
	// Note key is shared across all recursive calls, so must copy it when want recursive calls to have their own

	// ast_path_idx/cur_ast_path_idx is index in full ast_path
	cur_ast_path_idx := ast_path_idx

	if ast_path_idx < len(ast_path) {
		// Eat edges until find field to recurse on
		for i, ast_edge := range ast_path[ast_path_idx:] {
			cur_ast_path_idx = ast_path_idx + i
			if field, ok := strings.CutPrefix(ast_edge, golang.FIELD_NAME_PREFIX); ok {
				ctype_edge := dlvgraph.AstIdxToEdge(ctype_path, ast_path, cur_ast_path_idx)
				tags := ctype_edge.Source.Tags

				// Check for any non-custom fields - won't have an AST edge out, but are params if the node is a CType.
				for other_field := range tags {
					if other_field != field {
						if nonCustomField(other_field, ctype_edge, all_ctype_paths) {
							key_copy := *key
							appendFieldTag(other_field, tags[other_field], &key_copy)
							*keys = append(*keys, key_copy)
						}
					}
				}

				// Append corresponding field tag to key
				appendFieldTag(field, tags[field], key)

				// Recurse on field
				ASTPathToParams(ctype_path, all_ctype_paths, ast_path, cur_ast_path_idx+1, keys, key, leaf_keys)

				return
			}
		}
	}

	// Reached end of AST path => record key(s)
	last_node := ctype_path[len(ctype_path)-1].Target
	if !leaf_keys || len(last_node.Tags) == 0 {
		*keys = append(*keys, *key)
	} else {
		for field, tag := range last_node.Tags {
			key_copy := *key
			// Append tag to key
			appendFieldTag(field, tag, &key_copy)
			*keys = append(*keys, key_copy)
		}
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
		if param_key == "-" {
			param_key = ""
		}
	} else {
		// No tag => take key as lowercased field name:
		// Field could either be a key in the raw content (iff field name is uppercase, and lowercased version is in raw content),
		// or copied/otherwise derived from the raw content after unmarshaling
		param_key = strings.ToLower(field)
	}
	return param_key
}
