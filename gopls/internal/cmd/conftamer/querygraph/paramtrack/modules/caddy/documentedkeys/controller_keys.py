#!/usr/bin/env python3
"""Controller-side keys for the Caddy ingress controller (github.com/caddyserver/ingress).

See README.md (same directory) for what each output is and how it was decided.

Usage (needs PyYAML):
  uv run --with pyyaml python controller_keys.py --ingress-dir DIR

Reads only git-tracked files of the controller checkout, and caddy_keys_full.txt
(written by list_caddy_keys.py) to type-check the controller -> Caddy mapping.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent


def tracked(ingress, rel):
    files = subprocess.run(["git", "ls-files", "--error-unmatch", rel], cwd=ingress,
                           capture_output=True, text=True, timeout=30)
    if files.returncode != 0:
        raise SystemExit(f"{rel} is not git-tracked in {ingress}")
    return (Path(ingress) / rel).read_text()


def write(name, rows):
    with open(HERE / name, "w") as f:
        for row in rows:
            f.write("\t".join(row) + "\n")
    print(f"{name}: {len(rows)} rows", file=sys.stderr)


# ---------------------------------------------------------------- documented

def chart_readme_values(ingress):
    """Helm values table of charts/caddy-ingress-controller/README.md (helm-docs output)."""
    out = {}
    for line in tracked(ingress, "charts/caddy-ingress-controller/README.md").splitlines():
        m = re.match(r"\|\s*([\w.\[\]]+)\s*\|\s*(\w+)\s*\|", line)
        if m and m.group(1) != "Key":
            out[re.sub(r"\[\d+\]", "", m.group(1))] = m.group(2)  # list index left out
    return out


def values_yaml(ingress):
    """Leaves of the chart's values.yaml (the source helm-docs generates the table from)."""
    data = yaml.safe_load(tracked(ingress, "charts/caddy-ingress-controller/values.yaml"))
    out = {}

    def walk(v, path):
        if isinstance(v, dict) and v:
            for k, c in v.items():
                walk(c, path + [k])
        else:
            t = {bool: "bool", int: "int", str: "string", list: "list", dict: "object",
                 type(None): "null"}[type(v)]
            out[".".join(path)] = t

    walk(data, [])
    return out


def readme_config_mentions(ingress):
    """ConfigMap keys named in README.md prose: `--set ingressController.config.X` and
    backticked camelCase words on lines that say "config"."""
    keys = set()
    for line in tracked(ingress, "README.md").splitlines():
        keys.update(re.findall(r"ingressController\.config\.(\w+)", line))
        if "config" in line:
            keys.update(k for k in re.findall(r"`(\w+)`", line) if re.match(r"[a-z]+[A-Z]", k))
    return keys


def chart_flags(ingress):
    """Controller CLI flags the chart's deployment template passes, with the helm value
    each comes from (None if constant)."""
    out = {}
    tpl = tracked(ingress, "charts/caddy-ingress-controller/templates/deployment.yaml")
    for m in re.finditer(r"^\s*- -([a-z-]+)(=?)(.*)$", tpl, re.M):
        v = re.search(r"\.Values\.([\w.]+)", m.group(3))
        out[m.group(1)] = v.group(1) if v else None
        if not m.group(2) and not v:  # boolean flag like `- -verbose` inside an if
            prev = tpl[:m.start()].rsplit("{{- if .Values.", 1)[-1]
            out[m.group(1)] = prev.split()[0]
    return out


def documented(ingress):
    readme_tbl = chart_readme_values(ingress)
    vals = values_yaml(ingress)
    helm = []
    for k in sorted(set(readme_tbl) | set(vals)):
        src = "+".join(s for s, d in (("chart-README", readme_tbl), ("values.yaml", vals)) if k in d)
        helm.append((k, readme_tbl.get(k) or vals[k], src))
    write("controller_helm_values.txt", [(k, t) for k, t, _ in helm])

    tpl = tracked(ingress, "charts/caddy-ingress-controller/templates/configmap.yaml")
    assert "range keys .Values.ingressController.config" in tpl, "configmap template changed"
    cm_types = {}
    for k, t, _ in helm:
        if k.startswith("ingressController.config."):
            cm_types[k.split(".", 2)[2]] = t
    for k in readme_config_mentions(ingress):
        cm_types.setdefault(k, "string")  # README gives no type; onDemandAsk is a URL
    rows = [(f"configmap.{k}", t) for k, t in sorted(cm_types.items())]
    types = {k: t for k, t, _ in helm}
    for flag, val in sorted(chart_flags(ingress).items()):
        rows.append((f"flag.{flag}", types.get(val, "string") if val else "string"))
    write("controller_documented_keys.txt", rows)
    return {r[0] for r in rows}


