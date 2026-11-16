"""Fixtures for kube-apiserver's config roots in the prombench testbed (testbeds.py's KubeAPIServer): servers its
concrete configs point at, so that the config is used and sends messages. Each runs as a static pod on the host
network of kind's control-plane node (so it doesn't depend on the API server, and survives its restarts); the API
server reaches them by name, through its manifest's hostAliases (HOSTS: all 127.0.0.1).
- nginx (TLS, the CA ca.crt): ccfg-discovery.example.com:8443 serves the OIDC discovery document and JWKS for the
  authentication config's jwt issuer (ISSUER, signing key jwt.key); ccfg-authz.example.com:8443 is an authorization
  webhook that answers every SubjectAccessReview with no opinion (so the next authorizer decides).
- agnhost audit-proxy (the Kubernetes e2e images'): the audit webhook backend, http://ccfg-audit.example.com:8080.
- OpenTelemetry collector: OTLP gRPC at ccfg-otlp-collector.example.com:4317, for the tracing config.
- Kubernetes' mock KMS v2 plugin (staging/src/k8s.io/kms/internal/plugins/_mock, SoftHSM): KMS_ENDPOINT.
Keys: make_keys.py (test-only, committed). Run as a script, this is the JWT workload: requests to the API server with
tokens from the issuer (workload())."""

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import ssl

HERE = os.path.dirname(os.path.abspath(__file__))
HOSTS = ["ccfg-issuer.example.com", "ccfg-discovery.example.com", "ccfg-authz.example.com", "ccfg-audit.example.com",
         "ccfg-otlp-collector.example.com"]
ISSUER = "https://ccfg-issuer.example.com"
DISCOVERY = "https://ccfg-discovery.example.com:8443"
# A second issuer, whose claim mappings are CEL expressions: its discovery document is under ISSUER2_PATH (same JWKS)
ISSUER2 = "https://ccfg-issuer2.example.com"
ISSUER2_PATH = "/issuer2"
AUTHZ_URL = "https://ccfg-authz.example.com:8443/authorize"
AUDIT_URL = "http://ccfg-audit.example.com:8080/audit"
KMS_DIR = "/var/run/paramtrack-kms"  # on the node; the KMS socket's directory, mounted in the API server
KMS_ENDPOINT = f"unix://{KMS_DIR}/kms.socket"
NODE_DIR = "/etc/paramtrack-fixtures"  # on the node: the fixtures' files
KMS_IMAGE = "paramtrack-mock-kms:v1.31.1"
AGNHOST = "registry.k8s.io/e2e-test-images/agnhost:2.52"
NGINX = "nginx:1.27-alpine"
OTEL = "otel/opentelemetry-collector:0.114.0"
# The JWT workload's user: its tokens' claims, as the authentication config maps them (username and groups get its
# claim mappings' prefixes); bound to the view ClusterRole (RBAC)
JWT_GROUP = "ccfg-groups-prefix:ccfg-jwt-group"
JWT_GROUP2 = "ccfg-expr-group"  # the second issuer's tokens' group (its groups expression)
KID = "ccfg-jwt-key"

NGINX_CONF = """events {}
http {
  ssl_certificate %(dir)s/server.crt;
  ssl_certificate_key %(dir)s/server.key;
  server {
    listen 8443 ssl;
    server_name ccfg-discovery.example.com ccfg-issuer.example.com;
    location = /.well-known/openid-configuration { default_type application/json; alias %(dir)s/openid-configuration.json; }
    location = %(issuer2)s/.well-known/openid-configuration { default_type application/json; alias %(dir)s/openid-configuration-2.json; }
    location = /jwks { default_type application/json; alias %(dir)s/jwks.json; }
  }
  server {
    listen 8443 ssl;
    server_name ccfg-authz.example.com;
    location / {
      default_type application/json;
      return 200 '{"apiVersion":"authorization.k8s.io/v1","kind":"SubjectAccessReview","status":{"allowed":false}}';
    }
  }
}
"""

