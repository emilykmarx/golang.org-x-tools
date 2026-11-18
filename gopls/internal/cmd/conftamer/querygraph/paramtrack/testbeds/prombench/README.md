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
| [kube-apiserver/](kube-apiserver/) | The Kubernetes API server | kind's static pod `kube-apiserver-<node>` (namespace `kube-system`) | `KubeAPIServer` |
| [alertmanager/](alertmanager/) | Alertmanager | `alertmanager` | `Alertmanager` |
| [grafana/](grafana/) | Grafana | `grafana-core` | `Grafana` |

Shared by all:
- [upstream.py](upstream.py): prepares the patched tree and deploys the cluster. Its docstring says how.
- [patches/](patches/) and [patches-caddy/](patches-caddy/), applied in that order. Each patch's message gives its
  reason. The deployments run upstream's images; `testbeds.py` sets an instrumented module's image for each run.
- [vm_setup.sh](vm_setup.sh): sets up a fresh VM.

**Reused or fresh cluster:** a run reuses the cluster it finds, unless there's none or it's another variant's, in
which case it deploys one (`upstream.deploy`). Reusing is quicker; a fresh cluster gives runs the same starting state,
without earlier runs' leftovers (ConfigMaps, mounts, flags), so their labels compare exactly. To start fresh, delete
the cluster on the VM first (`kind delete cluster --name prombench`, about 20 s). On 2026-11-17 the deploy took 4 min
46 s, and a run with all five modules took 13 min with a deploy and 8 min without. Labels only a fresh cluster
produces: `kube-apiserver/TESTBED.md`.

**After the VM restarts,** Docker may give the kind nodes different IPs, and the API server's manifest keeps the
control plane's old one (`--advertise-address`), so it restarts in a loop and in-cluster clients get "connection
refused" (e.g. Caddy's controller, "not ready 600s after the restart"). Docker assigns IPs in start order, so restart
the nodes with the control plane first: `docker stop prombench-worker prombench-worker2 prombench-worker3
prombench-control-plane`, then `docker start` the control plane, then the workers. It should get `172.18.0.2` back
(`docker inspect`; compare `--advertise-address` in `/etc/kubernetes/manifests/kube-apiserver.yaml` on the node).

The name says `<environment>-<ingress controller>ingress` ("IC": ingress controller). Until 2026-11-15 there was one
testbed per instrumented module: `prombench-caddyICingress-caddyIC` (earlier `prombench-caddy`), the same as this one
with `--module caddy`, and `prombench-nginxingress-prom` (earlier `prombench`), Prometheus behind ingress-nginx, now
removed with the `docker` testbed. paramtrack still reads runs of the first (`testbeds.recorded`).
