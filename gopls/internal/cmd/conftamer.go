package cmd

import (
	"context"
	"flag"
	"fmt"
	"go/types"
	"log/slog"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"time"

	graph "github.com/emilykmarx/dominikbraun-graph"
	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
	parse "golang.org/x/tools/gopls/internal/cmd/conftamer/parse"
	"golang.org/x/tools/gopls/internal/golang"
	"golang.org/x/tools/gopls/internal/protocol"
	"golang.org/x/tools/gopls/internal/server"
	"golang.org/x/tools/internal/tool"
	"gopkg.in/yaml.v3"
)

// Conftamer implements the Conftamer verb for gopls
type Conftamer struct {
	app                  *Application
	ctx                  context.Context
	cli                  *client
	local_server         *server.Server
	unmarshaler_subgraph *ct.CTypes
	accessors            *ct.CTypes
	log                  *slog.Logger
	format               ct.Format
	scalar_like          map[ct.FullTypeName]bool // cache for scalarLike

	ConfigPath string `flag:"c,config" help:"YAML file specifying the config fields (alternative to passing them as flags)"`
	ConftamerConfig
}

// ConftamerConfig holds the Conftamer config fields, settable via flags or a YAML file
type ConftamerConfig struct {
	/* Flags about module code */
	UnmarshalFuncDefn  string `yaml:"unmarshal_fn" flag:"u-fn,unmarshal_fn" help:"Location of the unmarshal function definition (optional - if passed, will find unmarshalers passed to unmarshal calls)"`
	UnmarshalIfaceDefn string `yaml:"unmarshal_iface" flag:"u-iface,unmarshal_iface" help:"Location of the unmarshal interface definition (optional - if passed, will find unmarshalers that override unmarshal)"`

	/* Flags customizing the tool */
	// Flags for unmarshaler subgraph/accessors
	ShouldFindAccessors bool `yaml:"find_accessors" flag:"a,find_accessors" help:"Whether to find the accessors too (not just the unmarshaler subgraph)"`
	// Format of the config file, determining whose unmarshaling behavior to follow
	Format string `yaml:"format" flag:"format" help:"Config file format: yaml (follow gopkg.in/yaml.v2; default) or json (follow encoding/json; not yet supported)"`

	// Flags shared with the parser
	parse.ParserConfig `yaml:",inline"`
}

func (c *Conftamer) findingAccessors() bool {
	return c.accessors != nil
}

func (c *Conftamer) graph() *ct.CTypes {
	// Edit Unmarshaler Subgraph if Accessors isn't populated, else Accessors
	g := c.unmarshaler_subgraph
	if c.findingAccessors() {
		g = c.accessors
	}

	return g
}

func (c *Conftamer) Name() string      { return "conftamer" }
func (c *Conftamer) Parent() string    { return c.app.Name() }
func (c *Conftamer) Usage() string     { return "[conftamer-flags]" }
func (c *Conftamer) ShortHelp() string { return "Finds the CTypes graph" }
func (c *Conftamer) DetailedHelp(f *flag.FlagSet) {
	fmt.Fprint(f.Output(), `
	Find Unmarshaler Subgraph and/or Accessors.
	Strategy to find US defaults to struct tags, but if u-fn or u-iface is passed, will use that.

	conftamer-flags:`)
	printFlagDefaults(f)
}

// Get types that enclose this CType (which is defined at defn_locs)
func (c *Conftamer) getParentCTypes(defn_locs []string) ([]golang.TypeInfo, error) {
	parent_ctypes := []golang.TypeInfo{}

	for _, defn_loc := range defn_locs {
		p, err := locStrToRefParams(c.ctx, defn_loc, c.cli, false)
		ct.CheckErr(err)

		// Check CType's references for other types
		enclosing_types, err := c.local_server.ParentTypes(c.ctx, p)
		ct.CheckErr(err)

		parent_ctypes = append(parent_ctypes, enclosing_types...)
	}

	return parent_ctypes, nil
}

// Get types enclosed in this CType
func (c *Conftamer) getChildCTypes(defn_locs []string) ([]golang.TypeInfo, error) {
	child_ctypes := []golang.TypeInfo{}

	for _, defn_loc := range defn_locs {
		p, _, err := locStrToDefnParams(c.ctx, defn_loc, c.cli)
		ct.CheckErr(err)

		// Check CType's type definition for other types
		enclosed_types, err := c.local_server.EnclosedTypes(c.ctx, p)
		ct.CheckErr(err)
		child_ctypes = append(child_ctypes, enclosed_types...)
	}

	return child_ctypes, nil
}