OTEL_CONF = """receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 127.0.0.1:4317
exporters:
  debug:
    verbosity: basic
service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [debug]
"""

SOFTHSM_CONF = {"path": "/usr/lib/softhsm/libsofthsm2.so", "tokenLabel": "kms-test", "pin": "kms-test"}


def pod(name, containers, volumes, init=None):
    """A static pod on the host network, its files from NODE_DIR"""
    spec = {"hostNetwork": True, "containers": containers,
            "volumes": [{"name": "fixtures", "hostPath": {"path": NODE_DIR, "type": "Directory"}}] + volumes}
    if init:
        spec["initContainers"] = init
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": f"paramtrack-{name}", "namespace": "kube-system", "labels": {"app": "paramtrack-fixture"}},
            "spec": spec}


FIXTURES_MOUNT = {"name": "fixtures", "mountPath": NODE_DIR, "readOnly": True}


def pods():
    """name => static pod (manifest JSON, which the kubelet reads as YAML)"""
    kms_init = ("set -e; if [ $(ls -1 /var/lib/softhsm/tokens | wc -l) -ge 1 ]; then exit 0; fi; "
                "apk add --no-cache ccid opensc softhsm jq; "
                f"C={NODE_DIR}/softhsm-config.json; L=$(jq -r .tokenLabel $C); P=$(jq -r .pin $C); "
                "softhsm2-util --init-token --free --label $L --pin $P --so-pin $P; "
                "pkcs11-tool --module $(jq -r .path $C) --keygen --key-type aes:32 --pin $P --token-label $L "
                "--label kms-test")
    tokens = {"name": "tokens", "hostPath": {"path": "/var/lib/softhsm/tokens", "type": "DirectoryOrCreate"}}
    return {
        "nginx": pod("nginx", [{"name": "nginx", "image": NGINX, "command": ["nginx", "-g", "daemon off;", "-c",
                                                                           f"{NODE_DIR}/nginx.conf"],
                                "volumeMounts": [FIXTURES_MOUNT]}], []),
        "audit": pod("audit", [{"name": "audit-proxy", "image": AGNHOST, "args": ["audit-proxy"],
                                "volumeMounts": [FIXTURES_MOUNT]}], []),
        "otel": pod("otel", [{"name": "otel", "image": OTEL, "args": [f"--config={NODE_DIR}/otel.yaml"],
                              "volumeMounts": [FIXTURES_MOUNT]}], []),
        "kms": pod("kms", [{"name": "kms", "image": KMS_IMAGE, "imagePullPolicy": "Never",
                            "args": [f"--listen-addr={KMS_ENDPOINT}",
                                     f"--config-file-path={NODE_DIR}/softhsm-config.json"],
                            "volumeMounts": [FIXTURES_MOUNT, {"name": "sock", "mountPath": KMS_DIR},
                                             {"name": "tokens", "mountPath": "/var/lib/softhsm/tokens"}]}],
                   [{"name": "sock", "hostPath": {"path": KMS_DIR, "type": "DirectoryOrCreate"}}, tokens],
                   init=[{"name": "init-softhsm", "image": "alpine:3.20", "command": ["/bin/sh", "-c", kms_init],
                          "volumeMounts": [FIXTURES_MOUNT, {"name": "tokens", "mountPath": "/var/lib/softhsm/tokens"}]}]),
    }


# Checks run in the node: each fixture is ready when its command succeeds
READY = {
    "nginx": f"curl -sf --cacert {NODE_DIR}/ca.crt --resolve ccfg-discovery.example.com:8443:127.0.0.1 "
             f"{DISCOVERY}/jwks",
    "audit": "curl -sf -o /dev/null -d '{\"apiVersion\":\"audit.k8s.io/v1\",\"kind\":\"EventList\",\"items\":[]}' "
             "http://127.0.0.1:8080/",
    "otel": "ss -ltn | grep -q 127.0.0.1:4317",
    "kms": f"test -S {KMS_DIR}/kms.socket",
}


