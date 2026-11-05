#!/usr/bin/env python3
"""Generate the concrete config for Prometheus on prombench: upstream's shipped config's text, unchanged, with keys
inserted into `global`, new list entries in alerting.alertmanagers and scrape_configs, a top-level
alerting.alert_relabel_configs, and new remote_write, remote_read, tracing, otlp and scrape_config_files blocks. Also
writes the files the new entries refer to (added to the prometheus-meta ConfigMap, so they appear under
/etc/prometheus/config/).

Each choice is a row in decisions.md (next to this file). Values come from the skill's scripts/value.py (token());
deviations are noted where they're made. Index rule: a concrete key's value gets -<n> when n earlier new entries set
the same concrete key; names (job_name, remote `name`) use their list index instead.

Usage: uv run --with pyyaml --with pycryptodome --with cryptography python gen.py <shipped.yaml> <out config.yaml> \\
         <out files dir> [--omit am,arc,sc,rw,rr,tracing,otlp,global,scf]
"""
import datetime
import hashlib
import json
import os
import sys
import uuid

import yaml
from Crypto.PublicKey import RSA
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID

sys.path.insert(0, "/home/emily/projects/config_tracing/golang.org-x-tools/.claude/skills/concreteconfig/scripts")
from value import token  # noqa: E402

DIR = "/etc/prometheus/config"  # added files (read-only ConfigMap mount)
DATA = "/data"  # writable: the TSDB volume, for log files Prometheus writes
SA = "/var/run/secrets/kubernetes.io/serviceaccount"
API = "https://kubernetes.default.svc:443"
# The same API server under another name in its cert, so the two SD sections' api_server values differ
API_SC = "https://kubernetes.default.svc.cluster.local:443"
# proxy_connect_header needs proxy_url (ProxyConfig.Validate); no_proxy bypasses it for every address Prometheus
# contacts here (pod IPs 10.244/16, kind node IPs 172.18/16, *.svc names), so it's never dialed (see proxy()).
NO_PROXY = "localhost,127.0.0.1,10.0.0.0/8,172.16.0.0/12,.svc,.cluster.local"
# In-cluster HTTP server for token requests that must fail without leaving the testbed (Loki answers 404)
TOKEN_HOST = "http://loki.default.svc:3100"
LOKI = "loki.default.svc:3100"  # scrape target for new jobs: accepts any credentials
SINK = "http://prometheus-sink.prombench-16075.svc:9011/sink/prw"  # remote write receiver: checks no credentials
SCRAPE_PROTOCOLS = ["OpenMetricsText1.0.0", "OpenMetricsText0.0.1", "PrometheusText1.0.0", "PrometheusText0.0.4"]

files = {}  # ConfigMap key => content


def v(key, i=0):
    return token(key, i)


def label(key, i=0):
    return token(key, i).replace("-", "_")


def path(key, content, i=0, suffix=""):
    """A file added to the ConfigMap for a *_file key"""
    t = token(key, i) + suffix
    files[t] = content
    return f"{DIR}/{t}"


def secret_file(key, i=0):
    return path(key, token(key, i) + "-content\n", i)


def tls_files(p, i=0, ca=True, server_name=False):
    """tls_config with ca_file (unless ca=False), cert_file, key_file as added files (a throwaway self-signed pair, so
    the cert is also its CA), and server_name"""
    t = {}
    if ca:
        t["ca_file"] = path(f"{p}.tls_config.ca_file", CERT, i)
    t["cert_file"] = path(f"{p}.tls_config.cert_file", CERT, i)
    t["key_file"] = path(f"{p}.tls_config.key_file", KEY, i)
    if server_name:
        t["server_name"] = v(f"{p}.tls_config.server_name", i)
    return t


def http_headers(p, i=0):
    h = token(f"{p}.http_headers.*")
    return {"http_headers": {h: {"values": [v(f"{p}.http_headers.{h}.values", i)],
                                 "secrets": [v(f"{p}.http_headers.{h}.secrets", i)],
                                 "files": [secret_file(f"{p}.http_headers.{h}.files", i)]}}}