func (c *Conftamer) getInterfaceImpls(defn_locs []string, ignore_ifaces bool) ([]golang.TypeInfo, error) {
	impl_ctypes := []golang.TypeInfo{}

	for _, defn_loc := range defn_locs {
		p, err := locStrToImplParams(c.ctx, defn_loc, c.cli)
		ct.CheckErr(err)

		implementations, err := c.local_server.ImplementationMoreInfo(c.ctx, p)
		ct.CheckErr(err)

		if ignore_ifaces {
			// Also returns interfaces that have the same method set as the query => ignore
			implementations = slices.DeleteFunc(implementations, func(impl golang.TypeInfo) bool {
				if _, is_iface := impl.TypeInfo.Type().Underlying().(*types.Interface); is_iface {
					return true
				}

				return false
			})
		}

		impl_ctypes = append(impl_ctypes, implementations...)
	}

	return impl_ctypes, nil
}

// Which neighbors to find
type NeighFind struct {
	children                    bool
	parents                     bool
	iface_impls                 bool
	ignore_unmarshaler_subnodes bool
	// If path to newly found type has one of these edges or sources, ignore the new type
	excluded_ast_edges []string
	excluded_sources   []golang.TypeSource
}

// Whether to ignore the CType based on its neighbor
func (c *Conftamer) ignoreCType(typ golang.TypeInfo, neigh_find NeighFind, neigh_info *ct.NeighInfo, depth int) bool {
	cur_name := ct.TypeName(typ.TypeInfo)

	if neigh_info != nil {
		neigh_hash, ok := c.graph().GetHash(neigh_info.Name)
		if !ok {
			err := fmt.Errorf("neighbor %v doesn't exist", neigh_hash)
			ct.CheckErr(err)
		}
		neigh_node, err := c.graph().Graph.Vertex(neigh_hash)
		if err != nil {
			err := fmt.Errorf("neighbor %v doesn't exist", neigh_hash)
			ct.CheckErr(err)
		}

		// Ignore type if AST path to it has an excluded edge
		for _, ast_edge := range neigh_info.Typ.ASTPath {
			if slices.Contains(neigh_find.excluded_ast_edges, ast_edge) {
				// excluded edge
				return true
			}
		}

		// For Unmarshaler Subgraph, ignore type if the parent field it's in isn't a key in the config file
		if !c.findingAccessors() && c.ignoreField(typ, neigh_node, neigh_hash, neigh_info.Typ.ASTPath) {
			return true
		}

		// Ignore type if found via an excluded source
		if slices.Contains(neigh_find.excluded_sources, neigh_info.Typ.TypeSource) {
			return true
		}

		if neigh_find.ignore_unmarshaler_subnodes {
			// When finding initial edges from Unmarshaler Subgraph to Accessors, don't take edges to US nodes.
			// When finding edges from Accessors, stop if find one back into US - this wouldn't happen if
			// we found descendants in same way as ancestors, but we don't, hence this happens in a few cases:
			// - Edge from US => Accessor has an AST edge that US excludes
			// - Accessor finds a node the US doesn't - this was due to two things that should now be fixed:
			// Embedded fields and discovery/xds.KumaSDConfig
			if _, ok := c.unmarshaler_subgraph.GetHash(cur_name); ok {
				if depth != 1 {
					graph.Logf(c.log, slog.LevelInfo, "Accessors would have edge out of Unmarshaler Subgraph: %v => %v\n", cur_name, neigh_info.Name)
				}
				return true
			}
		}
	}

	return false
}

// For a new node with a custom UnmarshalYAML, find what it decodes the config value into (see ct.ClassifyUnmarshaler)
func (c *Conftamer) classifyUnmarshaler(node *ct.CTypeNode) {
	if !node.YAMLUnmarshaler {
		return
	}
	calls, err := c.local_server.FuncParamCalls(c.ctx, node.GoplsInfo[0].Loc, "UnmarshalYAML", 0)
	if err != nil {
		graph.Logf(c.log, slog.LevelWarn, "Treating UnmarshalYAML of %v as dynamic: %v", ct.CTypeNodeHash(*node), err)
		calls = nil
	}
	ct.ClassifyUnmarshaler(node, calls)
	graph.Logf(c.log, slog.LevelInfo, "UnmarshalYAML of %v decodes into: %v (decoded fields %v)", ct.CTypeNodeHash(*node), node.UnmarshalKinds, ct.KeyFieldNames(*node))
}

