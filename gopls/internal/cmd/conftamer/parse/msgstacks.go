package parse

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"

	graph "github.com/emilykmarx/dominikbraun-graph"
	"golang.org/x/tools/gopls/conftamer/stacklog"
	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
	"golang.org/x/tools/gopls/internal/cmd/conftamer/parse/modules/k8s_api_server"
	"golang.org/x/tools/gopls/internal/golang"
	"golang.org/x/tools/gopls/internal/protocol"
	"golang.org/x/tools/gopls/internal/server"
	"golang.org/x/tools/internal/stdlib"
	"gopkg.in/yaml.v3"
)

// The log format and its parsing are in stacklog, so tools outside gopls can use them
type SEND_OR_RECV = stacklog.SendOrRecv

const (
	SEND = stacklog.SEND
	RECV = stacklog.RECV
)

type ConnLog = stacklog.ConnLog

// Sanitize a stack frame's function name, switching to the function that defined it
// if it's e.g. an anonymous function (see SanitizeMethods)
func SanitizeMethod(method *string) {
	*method = SanitizeMethods(*method)[0]
}

// Sanitize a stack frame's function name, returning the declared functions it stands for:
// usually just the function, but if it's e.g. an anonymous function, the function that defined it,
// and also the function that was inlined into, if any (see stacklog.EnclosingFns)
func SanitizeMethods(method string) []string {
	fns := stacklog.EnclosingFns(method)
	for i, fn := range fns {
		fn = strings.ReplaceAll(fn, "*", "")
		fn = strings.ReplaceAll(fn, "(", "")
		fns[i] = strings.ReplaceAll(fn, ")", "")
	}
	return fns
}

func CleanConnPart(part string) string {
	part = strings.Trim(part, "{}")
	return part[strings.Index(part, ":")+1:]
}

// would be easier if net marshaled this, but that's annoying too
// return net, laddr, raddr
func ParseConn(conn string) []string {
	parts := strings.Split(conn, " ")
	for i, part := range parts {
		parts[i] = CleanConnPart(part)
	}
	return parts
}

func RAddr(conn string) string {
	parsed_conn := ParseConn(conn)
	return parsed_conn[len(parsed_conn)-1]
}

const CONTENTSATTR = "contents"
const ARGSATTR = "args"

func AddMsgNode(conn ConnLog, sending_types *ct.CTypes) ct.CTypeHash {
	hash := ct.CTypeHash(RAddr(conn.ConnInfo))
	msg_node := ct.CTypeNode{Names: []ct.FullTypeName{ct.FullTypeName(hash)}}
	// Add node if doesn't exist
	attrs := map[string]string{
		"color":      "red",
		CONTENTSATTR: conn.Contents,
	}
	err := sending_types.Graph.AddVertex(msg_node, graph.VertexAttributes(attrs))
	if err == nil {
		return hash // done
	}

	// Update attributes with new contents
	// Would be better if vertex attrs supported list values
	// (so we don't have to awkwardly convert to string, and perhaps to allow smarter gephi things) - same for arg types
	_, properties, err := sending_types.Graph.VertexWithProperties(hash)
	ct.CheckErr(err)
	attrs = properties.Attributes
	existing_contents := strings.Split(attrs[CONTENTSATTR], ",")
	if !slices.Contains(existing_contents, conn.Contents) {
		existing_contents = append(existing_contents, conn.Contents)
	}
	attrs[CONTENTSATTR] = strings.Join(existing_contents, ",")
	sending_types.Graph.UpdateVertex(hash, msg_node, nil, graph.VertexAttributes(attrs))
	return hash
}

// Shorten fn
func (p *Parser) ShortLabel(full string, pkg string) string {
	label, _ := strings.CutPrefix(full, p.ModulePrefix) // always cut the module name
	// module-specific shortening
	label = k8s_api_server.ShortLabel(label, pkg, p.ModulePrefix)
	return label
}

// Get pkg of a fully-qualified name
func Pkg(full string) string {
	return stacklog.Pkg(full)
}

// Modules we have message logs from
var SCANNED_MODULES = []string{
	"prometheus-meta",
	"prometheus-test",
	"grafana",
	"alertmanager",
	"kube",
}

// Will also include the "kubernetes" service
func (p *Parser) scanned_module(module_name string) bool {
	for _, prefix := range SCANNED_MODULES {
		if strings.HasPrefix(module_name, prefix) {
			return true
		}
	}
	return false
}

