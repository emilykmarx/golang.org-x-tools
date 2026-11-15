"""List kube-apiserver's documented config keys, one list per config file, from the kubernetes.io config-api
reference pages (generated from the Go doc comments). See README.md for the sources, format and judgment calls.

Usage (from this directory):
  uv run --with beautifulsoup4 python list_config_keys.py [--fetch] [--source K8S_CHECKOUT]

--fetch re-downloads the pages into raw/config-api/ at the pinned website commit. --source compares each
documented struct's fields with the json tags of the same-named Go struct in a kubernetes checkout (a version-drift
check; it doesn't change the lists).
"""
import argparse
import os
import re
import sys
import urllib.request

from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw", "config-api")
WEBSITE_COMMIT = "11f92a075a2a6b97448d8bb586ad7eac14871e04"  # kubernetes/website, branch release-1.31
PAGE_URL = f"https://raw.githubusercontent.com/kubernetes/website/{WEBSITE_COMMIT}/content/en/docs/reference/config-api/"
PAGES = ["apiserver-config.v1.md", "apiserver-config.v1alpha1.md", "apiserver-config.v1beta1.md", "apiserver-audit.v1.md"]

# Output list -> (root anchor on the pages, the other version's root anchor or None).
# The anchors' group prefix is the pages' (see README: not always the group the kind is decoded under).
LISTS = {
    "authentication_config_keys.txt": ("apiserver-k8s-io-v1beta1-AuthenticationConfiguration",
                                       "apiserver-k8s-io-v1alpha1-AuthenticationConfiguration"),
    "authorization_config_keys.txt": ("apiserver-k8s-io-v1beta1-AuthorizationConfiguration",
                                      "apiserver-k8s-io-v1alpha1-AuthorizationConfiguration"),
    "encryption_config_keys.txt": ("apiserver-config-k8s-io-v1-EncryptionConfiguration", None),
    "audit_policy_keys.txt": ("audit-k8s-io-v1-Policy", None),
    "egress_selector_config_keys.txt": ("apiserver-k8s-io-v1beta1-EgressSelectorConfiguration",
                                        "apiserver-k8s-io-v1alpha1-EgressSelectorConfiguration"),
    "tracing_config_keys.txt": ("apiserver-k8s-io-v1beta1-TracingConfiguration",
                                "apiserver-k8s-io-v1alpha1-TracingConfiguration"),
}

# No `k=v` segments: where a value picks which sibling fields are valid (authorizers.type=Webhook, egress
# proxyProtocol, connectionInfo.type), the fields are all in one Go struct, decoded the same whatever the value, so
# the static analysis's keys have no `k=v` there either (README: "Judgment calls").
# Fields that aren't config: Policy.metadata is ObjectMeta, "included for interoperability with API infrastructure"
SKIP = {("Policy", "metadata")}


def type_name(anchor):
    return anchor.rsplit("-", 1)[-1]


def parse_pages():
    """anchor -> {"alias": underlying type or None, "fields": [(name, type text, linked anchor or None, embedded)]}.
    A kind's apiVersion/kind rows (TypeMeta: a constant, no type) are left out; KMSConfiguration's apiVersion field
    is kept."""
    types = {}
    for page in PAGES:
        text = open(os.path.join(RAW, page)).read()
        for sec in re.split(r"^## `", text, flags=re.M)[1:]:
            m = re.match(r"[^`]+`\s*\{#([^}]+)\}", sec)
            if not m:
                continue
            anchor = m.group(1)
            alias = re.search(r"^\(Alias of `([^`]+)`\)", sec, re.M)
            fields = []
            table = BeautifulSoup(sec, "html.parser").find("table")
            for tr in table.find("tbody").find_all("tr", recursive=False) if table else []:
                tds = tr.find_all("td", recursive=False)
                name = tds[0].find("code").get_text()
                link = tds[0].find("a")
                if link:
                    typ = link.get_text()
                    href = link["href"]
                    linked = href[1:] if href.startswith("#") else None
                else:
                    codes = tds[0].find_all("code")
                    if len(codes) < 2:  # TypeMeta: `<code>apiVersion</code><br/>string`, value in the other cell
                        continue
                    typ = codes[1].get_text()
                    linked = None
                embedded = tds[1].get_text().lstrip().startswith("(Members of")
                fields.append((name, typ, linked, embedded))
            if anchor in types:  # a section repeated on two pages (component-base TracingConfiguration): same fields
                continue
            types[anchor] = {"alias": alias.group(1) if alias else None, "fields": fields}
    return types


