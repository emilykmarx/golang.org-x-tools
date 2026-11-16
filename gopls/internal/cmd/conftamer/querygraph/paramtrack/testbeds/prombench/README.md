# prombench

The prombench environment: prombench's kind cluster on the GCE VM, deployed from upstream prometheus/test-infra with
paramtrack's patches, with Caddy's ingress controller in place of ingress-nginx. It's paramtrack's one testbed,
`prombench-caddyICingress` (`NAME` in `../../testbeds.py`); paramtrack's README says which modules it instruments and
how the others run. One subdirectory per module that runs in it, holding the concreteconfig skill's `TESTBED.md` for
that module and the skill's helpers:

| Directory | Module | Deployment | `testbeds.py` class |
|---|---|---|---|
| [prometheus/](prometheus/) | Prometheus | `prometheus-meta` | `PrometheusMeta` |
| [caddy/](caddy/) | Caddy's ingress controller | `caddy-caddy-ingress-controller` (namespace `caddy-system`) | `CaddyController` |

Shared by both:
- [upstream.py](upstream.py): prepares the patched tree and deploys the cluster. Its docstring says how.
- [patches/](patches/) and [patches-caddy/](patches-caddy/), applied in that order. Each patch's message gives its
  reason. The deployments run upstream's images; `testbeds.py` sets an instrumented module's image for each run.
- [vm_setup.sh](vm_setup.sh): sets up a fresh VM.

The name says `<environment>-<ingress controller>ingress` ("IC": ingress controller). Until 2026-11-15 there was one
testbed per instrumented module: `prombench-caddyICingress-caddyIC` (earlier `prombench-caddy`), the same as this one
with `--module caddy`, and `prombench-nginxingress-prom` (earlier `prombench`), Prometheus behind ingress-nginx, now
removed with the `docker` testbed. paramtrack still reads runs of the first (`testbeds.recorded`).