// Whether config values of the type are scalars (see ct.CTypeNode.ScalarLike), as its node would be classified if added:
// from ct.CopyKeyInfo, or for types with a custom UnmarshalYAML, from classifyUnmarshaler
func (c *Conftamer) scalarLike(typ golang.TypeInfo) bool {
	name := ct.TypeName(typ.TypeInfo)
	if scalar, ok := c.scalar_like[name]; ok {
		return scalar
	}
	if _, exists := c.graph().GetHash(name); exists {
		return false // only non-scalar types are added
	}
	node := ct.CTypeNode{TypeInfo: typ.TypeInfo.Type(), Names: []ct.FullTypeName{name}, GoplsInfo: []golang.TypeInfo{typ}}
	ct.CopyKeyInfo(&node)
	c.classifyUnmarshaler(&node)
	if c.scalar_like == nil {
		c.scalar_like = map[ct.FullTypeName]bool{}
	}
	c.scalar_like[name] = node.ScalarLike
	return node.ScalarLike
}

// Whether the parent field(s) on the AST path from parent to child aren't keys in the config file,
// following the unmarshaling library for c.format (see ct.GetFieldKey). That includes our deliberate exception
// for untagged fields (see ct.FieldKey.Untagged; querygraph/configkeys makes the same exception).
// yaml:"-" fields aren't ignored: the parent's custom unmarshaler may populate them.
func (c *Conftamer) ignoreField(child golang.TypeInfo, parent ct.CTypeNode, parent_hash ct.CTypeHash, ast_path ct.ASTPath) bool {
	st := ct.IsStruct(parent.TypeInfo)
	if st == nil {
		// e.g. parent is interface, even if AST path has a field
		// (see Prometheus /discovery.Config => /discovery.DiscovererOptions)
		return false
	}
	child_name := ct.TypeName(child.TypeInfo)

	// Fields on the AST path: named fields have a Field: edge. Embedded fields have none
	// (ASTPath only adds field names), so find the embedded field whose type is the child.
	field_idxs := []int{}
	has_named := false
	for _, ast_edge := range ast_path {
		if name, ok := strings.CutPrefix(ast_edge, golang.FIELD_NAME_PREFIX); ok {
			has_named = true
			i := slices.IndexFunc(fieldIdxs(st), func(i int) bool { return st.Field(i).Name() == name })
			if i == -1 {
				// parent has fields, but no entry for this field - happens in grafana and k8s
				graph.Logf(c.log, slog.LevelWarn, "Child field %v not in parent struct: %v => %v", name, parent_hash, child_name)
				continue
			}
			field_idxs = append(field_idxs, i)
		}
	}
	if !has_named && len(ast_path) >= 2 && ast_path[0] == "StructType.Fields" && ast_path[1] == "FieldList.List" {
		for i := range st.NumFields() {
			// compare names, not objects: the same type can be a different *types.TypeName per importing package
			if tn := embeddedTypeName(st.Field(i).Type()); st.Field(i).Embedded() && tn != nil && ct.TypeNameSafe(tn) == ct.TypeNameSafe(child.TypeInfo) {
				field_idxs = append(field_idxs, i)
			}
		}
	}

	for _, i := range field_idxs {
		key, err := ct.GetFieldKey(ct.StructRawField(st, i), c.format)
		if err != nil {
			graph.Logf(c.log, slog.LevelWarn, "Not ignoring child: %v: %v => %v", err, parent_hash, child_name)
			continue
		}
		if key.Untagged || (key.Skip && !key.Dash) {
			graph.Logf(c.log, slog.LevelInfo, "Ignoring child since corresponding parent field %v isn't a key (%+v): %v => %v",
				st.Field(i).Name(), key, parent_hash, child_name)
			return true
		}
	}
	return false
}

func fieldIdxs(st *types.Struct) []int {
	idxs := make([]int, st.NumFields())
	for i := range idxs {
		idxs[i] = i
	}
	return idxs
}

// Type name of an embedded field's type (T or *T)
func embeddedTypeName(typ types.Type) *types.TypeName {
	if ptr, ok := types.Unalias(typ).(*types.Pointer); ok {
		typ = ptr.Elem()
	}
	if named, ok := types.Unalias(typ).(*types.Named); ok {
		return named.Obj()
	}
	return nil
}