def proxy(p, i=0):
    """proxy settings for parent key p: the proxy host is value.py's (it doesn't resolve, and is never dialed), and
    no_proxy gets a value.py host of its own appended, so each key's value is distinct"""
    h = token(f"{p}.proxy_connect_header.*")
    return {"proxy_url": f"http://{token(f'{p}.proxy_url', i)}.invalid:3128",
            "no_proxy": f"{NO_PROXY},{token(f'{p}.no_proxy', i)}.invalid",
            "proxy_connect_header": {h: [v(f"{p}.proxy_connect_header.{h}", i)]}}


def relabel(p, i=0, action="replace"):
    """A relabel rule that only writes a new label (default regex, so it matches the empty source value)"""
    return {"source_labels": [label(f"{p}.source_labels", i)], "separator": v(f"{p}.separator", i),
            "target_label": label(f"{p}.target_label", i), "replacement": v(f"{p}.replacement", i), "action": action}


def oauth2(p, i=0, full=True):
    """oauth2 block for parent key p (e.g. scrape_configs.oauth2). The token request goes to Loki, which answers 404,
    so the token fetch fails inside the testbed. full: client_secret, scopes, endpoint_params, proxy settings and
    tls_config; else client_secret_file only."""
    o = {"client_id": v(f"{p}.client_id", i), "token_url": f"{TOKEN_HOST}/{v(f'{p}.token_url', i)}"}
    if full:
        o["client_secret"] = v(f"{p}.client_secret", i)
        o["scopes"] = [v(f"{p}.scopes", i)]
        k = token(f"{p}.endpoint_params.*")
        o["endpoint_params"] = {k: v(f"{p}.endpoint_params.{k}", i)}
        o.update(proxy(p, i))
        o["tls_config"] = tls_files(p, i, server_name=True)
    else:
        o["client_secret_file"] = secret_file(f"{p}.client_secret_file", i)
    return o


def kubeconfig():
    return yaml.safe_dump({
        "apiVersion": "v1", "kind": "Config",
        "clusters": [{"name": "in-cluster", "cluster": {"server": API, "certificate-authority": f"{SA}/ca.crt"}}],
        "users": [{"name": "prometheus-meta", "user": {"tokenFile": f"{SA}/token"}}],
        "contexts": [{"name": "in-cluster", "context": {"cluster": "in-cluster", "user": "prometheus-meta"}}],
        "current-context": "in-cluster"}, sort_keys=False)


def k8s_sd(role, ns, selector, api=API, **http):
    """A kubernetes_sd_configs entry against the API server (api_server is needed with HTTP client settings:
    kubernetes.SDConfig.UnmarshalYAML); http: its HTTP client settings"""
    sd = {"role": role, "api_server": api, "namespaces": {"names": [ns]}, "selectors": [selector]}
    sd.update(http)
    sd.setdefault("tls_config", {"ca_file": f"{SA}/ca.crt"})
    return sd


# ---------------------------------------------------------------------------------------------------------------
# global: keys the shipped config leaves unset. Enums at their defaults; log files in the writable /data.
def global_block():
    g = token("global.external_labels.*").replace("-", "_")
    return {"external_labels": {g: v(f"global.external_labels.{g}")},
            "metric_name_validation_scheme": "utf8",
            "query_log_file": f"{DATA}/{token('global.query_log_file')}",
            "scrape_failure_log_file": f"{DATA}/{token('global.scrape_failure_log_file')}",
            "scrape_protocols": SCRAPE_PROTOCOLS}


# ---------------------------------------------------------------------------------------------------------------
# alerting.alertmanagers: one new entry per auth alternative. Each discovers the Alertmanager pod with the same SD
# config (so they share one watch; its bearer_token_file is the service account token, so it works), and sends to
# path_prefix /alertmanager, as the shipped entry does via __alerts_path__. Extra SD configs cover the SD client's
# alternatives; all but kubeconfig_file fail to authenticate (401 or token fetch error) and discover nothing.
AM = "alerting.alertmanagers"
AMSD = f"{AM}.kubernetes_sd_configs"
AM_POD = {"role": "pod", "label": "app=alertmanager", "field": "status.phase=Running"}


