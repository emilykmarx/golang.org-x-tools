package stacklog

import (
	"slices"
	"testing"
)

func TestEnclosingFns(t *testing.T) {
	for _, test := range []struct {
		fn   string
		want []string
	}{
		{"main.main", []string{"main.main"}},
		{"main.ID.F", []string{"main.ID.F"}},
		{"net/http.(*persistConn).addTLS", []string{"net/http.(*persistConn).addTLS"}},
		// Closures and wrappers
		{"net/http.(*persistConn).addTLS.func2", []string{"net/http.(*persistConn).addTLS"}},
		{"main.ID.F.func1", []string{"main.ID.F"}},
		{"k8s.io/client-go/tools/cache.(*Reflector).list.func1.2", []string{"k8s.io/client-go/tools/cache.(*Reflector).list"}},
		{"example.com/p.F.gowrap1", []string{"example.com/p.F"}},
		{"example.com/p.(*T).M.deferwrap2", []string{"example.com/p.(*T).M"}},
		{"net.init.func1", []string{"net.init"}},
		// Only whole funcN segments are closures
		{"example.com/p.T.funcs", []string{"example.com/p.T.funcs"}},
		{"example.com/p.(*T).funcs.func1", []string{"example.com/p.(*T).funcs"}},
		// Generics
		{"example.com/p.F[...].func1", []string{"example.com/p.F[...]"}},
		{"example.com/p.(*T[...]).M.func1", []string{"example.com/p.(*T[...]).M"}},
		// Closures in inlined functions
		{"github.com/prometheus/prometheus/web.(*Handler).Run.setPathWithPrefix.func5.1",
			[]string{"github.com/prometheus/prometheus/web.setPathWithPrefix", "github.com/prometheus/prometheus/web.(*Handler).Run"}},
		{"github.com/prometheus/prometheus/web.(*Handler).Run.withStackTracer.func1",
			[]string{"github.com/prometheus/prometheus/web.withStackTracer", "github.com/prometheus/prometheus/web.(*Handler).Run"}},
		{"github.com/prometheus/prometheus/web/api/v1.(*API).Register.(*API).Register.func3",
			[]string{"github.com/prometheus/prometheus/web/api/v1.(*API).Register"}},
		{"example.com/p.T.M.helper.func1", []string{"example.com/p.helper", "example.com/p.T.M"}},
		// Method values
		{"github.com/prometheus/prometheus/web.(*Handler).testReady-fm.(*Handler).testReady.func1",
			[]string{"github.com/prometheus/prometheus/web.(*Handler).testReady"}},
		{"example.com/p.T.M-fm", []string{"example.com/p.T.M"}},
	} {
		if got := EnclosingFns(test.fn); !slices.Equal(got, test.want) {
			t.Errorf("EnclosingFns(%q) = %q, want %q", test.fn, got, test.want)
		}
	}
}
