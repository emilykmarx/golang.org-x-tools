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
	"strings"
	"time"

	graph "github.com/emilykmarx/dominikbraun-graph"
	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
	"golang.org/x/tools/gopls/internal/cmd/conftamer/parse/modules/k8s_api_server"
	"golang.org/x/tools/gopls/internal/golang"
	"golang.org/x/tools/gopls/internal/protocol"
	"golang.org/x/tools/gopls/internal/server"
)

// Return info on the next conn in the scanner: the connection info, and the corresponding stacks
func parseOneConn(scanner *bufio.Scanner) (string, []string) {
	stacks := []string{}
	in_write := false // Should print without anything interleaved
	conn_info := ""

	for scanner.Scan() {
		line := scanner.Text()
		if strings.Contains(line, "BEGIN STACKS") {
			in_write = true // Start saving lines of this write
		} else if rest, ok := strings.CutPrefix(line, "CONN: "); ok {
			conn_info = rest
		} else if strings.Contains(line, "END STACKS") {
			return conn_info, stacks
		} else if in_write {
			stacks = append(stacks, line+"\n")
		} else {
			// keep scanning till enter write
		}
	}

	return "", nil
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

func ParseDstPort(conn string) string {
	parsed_conn := ParseConn(conn)
	_, dst_port, err := net.SplitHostPort(parsed_conn[len(parsed_conn)-1])
	ct.CheckErr(err)
	return dst_port
}

func AddSentMsgNode(conn string, sending_types *ct.CTypes) ct.CTypeHash {
	dst_port := ParseDstPort(conn)
	hash := "DPORT: " + dst_port
	new_ctype := ct.CTypeNode{Names: []ct.FullTypeName{ct.FullTypeName(hash)}}
	_ = sending_types.Graph.AddVertex(new_ctype, graph.VertexAttribute("color", "red"))
	return ct.CTypeNodeHash(new_ctype)
}

// Shorten fn
func (p *Parser) ShortLabel(full string, pkg string) string {
	label, _ := strings.CutPrefix(full, p.Module_prefix) // always cut the module name
	// module-specific shortening
	label = k8s_api_server.ShortLabel(label, pkg, p.Module_prefix)
	return label
}

// Get pkg of a fully-qualified name
func Pkg(full string) string {
	last_slash := strings.LastIndex(full, "/")
	if last_slash == -1 {
		last_slash = 0
	}
	first_dot := last_slash + strings.Index(full[last_slash:], ".") // first dot after last slash
	return full[:first_dot]
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
	arg_types, err := p.server.ArgTypes(context.Background(), &gopls_query)
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
	fmt.Printf("ARGS:\n")
	ret := []golang.TypeInfo{}
	for _, arg := range arg_types {
		if !ct.BasicType(arg) {
			fmt.Printf("%v\n", arg.TypeInfo.Name())
			ret = append(ret, arg)
		}
	}

	return ret
}

// Functions that don't need to be graphed
func ignoreFn(fn string) bool {
	ignore_libs := []string{
		// generic messages
		"net", "crypto", "google.golang.org/grpc", "golang.org/x/net",
		// entrypoints
		"testing",
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
	// dst port => ancestry graph
	ancestries map[string]*ct.CTypes

	server        *server.Server
	log           *slog.Logger
	Module_prefix string

	// Logs about gopls queries
	err_file    *os.File
	err_fns     map[string]struct{}
	success_fns int
}

// Parse the stacks of the given conn,
// writing any frames that failed to parse to err_file
func (p *Parser) parseConnStacks(conn string, stacks []string) {
	sending_g := 0
	_, err := fmt.Sscanf(stacks[0], "goroutine %d", &sending_g)
	ct.CheckErr(err)
	fmt.Printf("%+v\n", ParseConn(conn))
	fmt.Printf("SENDING G %v\n", sending_g)

	// Make a node for the message, identified by destination port
	dst_port := ParseDstPort(conn)
	ancestry, ok := p.ancestries[dst_port]
	if !ok {
		ancestry = ct.New(p.log)
		p.ancestries[dst_port] = ancestry
	}
	msg_hash := AddSentMsgNode(conn, ancestry)

	g_header := "[originating from goroutine "
	g := sending_g
	prev_fn := msg_hash
	for i, line := range stacks {
		if strings.Contains(line, g_header) {
			_, err := fmt.Sscanf(line, g_header+"%d", &g)
			ct.CheckErr(err)
			fmt.Printf("PARENT G %v\n", g)
		} else if strings.Contains(line, "created by") {
			// parent frame
		} else if strings.Contains(line, "(") {
			// If line has a (, try to parse it as a function name
			// Assume the last ( is the beginning of the args
			fn := line[:strings.LastIndex(line, "(")]
			SanitizeMethod(&fn)
			if ignoreFn(fn) {
				continue
			}

			fmt.Printf("FN: %v\n", fn)
			cur_fn, _ := p.AddSendFuncNode(fn, stacks[i:i+2], g, ancestry)
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

// Parse a log of stacktraces (including ancestor goroutines) with a specific format.
// Write parse failures to a log.
// Run from the directory containing module source code, since gopls will analyze it
func ParseStacksLog(module_prefix string, log *slog.Logger, send_log string, output_path string, local_server *server.Server) {
	send_file, err := os.Open(send_log)
	ct.CheckErr(err)
	defer send_file.Close()

	err_filepath := filepath.Join(output_path, "stackframe_fails.md")
	err_file, err := os.Create(err_filepath)
	ct.CheckErr(err)
	defer err_file.Close()
	parser := Parser{ancestries: make(map[string]*ct.CTypes), server: local_server,
		err_file: err_file, err_fns: make(map[string]struct{}),
		log: log, Module_prefix: module_prefix}

	start := time.Now()
	graph.Logf(log, slog.LevelInfo, "Parsing ancestry stacktrace for message sends")

	scanner := bufio.NewScanner(send_file)
	for conn, stacks := parseOneConn(scanner); conn != ""; conn, stacks = parseOneConn(scanner) {
		parser.parseConnStacks(conn, stacks)
	}

	if err := scanner.Err(); err != nil {
		panic(err)
	}

	graph.Logf(log, slog.LevelInfo, "%v gopls queries failed, %v succeeded - see %v",
		len(parser.err_fns), parser.success_fns, err_filepath)

	for dport, ancestry := range parser.ancestries {
		ancestry.LogGraphStats(log, start)
		out := fmt.Sprintf("dport%v_sending_types.text", dport)
		graph.Logf(log, slog.LevelInfo, "Serializing to %v", out)
		ancestry.Serialize(filepath.Join(output_path, out), module_prefix, true)
		graph.Logf(log, slog.LevelInfo, "Serialize: %v", time.Since(start))
	}
}