def am(auth, extra_sd=None, base=False):
    e = {"kubernetes_sd_configs": [k8s_sd("pod", "default", AM_POD, bearer_token_file=f"{SA}/token")]}
    if extra_sd:
        e["kubernetes_sd_configs"].append(extra_sd)
    e["path_prefix"] = "/alertmanager"
    if base:
        e.update({"api_version": "v2", "scheme": "http"})
        e.update(http_headers(AM))
        e.update(proxy(AM))
        e["relabel_configs"] = [relabel(f"{AM}.relabel_configs")]
        e["alert_relabel_configs"] = [relabel(f"{AM}.alert_relabel_configs")]
    e.update(auth)
    return e


def alertmanagers():
    return [
        am({"authorization": {"type": v(f"{AM}.authorization.type"), "credentials": v(f"{AM}.authorization.credentials")},
            "tls_config": tls_files(AM, server_name=True)},
           k8s_sd("pod", "default", AM_POD,
                  authorization={"type": v(f"{AMSD}.authorization.type"),
                                 "credentials": v(f"{AMSD}.authorization.credentials")},
                  tls_config={"ca_file": f"{SA}/ca.crt", **tls_files(AMSD, ca=False, server_name=True)},
                  **http_headers(AMSD), **proxy(AMSD)),
           base=True),
        am({"authorization": {"credentials_file": secret_file(f"{AM}.authorization.credentials_file")}},
           k8s_sd("pod", "default", AM_POD, basic_auth={"username": v(f"{AMSD}.basic_auth.username"),
                                                        "password": v(f"{AMSD}.basic_auth.password")})),
        am({"basic_auth": {"username": v(f"{AM}.basic_auth.username"),
                           "password_file": secret_file(f"{AM}.basic_auth.password_file")}},
           k8s_sd("pod", "default", AM_POD, basic_auth={"username_file": secret_file(f"{AMSD}.basic_auth.username_file"),
                                                        "password_file": secret_file(f"{AMSD}.basic_auth.password_file")})),
        am({"basic_auth": {"username_file": secret_file(f"{AM}.basic_auth.username_file"),
                           "password": v(f"{AM}.basic_auth.password")}},
           k8s_sd("pod", "default", AM_POD, bearer_token=v(f"{AMSD}.bearer_token"))),
        am({"bearer_token": v(f"{AM}.bearer_token")},
           # kubeconfig_file excludes api_server and any HTTP client setting (kubernetes.SDConfig.UnmarshalYAML)
           {"role": "pod", "kubeconfig_file": path(f"{AMSD}.kubeconfig_file", kubeconfig()),
            "namespaces": {"names": ["default"]}, "selectors": [AM_POD]}),
        am({"bearer_token_file": secret_file(f"{AM}.bearer_token_file")},
           k8s_sd("pod", "default", AM_POD, authorization={"credentials_file": secret_file(f"{AMSD}.authorization.credentials_file")})),
        am({"oauth2": oauth2(f"{AM}.oauth2")},
           k8s_sd("pod", "default", AM_POD, oauth2=oauth2(f"{AMSD}.oauth2"))),
        am({"oauth2": oauth2(f"{AM}.oauth2", 1, full=False)},
           k8s_sd("pod", "default", AM_POD, oauth2=oauth2(f"{AMSD}.oauth2", 1, full=False))),
        # role_arn skipped: it makes the signer call AWS STS, outside the testbed
        am({"sigv4": {"region": v(f"{AM}.sigv4.region"), "access_key": v(f"{AM}.sigv4.access_key"),
                      "secret_key": v(f"{AM}.sigv4.secret_key"), "profile": v(f"{AM}.sigv4.profile")}}),
    ]


