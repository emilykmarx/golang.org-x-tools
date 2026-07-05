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
// PERF cache the results for other method calls with same receiver (or precompute and persist with graph)
func UnmarshalerIngressParams(args ClientInfo, ingress_hash ct.CTypeHash) []string {
	// XXX Ignore types only defined in tests to reduce fake keys
	// (need conftamer to record the defn loc in TypeInfo, which it currently doesn't)

	// 1. Get key prefixes for all paths from a root to the ingress
	key_prefixes := []string{}
	opts := graph.DFSOpts[ct.CTypeHash, ct.CTypeNode]{Direction: graph.Backwards, All_paths: true}
	ctype_paths, ast_paths := dlvgraph.CTypePathsToOrFrom(args.unmarshaler_subgraph.Graph, ingress_hash, opts)

	for ctype_path_i, ctype_path := range ctype_paths {
		for _, ast_path := range ast_paths[ctype_path_i] {
			// Don't include non-custom fields (not part of the key prefix) or ingress keys (will be handled below)
			key_prefixes = append(key_prefixes, ASTPathToParams(ctype_path, nil, ast_path, false)...)
		}
	}

	// 2. Get key postfixes for all paths from the ingress to a leaf
	// Include non-custom fields (ingress has access to them)
	key_postfixes := []string{}
	opts.Direction = graph.Forwards
	ctype_paths, ast_paths = dlvgraph.CTypePathsToOrFrom(args.unmarshaler_subgraph.Graph, ingress_hash, opts)

	for ctype_path_i, ctype_path := range ctype_paths {
		for _, ast_path := range ast_paths[ctype_path_i] {
			key_postfixes = append(key_postfixes, ASTPathToParams(ctype_path, ctype_paths, ast_path, true)...)
		}
	}

	// 3. Final keys: prepend all prefixes to all postfixes
	// (if a key appears in multiple sections of file, the corresponding type has multiple paths to it in the graph)
	final_keys := []string{}
	for _, key_prefix := range key_prefixes {
		for _, key_postfix := range key_postfixes {
			final_key := strings.Trim(key_prefix+"."+key_postfix, ".")
			final_keys = append(final_keys, final_key)
			// TODO we sometimes find e.g. alerting.alertmanagers as a complete key even though it's not a leaf
			// (e.g. for ingress /discovery.Config)
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
				// TODO ignore for now
				fmt.Printf("Accessor leaf %v is not in Unmarshaler Subgraph - skipping\n", accessor_leaf)
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
	recvr_type := recvrType(method)

	recvr_hash, in_us := args.unmarshaler_subgraph.GetHash(ct.FullTypeName(recvr_type))
	// XXX If it's in the US, handle that.
	if in_us {
		panic(fmt.Errorf("Receiver %v is in Unmarshaler Subgraph - not handled yet", recvr_type))
	}

	recvr_hash, in_accessors := args.accessors.GetHash(ct.FullTypeName(recvr_type))
	if !in_accessors {
		// Shouldn't happen
		panic(fmt.Errorf("Receiver %v not in Accessors", recvr_type))
	}
	ingresses := UnmarshalerIngresses(args, recvr_hash)
	fmt.Printf("%v INGRESSES: %v\n", method, ingresses)
	param_keys := []string{}

	for _, ingress_hash := range ingresses {
		param_keys = append(param_keys, UnmarshalerIngressParams(args, ingress_hash)...)
	}

	// Sort and dedup for convenience
	slices.Sort(param_keys)
	param_keys = slices.Compact(param_keys)

	return param_keys
}

// The key and value of a config param that a CType has access to,
// via copy or alias.
type CTypeParam struct {
	Key   string
	Value api.Variable
}

func appendFieldTag(field string, tag string, key string) string {
	key_part := FieldToParamKey(field, tag)
	key = fmt.Sprintf("%v.%v", key, key_part)
	return strings.Trim(key, ".")
}

// Return true if field has no AST edges out on any ctype path.
// (Unless `all_ctype_paths` not passed)
func nonCustomField(field string, ctype_edge graph.Edge[ct.CTypeNode], all_ctype_paths [][]graph.Edge[ct.CTypeNode]) bool {
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

// Given an AST path (indexed by CType edge) and corresponding CType path,
// get the corresponding parameter key(s) from CType info.
// If `leaf_keys`: Append all of the last node's tags to the final key (won't have corresponding AST edges in given path).
// If `all_ctype_paths`: Also include non-custom fields.
// Assume the default behavior of UnmarshalYAML wrt mapping file keys to types.
func ASTPathToParams(ctype_path []graph.Edge[ct.CTypeNode], all_ctype_paths [][]graph.Edge[ct.CTypeNode],
	ast_path []dlvgraph.ASTPath, leaf_keys bool) []string {

	keys := []string{} // add when find a non-custom field or leaf
	key_prefix := ""   // append as add fields

	// Don't concatenate the ast_path since then we can't recover
	// the CType edge corresponding to an index (due to empty edge AST paths)
	for ctype_edge_i, edge_ast_path := range ast_path {
		// For each CType edge
		for _, ast_edge := range edge_ast_path {
			// For each AST edge on the CType edge
			if field, ok := strings.CutPrefix(ast_edge, golang.FIELD_NAME_PREFIX); ok {
				ctype_edge := ctype_path[ctype_edge_i]
				tags := ctype_edge.Source.Tags

				// Check for any non-custom fields - won't have an AST edge out, but are params if the node is a CType.
				for other_field := range tags {
					if other_field != field {
						if nonCustomField(other_field, ctype_edge, all_ctype_paths) {
							full_key := appendFieldTag(other_field, tags[other_field], key_prefix)
							keys = append(keys, full_key)
						}
					}
				}

				// Append corresponding field tag to key
				key_prefix = appendFieldTag(field, tags[field], key_prefix)
			}
		}
	}

	// Reached end of AST path => record key(s)
	last_node := ctype_path[len(ctype_path)-1].Target
	if !leaf_keys || len(last_node.Tags) == 0 {
		keys = append(keys, key_prefix)
	} else {
		for field, tag := range last_node.Tags {
			// Append tag to key
			full_key := appendFieldTag(field, tag, key_prefix)
			keys = append(keys, full_key)
		}
	}

	return keys
}

// Param key corresponding to struct field (tag key if tagged, else lowercase field name)
func FieldToParamKey(field string, tag string) string {
	param_key := ""

	// Get yaml tag key, if any
	// `(...) yaml:"[<key>][,<flag1>[,<flag2>]]" (...)`

	yaml_prefix := "yaml:\""
	yaml_idx := strings.Index(tag, yaml_prefix)
	if yaml_idx != -1 {
		key_idx := yaml_idx + len(yaml_prefix)
		end_tag_idx := strings.Index(tag[key_idx:], "\"")
		yaml_tag := tag[key_idx : key_idx+end_tag_idx]
		tag_parts := strings.Split(yaml_tag, ",")
		param_key = tag_parts[0]
		if param_key == "-" {
			param_key = ""
		}
	} else {
		// No yaml tag => take key as lowercased field name:
		// Field could either be a key in the raw content (iff field name is uppercase, and lowercased version is in raw content),
		// or copied/otherwise derived from the raw content after unmarshaling
		param_key = strings.ToLower(field)
	}
	return param_key
}
