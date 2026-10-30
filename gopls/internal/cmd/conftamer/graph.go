package conftamer

import (
	"cmp"
	"errors"
	"fmt"
	"go/types"
	"log/slog"
	"reflect"
	"slices"
	"strings"
	"time"

	graph "github.com/emilykmarx/dominikbraun-graph"
	"golang.org/x/tools/gopls/internal/golang"
	"golang.org/x/tools/gopls/internal/telemetry"
)

/*
 * Data structures holding CTypes.
 */

type CTypes struct {
	Graph   CTypeGraph
	List    CTypeList
	Latency map[string]telemetry.LatencyTotal // operation => timing
	// If set, called on each new node after its key info is populated (e.g. to classify custom unmarshalers)
	KeyInfoHook func(node *CTypeNode)
	// Config file format whose unmarshaling library determines edges' key segments (for DOT labels); "" means YAML
	Format Format
}

type CTypeGraph graph.Graph[CTypeHash, CTypeNode]

// Makes lookup less confusing
type CTypeHash FullTypeName

// Indicates what the string is
type FullTypeName string

// CType name => hash of node it's part of.
// Needed because a type can have multiple names (via `type X Y`), but a node can only have one hash
// (and having multiple nodes corresponding to the same type is a hassle)
type CTypeList map[FullTypeName]CTypeHash

type FieldInfo struct {
	// Field name in source code
	Field string
	// Inferred key in config file, for the section or parameter corresponding to field
	Tag string
}

var UNKNOWNFIELD = FieldInfo{Field: "<unknown>", Tag: "<unknown>"}

// NOTE to look up a type name in the CTypeGraph, first find its hash in the CTypeList
func (c *CTypes) GetHash(type_name FullTypeName) (CTypeHash, bool) {
	hash, ok := c.List[type_name]
	return hash, ok
}

func (c *CTypes) SetHash(type_name FullTypeName, hash CTypeHash) {
	c.List[type_name] = hash
}

type CTypeNode struct {
	// Package-qualified type name. Alphabetically ascending. Possibly multiple due to `type X Y`
	Names []FullTypeName

	// Info about the type
	TypeInfo types.Type

	// All the info gopls returned (multiple for `type X Y`)
	// (contains TypeInfo, but copied here to finding initial Accessors easier)
	GoplsInfo []golang.TypeInfo

	// (these are also in TypeInfo, but copied here to make [un]marshaling easier)
	Methods []FullTypeName
	Tags    map[string]string // Field name => tag or "" (populated if struct)
	// Whether the type, or each of its fields, is a map (after stripping pointers/slices/arrays) -
	// needed to match map keys when the map's value type isn't a CType (so there's no edge with MapType.Value)
	IsMap          bool
	MapFields      map[string]bool // Field name => true if field is a map (populated if struct)
	EmbeddedFields map[string]bool // Field name => true if field is embedded (populated if struct)
	// Whether *T has a custom unmarshaler that the library calls instead of its default rules (see fieldkeys.go)
	YAMLUnmarshaler bool // gopkg.in/yaml.v2 Unmarshaler
	TextUnmarshaler bool // encoding.TextUnmarshaler (used by go-yaml for scalars)
	// For YAMLUnmarshaler: what it decodes the config value into (see ClassifyUnmarshaler)
	UnmarshalKinds []UnmarshalKind
	DecodedFields  map[string]DecodedField // Field name => field, if it decodes into another struct
	ScalarLike     bool                    // config values of this type are scalars (no keys below it)
	// Kind of the values stored in the type and in each of its fields (if struct), for leaves of the document
	Kind       ValueKind
	FieldKinds map[string]ValueKind

	// Parameters this CType can access, and via which fields
	Stored_down  map[Stored]struct{} // becomes irrelevant once entire push down pass is done
	Stored_up    map[Stored]struct{}
	Stored_final map[Stored]struct{}

	Indent int
}

type ASTPath []string

func NodeSort(a, b CTypeNode) int {
	return cmp.Compare(string(CTypeNodeHash(a)), string(CTypeNodeHash(b)))
}
func NodeEqual(a, b CTypeNode) bool {
	return CTypeNodeHash(a) == CTypeNodeHash(b)
}