# ---------------------------------------------------------------------------------------------------------------
# scrape_configs: new jobs scraping Loki (static target), one per auth alternative the shipped jobs don't use. The
# first also holds the job-wide keys and the other SD mechanisms. Extra SD configs cover the SD client's
# alternatives: those that authenticate (authorization.credentials_file and bearer_token_file with the service
# account token, kubeconfig_file) discover only the loki service; the rest fail.
SC = "scrape_configs"
SCSD = f"{SC}.kubernetes_sd_configs"
LOKI_SVC = {"role": "service", "label": "app=loki", "field": "metadata.name=loki"}
N_SHIPPED_JOBS = 4


def job(n, auth, sds, extra=None):
    i = N_SHIPPED_JOBS + n  # list index of the new job
    e = {"job_name": v(f"{SC}.job_name", i)}
    e.update(extra or {})
    e.update(auth)
    e.setdefault("static_configs", [{"targets": [LOKI]}])
    e["kubernetes_sd_configs"] = sds
    return e


def scrape_configs():
    sl = label(f"{SC}.static_configs.labels.*")
    pk = token(f"{SC}.params.*")
    base = {
        "scheme": "http", "fallback_scrape_protocol": "PrometheusText0.0.4", "metric_name_validation_scheme": "utf8",
        "scrape_protocols": SCRAPE_PROTOCOLS, "params": {pk: [v(f"{SC}.params.{pk}")]},
        "scrape_failure_log_file": f"{DATA}/{token(f'{SC}.scrape_failure_log_file')}",
        **http_headers(SC), **proxy(SC),
        "static_configs": [{"targets": [LOKI], "labels": {sl: v(f"{SC}.static_configs.labels.{sl}")}}],
        # Real names, not value.py's: the DNS SD and file SD targets are scraped, so their keys influence messages
        "dns_sd_configs": [{"names": ["loki-headless.default.svc.cluster.local"], "type": "A", "port": 3100}],
        # file SD names must end in .json/.yml/.yaml (discovery/file)
        "file_sd_configs": [{"files": [path(f"{SC}.file_sd_configs.files",
                                            json.dumps([{"targets": [LOKI]}]) + "\n", suffix=".json")]}],
        "relabel_configs": [relabel(f"{SC}.relabel_configs")],
        "metric_relabel_configs": [relabel(f"{SC}.metric_relabel_configs")],
    }
    return [
        job(0, {"authorization": {"type": v(f"{SC}.authorization.type"), "credentials": v(f"{SC}.authorization.credentials")},
                "tls_config": tls_files(SC, server_name=True)},
            [k8s_sd("service", "default", LOKI_SVC, API_SC, authorization={"credentials_file": f"{SA}/token"}),
             k8s_sd("service", "default", LOKI_SVC, API_SC,
                    authorization={"type": v(f"{SCSD}.authorization.type"),
                                   "credentials": v(f"{SCSD}.authorization.credentials")},
                    tls_config={"ca_file": f"{SA}/ca.crt", **tls_files(SCSD, ca=False, server_name=True)},
                    **http_headers(SCSD), **proxy(SCSD))],
            base),
        job(1, {"basic_auth": {"username": v(f"{SC}.basic_auth.username"),
                               "password_file": secret_file(f"{SC}.basic_auth.password_file")}},
            [k8s_sd("service", "default", LOKI_SVC, API_SC, basic_auth={"username": v(f"{SCSD}.basic_auth.username"),
                                                                "password": v(f"{SCSD}.basic_auth.password")})]),
        job(2, {"basic_auth": {"username_file": secret_file(f"{SC}.basic_auth.username_file"),
                               "password": v(f"{SC}.basic_auth.password")}},
            [k8s_sd("service", "default", LOKI_SVC, API_SC,
                    basic_auth={"username_file": secret_file(f"{SCSD}.basic_auth.username_file"),
                                "password_file": secret_file(f"{SCSD}.basic_auth.password_file")})]),
        # metrics_path: value.py's (Loki answers 404), as /metrics is a code literal
        job(3, {"bearer_token": v(f"{SC}.bearer_token")},
            [k8s_sd("service", "default", LOKI_SVC, API_SC, bearer_token=v(f"{SCSD}.bearer_token"))],
            {"metrics_path": "/" + v(f"{SC}.metrics_path")}),
        job(4, {"bearer_token_file": secret_file(f"{SC}.bearer_token_file")},
            [k8s_sd("service", "default", LOKI_SVC, API_SC, bearer_token_file=f"{SA}/token")]),
        job(5, {"authorization": {"credentials_file": secret_file(f"{SC}.authorization.credentials_file")}},
            [{"role": "service", "kubeconfig_file": path(f"{SCSD}.kubeconfig_file", kubeconfig()),
              "namespaces": {"names": ["default"]}, "selectors": [LOKI_SVC]},
             k8s_sd("service", "default", LOKI_SVC, API_SC,
                    authorization={"credentials_file": secret_file(f"{SCSD}.authorization.credentials_file")})]),
        job(6, {"oauth2": oauth2(f"{SC}.oauth2")},
            [k8s_sd("service", "default", LOKI_SVC, API_SC, oauth2=oauth2(f"{SCSD}.oauth2"))]),
        job(7, {"oauth2": oauth2(f"{SC}.oauth2", 1, full=False)},
            [k8s_sd("service", "default", LOKI_SVC, API_SC, oauth2=oauth2(f"{SCSD}.oauth2", 1, full=False))]),
    ]


