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
	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
	"golang.org/x/tools/gopls/internal/cmd/conftamer/parse/modules/k8s_api_server"
	"golang.org/x/tools/gopls/internal/golang"
	"golang.org/x/tools/gopls/internal/protocol"
	"golang.org/x/tools/gopls/internal/server"
	"golang.org/x/tools/internal/stdlib"
)

type SEND_OR_RECV string

const (
	SEND SEND_OR_RECV = "send"
	RECV SEND_OR_RECV = "recv"
)

// Info about one connection log
type ConnLog struct {
	conn_info    string
	stacks       []string
	contents     string
	send_or_recv SEND_OR_RECV
	last_conn    bool
}

const (
	CONN_BEGIN_HDR  = "conn.log ("
	CONN_STACKS_HDR = "BEGIN STACKS"
	CONN_INFO_HDR   = "CONN: "
	CONTENTS_HDR    = "CONTENTS: "
)

func findContents(stacks []string, conn_log *ConnLog) int {
	for i, line := range stacks {
		if contents, ok := strings.CutPrefix(line, CONTENTS_HDR); ok {
			conn_log.contents = contents
			return i
		}
	}
	return -1
}

// If line is a continuation of previous that completes the line,
// format is "2026-10-07T20:10:20.730491584Z stderr F <continuation>"
func cutLogPrefix(line string) (string, error) {
	fields := strings.SplitN(line, " ", 4)
	if len(fields) != 4 {
		return "", fmt.Errorf("bad log prefix format: %v", line)
	}
	if _, err := time.Parse(time.RFC3339Nano, fields[0]); err != nil {
		return "", fmt.Errorf("bad log prefix timestamp: %v: %w", line, err)
	}
	if fields[1] != "stderr" && fields[1] != "stdout" {
		return "", fmt.Errorf("bad log prefix stream: %v", line)
	}
	if fields[2] != "F" {
		return "", fmt.Errorf("bad log prefix tag: %v", line)
	}
	return fields[3], nil
}

// Return info on the next conn in the scanner: the connection info, and the corresponding stacks
func parseOneConn(scanner *bufio.Scanner) ConnLog {
	in_write := false // Should print without anything interleaved
	conn_log := ConnLog{send_or_recv: SEND}

	for scanner.Scan() {
		line := scanner.Text()
		if idx := strings.Index(line, CONN_STACKS_HDR); idx != -1 {
			in_write = true // Start saving lines of this write
			s_or_r := strings.Index(line, CONN_BEGIN_HDR)
			if s_or_r == -1 {
				ct.CheckErr(fmt.Errorf("bad format %v", line))
			}
			if string(line[s_or_r+len(CONN_BEGIN_HDR)]) == "r" {
				conn_log.send_or_recv = RECV
			}
			next_char := len(CONN_STACKS_HDR) + idx
			if next_char > len(line)-1 {
				// end of line => assume format is newline-separated (e.g. k8s integration tests)
			} else {
				// not end of line => assume format is msg="<whole conn log with escaped \n>" (e.g. k8s from prombench pod log)
				stacks := strings.Split(line, "\\n")
				ok := false
				conn_log.conn_info, ok = strings.CutPrefix(stacks[1], CONN_INFO_HDR)
				if !ok {
					ct.CheckErr(fmt.Errorf("bad format %v", line))
				}
				// If contents have a \n (e.g. if we still logged binary since it happened to contain a \r\n),
				// contents won't be the second-to-last line after the split above
				content_idx := findContents(stacks, &conn_log)
				if content_idx == -1 {
					// The log statement gets a new line every 16424 bytes (e.g. k8s API server in prombench) =>
					// concatenate this line with the next
					if !scanner.Scan() {
						ct.CheckErr(fmt.Errorf("bad format %v", line))
					}
					nextline := scanner.Text()
					var err error
					nextline, err = cutLogPrefix(nextline)
					ct.CheckErr(err)

					line += nextline
					stacks = strings.Split(line, "\\n")
					content_idx = findContents(stacks, &conn_log)
					if content_idx == -1 {
						// Could support this by looping
						ct.CheckErr(fmt.Errorf("no message content - line broken in three? %v", line))
					}
				}
				conn_log.stacks = stacks[2:content_idx]
				return conn_log
			}
		} else if rest, ok := strings.CutPrefix(line, CONN_INFO_HDR); ok {
			conn_log.conn_info = rest
		} else if strings.Contains(line, CONTENTS_HDR) {
			conn_log.contents, ok = strings.CutPrefix(line, CONTENTS_HDR)
			return conn_log
		} else if in_write {
			conn_log.stacks = append(conn_log.stacks, line)
		} else {
			// keep scanning till enter write
		}
	}

	return ConnLog{last_conn: true}
}

func SanitizeMethod(method *string) {
	*method = strings.ReplaceAll(*method, "*", "")
	*method = strings.ReplaceAll(*method, "(", "")
	*method = strings.ReplaceAll(*method, ")", "")

	DeanonymizeFn(method)
}

