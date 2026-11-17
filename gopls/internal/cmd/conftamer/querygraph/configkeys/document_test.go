package main

import (
	"os"
	"path/filepath"
	"slices"
	"testing"

	ct "golang.org/x/tools/gopls/internal/cmd/conftamer"
)

func TestReadDocument(t *testing.T) {
	for _, test := range []struct {
		format ct.Format
		doc    string
		want   []occurrence
	}{
		{ct.YAML, `
base: &base
  a: x
  n: 5
c:
  <<: *base
  l: [p, "true", true]
  e: ""
  pem: |
    l1
    l2
d: *base
`, []occurrence{{"x", "base.a", true}, {"5", "base.n", false}, {"x", "c.a", true}, {"5", "c.n", false},
			{"p", "c.l", true}, {"true", "c.l", true}, {"true", "c.l", false}, {"l1\nl2\n", "c.pem", true},
			{"x", "d.a", true}, {"5", "d.n", false}}},
		{ct.JSON, `{"a": {"b": "x", "l": [1.50, {"c": true}, null]}, "e": ""}`,
			[]occurrence{{"x", "a.b", true}, {"1.50", "a.l", false}, {"true", "a.l.c", false}, {"null", "a.l", false}}},
	} {
		path := filepath.Join(t.TempDir(), "config")
		if err := os.WriteFile(path, []byte(test.doc), 0o644); err != nil {
			t.Fatal(err)
		}
		if got := readDocument(path, test.format, nil, nil); !slices.Equal(got, test.want) {
			t.Errorf("%v:\ngot  %v\nwant %v", test.format, got, test.want)
		}
	}
}

// JSON objects whose field takes an inline key: their other keys get a <inline key>=<value> segment
func TestReadDocumentInlineKeys(t *testing.T) {
	doc := `{"handle": [{"upstreams": [{"dial": "a:1"}], "handler": "reverse_proxy"}, {"handler": "rewrite", "uri": "/x"}],
		"other": {"handler": "not inline here", "v": "y"}}`
	want := []occurrence{{"a:1", "handle.handler=reverse_proxy.upstreams.dial", true},
		{"reverse_proxy", "handle.handler", true}, {"rewrite", "handle.handler", true},
		{"/x", "handle.handler=rewrite.uri", true}, {"not inline here", "other.handler", true}, {"y", "other.v", true}}
	path := filepath.Join(t.TempDir(), "config")
	if err := os.WriteFile(path, []byte(doc), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := readDocument(path, ct.JSON, map[string][]string{"handle": {"handler"}}, nil); !slices.Equal(got, want) {
		t.Errorf("got  %v\nwant %v", got, want)
	}
}

// YAML objects whose field's module is picked by a key of the object itself: the field's key gets a <key>=<value>
// segment before it; the object's other keys don't
func TestReadDocumentParentKeys(t *testing.T) {
	doc := `datasources:
- name: a
  type: prometheus
  jsonData:
    httpMethod: POST
- jsonData:
    timeout: 5
  type: loki
- jsonData:
    x: no type
`
	want := []occurrence{{"a", "datasources.name", true}, {"prometheus", "datasources.type", true},
		{"POST", "datasources.type=prometheus.jsonData.httpMethod", true},
		{"5", "datasources.type=loki.jsonData.timeout", false}, {"loki", "datasources.type", true},
		{"no type", "datasources.jsonData.x", true}}
	path := filepath.Join(t.TempDir(), "config")
	if err := os.WriteFile(path, []byte(doc), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := readDocument(path, ct.YAML, nil, map[string]string{"jsonData": "type"}); !slices.Equal(got, want) {
		t.Errorf("got  %v\nwant %v", got, want)
	}
}

func TestInstanceOf(t *testing.T) {
	for _, test := range []struct {
		key, pattern string
		want         bool
	}{
		{"a.b.c", "a.*.c", true},
		{"a.b.c", "a.b.c", true},
		{"a.b.c", "a.*", false},
		{"a.b", "a.*.c", false},
		{"a.b.c", "a.*.d", false},
	} {
		if got := instanceOf(test.key, test.pattern); got != test.want {
			t.Errorf("instanceOf(%q, %q) = %v, want %v", test.key, test.pattern, got, test.want)
		}
	}
}

// JSON keys spelled as the module does (encoding/json matches them case-insensitively)
func TestCanonicalKeys(t *testing.T) {
	patterns := map[string]*acc{"storage.module=secret_store.LeaseID": nil, "apps.*.Listen": nil, "a.b": nil, "a.B": nil}
	got := canonicalKeys([]string{"storage.module=secret_store.leaseId", "apps.myServer.listen", "a.b", "x.y", "A.b"}, patterns)
	want := map[string]string{
		"storage.module=secret_store.leaseId": "storage.module=secret_store.LeaseID", // case-folded match
		"apps.myServer.listen":                "apps.myServer.Listen",                // map key keeps its spelling
		"a.b":                                 "a.b",                                 // exact match wins
		"x.y":                                 "x.y",                                 // no pattern
		"A.b":                                 "A.b",                                 // two spellings (a.b, a.B): unchanged
	}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("%v: got %v, want %v", k, got[k], v)
		}
	}
}
