# prombench-caddyICingress (prombench, module: Caddy's ingress controller)

prombench with Caddy's ingress controller in place of ingress-nginx. The module runs as the controller's deployment
`caddy-caddy-ingress-controller` in namespace `caddy-system`, in prombench's kind cluster on the GCE VM
`paramtrack-e2e` (zone `us-central1-b`, project `conftamer`): `gcloud compute ssh --zone us-central1-b paramtrack-e2e
--project conftamer`. kubectl context `kind-prombench`. On the VM, the checkouts are under
`/home/emily/projects/config_tracing`.

paramtrack's testbed, with `--module caddy` (`CaddyController` in `testbeds.py`), builds the controller under
argmatch and contexttrack, deploys the cluster if there's none or it's an older variant (`../upstream.py`, variant
`caddy`: about 5 minutes), sets the controller's image to it, and loads the concrete config through Caddy's admin
API. prometheus-meta runs stock (upstream's image, the shipped config) unless the run instruments Prometheus too.
`../upstream.py`'s docstring and the patches in `../patches*/` say what's deployed and why.

- **Shipped concrete config:** [shipped-config.json](shipped-config.json), next to this file: the config the
  controller generates for prombench's Ingresses and its ConfigMap, as `cc_vm.py shipped` prints it.
- **Information about the testbed:** the deployed manifests, `$CT/paramtrack-prombench-caddy/prombench/manifests/`
  on the VM (upstream prombench with the patches), say which services exist. Clients reach the module through its
  service (`caddy-caddy-ingress-controller.caddy-system`, ports 80 and 443): the loadgen querier queries the benchmark
  Prometheus instances (`/<PR>/prometheus-pr`, `/<PR>/prometheus-release`), and the testbed's dashboard viewer loads
  Grafana's dashboards (`/grafana`). Anything else in the config is only reached if a client sends to it.

## Applying a config
`cc_vm.py` (next to this file) does all of this section's and "Adding files"' procedures, and the health numbers: copy
it to the VM and run `python3 cc_vm.py files <dir> | apply <config> | restart <config> [<files dir>] | shipped |
running | health` there (its docstring has the details). The rest of this section says what it does.

- **Apply (and check):** copy the config into the container and POST it to the admin API's `/load`, at
  `127.0.0.1:2019` in the container (busybox wget tries `localhost`'s `::1` first, where Caddy doesn't listen) (`kubectl exec ... wget --post-file`), with `Cache-Control: must-revalidate` (else
  Caddy skips a config equal to the running one). Caddy provisions it fully and keeps the running config if anything
  fails, answering 400 with the error. Then `GET /config/` must equal the config. This is the check command too
  (MODULE.md, "Checking a config").
- **A restart:** scale the deployment to 0, wait for the pod to be deleted, scale it back to 1, wait until the
  controller has loaded its own generated config (`GET /config/` has `apps`), then copy the files and load the config.
  The controller doesn't load its generated config again while it doesn't change (it compares with the last config
  *it* loaded), so yours stays.

## Adding files
Copy them into the controller's container under `/tmp/paramtrack-files/` (`cc_vm.py files <dir>`), and refer to
them there (`scripts/value.py --shape path --dir /tmp/paramtrack-files` gives such paths). `/tmp`
is an emptyDir: the files survive container restarts but not a pod restart, so `cc_vm.py restart` takes the directory
again. Put a copy of each in the output's `files/`; paramtrack installs them the same way when it runs the workflow.
Files Caddy writes (logs, storage) can go under `/tmp/` too.

## Health
After each applied config, through `cc_vm.py health`:
- the routes through Caddy answer as before: `/grafana/api/health` 200, `/prometheus-meta/-/ready` 200,
  `/<PR>/prometheus-pr/-/ready` and `/<PR>/prometheus-release/-/ready` 200;
- Caddy's metrics (`localhost:9765/metrics`): `caddy_http_requests_total` rising;
- `kubectl --context kind-prombench get pods -A`: no new restarts or failing pods anywhere (in particular the loadgen
  querier in `prombench-<PR>`, which queries through Caddy).

## Message labels (constraint 2)
SKILL.md's constraint 2 says how to check them. On prombench:
- **The baseline run** is made with `uv run --with pandas --with matplotlib --with tabulate python paramtrack.py
  --module caddy --out <dir> --config caddy=<shipped config> --until 3`, from paramtrack's directory. The controller
  alone is instrumented: the skill's runs must instrument the same modules as the baseline. It and each check take
  about 8 minutes, most of it the 240 s workload (plus a deploy if there's no cluster or it's an older variant).
- **Labels that depend on timing,** not the config: e.g. requests that arrive while the controller restarts, and
  which Grafana API calls the dashboard viewer happens to make.

paramtrack's testbed leaves the controller running the config it ran, on the instrumented image. When done, put back
the shipped config (a restart does: the controller loads its generated config, and then `cc_vm.py apply
shipped-config.json`, with this directory's file, is the same config). A paramtrack run that doesn't instrument the
controller puts back upstream's image and its generated config.
