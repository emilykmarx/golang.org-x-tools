package cmd

import (
	"context"
	"flag"
	"fmt"
	"go/types"
	"log/slog"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"

	graph "github.com/emilykmarx/dominikbraun-graph"
	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
	parse "golang.org/x/tools/gopls/internal/cmd/conftamer/stacks"
	"golang.org/x/tools/gopls/internal/golang"
	"golang.org/x/tools/gopls/internal/protocol"
	"golang.org/x/tools/gopls/internal/server"
	"golang.org/x/tools/internal/tool"
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

	// Flags about module code
	ModulePrefix       string `flag:"m,module_prefix" help:"module as in go.mod (used to pretty-print and possibly ignore unmarshaler subgraph nodes)"`
	UnmarshalFuncDefn  string `flag:"u-fn,unmarshal_fn" help:"Location of the unmarshal function definition (optional - if passed, will find unmarshalers passed to unmarshal calls)"`
	UnmarshalIfaceDefn string `flag:"u-iface,unmarshal_iface" help:"Location of the unmarshal interface definition (optional - if passed, will find unmarshalers that override unmarshal)"`

	// Flags customizing the tool
	OutputPath          string `flag:"out,output_path" help:"Output path for graph files"`
	ShouldFindAccessors bool   `flag:"a,find_accessors" help:"Whether to find the accessors too (not just the unmarshaler subgraph)"`
	SendLog             string `flag:"s,send_log" help:"A log of message sends - if passed, will find sending types from the log (rather than finding the Unmarshaler Subgraph and Accessors from the module source)"`
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

		// Ignore type if AST path to it has an excluded edge, or
		// for Unmarshaler Subgraph an untagged field
		for _, ast_edge := range neigh_info.Typ.ASTPath {
			if slices.Contains(neigh_find.excluded_ast_edges, ast_edge) {
				// excluded edge
				return true
			}
			if !c.findingAccessors() && len(neigh_node.Tags) > 0 {
				// neigh_node.Tags may be empty when AST path still has a field, e.g. when parent is interface
				// (see Prometheus /discovery.Config => /discovery.DiscovererOptions)
				if field, ok := strings.CutPrefix(ast_edge, golang.FIELD_NAME_PREFIX); ok {
					tag, ok := neigh_node.Tags[field]
					if !ok {
						// parent has fields, but no entry for this field - happens in grafana and k8s
						graph.Logf(c.log, slog.LevelWarn, "Child field %v not in tags %v: %v => %v",
							field, neigh_node.Tags, neigh_hash, cur_name)
					} else if tag == "" {
						graph.Logf(c.log, slog.LevelInfo, "Ignoring child since corresponding parent field is untagged: %v => %v", neigh_hash, cur_name)
						// untagged field => ignore type
						return true
					} else {
						// tagged field => don't ignore type
					}
				}
			}
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
			// - Accessor finds a node the US doesn't, due to two things we may want to fix:
			// Embedded fields and discovery/xds.KumaSDConfig - TODO(CT) for both
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
		unmarshalers, err = c.local_server.TaggedTypes(c.ctx)
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
	parse.ParseStacksLog(c.ModulePrefix, c.log, c.SendLog, c.OutputPath, c.local_server)
}

func (c *Conftamer) Run(ctx context.Context, args ...string) error {
	if len(args) != 0 {
		return tool.CommandLineErrorf("conftamer expects no arguments (but flags are ok)")
	}
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

	if c.SendLog != "" {
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