// Add all CTypes reachable from this one via neigh_find, stopping on reaching one we've already found
// neigh_info is info about the neighbor we found this obj via (if any)
func (c *Conftamer) addReachableCTypes(typ golang.TypeInfo, neigh_find NeighFind, neigh_info *ct.NeighInfo, depth int) error {
	if ct.BasicType(typ) {
		graph.Logf(c.log, slog.LevelDebug, "Ignoring non-package-scope type %v", ct.TypeNameSafe(typ.TypeInfo))
		return nil
	}

	cur_name := ct.TypeName(typ.TypeInfo)

	if c.ignoreCType(typ, neigh_find, neigh_info, depth) {
		return nil
	}

	// For the Unmarshaler Subgraph, don't add types whose config values are scalars (e.g. a custom unmarshaler decodes
	// a string): no config keys are at or below them, so they're values of their parents' fields, not CTypes
	if !c.findingAccessors() && c.scalarLike(typ) {
		graph.Logf(c.log, slog.LevelInfo, "Not adding scalar-like type %v", cur_name)
		return nil
	}

	// 1. Add the CType to the graph, combining with neighbor node if they're the same type.
	graph.Logf(c.log, slog.LevelDebug, "ADD CTYPE %v", cur_name)

	existed, err := c.graph().AddCType(typ, neigh_info)
	ct.CheckErr(err)

	// 2. Add edge to neighbor we found obj via, if we didn't combine it with the neighbor -
	// even if we had already added the node for obj (need edge for all of obj's neighbors)
	if neigh_info != nil {
		neigh_hash, ok := c.graph().GetHash(neigh_info.Name)
		if !ok {
			err := fmt.Errorf("neighbor %v doesn't exist", neigh_hash)
			ct.CheckErr(err)
		}
		own_hash, ok := c.graph().GetHash(cur_name)
		if !ok {
			err = fmt.Errorf("cur node %v doesn't exist", own_hash)
			ct.CheckErr(err)
		}
		if neigh_hash != own_hash {
			// didn't combine
			parent_hash := neigh_hash
			child_name := cur_name
			if neigh_info.Age == ct.NeighIsChild {
				parent_hash = own_hash
				child_name = neigh_info.Name
			}
			// Need parent's type info and child's type name =>
			// pass HASH of parent and NAME of child
			err = c.graph().AddCTypeEdge(parent_hash, child_name, neigh_info.Typ.ASTPath)
			ct.CheckErr(err)
		}
	}

	// Stop recursing if had already added this node,
	// now that we've handled what we needed to (combining and adding edges)
	if existed == ct.TypeNameExists {
		return nil
	}

	// 3. Find new neighbors (direct parents and/or children), and nodes reachable from them -
	// i.e. find enclosing (parent) CTypes, and/or enclosed (child) CTypes, then recurse on them
	defn_locs, err := locsToSpans(c.ctx, c.cli, []protocol.Location{typ.Loc})
	ct.CheckErr(err)

	// Parents
	if neigh_find.parents {
		parents, err := c.getParentCTypes(defn_locs)
		ct.CheckErr(err)
		graph.Logf(c.log, slog.LevelDebug, "PARENTS: %+v", parents)

		for _, new := range parents {
			// cur is now neigh => if found a parent, pass child as relation
			neigh_info := ct.NeighInfo{Name: cur_name, Age: ct.NeighIsChild, Typ: new}
			err = c.addReachableCTypes(new, neigh_find, &neigh_info, depth+1)
			ct.CheckErr(err)
		}
	}

	// Children via type definitions
	if neigh_find.children {
		children, err := c.getChildCTypes(defn_locs)
		ct.CheckErr(err)
		graph.Logf(c.log, slog.LevelDebug, "CHILDREN: %+v", children)

		for _, new := range children {
			neigh_info := ct.NeighInfo{Name: cur_name, Age: ct.NeighIsParent, Typ: new}
			err = c.addReachableCTypes(new, neigh_find, &neigh_info, depth+1)
			ct.CheckErr(err)
		}
	}

	// Children via interface implementations
	if neigh_find.iface_impls {
		if _, is_iface := typ.TypeInfo.Type().Underlying().(*types.Interface); is_iface {
			iface_impls, err := c.getInterfaceImpls(defn_locs, false)
			ct.CheckErr(err)

			for _, new := range iface_impls {
				neigh_info := ct.NeighInfo{Name: cur_name, Age: ct.NeighIsParent, Typ: new}
				err = c.addReachableCTypes(new, neigh_find, &neigh_info, depth+1)
				ct.CheckErr(err)
			}
		}
	}

	return nil
}

