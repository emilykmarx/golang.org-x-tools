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

// return hash and whether existed
func AddSendFuncNode(fn string, g int, sending_types *ct.CTypes) (ct.CTypeHash, bool) {
	// This will group calls from different goroutines, so e.g. if G1 is A => B => msg X and G2 is A' => B => msg Y,
	// it will look like both A or A' could lead to both msgs.
	// To avoid (at the cost of more nodes), uncomment line below:
	//hash := fmt.Sprintf("%v (%v)", fn, g)
	hash := fn
	new_ctype := ct.CTypeNode{Names: []ct.FullTypeName{ct.FullTypeName(hash)}}
	last_slash := strings.LastIndex(fn, "/")
	if last_slash == -1 {
		last_slash = 0
	}
	first_dot := last_slash + strings.Index(fn[last_slash:], ".") // first dot after last slash
	pkg := fn[:first_dot]

	existed := sending_types.Graph.AddVertex(new_ctype, graph.VertexAttribute("pkg", pkg))
	if existed != nil {
		if !errors.Is(existed, graph.ErrVertexAlreadyExists) {
			ct.CheckErr(existed)
		}
	}
	return ct.CTypeNodeHash(new_ctype), existed != nil
}

// Query gopls for the arg (and recvr) types of the fn
func ArgTypes(fn string, server *server.Server, err_file *os.File) {
	p := protocol.WorkspaceSymbolParams{
		Query: fn,
	}
	arg_types, err := server.ArgTypes(context.Background(), &p)
	if err != nil {
		// Write failure to log
		// Would be useful to print next line here too, and line as is w/o sanitize (so can search logs easier)
		err_log := []string{fn, err.Error()}
		_, err := err_file.WriteString(strings.Join(err_log, "\n") + "\n")
		ct.CheckErr(err)
		return
	}
	fmt.Printf("ARGS:\n")
	for _, arg := range arg_types {
		if !ct.BasicType(arg) {
			fmt.Printf("%v\n", arg.TypeInfo.Name())
		}
	}
}

// Functions that don't need to be graphed
func ignoreFn(fn string) bool {
	ignore_libs := []string{
		// generic messages
		"net", "crypto", "google.golang.org/grpc",
		// entrypoints
		"testing",
		"main.main",

		/* module-specific libs*/
		// wait
		"k8s.io/apimachinery/pkg/util/wait",
		// generic messages
		"k8s.io/client-go/rest",
		"k8s.io/client-go/transport",
	}
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
	server     *server.Server
	err_file   *os.File
	log        *slog.Logger
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
	for _, line := range stacks {
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
			cur_fn, existed := AddSendFuncNode(fn, g, ancestry)
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

				if !existed { // Get arg types
					//		ArgTypes(fn, server, err_file)
				}
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

	err_file, err := os.Create(filepath.Join(output_path, "stackframe_fails.md"))
	ct.CheckErr(err)
	defer err_file.Close()
	parser := Parser{ancestries: make(map[string]*ct.CTypes), server: local_server, err_file: err_file, log: log}

	start := time.Now()
	graph.Logf(log, slog.LevelInfo, "Parsing ancestry stacktrace for message sends")

	scanner := bufio.NewScanner(send_file)
	for conn, stacks := parseOneConn(scanner); conn != ""; conn, stacks = parseOneConn(scanner) {
		parser.parseConnStacks(conn, stacks)
	}

	if err := scanner.Err(); err != nil {
		panic(err)
	}

	for dport, ancestry := range parser.ancestries {
		ancestry.LogGraphStats(log, start)
		out := fmt.Sprintf("dport%v_sending_types.text", dport)
		graph.Logf(log, slog.LevelInfo, "Serializing to %v", out)
		ancestry.Serialize(filepath.Join(output_path, out), module_prefix, true)
		graph.Logf(log, slog.LevelInfo, "Serialize: %v", time.Since(start))
	}
}
