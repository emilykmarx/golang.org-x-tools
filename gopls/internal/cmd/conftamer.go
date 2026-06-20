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
	app           *Application
	ctx           context.Context
	cli           *client
	local_server  *server.Server
	ctypes        *ct.CTypes
	log           *slog.Logger
	UnmarshalDefn string `flag:"u,unmarshal_defn" help:"Location of the unmarshal interface definition"`
	ModulePrefix  string `flag:"m,module_prefix" help:"module as in go.mod (used to pretty-print)"`
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
	children    bool
	parents     bool
	iface_impls bool
	// If path to newly found type has one of these edges, ignore the new type
	excluded_ast_edges []string
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
func (c *conftamer) addReachableCTypes(typ golang.TypeInfo, neigh_find NeighFind, neigh_info *ct.NeighInfo) error {
	// Ignore types not declared in package scope
	if typ.TypeInfo.Parent() == nil || typ.TypeInfo.Parent().Parent() != types.Universe {
		// e.g. function-local types, or `error`
		// Can cause TypeName to segfault - don't call it here
		graph.Logf(c.log, slog.LevelDebug, "Ignoring non-package-scope type %v", ct.TypeNameSafe(typ.TypeInfo))
		return nil
	}

	// Ignore type if AST path to it has an excluded edge
	if neigh_info != nil {
		for _, ast_edge := range neigh_info.Ast_path {
			if slices.Contains(neigh_find.excluded_ast_edges, ast_edge) {
				return nil
			}
		}
	}

	cur_name := ct.TypeName(typ.TypeInfo)

	// 1. Add the CType to the graph, combining with neighbor node if they're the same type.
	graph.Logf(c.log, slog.LevelDebug, "ADD CTYPE %v", cur_name)

	existed, err := c.ctypes.AddCType(typ, neigh_info)
	ct.CheckErr(err)

	// 2. Add edge to neighbor we found obj via, if we didn't combine it with the neighbor -
	// even if we had already added the node for obj (need edge for all of obj's neighbors)
	if neigh_info != nil {
		neigh_hash, ok := c.ctypes.GetHash(neigh_info.Name)
		if !ok {
			err := fmt.Errorf("neighbor %v doesn't exist", neigh_hash)
			ct.CheckErr(err)
		}
		own_hash, ok := c.ctypes.GetHash(cur_name)
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
			err = c.ctypes.AddCTypeEdge(parent_hash, child_name, neigh_info.Ast_path)
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
			neigh_info := ct.NeighInfo{Name: cur_name, Age: ct.NeighIsChild, Ast_path: new.ASTPath}
			err = c.addReachableCTypes(new, neigh_find, &neigh_info)
			ct.CheckErr(err)
		}
	}

	// Children via type definitions
	if neigh_find.children {
		children, err := c.getChildCTypes(defn_locs)
		ct.CheckErr(err)
		graph.Logf(c.log, slog.LevelDebug, "CHILDREN: %+v", children)

		for _, new := range children {
			neigh_info := ct.NeighInfo{Name: cur_name, Age: ct.NeighIsParent, Ast_path: new.ASTPath}
			err = c.addReachableCTypes(new, neigh_find, &neigh_info)
			ct.CheckErr(err)
		}
	}

	// Children via interface implementations
	if neigh_find.iface_impls && !slices.Contains(IGNORED_INTERFACES, cur_name) {
		if _, is_iface := typ.TypeInfo.Type().Underlying().(*types.Interface); is_iface {
			iface_impls, err := c.getInterfaceImpls(defn_locs)
			ct.CheckErr(err)

			for _, new := range iface_impls {
				neigh_info := ct.NeighInfo{Name: cur_name, Age: ct.NeighIsParent, Ast_path: new.ASTPath}
				err = c.addReachableCTypes(new, neigh_find, &neigh_info)
				ct.CheckErr(err)
			}
		}
	}

	return nil
}

func (c *conftamer) LogGraphStats(start time.Time) {
	graph.Logf(c.log, slog.LevelInfo, "Begin graph stats")
	defer func() {
		graph.Logf(c.log, slog.LevelInfo, "End graph stats")
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
	for operation, time := range c.ctypes.Latency {
		graph.Logf(c.log, slog.LevelInfo, "graph %v: %v calls, %v", operation, time.NCalls, time.TotalTime)
		graph_time += time.TotalTime
	}
	graph.Logf(c.log, slog.LevelInfo, "graph total: %v", graph_time)

	// Size
	n_edges, err := c.ctypes.Graph.Size()
	ct.CheckErr(err)
	n_nodes, err := c.ctypes.Graph.Order()
	ct.CheckErr(err)
	graph.Logf(c.log, slog.LevelInfo, "%v nodes, %v edges", n_nodes, n_edges)
	roots, leaves, err := graph.RootsLeaves(c.ctypes.Graph)
	ct.CheckErr(err)

	graph.Logf(c.log, slog.LevelInfo, "%v roots", len(roots))
	graph.Logf(c.log, slog.LevelInfo, "%v leaves", len(leaves))
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

	cli, _, err := c.app.connect(ctx)
	if err != nil {
		return err
	}
	defer cli.terminate(ctx)
	c.ctx = ctx
	c.cli = cli
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
	c.ctypes = ct.New(c.log)

	start := time.Now()
	graph.Logf(c.log, slog.LevelInfo, "Start CTypes finder")

	// 1. Find "Unmarshalers": Types that implement UnmarshalYAML
	// TODO also find all types passed as 2nd arg to yaml.Unmarshal - for any that don't impl Unmarshal, record their params

	c.local_server = cli.server.(*server.Server)
	graph.Logf(c.log, slog.LevelInfo, "Finding types implementing UnmarshalYAML")
	unmarshalImpls, err := c.getInterfaceImpls([]string{c.UnmarshalDefn})
	ct.CheckErr(err)

	// 2. Find "Unmarshaler Subgraph": Descendants of Unmarshalers, via type definition and interface implementation.
	// Ignore descendant if AST path includes a function call (Unmarshal won't populate function arg/retval)
	for _, unmarshalImpl := range unmarshalImpls {
		graph.Logf(c.log, slog.LevelInfo, "Finding descendants of %v", ct.TypeName(unmarshalImpl.TypeInfo))

		unmarshaler_subgraph_find := NeighFind{children: true, parents: false, iface_impls: true,
			excluded_ast_edges: []string{"FuncType.Params", "FuncType.Results"}}

		err = c.addReachableCTypes(unmarshalImpl, unmarshaler_subgraph_find, nil)
		ct.CheckErr(err)
	}

	c.LogGraphStats(start)
	start = time.Now()
	graph.Logf(c.log, slog.LevelInfo, "Serializing Unmarshaler Subgraph")
	c.ctypes.Serialize("graph.text", c.ModulePrefix)
	graph.Logf(c.log, slog.LevelInfo, "Serialize: %v", time.Since(start))

	graph.Logf(c.log, slog.LevelInfo, "Exit CTypes finder")

	return nil
}