func (c *Conftamer) locInTest(loc protocol.Location, log bool) bool {
	// Assume if Unmarshal call is in a file whose path or filename contains "test", the call is during a test
	if strings.Contains(loc.URI.Path(), "test") {
		if strings.HasSuffix(loc.URI.Base(), "_test.go") {
			// Definitely a test file
		} else {
			// Probably a test file
			if log {
				graph.Logf(c.log, slog.LevelInfo, "Ignoring Unmarshal call assumed to be in test: 0-indexed call loc %v", loc)
			}
		}
		return true
	}
	// Not a test file
	return false
}

// Type was only passed to Unmarshal() during tests, if type was found that way
func (c *Conftamer) testOnlyUnmarshal(unmarshaler golang.TypeInfo) bool {
	if len(unmarshaler.UnmarshalLocs) == 0 {
		// Type not found via call to Unmarshal()
		return false
	}

	for _, unmarshal_loc := range unmarshaler.UnmarshalLocs {
		if !c.locInTest(unmarshal_loc, true) {
			return false
		}
	}
	return true
}

func (c *Conftamer) FindUnmarshalers() []golang.TypeInfo {
	log := "Finding Unmarshalers: Types likely populated by Unmarshal (via "

	unmarshalers := []golang.TypeInfo{}
	var err error

	if c.UnmarshalFuncDefn != "" {
		// Strategy: Type is passed to an Unmarshal call, and can be inferred from the calling line
		p, err := locStrToRefParams(c.ctx, c.UnmarshalFuncDefn, c.cli, false)
		ct.CheckErr(err)
		unmarshalers, err = c.local_server.FuncArgType(c.ctx, p)
		ct.CheckErr(err)
		log += "Unmarshal calls"
	} else if c.UnmarshalIfaceDefn != "" {
		// Strategy: Type implements the Unmarshal interface
		unmarshalers, err = c.getInterfaceImpls([]string{c.UnmarshalIfaceDefn}, true)
		ct.CheckErr(err)
		log += "Unmarshal interface"
	} else {
		// Strategy: Type has struct tags
		// A struct counts as tagged if it has a field that's a key for the config format
		// (e.g. a struct whose only tags are json tags isn't tagged for yaml)
		unmarshalers, err = c.local_server.TaggedTypes(c.ctx, func(st *types.Struct, i int) bool {
			key, err := ct.GetFieldKey(ct.StructRawField(st, i), c.format)
			return err == nil && key.IsKey()
		})
		ct.CheckErr(err)
		log += "struct tags"
	}

	// Exclude the specified types, if any (useful to see which types each strategy contributes)
	graph.Logf(c.log, slog.LevelInfo, log+")")

	return unmarshalers
}

// Find Unmarshalers and their descendants
func (c *Conftamer) FindUnmarshalerSubgraph() {
	start := time.Now()
	c.unmarshaler_subgraph = ct.New(c.log)
	c.unmarshaler_subgraph.Format = c.format
	c.unmarshaler_subgraph.KeyInfoHook = c.classifyUnmarshaler

	// 1. Find "Unmarshalers" by configured strategy
	unmarshalers := c.FindUnmarshalers()

	// 2. Find "Unmarshaler Subgraph": Descendants of Unmarshalers, via type definition and interface implementation.
	graph.Logf(c.log, slog.LevelInfo, "Finding rest of Unmarshaler Subgraph: Types contained in Unmarshalers")
	for _, unmarshaler := range unmarshalers {
		if c.testOnlyUnmarshal(unmarshaler) {
			// Unmarshal was only called on this type during tests => ignore
		} else if unmarshaler.TypeSource == golang.TypeSourceError {
			// Failed to find type that Unmarshal was called on outside tests => warn about all non-test calls
			// (all calls where type not found are grouped together)
			for _, unmarshal_loc := range unmarshaler.UnmarshalLocs {
				if !c.locInTest(unmarshal_loc, false) {
					graph.Logf(c.log, slog.LevelWarn, "Type passed to Unmarshal call not found: 0-indexed call loc %v", unmarshal_loc)
				}
			}
		} else {
			graph.Logf(c.log, slog.LevelInfo, "Finding descendants of Unmarshaler %v (0-indexed call locs %v)",
				ct.TypeName(unmarshaler.TypeInfo), unmarshaler.UnmarshalLocs)

			unmarshaler_subgraph_find := NeighFind{children: true, parents: false, iface_impls: true,
				ignore_unmarshaler_subnodes: false,
				// Ignore descendant if AST path includes a function call (Unmarshal won't populate function arg/retval)
				excluded_ast_edges: []string{"FuncType.Params", "FuncType.Results"}}

			err := c.addReachableCTypes(unmarshaler, unmarshaler_subgraph_find, nil, 0)
			ct.CheckErr(err)
		}
	}

	c.unmarshaler_subgraph.LogGraphStats(c.log, start)
	graph.Logf(c.log, slog.LevelInfo, "Serializing")
	c.graph().Serialize(filepath.Join(c.OutputPath, "unmarshaler_subgraph.text"), c.ModulePrefix, true)
	graph.Logf(c.log, slog.LevelInfo, "Serialize: %v", time.Since(start))
}

