package k8s_api_server

import "strings"

var IGNORE_FNS = []string{
	// wait
	"k8s.io/apimachinery/pkg/util/wait",
	// generic messages
	"k8s.io/client-go/rest",
	"k8s.io/client-go/transport",
}

func FuncLabel(cut_fn string, pkg string, module_prefix string) string {
	is_module_node := strings.HasPrefix(pkg, module_prefix)

	// Shortened package name
	prefixes := []string{
		module_prefix,
		"k8s.io/kubernetes",
		"go.etcd.io",
		// cut these after the module prefix
		"pkg",
		"test",
	}

	label := pkg

	for _, prefix := range prefixes {
		label, _ = strings.CutPrefix(label, prefix+"/")
	}

	if is_module_node {
		label = "/" + label // retain whether was module node
	}
	return label
}