// If appears to be an anonymous function, switch to the function that defined it
func DeanonymizeFn(fn *string) {
	// remove anything after ".func"
	*fn, _, _ = strings.Cut(*fn, ".func")
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

func ParseDst(conn string) string {
	parsed_conn := ParseConn(conn)
	return parsed_conn[len(parsed_conn)-1]
}

const CONTENTS = "contents"

func AddMsgNode(conn ConnLog, sending_types *ct.CTypes) ct.CTypeHash {
	hash := ct.CTypeHash(ParseDst(conn.conn_info))
	msg_node := ct.CTypeNode{Names: []ct.FullTypeName{ct.FullTypeName(hash)}}
	// Add node if doesn't exist
	attrs := map[string]string{
		"color":  "red",
		CONTENTS: conn.contents,
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
	existing_contents := strings.Split(attrs[CONTENTS], ",")
	if !slices.Contains(existing_contents, conn.contents) {
		existing_contents = append(existing_contents, conn.contents)
	}
	attrs[CONTENTS] = strings.Join(existing_contents, ",")
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
	last_slash := strings.LastIndex(full, "/")
	last_slash = max(last_slash, 0)
	first_dot := strings.Index(full[last_slash:], ".") // first dot after last slash
	first_dot = max(first_dot, 0)
	return full[:last_slash+first_dot]
}

// return hash and whether existed
func (p *Parser) AddSendFuncNode(fn string, frame []string, g int, sending_types *ct.CTypes) (ct.CTypeHash, bool) {
	// This will group calls from different goroutines, so e.g. if G1 is A => B => msg X and G2 is A' => B => msg Y,
	// it will look like both A or A' could lead to both msgs.
	// To avoid (at the cost of more nodes), uncomment line below:
	//hash := fmt.Sprintf("%v (%v)", fn, g)
	hash := ct.CTypeHash(fn)
	new_ctype := ct.CTypeNode{Names: []ct.FullTypeName{ct.FullTypeName(hash)}}
	if _, err := sending_types.Graph.Vertex(hash); err == nil {
		// avoid calling gopls
		return hash, true
	}
	pkg := Pkg(fn)
	attrs := map[string]string{
		"pkg":   pkg,
		"label": p.ShortLabel(fn, pkg),
		// else gephi only shows this in Data Lab, not Overview
		"fn": fn,
	}

	args := p.ArgTypes(fn, frame)
	arg_names := []string{}
	for _, arg := range args {
		arg_names = append(arg_names, string(ct.TypeNameSafe(arg.TypeInfo)))
	}
	attrs["args"] = strings.Join(arg_names, ",")

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

// Functions that don't need to be graphed
func IgnoreFn(fn string) bool {
	pkg := Pkg(fn)
	if stdlib.HasPackage(pkg) {
		// standard library
		return true
	}
	ignore_libs := []string{
		// generic messages
		"google.golang.org/grpc", "golang.org/x/net", "github.com/klauspost/compress",
		// entrypoints
		"main.main",
	}

	/* module-specific libs */
	ignore_libs = append(ignore_libs, k8s_api_server.IGNORE_FNS...)

	for _, lib := range ignore_libs {
		if strings.HasPrefix(fn, lib) {
			return true
		}
	}

	return false
}

type Parser struct {
	// For each of send and recv: destination => ancestry graph
	ancestries map[SEND_OR_RECV]map[string]*ct.CTypes

	// From conftamer verb entrypoint
	Server        *server.Server
	Log           *slog.Logger
	ModulePrefix  string
	SendLog       string
	OutputPath    string
	ModuleIPFiles []string

	module_ips map[string]string

	// Logs about gopls queries
	err_file    *os.File
	err_fns     map[string]struct{}
	success_fns int
}

// Parse the stacks of the given conn,
// writing any frames that failed to parse to err_file
func (p *Parser) parseConnStacks(conn_log ConnLog) {
	sending_g := 0
	_, err := fmt.Sscanf(conn_log.stacks[0], "goroutine %d", &sending_g)
	ct.CheckErr(err)

	// Make or update the node for the message, identified by destination endpoint
	// and labeled with all observed contents
	dst := ParseDst(conn_log.conn_info)
	ancestry, ok := p.ancestries[conn_log.send_or_recv][dst]
	if !ok {
		ancestry = ct.New(p.Log)
		p.ancestries[conn_log.send_or_recv][dst] = ancestry
	}
	msg_hash := AddMsgNode(conn_log, ancestry)

	g_header := "[originating from goroutine "
	g := sending_g
	prev_fn := msg_hash
	for i, line := range conn_log.stacks {
		if strings.Contains(line, g_header) {
			_, err := fmt.Sscanf(line, g_header+"%d", &g)
			ct.CheckErr(err)
		} else if strings.Contains(line, "created by") {
			// parent frame
		} else if strings.Contains(line, "(") {
			// If line has a (, try to parse it as a function name
			// Assume the last ( is the beginning of the args
			fn := line[:strings.LastIndex(line, "(")]
			SanitizeMethod(&fn)
			if IgnoreFn(fn) {
				continue
			}

			cur_fn, _ := p.AddSendFuncNode(fn, conn_log.stacks[i:i+2], g, ancestry)
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
	send_file, err := os.Open(p.SendLog)
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
	for conn_log := parseOneConn(scanner); !conn_log.last_conn; conn_log = parseOneConn(scanner) {
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
			dst_ip, _, err := net.SplitHostPort(dst)
			if err == nil {
				module_name, ok := p.module_ips[dst_ip]
				if !ok {
					graph.Logf(p.Log, slog.LevelWarn, "unknown module dst IP %v - known ips %v\n", dst_ip, p.module_ips)
				}
				graph_name = fmt.Sprintf("%v_%v", module_name, dst)
			} else {
				// Some Grafana connections have dst addr e.g. /tmp/plugin2698764150
				graph_name = strings.ReplaceAll(dst, "/", "_") // make it a proper filename
			}
			out := filepath.Join(p.OutputPath, string(send_or_recv), graph_name)
			graph.Logf(p.Log, slog.LevelInfo, "Serializing to %v.gv", out)
			ancestry.Serialize(out+".text", p.ModulePrefix, true) // Serialize function expects the .text postfix
			graph.Logf(p.Log, slog.LevelInfo, "Serialize: %v", time.Since(start))
		}
	}
}