// Accumulating info about a param a node has access to
type Stored struct {
	Path      string // can't put slice in map => separate by ,
	FieldInfo FieldInfo
}

func CTypeNodeHash(n CTypeNode) CTypeHash {
	// Node is uniquely identified by first name
	return CTypeHash(n.Names[0])
}

func New(log *slog.Logger) *CTypes {
	g := graph.New(CTypeNodeHash, log, graph.Directed())
	return &CTypes{Graph: g, List: make(CTypeList), Latency: make(map[string]telemetry.LatencyTotal)}
}

type TypeNameExistence int

const (
	TypeNameExists TypeNameExistence = iota
	TypeNameNotExists
)

// If obj and neighbor aren't same underlying type, return.
// If obj is already part of some node, combine that node and neighbor node.
// Else, add obj to neighbor node.
// Update the list accordingly.
// Return whether obj already existed in some node, and whether combined (or were already combined).
func (c *CTypes) combineTypes(typ golang.TypeInfo, neigh_name FullTypeName) (TypeNameExistence, error, bool) {
	// 1. Check whether to combine
	combined := false
	new_name := TypeName(typ.TypeInfo)

	existing_hash, exists := c.GetHash(new_name)
	existed := TypeNameNotExists
	if exists {
		existed = TypeNameExists
	}

	neigh_hash, ok := c.GetHash(neigh_name)
	if !ok {
		err := fmt.Errorf("combineTypes called via neighbor %v that doesn't exist", neigh_name)
		CheckErr(err)
	}
	new_node, err := c.Graph.Vertex(neigh_hash)
	CheckErr(err)

	if existing_hash == neigh_hash {
		// Already in the same node as neighbor
		return existed, nil, combined
	}

	// 2. Combine

	combined = true
	graph.Logf(c.Graph.Log(), slog.LevelDebug, "COMBINING new %v (hash if any: %v) + neigh %v (hash: %v)", new_name, existing_hash, neigh_name, neigh_hash)
	graph.Logf(c.Graph.Log(), slog.LevelDebug, "%v node BEFORE combine: %+v", neigh_hash, new_node)

	if existed == TypeNameExists {
		// Move over names from existing node (including new name)
		existing_node, err := c.Graph.Vertex(existing_hash)
		CheckErr(err)

		graph.Logf(c.Graph.Log(), slog.LevelDebug, "%v node BEFORE combine: %+v", existing_hash, existing_node)

		new_node.Names = append(new_node.Names, existing_node.Names...)
		new_node.GoplsInfo = append(new_node.GoplsInfo, existing_node.GoplsInfo...)
	} else {
		new_node.Names = append(new_node.Names, new_name)
		new_node.GoplsInfo = append(new_node.GoplsInfo, typ)
	}

	// Keep names sorted
	slices.Sort(new_node.Names)
	new_hash := CTypeNodeHash(new_node) // may still be neigh_hash, or not

	graph.Logf(c.Graph.Log(), slog.LevelDebug, "%v node AFTER combine: %+v", new_hash, new_node)

	// Update neighbor node with new names, and existing edges if any
	// (Stored* shouldn't be populated yet, and TypeInfo should stay the same - just need to update names)
	start := time.Now()
	if existed == TypeNameExists {
		// Combine with existing
		c.Graph.UpdateVertex(neigh_hash, new_node, &existing_hash, func(vp *graph.VertexProperties) {})
	} else {
		c.Graph.UpdateVertex(neigh_hash, new_node, nil, func(vp *graph.VertexProperties) {})
	}
	telemetry.RecordLatency(c.Latency, "UpdateVertex", time.Since(start))

	// 3. Update list for all possibly moved names
	// i.e. new obj, names in neighbor node (since its hash may have changed), and names in existing node (since they were moved)
	for _, new_name := range new_node.Names {
		c.SetHash(new_name, new_hash)
	}

	return existed, nil, combined
}

type NeighReason int

const (
	StructField NeighReason = iota
	NotStructField
)

type NeighAge int