# ---------------------------------------------------------------------------------------------------------------
# remote_write: new entries to the sink (which checks no credentials), sending only `up`, one per auth alternative.
# The first also holds the entry-wide keys.
RW = "remote_write"


def rw(i, auth, base=False):
    e = {"url": SINK, "name": v(f"{RW}.name", i),
         "write_relabel_configs": [{"source_labels": ["__name__"], "regex": "up", "action": "keep"}]}
    if base:
        h = token(f"{RW}.headers.*")
        e["headers"] = {h: v(f"{RW}.headers.{h}")}
        e["protobuf_message"] = "prometheus.WriteRequest"
        e["write_relabel_configs"].append(relabel(f"{RW}.write_relabel_configs"))
        e.update(http_headers(RW))
        e.update(proxy(RW))
    e.update(auth)
    return e


def google_creds():
    """A service account key file with a throwaway RSA key, whose token_uri is in the cluster (Loki, 404)"""
    t = token(f"{RW}.google_iam.credentials_file")
    return json.dumps({
        "type": "service_account", "project_id": t + "-project", "private_key_id": "0", "private_key": RSA_KEY,
        "client_email": f"{t}@ccfg.iam.gserviceaccount.com", "client_id": "0",
        "token_uri": f"{TOKEN_HOST}/{t}-token-uri"}, indent=2) + "\n"


def det_rsa(seed):
    """A throwaway 2048-bit RSA key, the same for the same seed: pycryptodome's RSA.generate with its randomness
    drawn from SHA-256 in counter mode over the seed, so reruns reproduce it without keeping a key in the repo"""
    buf, n = b"", 0

    def randfunc(k):
        nonlocal buf, n
        while len(buf) < k:
            buf += hashlib.sha256(f"{seed}:{n}".encode()).digest()
            n += 1
        out, buf = buf[:k], buf[k:]
        return out

    return RSA.generate(2048, randfunc=randfunc)


def det_cert(seed):
    """A throwaway self-signed cert and its key (PEM), deterministic: RSA PKCS#1 v1.5 signatures and fixed fields"""
    key = serialization.load_pem_private_key(det_rsa(seed).export_key(format="PEM", pkcs=8), password=None)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, seed)])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
            .not_valid_after(datetime.datetime(2036, 1, 1, tzinfo=datetime.timezone.utc))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    return cert.public_bytes(serialization.Encoding.PEM).decode(), key_pem.decode()