// Confirm a few things about the relationship between Accessors and Unmarshaler Subgraph
func (c *Conftamer) CheckAccessors(unmarshaler_subnodes []ct.CTypeNode) {
	adjacencyMap, err := c.graph().Graph.AdjacencyMap()
	ct.CheckErr(err)

	// 1. Every node in US is also in A, but as leaf
	for _, unmarshaler_subnode := range unmarshaler_subnodes {
		accessor_hash, in_us := c.graph().GetHash(ct.FullTypeName(ct.CTypeNodeHash(unmarshaler_subnode)))
		if !in_us {
			if !c.skipUnmarshalerSubnode(unmarshaler_subnode) {
				// not ignored
				graph.Logf(c.log, slog.LevelError, "Unmarshaler subnode %v is not in accessors\n", unmarshaler_subnode)
			}
		}

		outEdges := adjacencyMap[accessor_hash]
		if len(outEdges) > 0 {
			graph.Logf(c.log, slog.LevelError, "Unmarshaler subnode %v is in accessors, but not as leaf - has out edges:\n", unmarshaler_subnode)
		}
		for _, edge := range outEdges {
			graph.Logf(c.log, slog.LevelError, "=> %v", edge.Target)
		}
	}

	// 2. Every A leaf is in US, and every A non-leaf is not in US
	for accessor_hash, outEdges := range adjacencyMap {
		_, in_us := c.unmarshaler_subgraph.GetHash(ct.FullTypeName(accessor_hash))
		if len(outEdges) == 0 {
			// leaf
			if !in_us {
				graph.Logf(c.log, slog.LevelError, "Accessor leaf %v is not in Unmarshaler Subgraph\n", accessor_hash)
			}
		} else {
			// non-leaf
			if in_us {
				graph.Logf(c.log, slog.LevelError, "Accessor non-leaf %v IS in US\n", accessor_hash)
			}
		}
	}
}

// Whether to skip unmarshaler subnode when finding accessors
func (c *Conftamer) skipUnmarshalerSubnode(unmarshaler_subnode ct.CTypeNode) bool {
	module_repo := filepath.Dir(c.ModulePrefix)

	// Skip nodes with any name not defined in the repo containing the module (e.g. github.com/prometheus)
	for _, name := range unmarshaler_subnode.Names {
		if _, in_repo := strings.CutPrefix(string(name), module_repo); !in_repo {
			return true
		}
	}
	// NOTE if we change this policy, may want to update key-finding accordingly
	return false
}