def b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def jwt_key():
    from cryptography.hazmat.primitives import serialization
    return serialization.load_pem_private_key(open(f"{HERE}/jwt.key", "rb").read(), None)


def jwks():
    n = jwt_key().public_key().public_numbers()
    size = (n.n.bit_length() + 7) // 8
    return {"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": KID, "n": b64url(n.n.to_bytes(size, "big")),
                      "e": b64url(n.e.to_bytes(3, "big"))}]}


def token(claims):
    """An RS256 JWT from the issuer, with claims"""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    head = b64url(json.dumps({"alg": "RS256", "typ": "JWT", "kid": KID}).encode())
    body = b64url(json.dumps(claims).encode())
    sig = jwt_key().sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{b64url(sig)}"


def files():
    """name => content of the files the fixtures read, in NODE_DIR"""
    return {"ca.crt": open(f"{HERE}/ca.crt").read(), "server.crt": open(f"{HERE}/server.crt").read(),
            "server.key": open(f"{HERE}/server.key").read(), "nginx.conf": NGINX_CONF % {"dir": NODE_DIR, "issuer2": ISSUER2_PATH},
            "openid-configuration.json": json.dumps({"issuer": ISSUER, "jwks_uri": f"{DISCOVERY}/jwks",
                                                     "id_token_signing_alg_values_supported": ["RS256"]}),
            "openid-configuration-2.json": json.dumps({"issuer": ISSUER2, "jwks_uri": f"{DISCOVERY}/jwks",
                                                       "id_token_signing_alg_values_supported": ["RS256"]}),
            "jwks.json": json.dumps(jwks()), "otel.yaml": OTEL_CONF,
            "softhsm-config.json": json.dumps(SOFTHSM_CONF)}


def kubeconfig(server, ca=True):
    """A webhook's kubeconfig (JSON) for server, trusting ca.crt"""
    cluster = {"server": server}
    if ca:
        cluster["certificate-authority-data"] = base64.b64encode(open(f"{HERE}/ca.crt", "rb").read()).decode()
    return json.dumps({"apiVersion": "v1", "kind": "Config", "clusters": [{"name": "webhook", "cluster": cluster}],
                       "users": [{"name": "apiserver", "user": {}}],
                       "contexts": [{"name": "webhook", "context": {"cluster": "webhook", "user": "apiserver"}}],
                       "current-context": "webhook"}, indent=2)


def install(node, run, say, load, kms_source, ctx, timeout=600):
    """Installs the fixtures in the control-plane node (node: its container name), unless they're already there and
    ready, and binds the JWT workload's group to the view ClusterRole. run/say/load: testbeds' helpers (load(tag)
    loads a local image into the node); kms_source: the Kubernetes checkout whose mock KMS plugin to build. Needs the
    API server up (the binding)."""
    def in_node(cmd, check=True, input=None):
        p = subprocess.run(["docker", "exec", "-i", node, "sh", "-c", cmd], input=input, capture_output=True,
                           text=True, timeout=120)
        if check and p.returncode != 0:
            raise RuntimeError(f"in {node}: {cmd}: exit {p.returncode}: {p.stderr.strip()[-500:]}")
        return p.returncode == 0
    if subprocess.run(["docker", "image", "inspect", KMS_IMAGE], capture_output=True).returncode != 0:
        say(f"building {KMS_IMAGE} (Kubernetes' mock KMS plugin)")
        run(["docker", "build", "-f", f"{kms_source}/staging/src/k8s.io/kms/internal/plugins/_mock/Dockerfile",
             "-t", KMS_IMAGE, f"{kms_source}/staging/src/k8s.io/"], timeout=1800)
    load(KMS_IMAGE)  # (every time, it's small: `crictl images <ref>` lists every image when none matches)
    in_node(f"mkdir -p {NODE_DIR}")
    for name, content in files().items():
        in_node(f"cat > {NODE_DIR}/{name}", input=content)
    for name, p in pods().items():
        in_node(f"cat > /etc/kubernetes/manifests/paramtrack-{name}.yaml", input=json.dumps(p, indent=2))
    start = time.time()
    waiting = dict(READY)
    while waiting:
        waiting = {n: c for n, c in waiting.items() if not in_node(c, check=False)}
        if waiting and time.time() - start > timeout:
            raise RuntimeError(f"kube-apiserver fixtures not ready after {timeout}s: {', '.join(waiting)} (their "
                               f"static pods: crictl ps -a in {node})")
        if waiting:
            time.sleep(5)
    say(f"kube-apiserver fixtures ready ({', '.join(READY)})")
    with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
        json.dump({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
                   "metadata": {"name": "paramtrack-ccfg-jwt-group"},
                   "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "view"},
                   "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "Group", "name": g}
                                for g in (JWT_GROUP, JWT_GROUP2)]}, f)
        f.flush()
        run(["kubectl", "--context", ctx, "apply", "-f", f.name], timeout=120)