func (p *Parser) queriedGopls(fn string) (string, bool) {
	for _, ancestries := range p.ancestries {
		for _, ancestry := range ancestries {
			if _, properties, err := ancestry.Graph.VertexWithProperties(ct.CTypeHash(fn)); err == nil {
				if args, ok := properties.Attributes[ARGSATTR]; ok {
					// we only add this attr if we queried gopls
					return args, true
				}
			}
		}
	}
	return "", false
}

// return hash and whether existed
func (p *Parser) AddAncestorFuncNode(fn string, frame []string, g int, sending_types *ct.CTypes, dst string) (ct.CTypeHash, bool) {
	// This will group calls from different goroutines, so e.g. if G1 is A => B => msg X and G2 is A' => B => msg Y,
	// it will look like both A or A' could lead to both msgs.
	// To avoid (at the cost of more nodes), uncomment line below:
	//hash := fmt.Sprintf("%v (%v)", fn, g)
	hash := ct.CTypeHash(fn)
	new_ctype := ct.CTypeNode{Names: []ct.FullTypeName{ct.FullTypeName(hash)}}
	if _, err := sending_types.Graph.Vertex(hash); err == nil {
		// already in this graph
		return hash, true
	}
	pkg := Pkg(fn)
	attrs := map[string]string{
		"pkg":   pkg,
		"label": p.ShortLabel(fn, pkg),
		// else gephi only shows this in Data Lab, not Overview
		"fn": fn,
	}

	// To save gopls queries for now, only find arg types if the dst is a known module (or `kubernetes`)
	args, queried := p.queriedGopls(fn)
	find_arg_types := p.ShouldFindArgTypes && p.scanned_module(p.dstToModuleName(dst))
	if find_arg_types {
		if !queried {
			args := p.ArgTypes(fn, frame)
			arg_names := []string{}
			for _, arg := range args {
				arg_names = append(arg_names, string(ct.TypeNameSafe(arg.TypeInfo)))
			}
			attrs[ARGSATTR] = strings.Join(arg_names, ",")
		} else {
			// Copy from other graph
			attrs[ARGSATTR] = args
		}
	}

	// Hash becomes "Id" column; label is default node label
	err := sending_types.Graph.AddVertex(new_ctype, graph.VertexAttributes(attrs))
	ct.CheckErr(err)

	return ct.CTypeNodeHash(new_ctype), false
}

// Query gopls for the arg (and recvr) types of the fn.
// Write query errors to err_file (frame should be the erroring frame)
func (p *Parser) ArgTypes(fn string, frame []string) []golang.TypeInfo {
	gopls_query := protocol.WorkspaceSymbolParams{
		Query: fn,
	}
	arg_types, err := p.Server.ArgTypes(context.Background(), &gopls_query)
	if err != nil {
		if _, ok := p.err_fns[fn]; !ok {
			p.err_fns[fn] = struct{}{}
			// Write original frame to log, unless already did
			err_log := append(frame, err.Error())
			_, err := p.err_file.WriteString(strings.Join(err_log, "") + "\n\n")
			ct.CheckErr(err)
			return nil
		}
	} else {
		p.success_fns += 1
	}
	ret := []golang.TypeInfo{}
	for _, arg := range arg_types {
		if !ct.BasicType(arg) {
			ret = append(ret, arg)
		}
	}

	return ret
}

// Functions that don't need to be graphed, per IncludeLibs (unset means MODULE_ONLY)
func (p *Parser) IgnoreFn(fn string) bool {
	module_org := filepath.Dir(p.ModulePrefix)

	if p.IncludeLibs != IGNORE_COMMON {
		if _, in_org := strings.CutPrefix(fn, module_org); !in_org {
			// Not defined in the module's organization (e.g. github.com/prometheus)
			return true
		}
	}

	if p.IncludeLibs == MODULE_ONLY || p.IncludeLibs == "" {
		if _, in_module := strings.CutPrefix(fn, p.ModulePrefix); !in_module {
			// Not defined in the module (e.g. github.com/prometheus/prometheus)
			return true
		}
	}
	// One of the common ignored functions (e.g. std lib)
	return stacklog.IgnoreFn(fn, stdlib.HasPackage)
}

