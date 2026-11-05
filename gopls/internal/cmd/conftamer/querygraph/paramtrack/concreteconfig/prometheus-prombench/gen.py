#!/usr/bin/env python3
"""Generate the concrete config: the shipped config's text, unchanged, with new list entries appended to
alerting.alertmanagers, scrape_configs, remote_write and remote_read, and a tracing block. Also writes the files the
new entries refer to (added to the prometheus-meta ConfigMap, so they appear under /etc/prometheus/config/).

Values come from the skill's scripts/value.py (token()); deviations are noted where they're made, and recorded in the
decisions file. Index rule: a concrete key's value gets -<n> when n earlier list entries set the same concrete key.

Usage: uv run --with pyyaml --with pycryptodome python gen.py <shipped.yaml> <out config.yaml> <out files dir> [--omit name,...]
"""
import hashlib
import json
import os
import re
import sys
import uuid

import yaml
from Crypto.PublicKey import RSA

sys.path.insert(0, "/home/emily/projects/config_tracing/golang.org-x-tools/.claude/skills/concreteconfig/scripts")
from value import token  # noqa: E402

DIR = "/etc/prometheus/config"
SA = "/var/run/secrets/kubernetes.io/serviceaccount"
API = "https://kubernetes.default.svc:443"
# Needed for proxy_connect_header (ProxyConfig.Validate); bypassed for every address, as in the shipped config
PROXY = {"proxy_url": "http://127.0.0.1:3128", "no_proxy": "localhost,127.0.0.1,10.0.0.0/8,172.16.0.0/12,.svc,.cluster.local"}
# In-cluster HTTP server for token requests that must fail without leaving the testbed (Loki answers 404)
TOKEN_HOST = "http://loki.default.svc:3100"
SINK = "http://prometheus-sink.prombench-16075.svc:9011/sink/prw"

files = {}  # ConfigMap key => content


def v(key, i=0):
    return token(key, i)


def label(key, i=0):
    return token(key, i).replace("-", "_")


def path(key, content, i=0):
    """A file added to the ConfigMap for a *_file key"""
    t = token(key, i)
    files[t] = content
    return f"{DIR}/{t}"


def secret_file(key, i=0):
    return path(key, token(key, i) + "-content\n", i)


def tls_files(p, i=0, ca=True, server_name=False):
    """tls_config with ca_file (unless ca=False), cert_file, key_file as added files (the shipped config's throwaway
    pair; the cert is self-signed, so it's also the CA)"""
    t = {}
    if ca:
        t["ca_file"] = path(f"{p}.tls_config.ca_file", CERT, i)
    t["cert_file"] = path(f"{p}.tls_config.cert_file", CERT, i)
    t["key_file"] = path(f"{p}.tls_config.key_file", KEY, i)
    if server_name:
        t["server_name"] = v(f"{p}.tls_config.server_name", i)
    return t


def oauth2(p, i=0, full=True):
    """oauth2 block for parent key p (e.g. scrape_configs.oauth2). The token request goes to Loki, which answers 404,
    so the token fetch fails inside the testbed. full: client_secret, scopes, endpoint_params, proxy_connect_header and
    tls_config; else client_secret_file only."""
    o = {"client_id": v(f"{p}.client_id", i), "token_url": f"{TOKEN_HOST}/{v(f'{p}.token_url', i)}"}
    if full:
        o["client_secret"] = v(f"{p}.client_secret", i)
        o["scopes"] = [v(f"{p}.scopes", i)]
        k = token(f"{p}.endpoint_params.*")
        o["endpoint_params"] = {k: v(f"{p}.endpoint_params.{k}", i)}
        h = token(f"{p}.proxy_connect_header.*")
        o["proxy_connect_header"] = {h: [v(f"{p}.proxy_connect_header.{h}", i)]}
        o.update(PROXY)
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


def k8s_sd(role, ns, selector, **http):
    """A kubernetes_sd_configs entry against the API server; http: its HTTP client settings"""
    sd = {"role": role, "api_server": API, "namespaces": {"names": [ns]}, "selectors": [selector]}
    sd.update(http)
    sd.setdefault("tls_config", {"ca_file": f"{SA}/ca.crt"})
    return sd