def workload(ctx, duration, log):
    """Sends requests to the API server (ctx's) for `duration` seconds with tokens from the issuer: one whose claims
    pass the authentication config's rules (the user can view), one its user validation rule rejects, one missing
    a required claim, and a valid one from the second issuer; every 5 seconds. Logs each response's status to log."""
    cfg = json.loads(subprocess.run(["kubectl", "config", "view", "--raw", "--minify", "--context", ctx, "-o", "json"],
                                    capture_output=True, text=True, check=True, timeout=60).stdout)
    cluster = cfg["clusters"][0]["cluster"]
    tls = ssl.create_default_context(cadata=base64.b64decode(cluster["certificate-authority-data"]).decode())
    now = int(time.time())
    base = {"iss": ISSUER, "aud": "ccfg-audience-a", "iat": now, "nbf": now, "exp": now + duration + 3600,
            "ccfg_required_claim": "ccfg-required-value", "ccfg_expr_claim": "ccfg-expr-value",
            "ccfg_username_claim": "ccfg-jwt-user", "ccfg_groups_claim": ["ccfg-jwt-group"],
            "ccfg_uid_claim": "ccfg-jwt-uid", "ccfg_extra_claim": "ccfg-jwt-extra", "sub": "ccfg-jwt-sub"}
    second = {"iss": ISSUER2, "aud": "ccfg-audience-c", "iat": now, "nbf": now, "exp": now + duration + 3600,
              "sub": "ccfg-jwt2-sub", "ccfg_expr_username": "ccfg-jwt2-user", "ccfg_expr_groups": [JWT_GROUP2],
              "ccfg_expr_uid": "ccfg-jwt2-uid"}
    tokens = {"valid": token(base), "valid (issuer 2)": token(second),
              "forbidden user": token({**base, "ccfg_username_claim": "ccfg-forbidden-user"}),
              "missing claim": token({k: v for k, v in base.items() if k != "ccfg_required_claim"})}
    with open(log, "w") as out:
        end = time.time() + duration
        while time.time() < end:
            for name, t in tokens.items():
                req = urllib.request.Request(f"{cluster['server']}/api/v1/namespaces/default/pods?limit=1",
                                             headers={"Authorization": f"Bearer {t}"})
                try:
                    code = urllib.request.urlopen(req, context=tls, timeout=30).status
                except urllib.error.HTTPError as e:
                    code = e.code
                except Exception as e:  # noqa: BLE001 (logged; the next round retries)
                    code = repr(e)
                out.write(f"{time.strftime('%H:%M:%S')} {name}: {code}\n")
                out.flush()
            time.sleep(5)


if __name__ == "__main__":
    # fixtures.py workload CTX SECONDS LOG
    if sys.argv[1:2] != ["workload"] or len(sys.argv) != 5:
        sys.exit("usage: fixtures.py workload CTX SECONDS LOG")
    workload(sys.argv[2], int(sys.argv[3]), sys.argv[4])
