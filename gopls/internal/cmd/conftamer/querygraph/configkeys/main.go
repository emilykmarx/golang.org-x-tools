package main

// Map config keys to their paths through the Unmarshaler Subgraph: the keys in a config file (concrete keys), and all
// keys the graph allows (patterns). Also writes the config file's values and keys for searching memory
// (param_values.txt, param_keys.txt). See README.md.

import (
	"encoding/csv"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"maps"
	"os"
	"slices"
	"strconv"
	"strings"

	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
)

// Read keys from a file with lines "<value>:<key>[,<key>...]" (e.g. param_keys.txt)
func readKeysTxt(keys_txt string) []string {
	contents, err := os.ReadFile(keys_txt)
	ct.CheckErr(err)
	keys := []string{}
	for _, line := range strings.Split(string(contents), "\n") {
		i := strings.LastIndex(line, ":")
		if i == -1 {
			continue
		}
		for _, key := range strings.Split(line[i+1:], ",") {
			if key = strings.TrimSpace(key); key != "" {
				keys = append(keys, key)
			}
		}
	}
	slices.Sort(keys)
	return slices.Compact(keys)
}

// Read keys from a csv's param_keys column (comma-separated keys)
func readKeysCSV(keys_csv string) []string {
	fd, err := os.Open(keys_csv)
	ct.CheckErr(err)
	defer fd.Close()
	records, err := csv.NewReader(fd).ReadAll()
	ct.CheckErr(err)

	col := slices.Index(records[0], "param_keys")
	if col == -1 {
		log.Fatalf("No param_keys column in %v", keys_csv)
	}
	keys := []string{}
	for _, record := range records[1:] {
		for _, key := range strings.Split(record[col], ",") {
			if key = strings.TrimSpace(key); key != "" {
				keys = append(keys, key)
			}
		}
	}
	slices.Sort(keys)
	return slices.Compact(keys)
}

// One output row: a concrete key from the config, a pattern from the graph, or both (a pattern with no MAP_KEY)
type row struct {
	key       string
	in_config bool
	in_graph  bool
	values    []string // config values, in document order
	patterns  []string // for concrete keys: the patterns it instantiates via the same path
	acc       *acc
}

// What a row's paths say about its key
type summary struct {
	full         bool     // has a full path (else paths are partial)
	paths        []string // full paths, or if none, partial paths with " (matched: ...)"
	unmarshalers []string // last node of each path
	section_keys []string // keys in the sections containing the key (full paths only)
	kinds        []ct.ValueKind
}

func (a *acc) summarize() summary {
	s := summary{full: len(a.full) > 0}
	if s.full {
		section, kinds := map[string]bool{}, map[ct.ValueKind]bool{}
		for p, info := range a.full {
			s.paths = append(s.paths, p)
			maps.Copy(section, info.section)
			maps.Copy(kinds, info.kinds)
		}
		s.section_keys = slices.Sorted(maps.Keys(section))
		s.kinds = slices.Sorted(maps.Keys(kinds))
	} else {
		s.paths = slices.Collect(maps.Keys(a.partial))
	}
	for _, p := range s.paths {
		nodes := pathNodes(p)
		s.unmarshalers = append(s.unmarshalers, nodes[len(nodes)-1])
	}
	slices.Sort(s.paths)
	slices.Sort(s.unmarshalers)
	s.unmarshalers = slices.Compact(s.unmarshalers)
	return s
}

// Whether --strings-only keeps a value: its key's values are stored as strings or byte arrays/slices, or structs
// (e.g. config.URL, relabel.Regexp - memory is searched for strings inside them). Excludes e.g. numbers (including
// model.Duration), bools. If the key's kind is unknown (no full path, or decided by a custom unmarshaler) or an
// interface, falls back to whether the parser resolves the value to a string.
func keepString(kinds []ct.ValueKind, is_str bool) bool {
	if len(kinds) == 0 {
		return is_str
	}
	for _, k := range kinds {
		switch k {
		case ct.KindString, ct.KindStruct:
			return true
		case ct.KindInterface, "":
			if is_str {
				return true
			}
		}
	}
	return false
}

