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

	"github.com/dominikbraun/graph"
	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
	"golang.org/x/tools/gopls/internal/golang"
	"golang.org/x/tools/gopls/internal/protocol"
	"golang.org/x/tools/gopls/internal/server"
	"golang.org/x/tools/gopls/internal/telemetry"
	"golang.org/x/tools/internal/tool"
)

// conftamer implements the conftamer verb for gopls
type conftamer struct {
	app                     *Application
	ctx                     context.Context
	cli                     *client
	local_server            *server.Server
	unmarshaler_subgraph    *ct.CTypes
	accessors               *ct.CTypes
	log                     *slog.Logger
	UnmarshalDefn           string `flag:"u-defn,unmarshal_defn" help:"Location of the unmarshal interface definition (optional - defaults to DEFAULT_UNMARSHAL_DEFN)"`
	ModulePrefix            string `flag:"m,module_prefix" help:"module as in go.mod (used to pretty-print and possibly ignore unmarshaler subgraph nodes)"`
	UnmarshalerSubgraphFile string `flag:"u-out,unmarshaler_subgraph" help:"Unmarshaler subgraph outfile (mandatory)"`
	AccessorsFile           string `flag:"a-out,accessors" help:"Accessor subgraph outfile (optional - will find accessors iff passed)"`
}

func (c *conftamer) graph() *ct.CTypes {
	// Edit Unmarshaler Subgraph if Accessors isn't populated, else Accessors
	g := c.unmarshaler_subgraph
	if c.accessors != nil {
		g = c.accessors
	}

	return g
}

const (
	// TODO find definition of UnmarshalYAML properly
	DEFAULT_UNMARSHAL_DEFN = "/home/emily/go/pkg/mod/gopkg.in/yaml.v2@v2.4.0/yaml.go:32:6"
)

func (c *conftamer) Name() string      { return "conftamer" }
func (c *conftamer) Parent() string    { return c.app.Name() }
func (c *conftamer) Usage() string     { return "[conftamer-flags]" }
func (c *conftamer) ShortHelp() string { return "Finds the CTypes graph" }
func (c *conftamer) DetailedHelp(f *flag.FlagSet) {
	fmt.Fprint(f.Output(), `
	Find Unmarshaler Subgraph and/or Accessors.

	conftamer-flags:`)
	printFlagDefaults(f)
	fmt.Fprintf(f.Output(), `
	Default: %[1]v
`, DEFAULT_UNMARSHAL_DEFN) // unsure how to put this in the struct tag for the flag
}

// Get types that enclose this CType (which is defined at defn_locs)
func (c *conftamer) getParentCTypes(defn_locs []string) ([]golang.TypeInfo, error) {
	parent_ctypes := []golang.TypeInfo{}

	for _, defn_loc := range defn_locs {
		p, err := locStrToRefParams(c.ctx, defn_loc, c.cli, false)
		ct.CheckErr(err)

		// Check CType's references for other types
		_, enclosing_types, err := c.local_server.ReferencesMoreInfo(c.ctx, p)
		ct.CheckErr(err)

		parent_ctypes = append(parent_ctypes, enclosing_types...)
	}

	return parent_ctypes, nil
}