// ParserConfig holds the Parser fields that are also conftamer verb config fields
// (embedded in both, so the verb can pass them through as a unit)
type ParserConfig struct {
	ModulePrefix       string      `yaml:"module_prefix" flag:"m,module_prefix" help:"module as in go.mod (used to pretty-print and possibly ignore unmarshaler subgraph nodes)"`
	OutputPath         string      `yaml:"output_path" flag:"out,output_path" help:"Output path for graph files"`
	AncestryLog        string      `yaml:"ancestry_log" flag:"s,ancestry_log" help:"A log of message sends and receives - if passed, will find send/receive ancestors from the log (rather than finding the Unmarshaler Subgraph and Accessors from the module source)"`
	ModuleIPFiles      StringList  `yaml:"module_ip_files" flag:"ips,module_ip_files" help:"Files containing module IPs (e.g. pods and services)"`
	ShouldFindArgTypes bool        `yaml:"find_arg_types" flag:"arg-types,find_arg_types" help:"Whether to find arg types of message ancestors"`
	IncludeLibs        IncludeLibs `yaml:"include_libs" flag:"include-libs,include_libs" help:"Which functions to include in ancestries: MODULE_ONLY (default), MODULE_ORG_ONLY, or IGNORE_COMMON"`
}

// IncludeLibs is which functions to include in ancestries
type IncludeLibs string

const (
	MODULE_ONLY     IncludeLibs = "MODULE_ONLY"
	MODULE_ORG_ONLY IncludeLibs = "MODULE_ORG_ONLY"
	IGNORE_COMMON   IncludeLibs = "IGNORE_COMMON"
)

func (i *IncludeLibs) String() string { return string(*i) }
func (i *IncludeLibs) Set(v string) error {
	switch IncludeLibs(v) {
	case MODULE_ONLY, MODULE_ORG_ONLY, IGNORE_COMMON:
		*i = IncludeLibs(v)
		return nil
	}
	return fmt.Errorf("invalid include_libs %q: want %v, %v, or %v", v, MODULE_ONLY, MODULE_ORG_ONLY, IGNORE_COMMON)
}

// Validate when set from the YAML config too
func (i *IncludeLibs) UnmarshalYAML(value *yaml.Node) error {
	var v string
	if err := value.Decode(&v); err != nil {
		return err
	}
	return i.Set(v)
}

// StringList is a []string settable via a repeated flag (e.g. -ip=a -ip=b)
type StringList []string

func (s *StringList) String() string     { return strings.Join(*s, ",") }
func (s *StringList) Set(v string) error { *s = append(*s, v); return nil }

type Parser struct {
	// For each of send and recv: destination => ancestry graph
	ancestries map[SEND_OR_RECV]map[string]*ct.CTypes

	// From conftamer verb entrypoint
	Server *server.Server
	Log    *slog.Logger
	ParserConfig

	module_ips map[string]string

	// Logs about gopls queries
	err_file    *os.File
	err_fns     map[string]struct{}
	success_fns int
}

// Parse the stacks of the given conn,
// writing any frames that failed to parse to err_file.
// Each ancestry graph corresponds to sends or receives from one destination endpoint (IP:port).
// The single message node is labeled with all observed contents.
func (p *Parser) parseConnStacks(conn_log ConnLog) {
	sending_g := 0
	_, err := fmt.Sscanf(conn_log.Stacks[0], "goroutine %d", &sending_g)
	ct.CheckErr(err)

	// Make or update the node for the message
	dst := RAddr(conn_log.ConnInfo)
	ancestry, ok := p.ancestries[conn_log.SendOrRecv][dst]
	if !ok {
		ancestry = ct.New(p.Log)
		p.ancestries[conn_log.SendOrRecv][dst] = ancestry
	}
	msg_hash := AddMsgNode(conn_log, ancestry)

	g := sending_g
	prev_fn := msg_hash
	for i, line := range conn_log.Stacks {
		if strings.Contains(line, stacklog.G_HEADER) {
			_, err := fmt.Sscanf(line, stacklog.G_HEADER+"%d", &g)
			ct.CheckErr(err)
		} else if frame_fn, ok := stacklog.FrameFunc(line); ok {
			// A closure in an inlined function stands for two functions: the one that defined it,
			// and (as its caller) the one it was inlined into
			for _, fn := range SanitizeMethods(frame_fn) {
				if p.IgnoreFn(fn) {
					continue
				}

				cur_fn, _ := p.AddAncestorFuncNode(fn, conn_log.Stacks[i:i+2], g, ancestry, dst)
				if prev_fn != cur_fn { // don't add self-edges
					// add edge to previous frame (in parent g's stack if applicable),
					// or to sent message (if this is the sending frame)
					err := ancestry.Graph.AddEdge(cur_fn, prev_fn, graph.EdgeWeight(1))
					if err != nil {
						if !errors.Is(err, graph.ErrEdgeAlreadyExists) {
							ct.CheckErr(err)
						}
					}
					prev_fn = cur_fn
				}
			}
		}
	}
}