func kindsString(kinds []ct.ValueKind) string {
	strs := []string{}
	for _, k := range kinds {
		if k == "" {
			k = "unknown"
		}
		strs = append(strs, string(k))
	}
	return strings.Join(strs, "; ")
}

func main() {
	var config, keys_csv, keys_txt, txt_dir, unmarshaler_subgraph, module_prefix, output, root, through, format_arg string
	var enumerate, strings_only bool
	flag.StringVar(&config, "config", "", "Config file: its keys are matched, and its values written to --txt-dir (optional)")
	flag.StringVar(&keys_txt, "keys-txt", "", `Instead of --config: file with lines "<value>:<key>[,<key>...]", e.g. param_keys.txt (optional)`)
	flag.StringVar(&keys_csv, "keys-csv", "", "Instead of --config: csv with a param_keys column (optional)")
	flag.StringVar(&txt_dir, "txt-dir", "", "With --config: directory to write param_values.txt and param_keys.txt to (optional)")
	flag.BoolVar(&strings_only, "strings-only", false, "With --txt-dir: only write values stored as strings, byte arrays/slices or structs (see keepString)")
	flag.BoolVar(&enumerate, "enumerate", true, "Also output all keys the graph allows, as patterns (* for any one segment)")
	flag.StringVar(&unmarshaler_subgraph, "unmarshaler-subgraph", "", "File containing serialized unmarshaler subgraph")
	flag.StringVar(&module_prefix, "module-prefix", "", "module as in go.mod")
	flag.StringVar(&output, "output", "", "Output csv")
	flag.StringVar(&format_arg, "format", string(ct.YAML), "Config file format: yaml (follow gopkg.in/yaml.v2) or json (follow encoding/json; not yet supported) - use the format the graph was generated with")
	flag.StringVar(&root, "root", "", "Node to search from, as named in the serialized graph (e.g. /config.Config) - optional, default all roots")
	flag.StringVar(&through, "through", "", "Only output keys (and paths) whose paths go through this node, e.g. /config.ScrapeConfig (optional)")
	flag.Parse()
	n_inputs := 0
	for _, in := range []string{config, keys_csv, keys_txt} {
		if in != "" {
			n_inputs++
		}
	}
	if n_inputs > 1 || unmarshaler_subgraph == "" || module_prefix == "" || output == "" || (txt_dir != "" && config == "") {
		flag.Usage()
		log.Fatalf("Missing mandatory argument, or invalid combination")
	}

	format, err := ct.ParseFormat(format_arg)
	if err != nil {
		log.Fatalf("--format: %v", err)
	}

	g, _ := ct.Deserialize(unmarshaler_subgraph)
	f := newFinder(g, format)
	for _, node := range []string{root, through} {
		if node != "" {
			if _, err := g.Vertex(ct.CTypeHash(node)); err != nil {
				log.Fatalf("%v not in graph: %v", node, err)
			}
		}
	}
	if root != "" {
		if !slices.Contains(f.roots, ct.CTypeHash(root)) {
			log.Printf("Warning: --root %v is not a root of the graph (it has in-edges)", root)
		}
		f.roots = []ct.CTypeHash{ct.CTypeHash(root)}
	}

	// 1. Concrete keys
	rows := map[string]*row{}
	occs := []occurrence{}
	var keys []string
	switch {
	case config != "":
		occs = readDocument(config, format)
		for _, occ := range occs {
			keys = append(keys, occ.key)
		}
	case keys_txt != "":
		keys = readKeysTxt(keys_txt)
	case keys_csv != "":
		keys = readKeysCSV(keys_csv)
	}
	for _, key := range keys {
		if _, ok := rows[key]; !ok {
			rows[key] = &row{key: key, in_config: true, acc: f.find(key, module_prefix)}
		}
	}
	for _, occ := range occs {
		if r := rows[occ.key]; !slices.Contains(r.values, occ.value) {
			r.values = append(r.values, occ.value)
		}
	}

	// 2. Values for searching memory
	if txt_dir != "" {
		kept := []occurrence{}
		for _, occ := range occs {
			if !strings_only || keepString(rows[occ.key].acc.summarize().kinds, occ.is_str) {
				kept = append(kept, occ)
			}
		}
		writeTxt(txt_dir, kept)
	}

	// 3. Patterns, linked to the concrete keys that instantiate them via the same path
	n_patterns, n_unlinked := 0, 0
	if enumerate {
		patterns := f.enumerate(module_prefix)
		n_patterns = len(patterns)
		by_path := map[string][]string{} // full path => patterns
		for pattern, a := range patterns {
			for p := range a.full {
				by_path[p] = append(by_path[p], pattern)
			}
			if r, ok := rows[pattern]; ok {
				r.in_graph = true
				r.acc.merge(a)
			} else {
				rows[pattern] = &row{key: pattern, in_graph: true, acc: a}
			}
		}
		for _, r := range rows {
			if !r.in_config {
				continue
			}
			for p := range r.acc.full {
				for _, pattern := range by_path[p] {
					if instanceOf(r.key, pattern) && !slices.Contains(r.patterns, pattern) {
						r.patterns = append(r.patterns, pattern)
					}
				}
			}
			slices.Sort(r.patterns)
			if len(r.acc.full) > 0 && len(r.patterns) == 0 {
				n_unlinked++
				log.Printf("Warning: config key %v has a full path, but no pattern via the same path", r.key)
			}
		}
	}

	// 4. Output
	// Groups: 0 = no full path, 1 = multiple unmarshalers, 2 = one unmarshaler
	groups := make([][]*row, 3)
	n_config := [3]int{}
	for _, key := range slices.Sorted(maps.Keys(rows)) {
		r := rows[key]
		if through != "" && !r.acc.filterThrough(through) {
			continue
		}
		s := r.acc.summarize()
		group := 2
		switch {
		case !s.full:
			group = 0
		case len(s.unmarshalers) > 1:
			group = 1
		}
		groups[group] = append(groups[group], r)
		if r.in_config {
			n_config[group]++
		}
	}
	fmt.Printf("config keys: %v (no path: %v, multiple unmarshalers: %v, one unmarshaler: %v)\n",
		n_config[0]+n_config[1]+n_config[2], n_config[0], n_config[1], n_config[2])
	if enumerate {
		fmt.Printf("graph patterns: %v; config keys with a full path but no pattern: %v\n", n_patterns, n_unlinked)
	}

	fd, err := os.Create(output)
	ct.CheckErr(err)
	defer fd.Close()
	w := csv.NewWriter(fd)
	ct.CheckErr(w.Write([]string{"param_key", "source", "pattern", "values", "value kind",
		"Unmarshaler", "Num Unmarshalers", "Unmarshaler Path", "section keys"}))
	for _, group := range groups {
		for _, r := range group {
			s := r.acc.summarize()
			source := "config"
			if r.in_graph {
				source = "graph"
				if r.in_config {
					source = "both"
				}
			}
			values := ""
			if len(r.values) > 0 {
				b, err := json.Marshal(r.values)
				ct.CheckErr(err)
				values = string(b)
			}
			// Unmarshalers of partial paths don't count
			num_unmarshalers := 0
			if s.full {
				num_unmarshalers = len(s.unmarshalers)
			}
			ct.CheckErr(w.Write([]string{r.key, source, strings.Join(r.patterns, "; "), values, kindsString(s.kinds),
				strings.Join(s.unmarshalers, "; "), strconv.Itoa(num_unmarshalers), strings.Join(s.paths, "; "),
				strings.Join(s.section_keys, "; ")}))
		}
	}
	w.Flush()
	ct.CheckErr(w.Error())
}