# ------------------------------------------------------------- from the code

def code_keys(ingress):
    """Controller inputs as the code defines them (regex over the Go source)."""
    rows = []
    src = tracked(ingress, "pkg/store/configmap_parser.go")
    struct = src.split("type ConfigMapOptions struct", 1)[1].split("}", 1)[0]
    for name, typ, tag in re.findall(r"^\s*(\w+)\s+([\w.]+)\s+`json:\"(\w+)", struct, re.M):
        rows.append((f"configmap.{tag}", typ))
    src = tracked(ingress, "cmd/caddy/flag.go")
    for kind, name in re.findall(r"flag\.(\w+)Var\(&\w+,\s*\"([\w-]+)\"", src):
        rows.append((f"flag.{name}", kind.lower()))
    src = tracked(ingress, "internal/caddy/ingress/annotations.go")
    prefix = re.search(r'annotationPrefix\s*=\s*"([^"]+)"', src).group(1)
    for name in re.findall(r'^\s*\w+Annotation\w*\s*=\s*"([\w-]+)"|^\s*\w+\s*=\s*"([\w-]+)"', src, re.M):
        name = name[0] or name[1]
        if name != prefix:
            rows.append((f"annotation.{prefix}/{name}", "string"))
    src = tracked(ingress, "internal/k8s/ingress.go")
    for a in re.findall(r'Annotations\["([^"]+)"\]', src):
        rows.append((f"annotation.{a}", "string"))
    write("controller_code_keys.txt", sorted(set(rows)))
    return {r[0] for r in rows}


# --------------------------------------------------- controller -> Caddy JSON

