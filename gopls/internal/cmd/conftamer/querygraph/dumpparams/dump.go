package main

import (
	"flag"
	"fmt"
	"log"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"

	"github.com/dominikbraun/graph"
	util "github.com/emilykmarx/conftamer/paramtrack/util"
	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
)

func main() {
	var output_path, module_prefix, unmarshaler_subgraph string
	flag.StringVar(&output_path, "output-path", "", "Path for output")
	flag.StringVar(&module_prefix, "module-prefix", "", "module as in go.mod")
	flag.StringVar(&unmarshaler_subgraph, "unmarshaler-subgraph", "", "File containing serialized unmarshaler subgraph")
	flag.Parse()
	if output_path == "" || module_prefix == "" || unmarshaler_subgraph == "" {
		flag.Usage()
		log.Fatalf("Missing mandatory argument")
	}

	g, _ := ct.Deserialize(unmarshaler_subgraph)

	// needed for logging in graph lib
	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelDebug,
		ReplaceAttr: func(groups []string, a slog.Attr) slog.Attr {
			if a.Key == "msg" {
				return a
			} else {
				return slog.Attr{}
			}
		}}))

	g.SetLog(log)
	roots, _, err := graph.RootsLeaves(g)
	ct.CheckErr(err)

	err = os.Chdir("../../dlv")
	ct.CheckErr(err)
	out, err := exec.Command("go", "build", ".").CombinedOutput()
	util.CheckCmd(out, err)

	// Dump params for each root to their own dir
	dump_args := []string{
		"--module-prefix=" + module_prefix,
		"--unmarshaler-subgraph=" + unmarshaler_subgraph,
	}
	for _, root := range roots {
		fmt.Printf("Dumping params from root %v\n", root)

		full_output_path := filepath.Join(output_path, string(root))
		full_dump_args := append(dump_args,
			"--output-path="+full_output_path,
			"--dump-params="+string(root))

		err = os.MkdirAll(full_output_path, 0777)
		ct.CheckErr(err)

		dump := exec.Command("./dlv", full_dump_args...)
		// get live results
		dump.Stdout = os.Stdout
		dump.Stderr = os.Stderr
		err = dump.Run()
		util.CheckCmd(nil, err)
	}
}
