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
		if got := readDocument(path, test.format); !slices.Equal(got, test.want) {
			t.Errorf("%v:\ngot  %v\nwant %v", test.format, got, test.want)
		}
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
