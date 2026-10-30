package main

// Walk a config file, with the format's own parser, collecting its scalar values and their keys

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"os"
	"slices"
	"strconv"
	"strings"

	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
	"gopkg.in/yaml.v3"
)

// A scalar value in the config file, at a key (dot-separated map keys; list indices aren't part of keys)
type occurrence struct {
	value string // as written in the file (after unquoting)
	key   string
	// The parser resolves the value to a string (e.g. not `true`, `5`, `null`; but quoted "true" is a string).
	// Only used for --strings-only when the key's Go type is unknown.
	is_str bool
}

// Scalar values of the config file in document order. Empty values are skipped. YAML aliases are followed (so each
// use is a separate occurrence), and merge keys (<<) add the merged mapping's keys to the enclosing mapping.
func readDocument(path string, format ct.Format) []occurrence {
	contents, err := os.ReadFile(path)
	ct.CheckErr(err)
	out := []occurrence{}
	switch format {
	case ct.YAML:
		var doc yaml.Node
		if err := yaml.Unmarshal(contents, &doc); err != nil {
			log.Fatalf("Parsing %v: %v", path, err)
		}
		walkYAML(&doc, nil, &out)
	case ct.JSON:
		dec := json.NewDecoder(strings.NewReader(string(contents)))
		dec.UseNumber() // keep numbers as written
		if err := walkJSON(dec, nil, &out); err != nil {
			log.Fatalf("Parsing %v: %v", path, err)
		}
	default:
		log.Fatalf("Unsupported format %v", format)
	}
	for _, occ := range out {
		if strings.Contains(occ.key, ":") {
			// param_keys.txt lines split on the last ':' of "<value>:<key>[,<key>...]"
			log.Printf("Warning: key %q contains ':', so its param_keys.txt line won't parse", occ.key)
		}
	}
	return out
}

func walkYAML(n *yaml.Node, path []string, out *[]occurrence) {
	switch n.Kind {
	case yaml.DocumentNode:
		for _, c := range n.Content {
			walkYAML(c, path, out)
		}
	case yaml.AliasNode:
		walkYAML(n.Alias, path, out)
	case yaml.SequenceNode:
		for _, c := range n.Content {
			walkYAML(c, path, out)
		}
	case yaml.MappingNode:
		for i := 0; i+1 < len(n.Content); i += 2 {
			k, v := n.Content[i], n.Content[i+1]
			if k.Kind == yaml.AliasNode {
				k = k.Alias
			}
			if k.ShortTag() == "!!merge" {
				walkYAML(v, path, out) // mapping, or sequence of mappings
				continue
			}
			walkYAML(v, append(slices.Clone(path), k.Value), out)
		}
	case yaml.ScalarNode:
		if n.Value != "" && len(path) > 0 {
			*out = append(*out, occurrence{value: n.Value, key: strings.Join(path, "."), is_str: n.ShortTag() == "!!str"})
		}
	}
}

func walkJSON(dec *json.Decoder, path []string, out *[]occurrence) error {
	tok, err := dec.Token()
	if err != nil {
		return err
	}
	add := func(value string, is_str bool) {
		if value != "" && len(path) > 0 {
			*out = append(*out, occurrence{value: value, key: strings.Join(path, "."), is_str: is_str})
		}
	}
	switch t := tok.(type) {
	case json.Delim:
		switch t {
		case '{':
			for dec.More() {
				key_tok, err := dec.Token()
				if err != nil {
					return err
				}
				if err := walkJSON(dec, append(slices.Clone(path), key_tok.(string)), out); err != nil {
					return err
				}
			}
		case '[':
			for dec.More() {
				if err := walkJSON(dec, path, out); err != nil {
					return err
				}
			}
		default:
			return fmt.Errorf("unexpected %v", t)
		}
		_, err := dec.Token() // closing delimiter
		return err
	case string:
		add(t, true)
	case json.Number:
		add(t.String(), false)
	case bool:
		add(strconv.FormatBool(t), false)
	case nil:
		add("null", false)
	}
	return nil
}

// Write param_values.txt (each distinct value once, in order of first appearance) and
// param_keys.txt ("<value>:<key>[,<key>...]", each distinct key of the value) to dir.
// Newlines in values are written as a literal \n.
func writeTxt(dir string, occs []occurrence) {
	values := []string{}
	keys := map[string][]string{}
	for _, occ := range occs {
		v := strings.ReplaceAll(occ.value, "\n", `\n`)
		if _, ok := keys[v]; !ok {
			values = append(values, v)
		}
		if !slices.Contains(keys[v], occ.key) {
			keys[v] = append(keys[v], occ.key)
		}
	}
	write := func(name string, line func(v string) string) {
		fd, err := os.Create(dir + "/" + name)
		ct.CheckErr(err)
		defer fd.Close()
		for _, v := range values {
			_, err := io.WriteString(fd, line(v)+"\n")
			ct.CheckErr(err)
		}
	}
	write("param_values.txt", func(v string) string { return v })
	write("param_keys.txt", func(v string) string { return v + ":" + strings.Join(keys[v], ",") })
	fmt.Printf("%v occurrences, %v distinct values\n", len(occs), len(values))
}