# ---------------------------------------------------------------------------------------------------------------
# alerting.alertmanagers: one new entry per auth alternative. Each discovers the Alertmanager pod with the same SD
# config (so they share one watch), which also covers kubernetes_sd_configs.bearer_token_file (the service account
# token, so it works). Extra SD configs cover the SD client's alternatives; all but kubeconfig_file fail to
# authenticate (401 or token fetch error) and discover nothing.
AM = "alerting.alertmanagers"
AMSD = f"{AM}.kubernetes_sd_configs"
AM_POD = {"role": "pod", "label": "app=alertmanager"}


def am(i, auth, extra_sd=None):
    e = {"kubernetes_sd_configs": [k8s_sd("pod", "default", AM_POD, bearer_token_file=f"{SA}/token")]}
    if extra_sd:
        e["kubernetes_sd_configs"].append(extra_sd)
    e["path_prefix"] = "/alertmanager"
    e.update(auth)
    return e


def alertmanagers():
    return [
        am(1, {"authorization": {"type": v(f"{AM}.authorization.type"), "credentials": v(f"{AM}.authorization.credentials")},
               "tls_config": tls_files(AM, ca=False)},
           k8s_sd("pod", "default", AM_POD, authorization={"credentials": v(f"{AMSD}.authorization.credentials")},
                  tls_config={"ca_file": f"{SA}/ca.crt", **tls_files(AMSD, ca=False)})),
        am(2, {"authorization": {"credentials_file": secret_file(f"{AM}.authorization.credentials_file")}},
           k8s_sd("pod", "default", AM_POD, basic_auth={"username": v(f"{AMSD}.basic_auth.username"),
                                                        "password": v(f"{AMSD}.basic_auth.password")})),
        am(3, {"basic_auth": {"username": v(f"{AM}.basic_auth.username"),
                              "password_file": secret_file(f"{AM}.basic_auth.password_file")}},
           k8s_sd("pod", "default", AM_POD, basic_auth={"username_file": secret_file(f"{AMSD}.basic_auth.username_file"),
                                                        "password_file": secret_file(f"{AMSD}.basic_auth.password_file")})),
        am(4, {"basic_auth": {"username_file": secret_file(f"{AM}.basic_auth.username_file"),
                              "password": v(f"{AM}.basic_auth.password")}},
           k8s_sd("pod", "default", AM_POD, bearer_token=v(f"{AMSD}.bearer_token"))),
        am(5, {"bearer_token": v(f"{AM}.bearer_token")},
           # kubeconfig_file excludes api_server and any HTTP client setting (kubernetes.SDConfig.UnmarshalYAML)
           {"role": "pod", "kubeconfig_file": path(f"{AMSD}.kubeconfig_file", kubeconfig()),
            "namespaces": {"names": ["default"]}, "selectors": [AM_POD]}),
        am(6, {"oauth2": oauth2(f"{AM}.oauth2")},
           k8s_sd("pod", "default", AM_POD, oauth2=oauth2(f"{AMSD}.oauth2"))),
        am(7, {"oauth2": oauth2(f"{AM}.oauth2", 1, full=False)},
           k8s_sd("pod", "default", AM_POD, oauth2=oauth2(f"{AMSD}.oauth2", 1, full=False))),
        # role_arn skipped: it makes the signer call AWS STS, outside the testbed
        am(8, {"sigv4": {"region": v(f"{AM}.sigv4.region"), "access_key": v(f"{AM}.sigv4.access_key"),
                         "secret_key": v(f"{AM}.sigv4.secret_key"), "profile": v(f"{AM}.sigv4.profile")}}),
    ]


# ---------------------------------------------------------------------------------------------------------------
# scrape_configs: new jobs scraping Loki (static target), one per auth alternative the shipped jobs don't use. Extra
# SD configs cover the SD client's alternatives: those that authenticate (bearer_token_file, kubeconfig_file)
# discover only the loki service; the rest fail.
SC = "scrape_configs"
SCSD = f"{SC}.kubernetes_sd_configs"
LOKI_SVC = {"role": "service", "field": "metadata.name=loki"}
N_SHIPPED_JOBS = 5


def job(n, auth, sds):
    i = N_SHIPPED_JOBS + n  # list index of the new job
    e = {"job_name": v(f"{SC}.job_name", i), "static_configs": [{"targets": ["loki.default.svc:3100"]}]}
    e.update(auth)
    e["kubernetes_sd_configs"] = sds
    return e