// Return map: pod/service IP => name
func ParseKubectlIPs(file string, module_ips map[string]string) {
	raw_lines, err := os.ReadFile(file)
	ct.CheckErr(err)
	lines := strings.Split(string(raw_lines), "\n")
	name_idx := 1 // 2nd col for both pods and services
	ip_idx := 6
	if strings.Contains(lines[0], "CLUSTER-IP") {
		// services
		ip_idx = 3
	}
	for _, line := range lines[1:] {
		cols := strings.Fields(line)
		if len(cols) > 0 {
			module_ips[cols[ip_idx]] = cols[name_idx]
		}
	}
}

// Parse a log of stacktraces (including ancestor goroutines) with a specific format.
// Separate resulting sends and recvs into two directories, each with a file for each dst endpoint.
// Write parse failures to a log.
// Run from the directory containing module source code, since gopls will analyze it.
func ParseStacksLog(p Parser) {
	// Get module IPs, to name the graph files
	p.module_ips = make(map[string]string)
	for _, file := range p.ModuleIPFiles {
		ParseKubectlIPs(file, p.module_ips)
	}
	send_file, err := os.Open(p.AncestryLog)
	ct.CheckErr(err)
	defer send_file.Close()
	for _, send_or_recv := range []SEND_OR_RECV{SEND, RECV} {
		ct.CheckErr(os.MkdirAll(filepath.Join(p.OutputPath, string(send_or_recv)), 0777))
	}

	err_filepath := filepath.Join(p.OutputPath, "stackframe_fails.md")
	p.err_file, err = os.Create(err_filepath)
	ct.CheckErr(err)
	defer p.err_file.Close()
	p.ancestries = map[SEND_OR_RECV]map[string]*ct.CTypes{
		SEND: make(map[string]*ct.CTypes),
		RECV: make(map[string]*ct.CTypes),
	}
	p.err_fns = make(map[string]struct{})

	start := time.Now()
	graph.Logf(p.Log, slog.LevelInfo, "Parsing ancestry stacktrace for message sends")

	scanner := bufio.NewScanner(send_file)
	for {
		conn_log, err := stacklog.ParseOneConn(scanner)
		ct.CheckErr(err)
		if conn_log.LastConn {
			break
		}
		p.parseConnStacks(conn_log)
	}

	if err := scanner.Err(); err != nil {
		panic(err)
	}
	if len(p.ancestries[SEND]) == 0 && len(p.ancestries[RECV]) == 0 {
		// sanity check
		graph.Logf(p.Log, slog.LevelError, "No ancestries found")
	}

	graph.Logf(p.Log, slog.LevelInfo, "%v gopls queries failed, %v succeeded - see %v",
		len(p.err_fns), p.success_fns, err_filepath)

	for send_or_recv, ancestries := range p.ancestries {
		for dst, ancestry := range ancestries {
			graph_name := dst
			ancestry.LogGraphStats(p.Log, start)
			module_name := p.dstToModuleName(dst)
			if module_name == "" {
				graph.Logf(p.Log, slog.LevelWarn, "unknown module dst %v - known ips %v\n", dst, p.module_ips)
				// Some Grafana connections have dst addr e.g. /tmp/plugin2698764150
				graph_name = strings.ReplaceAll(dst, "/", "_") // make it a proper filename
			} else {
				graph_name = fmt.Sprintf("%v_%v", module_name, dst)
			}
			out := filepath.Join(p.OutputPath, string(send_or_recv), graph_name)
			graph.Logf(p.Log, slog.LevelInfo, "Serializing to %v.gv", out)
			ancestry.Serialize(out+".text", p.ModulePrefix, true) // Serialize function expects the .text postfix
			graph.Logf(p.Log, slog.LevelInfo, "Serialize: %v", time.Since(start))
		}
	}
}

func (p *Parser) dstToModuleName(dst string) string {
	dst_ip, _, err := net.SplitHostPort(dst)
	if err != nil {
		// Not a host:port
		return ""
	}
	module_name, ok := p.module_ips[dst_ip]

	if !ok {
		// Unknown host
		return ""
	}
	return module_name
}