def det_uuid(key):
    """azuread client_id must be a UUID (AzureADConfig.Validate): a UUID derived from value.py's token"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, token(key)))


def remote_write():
    return [
        rw(0, {"authorization": {"type": v(f"{RW}.authorization.type"), "credentials": v(f"{RW}.authorization.credentials")},
               "tls_config": tls_files(RW, server_name=True)}, base=True),
        rw(1, {"authorization": {"credentials_file": secret_file(f"{RW}.authorization.credentials_file")}}),
        rw(2, {"basic_auth": {"username": v(f"{RW}.basic_auth.username"),
                              "password_file": secret_file(f"{RW}.basic_auth.password_file")}}),
        rw(3, {"basic_auth": {"username_file": secret_file(f"{RW}.basic_auth.username_file"),
                              "password": v(f"{RW}.basic_auth.password")}}),
        rw(4, {"bearer_token": v(f"{RW}.bearer_token")}),
        rw(5, {"bearer_token_file": secret_file(f"{RW}.bearer_token_file")}),
        rw(6, {"google_iam": {"credentials_file": path(f"{RW}.google_iam.credentials_file", google_creds())}}),
        # cloud is an enum (AzurePublic is the default). managed_identity and sdk get tokens from the instance
        # metadata address (169.254.169.254, the VM's own); azuread.oauth is skipped: it asks login.microsoftonline.com.
        rw(7, {"azuread": {"cloud": "AzurePublic",
                           "managed_identity": {"client_id": det_uuid(f"{RW}.azuread.managed_identity.client_id")}}}),
        rw(8, {"azuread": {"sdk": {"tenant_id": v(f"{RW}.azuread.sdk.tenant_id")}}}),
        rw(9, {"oauth2": oauth2(f"{RW}.oauth2")}),
        rw(10, {"oauth2": oauth2(f"{RW}.oauth2", 1, full=False)}),
        # role_arn skipped (AWS STS, outside the testbed)
        rw(11, {"sigv4": {"region": v(f"{RW}.sigv4.region"), "access_key": v(f"{RW}.sigv4.access_key"),
                          "secret_key": v(f"{RW}.sigv4.secret_key"), "profile": v(f"{RW}.sigv4.profile")}}),
    ]


# ---------------------------------------------------------------------------------------------------------------
# remote_read: inert new entries, one per auth alternative: each requires its own matcher, which no query has, so
# none is ever read from (the URL doesn't resolve either). The first also holds the entry-wide keys.
RR = "remote_read"


def rr(i, auth, base=False):
    m = label(f"{RR}.required_matchers.*", i)
    e = {"url": f"http://{token(f'{RR}.url', i)}.invalid/api/v1/read", "name": v(f"{RR}.name", i),
         "required_matchers": {m: v(f"{RR}.required_matchers.{m}", i)}}
    if base:
        h = token(f"{RR}.headers.*")
        e["headers"] = {h: v(f"{RR}.headers.{h}")}
        e.update(http_headers(RR))
        e.update(proxy(RR))
    e.update(auth)
    return e


def remote_read():
    return [
        rr(0, {"authorization": {"type": v(f"{RR}.authorization.type"), "credentials": v(f"{RR}.authorization.credentials")},
               "tls_config": tls_files(RR, server_name=True)}, base=True),
        rr(1, {"authorization": {"credentials_file": secret_file(f"{RR}.authorization.credentials_file")}}),
        rr(2, {"basic_auth": {"username": v(f"{RR}.basic_auth.username"),
                              "password_file": secret_file(f"{RR}.basic_auth.password_file")}}),
        rr(3, {"basic_auth": {"username_file": secret_file(f"{RR}.basic_auth.username_file"),
                              "password": v(f"{RR}.basic_auth.password")}}),
        rr(4, {"bearer_token": v(f"{RR}.bearer_token")}),
        rr(5, {"bearer_token_file": secret_file(f"{RR}.bearer_token_file")}),
        rr(6, {"oauth2": oauth2(f"{RR}.oauth2")}),
        rr(7, {"oauth2": oauth2(f"{RR}.oauth2", 1, full=False)}),
    ]


# ---------------------------------------------------------------------------------------------------------------
# tracing: the endpoint doesn't resolve, and sampling_fraction is left at 0, so nothing is exported.
def tracing():
    h = token("tracing.headers.*")
    return {"endpoint": f"{token('tracing.endpoint')}.invalid:4317", "client_type": "grpc", "compression": "gzip",
            "headers": {h: v(f"tracing.headers.{h}")}, "tls_config": tls_files("tracing", server_name=True)}


class NoAliases(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def dump(obj, indent):
    text = yaml.dump(obj, Dumper=NoAliases, sort_keys=False, default_flow_style=False, width=1000)
    return "".join(indent + line if line.strip() else line for line in text.splitlines(True))


def insert_before(text, marker, block):
    i = text.index(marker)
    return text[:i] + block + text[i:]


def insert_after(text, marker, block):
    i = text.index(marker) + len(marker)
    return text[:i] + block + text[i:]


def main():
    global CERT, KEY, RSA_KEY
    shipped, out, fdir = sys.argv[1:4]
    omit = set(sys.argv[sys.argv.index("--omit") + 1].split(",")) if "--omit" in sys.argv else set()
    text = open(shipped).read()
    CERT, KEY = det_cert("ccfg-throwaway-tls")
    RSA_KEY = det_rsa("ccfg-remote-write-google-iam-credentials-file").export_key(format="PEM", pkcs=8).decode() + "\n"

    note = "# Added by the concreteconfig skill (see its decisions file)"
    if "global" not in omit:
        text = insert_after(text, "  scrape_interval: 5s\n", f"  {note}:\n" + dump(global_block(), "  "))
    alerting = ""
    if "am" not in omit:
        alerting += f"  {note}: one entry per alternative.\n" + dump(alertmanagers(), "  ")
    if "arc" not in omit:
        alerting += f"  {note}:\n" + dump({"alert_relabel_configs": [relabel("alerting.alert_relabel_configs")]}, "  ")
    if alerting:
        text = insert_before(text, "\nscrape_configs:\n", "\n" + alerting)
    if "sc" not in omit:
        text += f"\n{note}: one job per alternative.\n" + dump(scrape_configs(), "")
    if "rw" not in omit:
        text += f"\n{note}: one entry per alternative, sending only `up`.\n" + dump({"remote_write": remote_write()}, "")
    if "rr" not in omit:
        text += (f"\n{note}. Inert: each entry requires a matcher no query has, and its URL doesn't resolve.\n"
                 + dump({"remote_read": remote_read()}, ""))
    if "tracing" not in omit:
        text += f"\n{note}. Inert: the endpoint doesn't resolve, and sampling_fraction is 0.\n" + dump({"tracing": tracing()}, "")
    if "otlp" not in omit:
        text += (f"\n{note}. Inert: the OTLP receiver isn't enabled (--web.enable-otlp-receiver).\n"
                 + dump({"otlp": {"promote_resource_attributes": [v("otlp.promote_resource_attributes")],
                                  "translation_strategy": "UnderscoreEscapingWithSuffixes"}}, ""))
    if "scf" not in omit:
        text += (f"\n{note}. Matches no file.\n"
                 + dump({"scrape_config_files": [f"{DIR}/{token('scrape_config_files')}"]}, ""))
    yaml.safe_load(text)  # check it parses
    open(out, "w").write(text)
    os.makedirs(fdir, exist_ok=True)
    for name in os.listdir(fdir):
        os.remove(os.path.join(fdir, name))
    for name, content in files.items():
        open(os.path.join(fdir, name), "w").write(content)
    print(f"wrote {out} ({len(text.splitlines())} lines) and {len(files)} files in {fdir}")


if __name__ == "__main__":
    main()
