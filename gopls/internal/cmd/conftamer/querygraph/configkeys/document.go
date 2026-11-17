package main

// Walk a config file, with the format's own parser, collecting its scalar values and their keys

import (
	"bytes"
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
// inline_keys (JSON): field key => the keys inside its objects whose value picks a module (from the graph's
// selectors, e.g. handle => [handler]): the object's other keys get a `<inline key>=<value>` segment after the field's
// key, as the graph's extra edges do (e.g. handle.handler=reverse_proxy.upstreams.dial).
// parent_keys: field key => the key of the field's own object whose value picks the field's module (from the graph's
// extra edges with a Before segment, e.g. jsonData => type): the field's key gets a `<key>=<value>` segment before it
// (e.g. datasources.type=prometheus.jsonData.httpMethod).
func readDocument(path string, format ct.Format, inline_keys map[string][]string, parent_keys map[string]string) []occurrence {
	contents, err := os.ReadFile(path)
	ct.CheckErr(err)
	out := []occurrence{}
	switch format {
	case ct.YAML:
		var doc yaml.Node
		if err := yaml.Unmarshal(contents, &doc); err != nil {
			log.Fatalf("Parsing %v: %v", path, err)
		}
		walkYAML(&doc, nil, parent_keys, &out)
	case ct.JSON:
		if err := walkJSON(contents, nil, inline_keys, parent_keys, &out); err != nil {
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

// The `<key>=<value>` segment that goes before field key k in an object whose scalar members are values (see
// readDocument's parent_keys), or ""
func parentSegment(k string, parent_keys map[string]string, values map[string]string) string {
	if sel, ok := parent_keys[k]; ok && values[sel] != "" {
		return sel + "=" + values[sel]
	}
	return ""
}

func walkYAML(n *yaml.Node, path []string, parent_keys map[string]string, out *[]occurrence) {
	switch n.Kind {
	case yaml.DocumentNode:
		for _, c := range n.Content {
			walkYAML(c, path, parent_keys, out)
		}
	case yaml.AliasNode:
		walkYAML(n.Alias, path, parent_keys, out)
	case yaml.SequenceNode:
		for _, c := range n.Content {
			walkYAML(c, path, parent_keys, out)
		}
	case yaml.MappingNode:
		values := map[string]string{} // the object's scalar members (for parentSegment)
		for i := 0; i+1 < len(n.Content); i += 2 {
			if v := n.Content[i+1]; v.Kind == yaml.ScalarNode {
				values[n.Content[i].Value] = v.Value
			}
		}
		for i := 0; i+1 < len(n.Content); i += 2 {
			k, v := n.Content[i], n.Content[i+1]
			if k.Kind == yaml.AliasNode {
				k = k.Alias
			}
			if k.ShortTag() == "!!merge" {
				walkYAML(v, path, parent_keys, out) // mapping, or sequence of mappings
				continue
			}
			p := slices.Clone(path)
			if seg := parentSegment(k.Value, parent_keys, values); seg != "" {
				p = append(p, seg)
			}
			walkYAML(v, append(p, k.Value), parent_keys, out)
		}
	case yaml.ScalarNode:
		if n.Value != "" && len(path) > 0 {
			*out = append(*out, occurrence{value: n.Value, key: strings.Join(path, "."), is_str: n.ShortTag() == "!!str"})
		}
	}
}

// Walk a JSON value (raw), at key path. Objects are read whole (keys and raw values, in order), so that an inline key's
// value is known before the object's other keys are walked.
func walkJSON(raw []byte, path []string, inline_keys map[string][]string, parent_keys map[string]string, out *[]occurrence) error {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber() // keep numbers as written
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
			type member struct {
				key   string
				value json.RawMessage
			}
			members := []member{}
			for dec.More() {
				key_tok, err := dec.Token()
				if err != nil {
					return err
				}
				var v json.RawMessage
				if err := dec.Decode(&v); err != nil {
					return err
				}
				members = append(members, member{key_tok.(string), v})
			}
			// The module this object picks, if its field takes an inline key
			selector, selector_key := "", ""
			if len(path) > 0 {
				for _, k := range inline_keys[path[len(path)-1]] {
					for _, m := range members {
						var name string
						if m.key == k && json.Unmarshal(m.value, &name) == nil {
							selector, selector_key = k+"="+name, k
						}
					}
				}
			}
			values := map[string]string{} // the object's string members (for parentSegment)
			for _, m := range members {
				var v string
				if json.Unmarshal(m.value, &v) == nil {
					values[m.key] = v
				}
			}
			for _, m := range members {
				p := slices.Clone(path)
				if selector != "" && m.key != selector_key {
					p = append(p, selector)
				}
				if seg := parentSegment(m.key, parent_keys, values); seg != "" {
					p = append(p, seg)
				}
				if err := walkJSON(m.value, append(p, m.key), inline_keys, parent_keys, out); err != nil {
					return err
				}
			}
		case '[':
			for dec.More() {
				var v json.RawMessage
				if err := dec.Decode(&v); err != nil {
					return err
				}
				if err := walkJSON(v, path, inline_keys, parent_keys, out); err != nil {
					return err
				}
			}
		default:
			return fmt.Errorf("unexpected %v", t)
		}
		return nil
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
// Newlines in values are written as a literal \n, which argmatch's -matchfile reads back as a newline.
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
