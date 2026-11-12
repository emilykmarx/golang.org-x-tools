#!/usr/bin/env python3
"""Contents of Prometheus-specific files a concrete config refers to. The generic ones (certs, keys, UUIDs) are in
the skill's scripts/files.py. Import it with this directory on sys.path."""

import json

import yaml


def google_credentials(name, private_key_pem, token_uri):
    """A Google service account key file, for remote_write's google_iam.credentials_file: `name` (e.g. value.py's
    token for the key) names the project and account, private_key_pem is e.g. scripts/files.py's rsa_key_pem, and
    token_uri is where the client asks for tokens (point it at something in the testbed that answers, e.g. with 404,
    so nothing leaves it)"""
    return json.dumps({
        "type": "service_account", "project_id": name + "-project", "private_key_id": "0",
        "private_key": private_key_pem, "client_email": f"{name}@ccfg.iam.gserviceaccount.com", "client_id": "0",
        "token_uri": token_uri}, indent=2) + "\n"


def kubeconfig(server, ca_file, token_file):
    """A kubeconfig for kubernetes_sd_configs' kubeconfig_file: one cluster (server, with ca_file), one user
    authenticating with token_file, and one context, the current one (e.g. the pod's service account files)"""
    return yaml.safe_dump({
        "apiVersion": "v1", "kind": "Config",
        "clusters": [{"name": "in-cluster", "cluster": {"server": server, "certificate-authority": ca_file}}],
        "users": [{"name": "prometheus-meta", "user": {"tokenFile": token_file}}],
        "contexts": [{"name": "in-cluster", "context": {"cluster": "in-cluster", "user": "prometheus-meta"}}],
        "current-context": "in-cluster"}, sort_keys=False)