# Hand-written from reading the controller at faf444a. Columns: controller input
# (or "constant"), Caddy key (same notation as caddy_keys_full.txt), code location.
# HS = the ingress_server / metrics_server entries of apps.http.servers.*.
HS = "apps.http.servers.*"
RP = f"{HS}.routes.handle.handler=reverse_proxy"
ACME = "apps.tls.automation.policies.issuers.module=acme"
SS = "storage.module=secret_store"
MAPPING = [
    ("constant", "storage.module", "internal/caddy/global/secrets_store.go:23"),
    ("env POD_NAMESPACE / flag.config-map namespace", f"{SS}.namespace", "internal/caddy/global/secrets_store.go:23, internal/controller/controller.go:115"),
    ("flag.lease-id", f"{SS}.leaseId", "internal/caddy/global/secrets_store.go:23"),
    ("constant", f"{HS}.listen", "pkg/converter/config.go:45-60"),
    ("constant", f"{HS}.automatic_https.disable", "pkg/converter/config.go:58 (metrics_server)"),
    ("constant", f"{HS}.routes.match.path", "internal/caddy/global/healthz.go, metrics.go (metrics_server)"),
    ("constant", f"{HS}.routes.handle.handler", "every route handler"),
    ("constant", f"{HS}.routes.handle.handler=static_response.status_code", "internal/caddy/global/healthz.go (metrics_server /healthz)"),
    ("configmap.metrics", f"{HS}.routes.handle.handler=metrics", "internal/caddy/global/metrics.go (metrics_server /metrics)"),
    ("configmap.debug", "logging.logs.*.level", "internal/caddy/global/configmap.go:33 (log \"default\", DEBUG)"),
    ("configmap.acmeCA", f"{ACME}.ca", "internal/caddy/global/configmap.go:40"),
    ("configmap.email", f"{ACME}.email", "internal/caddy/global/configmap.go:52"),
    ("configmap.acmeCA / configmap.email", "apps.tls.automation.policies.issuers.module", "internal/caddy/global/configmap.go:71 (module acme)"),
    ("configmap.acmeEABKeyId", f"{ACME}.external_account.key_id", "internal/caddy/global/configmap.go:45"),
    ("configmap.acmeEABMacKey", f"{ACME}.external_account.mac_key", "internal/caddy/global/configmap.go:45"),
    ("configmap.onDemandTLS", "apps.tls.automation.policies.on_demand", "internal/caddy/global/configmap.go:73"),
    ("configmap.onDemandAsk", "apps.tls.automation.on_demand.ask", "internal/caddy/global/configmap.go:57"),
    ("configmap.ocspCheckInterval", "apps.tls.automation.ocsp_interval", "internal/caddy/global/configmap.go:66"),
    ("configmap.proxyProtocol", f"{HS}.listener_wrappers.wrapper", "internal/caddy/global/configmap.go:80"),
    ("configmap.proxyProtocol", f"{HS}.listener_wrappers.wrapper=proxy_protocol", "internal/caddy/global/configmap.go:80 (no fields set)"),
    ("configmap.proxyProtocol", f"{HS}.listener_wrappers.wrapper=tls", "internal/caddy/global/configmap.go:81"),
    ("configmap.experimentalSmartSort", f"{HS}.routes", "internal/caddy/global/ingress_sort.go (reorders routes; sets no key)"),
    ("Ingress spec.tls[].hosts", f"{HS}.automatic_https.skip_certificates", "internal/caddy/global/tls.go:44"),
    ("Ingress spec.tls[].hosts + TLS secrets", "apps.tls.certificates.load_folders", "internal/caddy/global/tls.go:43"),
    ("Ingress spec.rules[].host", f"{HS}.routes.match.host", "internal/caddy/ingress/matcher.go"),
    ("Ingress spec.rules[].http.paths[].path, pathType", f"{HS}.routes.match.path", "internal/caddy/ingress/matcher.go"),
    ("annotation caddy.ingress.kubernetes.io/disable-ssl-redirect", f"{HS}.routes.match.protocol", "internal/caddy/ingress/matcher.go (set to https unless \"true\")"),
    ("Ingress backend.service.name, port.number, metadata.namespace", f"{RP}.upstreams.dial", "internal/caddy/ingress/reverseproxy.go"),
    ("constant", f"{RP}.transport.protocol", "internal/caddy/ingress/reverseproxy.go (http)"),
    ("annotation caddy.ingress.kubernetes.io/backend-protocol", f"{RP}.transport.protocol=http.tls", "internal/caddy/ingress/reverseproxy.go (set if https)"),
    ("annotation caddy.ingress.kubernetes.io/insecure-skip-verify", f"{RP}.transport.protocol=http.tls.insecure_skip_verify", "internal/caddy/ingress/reverseproxy.go (default true!)"),
    ("annotation caddy.ingress.kubernetes.io/trusted-proxies", f"{RP}.trusted_proxies", "internal/caddy/ingress/reverseproxy.go"),
    ("annotation caddy.ingress.kubernetes.io/rewrite-to", f"{HS}.routes.handle.handler=rewrite.uri", "internal/caddy/ingress/rewrite.go"),
    ("annotation caddy.ingress.kubernetes.io/rewrite-strip-prefix", f"{HS}.routes.handle.handler=rewrite.strip_path_prefix", "internal/caddy/ingress/rewrite.go"),
    ("annotation caddy.ingress.kubernetes.io/permanent-redirect, temporal-redirect", f"{HS}.routes.handle.handler=static_response.headers", "internal/caddy/ingress/redirect.go (Location)"),
    ("annotation caddy.ingress.kubernetes.io/permanent-redirect-code, permanent-redirect, temporal-redirect", f"{HS}.routes.handle.handler=static_response.status_code", "internal/caddy/ingress/redirect.go"),
]


def mapping():
    full = {}
    for line in (HERE / "caddy_keys_full.txt").read_text().splitlines():
        k, t = line.split("\t")
        full[k] = t
    # Keys that only group others (not leaves in caddy_keys_full.txt) but which the
    # controller sets as a whole.
    grouping = {f"{HS}.listener_wrappers.wrapper=proxy_protocol": "{}",
                f"{HS}.routes": "[]caddyhttp.Route",
                f"{RP}.transport.protocol=http.tls": "reverseproxy.TLSConfig",
                f"{HS}.routes.handle.handler=metrics": "metrics.Metrics"}
    rows, keys = [], {}
    for src, key, where in MAPPING:
        if key.startswith(SS):
            typ = "string:controller-module-undocumented"
        elif key in full:
            typ = full[key]
        elif key in grouping:
            typ = grouping[key]
        else:
            raise SystemExit(f"mapping key not in caddy_keys_full.txt: {key}")
        rows.append((src, key, where))
        keys[key] = typ
    write("controller_to_caddy.tsv", [("controller_input", "caddy_key", "code")] + rows)
    write("caddy_keys_controller.txt", sorted(keys.items()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ingress-dir", required=True)
    args = ap.parse_args()
    doc = documented(args.ingress_dir)
    code = code_keys(args.ingress_dir)
    print(f"in code, not documented: {sorted(code - doc)}", file=sys.stderr)
    print(f"documented, not in code: {sorted(doc - code)}", file=sys.stderr)
    mapping()


if __name__ == "__main__":
    main()