const (
	NeighIsParent NeighAge = iota
	NeighIsChild
)

// Info about the neighbor CType from which we found a new CType
type NeighInfo struct {
	Name FullTypeName
	Age  NeighAge
	Typ  golang.TypeInfo
}

// Given the object defined at the location, record its info.
// If found via enclosure, combine with the corresponding existing node if any.
// Return whether existed
func (c *CTypes) AddCType(typ golang.TypeInfo, neigh_info *NeighInfo) (TypeNameExistence, error) {
	// TODO(CT) currently we don't combine any Unmarshalers (since no neigh_info) - should we do a pass to check?
	if (typ.TypeSource == golang.Enclosed || typ.TypeSource == golang.Enclosing) && neigh_info != nil {
		// Found via a neighbor we may need to combine with
		combine := len(neigh_info.Typ.ASTPath) == 0 || slices.Compare(neigh_info.Typ.ASTPath, []string{"SelectorExpr.Sel"}) == 0
		if combine {
			// If no AST edges (i.e. only the TypeSpec_Type one) from enclosed/enclosing (or just one for pkg.T), `type X Y` => combine
			// (If not iface implementer, should always have a neigh_info)
			existed, err, combined := c.combineTypes(typ, neigh_info.Name)
			CheckErr(err)
			if combined {
				// If combineTypes combined nodes, it already updated the list => check the value of existed it returned
				return existed, nil
			}
		}
	}

	_, exists := c.GetHash(TypeName(typ.TypeInfo))
	if exists {
		return TypeNameExists, nil
	}

	// Make new node
	new_ctype := CTypeNode{TypeInfo: typ.TypeInfo.Type(), Names: []FullTypeName{TypeName(typ.TypeInfo)},
		GoplsInfo: []golang.TypeInfo{typ}}

	// TODO(CT) if we combine nodes, do we need to add the methods of the new type?
	CopyMethods(&new_ctype)
	CopyTags(&new_ctype)
	CopyKeyInfo(&new_ctype)
	if c.KeyInfoHook != nil {
		c.KeyInfoHook(&new_ctype)
	}

	err := c.Graph.AddVertex(new_ctype, func(vp *graph.VertexProperties) {})
	// Shouldn't have existed - checked that above
	CheckErr(err)
	graph.Logf(c.Graph.Log(), slog.LevelDebug, "NEW NODE for %v", TypeName(typ.TypeInfo))

	// Add to list
	c.SetHash(TypeName(typ.TypeInfo), CTypeNodeHash(new_ctype))
	return TypeNameNotExists, nil
}

type EdgeAttrKey string

const (
	Field EdgeAttrKey = "Field"
)

// Convert edge data (any type) to DOT attributes (map[string]string)
func (c *CTypes) edgeDataToAttributes(edge graph.Edge[CTypeHash]) map[string]string {
	// Assume already done (e.g. serializing a graph produced by deserializing).
	// (Check length, not nil: the graph library creates edges with an empty Attributes map.)
	if len(edge.Properties.Attributes) > 0 {
		return edge.Properties.Attributes
	}

	edge_attrs := make(map[string]string)
	edge_data := edge.Properties.Data.([]ASTPath)
	all_tags := []string{}
	parent_node, err := c.Graph.Vertex(edge.Source)
	CheckErr(err)
	child_node, err := c.Graph.Vertex(edge.Target)
	CheckErr(err)
	format := c.Format
	if format == "" {
		format = YAML
	}

	for _, edge_ast_path := range edge_data {
		// For each possible AST path from one CType to another: get its config file key segment, as for param keys
		// (MAP_KEY for map keys, and for yaml:"-" fields, whose key a custom unmarshaler picks)
		seg, ok := ASTPathKeySegment(parent_node, child_node, edge_ast_path, format)
		if !ok {
			continue // can't produce a key (e.g. field isn't a key)
		}
		label := seg.Pattern
		if seg.Dash {
			label = JoinKey(label, MAP_KEY)
		}
		if label != "" {
			all_tags = append(all_tags, label)
		}
	}

	// dedup
	slices.Sort(all_tags)
	all_tags = slices.Compact(all_tags)
	edge_attrs[string(Field)] = strings.Join(all_tags, ",")
	return edge_attrs
}

