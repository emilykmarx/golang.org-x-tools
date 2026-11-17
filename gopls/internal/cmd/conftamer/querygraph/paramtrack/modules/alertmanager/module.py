"""Prometheus' Alertmanager: paramtrack's settings and hooks for this module (see ../__init__.py for what each is).

Not onboarded yet: no ROOTS (the documentedkeys skill's job), so a run can only instrument it with the testbed's shipped
config, through step 3 (e.g. a baseline run). The testbed builds it from argmatch.Dockerfile here."""

CT = "/home/emily/projects/config_tracing"

GO_MODULE = "github.com/prometheus/alertmanager"
# Upstream's v0.27.0, the version prombench runs (prombench/manifests/cluster-infra/5a_alertmanager_deployment.yaml)
MODULE_DIR = f"{CT}/alertmanager-paramtrack"
FORMAT = "yaml"
ROOTS = {}
