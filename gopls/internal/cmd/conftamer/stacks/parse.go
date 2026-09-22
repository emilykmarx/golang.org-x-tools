package parse

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"

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
	_ = sending_types.Graph.AddVertex(new_ctype, func(vp *graph.VertexProperties) {}) // ok if existed
	return ct.CTypeNodeHash(new_ctype)
}

// return hash and whether existed
func AddSendFuncNode(fn string, sending_types *ct.CTypes) (ct.CTypeHash, bool) {
	hash := fn
	new_ctype := ct.CTypeNode{Names: []ct.FullTypeName{ct.FullTypeName(hash)}}
	existed := sending_types.Graph.AddVertex(new_ctype, func(vp *graph.VertexProperties) {}) // ok if existed
	if existed != nil {
		if !errors.Is(existed, graph.ErrVertexAlreadyExists) {
			ct.CheckErr(existed)
		}
	}
	return ct.CTypeNodeHash(new_ctype), existed != nil
}

// Parse the stacks of the given conn,
// writing any frames that failed to parse to err_file
func parseConnStacks(conn string, stacks []string, sending_types *ct.CTypes,
	server *server.Server, err_file *os.File) {

	sending_g := 0
	_, err := fmt.Sscanf(stacks[0], "goroutine %d", &sending_g)
	ct.CheckErr(err)
	fmt.Printf("%+v\n", ParseConn(conn))
	fmt.Printf("SENDING G %v\n", sending_g)

	// Make a node for the message, identified by destination port
	msg_hash := AddSentMsgNode(conn, sending_types)

	g_header := "[originating from goroutine "
	g := 0
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
			if fn == "main.main" || fn == "net.conn.log" || fn == "net.conn.Write" {
				// Ignore logging function
				continue
			}
			fmt.Printf("FN: %v\n", fn)
			cur_fn, existed := AddSendFuncNode(fn, sending_types)
			if prev_fn != cur_fn { // don't add self-edges
				// add edge to previous frame (in parent g's stack if applicable),
				// or to sent message (if this is the sending frame)
				err := sending_types.Graph.AddEdge(cur_fn, prev_fn, graph.EdgeWeight(1))
				if err != nil {
					if !errors.Is(err, graph.ErrEdgeAlreadyExists) {
						ct.CheckErr(err)
					}
				}
				prev_fn = cur_fn

				p := protocol.WorkspaceSymbolParams{
					Query: fn,
				}
				if !existed { // avoid gopls call if already did it for this fn
					arg_types, err := server.ArgTypes(context.Background(), &p)
					if err != nil {
						// Write failure to log
						// Would be useful to print next line here too, and line as is w/o sanitize (so can search logs easier)
						err_log := []string{fn, err.Error()}
						_, err := err_file.WriteString(strings.Join(err_log, "\n") + "\n")
						ct.CheckErr(err)
						continue
					}
					fmt.Printf("ARGS:\n")
					for _, arg := range arg_types {
						if !ct.BasicType(arg) {
							fmt.Printf("%v\n", arg.TypeInfo.Name())
						}
					}
				}
			}
		}
	}
}

// Parse a log of stacktraces (including ancestor goroutines) with a specific format.
// Write parse failures to a log.
// Run from the directory containing module source code, since gopls will analyze it
func ParseStacksLog(sending_types *ct.CTypes, send_log string, output_path string, local_server *server.Server) {
	send_file, err := os.Open(send_log)
	ct.CheckErr(err)
	defer send_file.Close()

	err_file, err := os.Create(filepath.Join(output_path, "stackframe_fails.md"))
	ct.CheckErr(err)
	defer err_file.Close()

	scanner := bufio.NewScanner(send_file)
	for conn, stacks := parseOneConn(scanner); conn != ""; conn, stacks = parseOneConn(scanner) {
		parseConnStacks(conn, stacks, sending_types, local_server, err_file)
	}

	if err := scanner.Err(); err != nil {
		panic(err)
	}

}