// Get types enclosed in this CType (which is defined at defn_locs)
func (c *conftamer) getChildCTypes(defn_locs []string) ([]golang.TypeInfo, error) {
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

func (c *conftamer) getInterfaceImpls(defn_locs []string) ([]golang.TypeInfo, error) {
	impl_ctypes := []golang.TypeInfo{}

	for _, defn_loc := range defn_locs {
		p, err := locStrToImplParams(c.ctx, defn_loc, c.cli)
		ct.CheckErr(err)

		implementations, err := c.local_server.ImplementationMoreInfo(c.ctx, p)
		ct.CheckErr(err)

		// Also returns the other unmarshal interfaces => ignore
		implementations = slices.DeleteFunc(implementations, func(a golang.TypeInfo) bool {
			return slices.Contains(UNMARSHAL_INTERFACES, ct.TypeName(a.TypeInfo))
		})
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

var UNMARSHAL_INTERFACES = []ct.FullTypeName{
	"gopkg.in/yaml.v3.obsoleteUnmarshaler",
	"sigs.k8s.io/yaml/goyaml.v2.Unmarshaler",
	"gopkg.in/yaml.v2.Unmarshaler",
}
var STRING_INTERFACES = []ct.FullTypeName{
	// String()
	"fmt.Stringer",
	"expvar.Var",
	"runtime.stringer",
	"context.stringer",
	"os/signal.stringer",
	"github.com/distribution/reference.Reference",
}

// Interfaces whose implementations we won't search for
var (
	IGNORED_INTERFACES = append(UNMARSHAL_INTERFACES, STRING_INTERFACES...)
)

// Add all CTypes reachable from this one via neigh_find, stopping on reaching one we've already found
// neigh_info is info about the neighbor we found this obj via (if any)
// defn_locs is of the obj (the 1-indexed format, which is what the gopls functions take but not what they return)
func (c *conftamer) addReachableCTypes(typ golang.TypeInfo, neigh_find NeighFind, neigh_info *ct.NeighInfo, depth int) error {
	// Ignore types not declared in package scope
	if typ.TypeInfo.Parent() == nil || typ.TypeInfo.Parent().Parent() != types.Universe {
		// e.g. function-local types, or `error`
		// Can cause TypeName to segfault - don't call it here
		graph.Logf(c.log, slog.LevelDebug, "Ignoring non-package-scope type %v", ct.TypeNameSafe(typ.TypeInfo))
		return nil
	}

	cur_name := ct.TypeName(typ.TypeInfo)

	if neigh_info != nil {
		// Ignore type if AST path to it has an excluded edge
		for _, ast_edge := range neigh_info.Typ.ASTPath {
			if slices.Contains(neigh_find.excluded_ast_edges, ast_edge) {
				return nil
			}
		}

		// Ignore type if found via an excluded source
		if slices.Contains(neigh_find.excluded_sources, neigh_info.Typ.TypeSource) {
			return nil
		}

		if neigh_find.ignore_unmarshaler_subnodes {
			// When finding initial edges from Unmarshaler Subgraph to Accessors, don't take edges to US nodes.
			// When finding edges from Accessors, stop if find one back into US - this wouldn't happen if
			// we found descendants in same way as ancestors, but we don't, hence this happens in a few cases:
			// - Edge from US => Accessor has an AST edge that US excludes
			// - Accessor finds a node the US doesn't, due to two things we may want to fix:
			// Embedded fields and discovery/xds.KumaSDConfig - TODO for both
			if _, ok := c.unmarshaler_subgraph.GetHash(cur_name); ok {
				if depth != 1 {
					graph.Logf(c.log, slog.LevelInfo, "Accessors would have edge out of Unmarshaler Subgraph: %v => %v\n", cur_name, neigh_info.Name)
				}
				return nil
			}
		}
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
	if neigh_find.iface_impls && !slices.Contains(IGNORED_INTERFACES, cur_name) {
		if _, is_iface := typ.TypeInfo.Type().Underlying().(*types.Interface); is_iface {
			iface_impls, err := c.getInterfaceImpls(defn_locs)
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

func (c *conftamer) LogGraphStats(start time.Time) {
	graph.Logf(c.log, slog.LevelInfo, "Begin stats")
	defer func() {
		graph.Logf(c.log, slog.LevelInfo, "End stats")
	}()

	// Time
	graph.Logf(c.log, slog.LevelInfo, "Total time: %v", time.Since(start))

	var gopls_time time.Duration
	for operation, time := range telemetry.GetLatencyTotals() {
		graph.Logf(c.log, slog.LevelInfo, "gopls %v: %v calls, %v", operation, time.NCalls, time.TotalTime)
		gopls_time += time.TotalTime
	}
	graph.Logf(c.log, slog.LevelInfo, "gopls total: %v", gopls_time)

	var graph_time time.Duration
	for operation, time := range c.graph().Latency {
		graph.Logf(c.log, slog.LevelInfo, "graph lib %v: %v calls, %v", operation, time.NCalls, time.TotalTime)
		graph_time += time.TotalTime
	}
	graph.Logf(c.log, slog.LevelInfo, "graph lib total: %v", graph_time)

	// Size
	n_edges, err := c.graph().Graph.Size()
	ct.CheckErr(err)
	n_nodes, err := c.graph().Graph.Order()
	ct.CheckErr(err)
	graph.Logf(c.log, slog.LevelInfo, "%v nodes, %v edges", n_nodes, n_edges)
	roots, leaves, err := graph.RootsLeaves(c.graph().Graph)
	ct.CheckErr(err)

	graph.Logf(c.log, slog.LevelInfo, "%v roots", len(roots))
	graph.Logf(c.log, slog.LevelInfo, "%v leaves", len(leaves))
}

func (c *conftamer) FindUnmarshalerSubgraph() {
	start := time.Now()
	c.unmarshaler_subgraph = ct.New(c.log)

	// 1. Find "Unmarshalers": Types that implement UnmarshalYAML
	// TODO also find all types passed as 2nd arg to yaml.Unmarshal - for any that don't impl Unmarshal, record their params
	graph.Logf(c.log, slog.LevelInfo, "Finding Unmarshalers: Types implementing UnmarshalYAML")
	unmarshalImpls, err := c.getInterfaceImpls([]string{c.UnmarshalDefn})
	ct.CheckErr(err)

	graph.Logf(c.log, slog.LevelInfo, "Finding rest of Unmarshaler Subgraph: Types contained in Unmarshalers")
	// 2. Find "Unmarshaler Subgraph": Descendants of Unmarshalers, via type definition and interface implementation.
	// Ignore descendant if AST path includes a function call (Unmarshal won't populate function arg/retval)
	for _, unmarshalImpl := range unmarshalImpls {
		graph.Logf(c.log, slog.LevelInfo, "Finding descendants of %v", ct.TypeName(unmarshalImpl.TypeInfo))

		unmarshaler_subgraph_find := NeighFind{children: true, parents: false, iface_impls: true,
			ignore_unmarshaler_subnodes: false,
			excluded_ast_edges:          []string{"FuncType.Params", "FuncType.Results"}}

		err = c.addReachableCTypes(unmarshalImpl, unmarshaler_subgraph_find, nil, 0)
		ct.CheckErr(err)
	}

	c.LogGraphStats(start)
	graph.Logf(c.log, slog.LevelInfo, "Serializing")
	c.graph().Serialize(c.UnmarshalerSubgraphFile, c.ModulePrefix, true)
	graph.Logf(c.log, slog.LevelInfo, "Serialize: %v", time.Since(start))
}

// Confirm a few things about the relationship between Accessors and Unmarshaler Subgraph
func (c *conftamer) CheckAccessors(unmarshaler_subnodes []ct.CTypeNode) {
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
func (c *conftamer) skipUnmarshalerSubnode(unmarshaler_subnode ct.CTypeNode) bool {
	module_repo := filepath.Dir(c.ModulePrefix)

	// Skip nodes not defined in the repo containing the module (e.g. github.com/prometheus)
	if _, ok := ct.IsModuleNode(ct.CTypeNodeHash(unmarshaler_subnode), module_repo, c.unmarshaler_subgraph.Graph); !ok {
		return true
	}
	// NOTE if we change this policy, may want to update key-finding accordingly
	return false
}

func (c *conftamer) FindAccessors() {
	start := time.Now()

	// 3. Find "Accessors": Ancestors of Unmarshaler Subgraph, via type definition only.
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

	c.LogGraphStats(start)
	graph.Logf(c.log, slog.LevelInfo, "Serializing")
	c.graph().Serialize(c.AccessorsFile, c.ModulePrefix, true)
	graph.Logf(c.log, slog.LevelInfo, "Serialize: %v", time.Since(start))

	c.CheckAccessors(unmarshaler_subnodes)
}

func (c *conftamer) Run(ctx context.Context, args ...string) error {
	if len(args) != 0 {
		return tool.CommandLineErrorf("conftamer expects no arguments (but flags are ok)")
	}
	if c.UnmarshalDefn == "" {
		c.UnmarshalDefn = DEFAULT_UNMARSHAL_DEFN
	}
	if c.ModulePrefix == "" {
		if _, trailing_slash := strings.CutSuffix(c.ModulePrefix, "/"); trailing_slash {
			// Quick validation - shouldn't have trailing slash
			// (leave it in, so we can tell which nodes had the module prefix after we cut it)
			return tool.CommandLineErrorf("module prefix should be from go.mod")
		}
		graph.Logf(c.log, slog.LevelWarn, "Module prefix not set")
	}
	if c.UnmarshalerSubgraphFile == "" {
		return tool.CommandLineErrorf("unmarshaler subgraph not set")
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

	c.FindUnmarshalerSubgraph()
	if c.AccessorsFile != "" {
		c.FindAccessors()
	}

	graph.Logf(c.log, slog.LevelInfo, "Exit CTypes finder")

	return nil
}