func (c *Conftamer) FindAccessors() {
	start := time.Now()

	// 3. Find "Accessors": Ancestors of Unmarshaler Subgraph, via type definition and configurable rules.
	// Each leaf is a copy of the ingress node in the Unmarshaler Subgraph (generally - see CheckAccessors) -
	// the rest of the path is outside the Unmarshaler Subgraph.

	graph.Logf(c.log, slog.LevelInfo, "Finding Accessors: Types containing types in Unmarshaler Subgraph")
	// Wait to create until now, since other functions switch from editing unmarshaler subgraph to accessors once it's created
	c.accessors = ct.New(c.log)
	c.accessors.Format = c.format

	unmarshaler_subnodes, err := c.unmarshaler_subgraph.Graph.Vertices()
	ct.CheckErr(err)

	for _, unmarshaler_subnode := range unmarshaler_subnodes {
		if c.skipUnmarshalerSubnode(unmarshaler_subnode) {
			graph.Logf(c.log, slog.LevelInfo, "Skipping ancestors of %v", ct.CTypeNodeHash(unmarshaler_subnode))
			continue
		}

		graph.Logf(c.log, slog.LevelInfo, "Finding ancestors of %v", ct.CTypeNodeHash(unmarshaler_subnode))
		accessor_find := NeighFind{children: false, parents: true, iface_impls: false,
			ignore_unmarshaler_subnodes: true,
			excluded_ast_edges:          []string{},
			excluded_sources:            []golang.TypeSource{golang.ArgToRet}}

		for _, gopls_info := range unmarshaler_subnode.GoplsInfo {
			err = c.addReachableCTypes(gopls_info, accessor_find, nil, 0)
			ct.CheckErr(err)
		}
	}

	c.accessors.LogGraphStats(c.log, start)
	graph.Logf(c.log, slog.LevelInfo, "Serializing")
	c.graph().Serialize(filepath.Join(c.OutputPath, "accessors.text"), c.ModulePrefix, true)
	graph.Logf(c.log, slog.LevelInfo, "Serialize: %v", time.Since(start))

	c.CheckAccessors(unmarshaler_subnodes)
}

func (c *Conftamer) FindSendingTypes() {
	parser := parse.Parser{Server: c.local_server, Log: c.log, ParserConfig: c.ParserConfig}
	parse.ParseStacksLog(parser)
}

// Populate ConftamerConfig from the YAML file at ConfigPath
func (c *Conftamer) loadConfig() error {
	f, err := os.Open(c.ConfigPath)
	if err != nil {
		return fmt.Errorf("opening config file: %v", err)
	}
	defer f.Close()

	dec := yaml.NewDecoder(f)
	// Catch typos in field names
	dec.KnownFields(true)
	if err := dec.Decode(&c.ConftamerConfig); err != nil {
		return fmt.Errorf("parsing config file %v: %v", c.ConfigPath, err)
	}
	return nil
}

func (c *Conftamer) Run(ctx context.Context, args ...string) error {
	if len(args) != 0 {
		return tool.CommandLineErrorf("conftamer expects no arguments (but flags are ok)")
	}
	if c.ConfigPath != "" {
		if !reflect.ValueOf(c.ConftamerConfig).IsZero() {
			return tool.CommandLineErrorf("Specify config via either a config file or flags, not both")
		}
		if err := c.loadConfig(); err != nil {
			return err
		}
	}
	if c.Format == "" {
		c.Format = string(ct.YAML)
	}
	format, err := ct.ParseFormat(c.Format)
	if err != nil {
		return tool.CommandLineErrorf("%v", err)
	}
	c.format = format
	if c.UnmarshalFuncDefn != "" && c.UnmarshalIfaceDefn != "" {
		return tool.CommandLineErrorf("Specify neither or one flag about unmarshal")
	}
	if c.ModulePrefix == "" {
		if _, trailing_slash := strings.CutSuffix(c.ModulePrefix, "/"); trailing_slash {
			// Quick validation - shouldn't have trailing slash
			// (leave it in, so we can tell which nodes had the module prefix after we cut it)
			return tool.CommandLineErrorf("module prefix should be from go.mod")
		}
		graph.Logf(c.log, slog.LevelWarn, "Module prefix not set")
	}
	if c.OutputPath == "" {
		return tool.CommandLineErrorf("output dir not set")
	}

	cli, _, err := c.app.connect(ctx)
	if err != nil {
		return err
	}
	defer cli.terminate(ctx)
	c.ctx = ctx
	c.cli = cli
	c.local_server = cli.server.(*server.Server)
	c.log = slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{AddSource: true, Level: slog.LevelInfo,
		// Shorten paths
		ReplaceAttr: func(groups []string, a slog.Attr) slog.Attr {
			if a.Key == slog.SourceKey {
				source, _ := a.Value.Any().(*slog.Source)
				if source != nil {
					source.File = filepath.Base(source.File)
				}
			}
			return a
		}}))

	if c.AncestryLog != "" {
		c.FindSendingTypes()
	} else {
		// Find unmarshaler subgraph, and optionally accessors
		c.FindUnmarshalerSubgraph()
		if c.ShouldFindAccessors {
			c.FindAccessors()
		}
	}

	graph.Logf(c.log, slog.LevelInfo, "Exit CTypes finder")

	return nil
}