def scrape_configs():
    return [
        job(0, {"authorization": {"credentials_file": secret_file(f"{SC}.authorization.credentials_file")},
                "tls_config": tls_files(SC, ca=False)},
            [k8s_sd("service", "default", LOKI_SVC, authorization={"credentials": v(f"{SCSD}.authorization.credentials")},
                    tls_config={"ca_file": f"{SA}/ca.crt", **tls_files(SCSD, ca=False)}),
             k8s_sd("service", "default", LOKI_SVC, basic_auth={"username": v(f"{SCSD}.basic_auth.username"),
                                                                "password": v(f"{SCSD}.basic_auth.password")})]),
        job(1, {"basic_auth": {"username_file": secret_file(f"{SC}.basic_auth.username_file"),
                               "password": v(f"{SC}.basic_auth.password")}},
            [k8s_sd("service", "default", LOKI_SVC,
                    basic_auth={"username_file": secret_file(f"{SCSD}.basic_auth.username_file"),
                                "password_file": secret_file(f"{SCSD}.basic_auth.password_file")}),
             k8s_sd("service", "default", LOKI_SVC, bearer_token=v(f"{SCSD}.bearer_token"))]),
        job(2, {"bearer_token": v(f"{SC}.bearer_token")},
            [k8s_sd("service", "default", LOKI_SVC, bearer_token_file=f"{SA}/token")]),
        job(3, {"oauth2": oauth2(f"{SC}.oauth2")},
            [k8s_sd("service", "default", LOKI_SVC, oauth2=oauth2(f"{SCSD}.oauth2")),
             {"role": "service", "kubeconfig_file": path(f"{SCSD}.kubeconfig_file", kubeconfig()),
              "namespaces": {"names": ["default"]}, "selectors": [LOKI_SVC]}]),
        job(4, {"oauth2": oauth2(f"{SC}.oauth2", 1, full=False)},
            [k8s_sd("service", "default", LOKI_SVC, oauth2=oauth2(f"{SCSD}.oauth2", 1, full=False))]),
    ]


# ---------------------------------------------------------------------------------------------------------------
# remote_write: new entries to the sink (which checks no credentials), sending only `up`, one per auth alternative.
RW = "remote_write"
N_SHIPPED_RW = 2


def rw(n, auth):
    i = N_SHIPPED_RW + n
    e = {"url": SINK, "name": v(f"{RW}.name", i),
         "write_relabel_configs": [{"source_labels": ["__name__"], "regex": "up", "action": "keep"}]}
    e.update(auth)
    return e


def google_creds():
    """A service account key file with a throwaway RSA key, whose token_uri is in the cluster (Loki, 404)"""
    return json.dumps({
        "type": "service_account", "project_id": v(f"{RW}.google_iam.credentials_file") + "-project",
        "private_key_id": "0", "private_key": RSA_KEY,
        "client_email": "ccfg-remote-write-google-iam@ccfg.iam.gserviceaccount.com", "client_id": "0",
        "token_uri": f"{TOKEN_HOST}/{v(f'{RW}.google_iam.credentials_file')}-token-uri"}, indent=2) + "\n"


def deterministic_rsa_key(seed):
    """A throwaway 2048-bit RSA private key (PKCS#8 PEM), the same for the same seed: pycryptodome's RSA.generate,
    with its randomness drawn from SHAKE-256 of the seed instead of the OS. Only for the fake google_iam credentials
    file, so reruns reproduce the config without keeping a key in the repo."""
    stream = hashlib.shake_256(seed.encode())
    pos = 0

    def randfunc(n):
        nonlocal pos
        pos += n
        return stream.digest(pos)[-n:]

    return RSA.generate(2048, randfunc=randfunc).export_key(format="PEM", pkcs=8).decode() + "\n"


