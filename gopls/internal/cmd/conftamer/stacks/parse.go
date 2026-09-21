package parse

import (
	"bufio"
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"

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

// Parse the stacks of the given conn,
// writing any frames that failed to parse to err_file
func parseConnStacks(conn string, stacks []string, sending_types *ct.CTypes,
	server *server.Server, err_file *os.File, seen_fns map[string]struct{}) {

	// LEFT OFF what should we do with the sending types? Some way to ID the msg would be useful, e.g. what packages involved in send (i.e. packages of fns in stack)
	// May want to ignore udp or treat separately (will likely see lots of DNS)
	sending_g := 0
	_, err := fmt.Sscanf(stacks[0], "goroutine %d", &sending_g)
	ct.CheckErr(err)
	fmt.Printf("%+v\n", conn)
	fmt.Printf("SENDING G %v\n", sending_g)

	g_header := "[originating from goroutine "
	g := 0
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
			if fn == "main.main" || fn == "net.conn.log" {
				// Ignore logging function
				continue
			}
			fmt.Printf("FN: %v\n", fn)
			if _, seen := seen_fns[fn]; !seen {
				seen_fns[fn] = struct{}{}

				p := protocol.WorkspaceSymbolParams{
					Query: fn,
				}
				arg_types, err := server.ArgTypes(context.Background(), &p)
				if err != nil {
					// Write failure to log
					err_log := []string{fn, err.Error()}
					_, err := err_file.WriteString(strings.Join(err_log, "\n") + "\n")
					ct.CheckErr(err)
					continue
				}
				// If slow, can skip gopls calls for fns we've already made (e.g. conn.Write)
				fmt.Printf("ARGS:\n")
				for _, arg := range arg_types {
					if !ct.BasicType(arg) {
						fmt.Printf("%v\n", arg.TypeInfo.Name())
						_, err := sending_types.AddCType(arg, nil)
						ct.CheckErr(err)
					}
				}
			}
		}
	}
}

// Parse a log of stacktraces (including ancestor goroutines) with a specific format.
// Write parse failures to a log.
func ParseStacksLog(sending_types *ct.CTypes, send_log string, output_path string, local_server *server.Server) {
	send_file, err := os.Open(send_log)
	ct.CheckErr(err)
	defer send_file.Close()

	err_file, err := os.Create(filepath.Join(output_path, "stackframe_fails.md"))
	ct.CheckErr(err)
	defer err_file.Close()

	seen_fns := make(map[string]struct{}) // avoid querying gopls multiple times for a function

	scanner := bufio.NewScanner(send_file)
	for conn, stacks := parseOneConn(scanner); conn != ""; conn, stacks = parseOneConn(scanner) {
		parseConnStacks(conn, stacks, sending_types, local_server, err_file, seen_fns)
	}

	if err := scanner.Err(); err != nil {
		panic(err)
	}

}
