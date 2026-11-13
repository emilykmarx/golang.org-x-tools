# prombench

The prombench environment: prombench's kind cluster on the GCE VM, deployed from upstream prometheus/test-infra with
paramtrack's patches. One subdirectory per module that runs in it, holding the concreteconfig skill's `TESTBED.md`
for that module and the skill's helpers:

| Directory | Module | paramtrack `--testbed` | `upstream.py` variant |
|---|---|---|---|
| [prometheus/](prometheus/) | Prometheus (prometheus-meta) | `prombench-nginxingress-prom` | `nginx` (upstream's ingress controller) |
| [caddy/](caddy/) | Caddy's ingress controller, in place of ingress-nginx | `prombench-caddyICingress-caddyIC` | `caddy` |

Shared by both:
- [upstream.py](upstream.py): prepares the patched tree and deploys a variant. Its docstring says how.
- [patches/](patches/) (both variants; `caddy` skips 0003) and [patches-caddy/](patches-caddy/). Each patch's message
  gives its reason.
- [vm_setup.sh](vm_setup.sh): sets up a fresh VM.

The testbed names say `<environment>-<ingress controller>ingress-<module>` ("IC": ingress controller). They were
`prombench` and `prombench-caddy` until 2026-11-13; paramtrack still accepts those (`testbeds.RENAMED`).

The testbeds themselves are classes in `../../testbeds.py` (`Prombench`, `PrombenchCaddy` and their `...VM` wrappers).