def det_uuid(key):
    """azuread client_id must be a UUID (AzureADConfig.Validate): a UUID derived from value.py's token"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, token(key)))


def remote_write():
    return [
        rw(0, {"authorization": {"type": v(f"{RW}.authorization.type"), "credentials": v(f"{RW}.authorization.credentials")},
               "tls_config": tls_files(RW)}),
        rw(1, {"authorization": {"credentials_file": secret_file(f"{RW}.authorization.credentials_file")}}),
        rw(2, {"basic_auth": {"username": v(f"{RW}.basic_auth.username"),
                              "password_file": secret_file(f"{RW}.basic_auth.password_file")}}),
        rw(3, {"bearer_token": v(f"{RW}.bearer_token")}),
        rw(4, {"bearer_token_file": secret_file(f"{RW}.bearer_token_file")}),
        rw(5, {"google_iam": {"credentials_file": path(f"{RW}.google_iam.credentials_file", google_creds())}}),
        # cloud is an enum (AzurePublic is the default). managed_identity and sdk get tokens from the instance
        # metadata address (169.254.169.254); azuread.oauth is skipped: it asks login.microsoftonline.com.
        rw(6, {"azuread": {"cloud": "AzurePublic",
                           "managed_identity": {"client_id": det_uuid(f"{RW}.azuread.managed_identity.client_id")}}}),
        rw(7, {"azuread": {"sdk": {"tenant_id": v(f"{RW}.azuread.sdk.tenant_id")}}}),
        rw(8, {"oauth2": oauth2(f"{RW}.oauth2")}),
        rw(9, {"oauth2": oauth2(f"{RW}.oauth2", 1, full=False)}),
        # role_arn skipped (AWS STS, outside the testbed)
        rw(10, {"sigv4": {"region": v(f"{RW}.sigv4.region", 1), "access_key": v(f"{RW}.sigv4.access_key", 1),
                          "secret_key": v(f"{RW}.sigv4.secret_key", 1), "profile": v(f"{RW}.sigv4.profile")}}),
    ]


# ---------------------------------------------------------------------------------------------------------------
# remote_read: inert new entries, one per auth alternative: each requires its own matcher, which no query has, so
# none is ever read from (the URL doesn't resolve either).
RR = "remote_read"
N_SHIPPED_RR = 1


def rr(n, auth):
    i = N_SHIPPED_RR + n
    m = label(f"{RR}.required_matchers.*", i)
    e = {"url": f"http://{token(f'{RR}.url', i)}.invalid/api/v1/read", "name": v(f"{RR}.name", i),
         "required_matchers": {m: v(f"{RR}.required_matchers.{m}", i)}}
    e.update(auth)
    return e


def remote_read():
    return [
        rr(0, {"authorization": {"credentials_file": secret_file(f"{RR}.authorization.credentials_file")},
               "tls_config": tls_files(RR)}),
        rr(1, {"basic_auth": {"username": v(f"{RR}.basic_auth.username"),
                              "password_file": secret_file(f"{RR}.basic_auth.password_file")}}),
        rr(2, {"basic_auth": {"username_file": secret_file(f"{RR}.basic_auth.username_file"),
                              "password": v(f"{RR}.basic_auth.password")}}),
        rr(3, {"bearer_token": v(f"{RR}.bearer_token")}),
        rr(4, {"bearer_token_file": secret_file(f"{RR}.bearer_token_file")}),
        rr(5, {"oauth2": oauth2(f"{RR}.oauth2")}),
        rr(6, {"oauth2": oauth2(f"{RR}.oauth2", 1, full=False)}),
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


def main():
    global CERT, KEY, RSA_KEY
    shipped, out, fdir = sys.argv[1:4]
    omit = set(sys.argv[sys.argv.index("--omit") + 1].split(",")) if "--omit" in sys.argv else set()
    text = open(shipped).read()
    pems = re.findall(r"-----BEGIN (?:CERTIFICATE|PRIVATE KEY)-----.*?-----END (?:CERTIFICATE|PRIVATE KEY)-----", text, re.S)
    CERT, KEY = [re.sub(r"(?m)^\s+", "", p) + "\n" for p in pems[:2]]
    RSA_KEY = deterministic_rsa_key("ccfg-remote-write-google-iam-credentials-file")

    head = "\n# Added by the concreteconfig skill (see its decisions file): one entry per alternative.\n"
    if "am" not in omit:
        text = insert_before(text, "\nscrape_configs:\n", head + dump(alertmanagers(), "  "))
    if "sc" not in omit:
        text = insert_before(text, "\nremote_write:\n", head + dump(scrape_configs(), ""))
    if "rw" not in omit:
        text = insert_before(text, "\n# Only used by queries", head + dump(remote_write(), ""))
    if "rr" not in omit:
        text = insert_before(text, "\n# Inert: the OTLP", head + dump(remote_read(), ""))
    if "tracing" not in omit:
        text += "\n# Added by the concreteconfig skill. Inert: the endpoint doesn't resolve, and sampling_fraction is 0.\n"
        text += dump({"tracing": tracing()}, "")
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