def expand(types, anchor, prefix, path):
    """[(key, type)] for the struct at anchor; path is the anchors above it (to stop at recursion)"""
    out = []
    tname = type_name(anchor)
    for name, typ, linked, embedded in types[anchor]["fields"]:
        if (tname, name) in SKIP:
            continue
        target = types.get(linked) if linked else None
        if embedded:
            out += expand(types, linked, prefix, path + [linked])
            continue
        key = prefix + [name]
        if target and target["fields"]:
            if linked in path:
                print(f"recursion, not expanded: {'.'.join(key)} ({typ})", file=sys.stderr)
                continue
            out += expand(types, linked, key, path + [linked])
        elif target and not target["alias"]:  # a struct with no fields: a provider choice (identity: {})
            print(f"no fields, not listed: {'.'.join(key)} ({typ})", file=sys.stderr)
        elif target:  # an enum-like named type: underlying:Name, as in the Caddy lists
            list_ = "[]" if typ.startswith("[]") else ""
            out.append((".".join(key), f"{list_}{target['alias']}:{typ[len(list_):]}"))
        else:
            if linked and target is None:
                print(f"link to a missing section: {'.'.join(key)} -> {linked}", file=sys.stderr)
            out.append((".".join(key), typ))
    return out


def struct_tags(source, struct):
    """json tag names of `type <struct> struct` in Go files of the given dirs (first match), or None"""
    for d in source:
        for f in sorted(os.listdir(d)):
            if not f.endswith(".go") or f.startswith("zz_generated") or f.endswith("_test.go"):
                continue
            m = re.search(rf"^type {struct} struct \{{\n(.*?)^\}}", open(os.path.join(d, f)).read(), re.M | re.S)
            if m:
                return {t for t in re.findall(r'json:"([^",]*)', m.group(1)) if t not in ("", "-")}
    return None


def check_source(types, k8s):
    """Print documented structs whose fields differ from the json tags in the checkout"""
    st = os.path.join(k8s, "staging/src/k8s.io")
    dirs = {
        "apiserver-config-k8s-io-v1-": [f"{st}/apiserver/pkg/apis/apiserver/v1"],
        "apiserver-k8s-io-v1beta1-": [f"{st}/apiserver/pkg/apis/apiserver/v1beta1"],
        "apiserver-k8s-io-v1alpha1-": [f"{st}/apiserver/pkg/apis/apiserver/v1alpha1"],
        "audit-k8s-io-v1-": [f"{st}/apiserver/pkg/apis/audit/v1"],
        "": [f"{st}/component-base/tracing/api/v1"],
    }
    for anchor, t in sorted(types.items()):
        if not t["fields"]:
            continue
        prefix = max((p for p in dirs if anchor.startswith(p)), key=len, default=None)
        if prefix is None or (prefix == "" and "-" in anchor):
            continue
        tags = struct_tags(dirs[prefix], type_name(anchor))
        documented = {n for n, _, _, emb in t["fields"] if not emb}
        if tags is None:
            print(f"source check: {anchor}: no struct in {dirs[prefix]}")
        elif tags != documented:
            print(f"source check: {anchor}: only in source {sorted(tags - documented)}, "
                  f"only in docs {sorted(documented - tags)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="re-download the pages at the pinned commit")
    ap.add_argument("--source", help="kubernetes checkout to compare the documented structs with")
    args = ap.parse_args()
    if args.fetch:
        os.makedirs(RAW, exist_ok=True)
        for page in PAGES:
            with urllib.request.urlopen(PAGE_URL + page, timeout=60) as r:
                open(os.path.join(RAW, page), "wb").write(r.read())
    types = parse_pages()
    for out, (root, other) in LISTS.items():
        keys = expand(types, root, [], [root])
        assert len({k for k, _ in keys}) == len(keys), f"duplicate keys in {out}"
        for k, _ in keys:
            assert not re.search(r"[<*]", k), k
        with open(os.path.join(HERE, out), "w") as f:
            f.writelines(f"{k}\t{t}\n" for k, t in keys)
        print(f"{out}: {len(keys)} keys (root {root})")
        if other:
            okeys = dict(expand(types, other, [], [other]))
            mine = dict(keys)
            for k in sorted(set(okeys) - set(mine)):
                print(f"  only in {type_name(other)} {other.split('-')[-2]}: {k}\t{okeys[k]}")
            for k in sorted(set(mine) - set(okeys)):
                print(f"  only in the listed version: {k}\t{mine[k]}")
            for k in sorted(set(mine) & set(okeys)):
                if mine[k] != okeys[k]:
                    print(f"  type differs: {k}: {mine[k]} vs {okeys[k]}")
    if args.source:
        check_source(types, args.source)


if __name__ == "__main__":
    main()
