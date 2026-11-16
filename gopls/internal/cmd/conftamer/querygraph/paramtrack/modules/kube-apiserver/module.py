"""The Kubernetes API server: paramtrack's settings and hooks for this module (see ../__init__.py for what each is;
documentedkeys/README.md for what its config is and the decisions behind the roots)."""

import os

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = f"{HERE}/documentedkeys"

GO_MODULE = "k8s.io/kubernetes"
# v1.31.1 plus 3 Dockerfile-only commits; go.work includes the staging modules (k8s.io/apiserver, ...), where the
# roots' types are
MODULE_DIR = "/home/emily/go/src/k8s.io/kubernetes"
# The files are YAML or JSON, but the k8s codec converts YAML to JSON and decodes it by the json tags (the types have
# no yaml tags)
FORMAT = "json"
# The six versioned config files in scope, each named by its own flag: the type each file's loader decodes into
# (documentedkeys/README.md, "The six files in scope")
_V = "k8s.io/apiserver/pkg/apis/apiserver"
ROOTS = {
    "authentication": {"root": f"{_V}/v1beta1.AuthenticationConfiguration",  # --authentication-config
                       "docs": f"{DOCS}/authentication_config_keys.txt"},
    "authorization": {"root": f"{_V}/v1beta1.AuthorizationConfiguration",  # --authorization-config
                      "docs": f"{DOCS}/authorization_config_keys.txt"},
    "encryption": {"root": f"{_V}/v1.EncryptionConfiguration",  # --encryption-provider-config
                   "docs": f"{DOCS}/encryption_config_keys.txt"},
    "audit_policy": {"root": "k8s.io/apiserver/pkg/apis/audit/v1.Policy",  # --audit-policy-file
                     "docs": f"{DOCS}/audit_policy_keys.txt"},
    "egress_selector": {"root": f"{_V}/v1beta1.EgressSelectorConfiguration",  # --egress-selector-config-file
                        "docs": f"{DOCS}/egress_selector_config_keys.txt"},
    "tracing": {"root": f"{_V}/v1beta1.TracingConfiguration",  # --tracing-config-file
                "docs": f"{DOCS}/tracing_config_keys.txt"},
}
# argmatch's search caps per call (over testbeds.py's ARGMATCH_ARGS, which has none), as Caddy's: every API request
# goes through the instrumented functions (run 1's stack log of 2026-11-15: 2.7M lines), and the cluster waits on it
ARGMATCH_ARGS = {"MAXTIME": "2s", "MAXMEM": "1GB"}
GOLITS_PACKAGE = "./cmd/kube-apiserver"
GOLITS_TAGS = ""
TITLE = "Kubernetes API server, roots: its six versioned config files"
# apiVersion/kind (every file) and the audit Policy's metadata.* (decision, 2026-11-14)
META_TYPES = {
    "k8s.io/apimachinery/pkg/apis/meta/v1.TypeMeta": "apiVersion/kind: pick the Go type the file decodes into",
    "k8s.io/apimachinery/pkg/apis/meta/v1.ObjectMeta": "object metadata, accepted for interoperability, not config",
}


class CustomKeys:
    """No keys picked by custom unmarshalers: each file's type is picked by apiVersion/kind (meta keys), and the rest
    decodes by json tags"""

    names = {}
    parents = ()

    def __init__(self, module_dir):
        pass

    def of(self, fk):
        return None


def docs_kind(typ):
    """Kind of Go value we'd expect for a documented type (documentedkeys/README.md, "Types": string, []string, bool,
    int32, meta/v1.Duration (a duration string, a struct in Go), string:<Enum>, []string:Stage)"""
    t = typ.removeprefix("[]")
    if t == "bool":
        return "bool"
    if t.startswith(("int", "uint", "float")):
        return "number"
    if t.startswith("map"):
        return "map"
    return "string"


def graph_docs_reason(k, in_graph):
    """Why a key is only in the graph (in_graph) or only in the docs. As of v1.31.1 the only differences are the meta
    keys (META_TYPES)"""
    return "other: " + k.split(".")[-1]


UNSET_REASONS = []