// Add edge from enclosing CType (parent) to enclosed CType (child).
// Annotate edge with info on how parent type can access child type name:
// e.g. via fields (possibly multiple), or slice indexing.
func (c *CTypes) AddCTypeEdge(parent_hash CTypeHash, child_name FullTypeName, neigh_ast_path ASTPath) error {
	child_hash, ok := c.GetHash(child_name)
	if !ok {
		err := fmt.Errorf("AddCTypeEdge - child %v does not exist\n", child_name)
		CheckErr(err)
	}

	edge_data := []ASTPath{neigh_ast_path}
	// Set edge weight to 1, else gephi will ignore it
	err := c.Graph.AddEdge(parent_hash, child_hash, graph.EdgeWeight(1), graph.EdgeData(edge_data))
	if err != nil {
		if !errors.Is(err, graph.ErrEdgeAlreadyExists) {
			CheckErr(err)
		} else {
			// existed => add path
			edge, err := c.Graph.Edge(parent_hash, child_hash)
			CheckErr(err)
			if edge.Properties.Data != nil {
				existing_edge_data := edge.Properties.Data.([]ASTPath)
				dup := slices.ContainsFunc(existing_edge_data, func(existing_path ASTPath) bool {
					return reflect.DeepEqual(existing_path, neigh_ast_path)
				})
				if !dup {
					edge_data = append(existing_edge_data, edge_data...)
					c.Graph.UpdateEdge(parent_hash, child_hash, graph.EdgeData(edge_data))
				}
			}
		}
	}
	graph.Logf(c.Graph.Log(), slog.LevelDebug, "ADDED EDGE %v => %v", parent_hash, child_hash)

	return nil
}

// Length of path
func pathLen(path string) int {
	path_parts := strings.Split(path, ",")
	return len(path_parts)
}

// Index of n in path
func pathIdx(path string, n CTypeHash) int {
	path_parts := strings.Split(path, ",")
	return slices.Index(path_parts, string(n))
}

// Find the node preceding child in path
func pathParent(path string, child CTypeHash) CTypeHash {
	child_i := pathIdx(path, child)
	path_parts := strings.Split(path, ",")
	return CTypeHash(path_parts[child_i-1])
}

// Panic on err (if running in dlv, will stop at a breakpoint)
func CheckErr(err error) {
	if err != nil {
		panic(err)
	}
}

func (c *CTypes) LogGraphStats(log *slog.Logger, start time.Time) {
	graph.Logf(log, slog.LevelInfo, "Begin stats")
	defer func() {
		graph.Logf(log, slog.LevelInfo, "End stats")
	}()

	// Time
	graph.Logf(log, slog.LevelInfo, "Total time: %v", time.Since(start))

	var gopls_time time.Duration
	for operation, time := range telemetry.GetLatencyTotals() {
		graph.Logf(log, slog.LevelInfo, "gopls %v: %v calls, %v", operation, time.NCalls, time.TotalTime)
		gopls_time += time.TotalTime
	}
	graph.Logf(log, slog.LevelInfo, "gopls total: %v", gopls_time)

	var graph_time time.Duration
	for operation, time := range c.Latency {
		graph.Logf(log, slog.LevelInfo, "graph lib %v: %v calls, %v", operation, time.NCalls, time.TotalTime)
		graph_time += time.TotalTime
	}
	graph.Logf(log, slog.LevelInfo, "graph lib total: %v", graph_time)

	// Size
	n_edges, err := c.Graph.Size()
	CheckErr(err)
	n_nodes, err := c.Graph.Order()
	CheckErr(err)
	graph.Logf(log, slog.LevelInfo, "%v nodes, %v edges", n_nodes, n_edges)
	roots, leaves, err := graph.RootsLeaves(c.Graph)
	CheckErr(err)

	graph.Logf(log, slog.LevelInfo, "%v roots", len(roots))
	graph.Logf(log, slog.LevelInfo, "%v leaves", len(leaves))
}
