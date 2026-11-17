"""Grafana: paramtrack's settings and hooks for this module (see ../__init__.py for what each is).

Not onboarded yet: no ROOTS (the documentedkeys skill's job: what Grafana's config is, e.g. grafana.ini and its
provisioning files, and in what format), so a run can only instrument it with the testbed's shipped config, through
step 3 (e.g. a baseline run). The testbed builds it from argmatch.Dockerfile here."""

CT = "/home/emily/projects/config_tracing"

GO_MODULE = "github.com/grafana/grafana"
# Upstream's v13.1.0, the version contexttrack's Grafana patches were made against (prombench ships 11.0.0; the testbed
# runs 13.1.0, stock or instrumented)
MODULE_DIR = f"{CT}/grafana-paramtrack"
FORMAT = None  # (to be decided with the roots)
ROOTS = {}
