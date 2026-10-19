// Package stacklog parses logs of connection sends and receives, each
// with the stacktraces (including ancestor goroutines) that led to it.
//
// It has no dependencies, so that tools outside gopls (e.g. argmatch) can
// use it too.
package stacklog

import (
	"bufio"
	"fmt"
	"strings"
	"time"
)

type SendOrRecv string

const (
	SEND SendOrRecv = "send"
	RECV SendOrRecv = "recv"
)

// Info about one connection log
type ConnLog struct {
	ConnInfo   string
	Stacks     []string
	Contents   string
	SendOrRecv SendOrRecv
	LastConn   bool
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
			conn_log.Contents = contents
			return i
		}
	}
	return -1
}

// If line is a continuation of previous that completes the line,
// format is "2026-10-07T20:10:20.730491584Z stderr F <continuation>"
func CutLogPrefix(line string) (string, error) {
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

// Return info on the next conn in the scanner: the connection info, and the corresponding stacks.
// After the last conn, returns a ConnLog with LastConn set.
func ParseOneConn(scanner *bufio.Scanner) (ConnLog, error) {
	in_write := false // Should print without anything interleaved
	conn_log := ConnLog{SendOrRecv: SEND}

	for scanner.Scan() {
		line := scanner.Text()
		if idx := strings.Index(line, CONN_STACKS_HDR); idx != -1 {
			in_write = true // Start saving lines of this write
			s_or_r := strings.Index(line, CONN_BEGIN_HDR)
			if s_or_r == -1 {
				return ConnLog{}, fmt.Errorf("bad format %v", line)
			}
			if string(line[s_or_r+len(CONN_BEGIN_HDR)]) == "r" {
				conn_log.SendOrRecv = RECV
			}
			next_char := len(CONN_STACKS_HDR) + idx
			if next_char > len(line)-1 {
				// end of line => assume format is newline-separated (e.g. k8s integration tests)
			} else {
				// not end of line => assume format is msg="<whole conn log with escaped \n>" (e.g. k8s from prombench pod log)
				stacks := strings.Split(line, "\\n")
				ok := false
				conn_log.ConnInfo, ok = strings.CutPrefix(stacks[1], CONN_INFO_HDR)
				if !ok {
					return ConnLog{}, fmt.Errorf("bad format %v", line)
				}
				// If contents have a \n (e.g. if we still logged binary since it happened to contain a \r\n),
				// contents won't be the second-to-last line after the split above
				content_idx := findContents(stacks, &conn_log)
				if content_idx == -1 {
					// The log statement gets a new line every 16424 bytes (e.g. k8s API server in prombench) =>
					// concatenate this line with the next
					if !scanner.Scan() {
						return ConnLog{}, fmt.Errorf("bad format %v", line)
					}
					nextline, err := CutLogPrefix(scanner.Text())
					if err != nil {
						return ConnLog{}, err
					}

					line += nextline
					stacks = strings.Split(line, "\\n")
					content_idx = findContents(stacks, &conn_log)
					if content_idx == -1 {
						// Could support this by looping
						return ConnLog{}, fmt.Errorf("no message content - line broken in three? %v", line)
					}
				}
				conn_log.Stacks = stacks[2:content_idx]
				return conn_log, nil
			}
		} else if rest, ok := strings.CutPrefix(line, CONN_INFO_HDR); ok {
			conn_log.ConnInfo = rest
		} else if strings.Contains(line, CONTENTS_HDR) {
			conn_log.Contents, ok = strings.CutPrefix(line, CONTENTS_HDR)
			return conn_log, nil
		} else if in_write {
			conn_log.Stacks = append(conn_log.Stacks, line)
		} else {
			// keep scanning till enter write
		}
	}

	return ConnLog{LastConn: true}, nil
}

const G_HEADER = "[originating from goroutine "

// FrameFunc returns the function name of a line of a stack, if the line is
// a function frame (as opposed to an "originating from goroutine" header,
// a "created by" line or a file:line line), e.g. net/http.(*persistConn).addTLS.func2
// for "net/http.(*persistConn).addTLS.func2()".
func FrameFunc(line string) (string, bool) {
	if strings.Contains(line, G_HEADER) {
		return "", false
	} else if strings.Contains(line, "created by") {
		// parent frame
		return "", false
	} else if strings.Contains(line, "(") {
		// If line has a (, try to parse it as a function name
		// Assume the last ( is the beginning of the args
		return line[:strings.LastIndex(line, "(")], true
	}
	return "", false
}

// EnclosingFns returns the declared functions that a stack frame's function
// name fn stands for, the function that defines it first. Usually that's
// just fn, but:
//   - A closure, go statement wrapper or defer wrapper (pkg.F.func1,
//     pkg.F.func1.2, pkg.F.gowrap1, pkg.F.deferwrap1) stands for the function
//     that contains it (pkg.F).
//   - A method value wrapper (pkg.T.M-fm) stands for the method (pkg.T.M).
//   - A closure in a function that was inlined into another is named after
//     both, e.g. pkg.(*T).Run.helper.func5 for a closure in pkg.helper inlined
//     into pkg.(*T).Run. That stands for pkg.helper and pkg.(*T).Run.
//
// fn is expected to be unsanitized, i.e. with receivers like (*T).
//
// The inlined case is guessed from the name, which is ambiguous:
//   - The inlined function is assumed to be in fn's package. One from another
//     package can't be recovered, because its package isn't in the name (e.g.
//     pkg.(*T).Run.StripPrefix.func1 for net/http.StripPrefix).
//   - With a value receiver, pkg.A.B could be method A.B or function B inlined
//     into function A. Two segments are always read as a method, so the second
//     case keeps the name pkg.A.B, which matches no declaration. With three or
//     more, the last segment (with its receiver, if that's in parentheses) is
//     read as the inlined function, so a value-receiver method inlined into a
//     function (pkg.F.T.M) is misread as pkg.M and pkg.F.T.
//
// An exact alternative (not implemented) is to use the frame's file:line,
// which follows each function line in a stack (e.g. "\t/src/web/web.go:905
// +0x2c5"), instead of the name: the declaration containing that line is the
// defining function, whatever the inlining, and the "originating from" and
// caller frames give the rest. That needs the source matching the log, and
// the paths are from the machine that built the logged program (e.g. /src/...,
// /go/pkg/mod/..., /usr/local/go/src/...), so they'd have to be mapped to
// module and package-relative paths rather than compared directly. A tool
// that sees the source as it compiles (e.g. argmatch) could then pick the
// enclosing declaration by position.
func EnclosingFns(fn string) []string {
	pkg := Pkg(fn)
	rest, ok := strings.CutPrefix(fn, pkg+".")
	if !ok {
		return []string{fn}
	}
	segs := splitName(rest)
	for i, s := range segs {
		if isClosureSeg(s) {
			segs = segs[:i]
			break
		}
	}
	for i, s := range segs {
		segs[i] = strings.TrimSuffix(s, "-fm")
	}
	if len(segs) == 0 {
		return []string{fn}
	}
	if len(segs) <= 2 {
		return []string{pkg + "." + strings.Join(segs, ".")}
	}
	// Defining (inlined) function last, the one it was inlined into first.
	split := len(segs) - 1
	if strings.HasPrefix(segs[split-1], "(") {
		split-- // pointer receiver of the inlined function
	}
	inner := pkg + "." + strings.Join(segs[split:], ".")
	outer := pkg + "." + strings.Join(segs[:split], ".")
	if inner == outer {
		return []string{inner}
	}
	return []string{inner, outer}
}

// splitName splits a function name (without its package) at dots outside
// brackets and parentheses, so that pkg.F[...] and pkg.(*T[...]).M stay whole.
func splitName(s string) []string {
	var segs []string
	depth, start := 0, 0
	for i, c := range s {
		switch c {
		case '[', '(':
			depth++
		case ']', ')':
			depth--
		case '.':
			if depth == 0 {
				segs = append(segs, s[start:i])
				start = i + 1
			}
		}
	}
	return append(segs, s[start:])
}

// isClosureSeg reports whether s is funcN, gowrapN or deferwrapN.
func isClosureSeg(s string) bool {
	for _, prefix := range []string{"func", "gowrap", "deferwrap"} {
		if n, ok := strings.CutPrefix(s, prefix); ok && n != "" && strings.Trim(n, "0123456789") == "" {
			return true
		}
	}
	return false
}

// Get pkg of a fully-qualified name
func Pkg(full string) string {
	last_slash := strings.LastIndex(full, "/")
	last_slash = max(last_slash, 0)
	first_dot := strings.Index(full[last_slash:], ".") // first dot after last slash
	first_dot = max(first_dot, 0)
	return full[:last_slash+first_dot]
}

// Function name prefixes that don't need to be graphed
var IGNORE_LIBS = []string{
	// generic messages
	"google.golang.org/grpc", "golang.org/x/net", "github.com/klauspost/compress",
	// entrypoints
	"main.main",

	/* module-specific libs */
	// k8s API server
	// wait
	"k8s.io/apimachinery/pkg/util/wait",
	// generic messages
	"k8s.io/client-go/rest",
	"k8s.io/client-go/transport",
}

// Functions that don't need to be graphed: those in the standard library
// (as reported by isStd, given the function's package) or IGNORE_LIBS
func IgnoreFn(fn string, isStd func(pkg string) bool) bool {
	if isStd(Pkg(fn)) {
		// standard library
		return true
	}
	for _, lib := range IGNORE_LIBS {
		if strings.HasPrefix(fn, lib) {
			return true
		}
	}
	return false
}
