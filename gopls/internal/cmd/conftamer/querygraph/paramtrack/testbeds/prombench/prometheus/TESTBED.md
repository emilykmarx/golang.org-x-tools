# prombench-caddyICingress (prombench, module: Prometheus, as prometheus-meta)

The module runs as **prometheus-meta**, in prombench's kind cluster on the GCE VM `paramtrack-e2e` (zone
`us-central1-b`, project `conftamer`): `gcloud compute ssh --zone us-central1-b paramtrack-e2e --project conftamer`.
kubectl context `kind-prombench`. On the VM, the checkouts are under `/home/emily/projects/config_tracing`.
paramtrack's testbed, with `--module prometheus` (`PrometheusMeta` in `testbeds.py`), builds Prometheus under argmatch
and contexttrack, deploys prombench when the VM has no cluster (or an older variant's), sets prometheus-meta's image to
it, and puts the concrete config in its ConfigMap. `../upstream.py` (its docstring says how) deploys upstream
prometheus/test-infra at `e85cac548` with the patches in `../patches/` and `../patches-caddy/`, from the tree
`$CT/paramtrack-prombench-caddy` on the VM: Caddy's ingress controller is the ingress, and runs stock (upstream's image,
the config it generates) unless the run instruments it too.

- **Shipped concrete config:** the config upstream prombench ships at that commit: the `prometheus.yaml` key of the
  `prometheus-meta` ConfigMap, `git -C $CT/prometheus-test-infra-upstream show
  e85cac548:prombench/manifests/cluster-infra/3b_prometheus-meta.yaml` (141 lines, no template placeholders;
  identical on upstream master as of 2026-11-05). The patches don't change it. A copy is
  [shipped-config.yaml](shipped-config.yaml), next to this file, which the testbed puts back when it runs Prometheus
  stock.
- **Information about the testbed:** the deployed manifests, `$CT/paramtrack-prombench-caddy/prombench/manifests/` on
  the VM: the other components' manifests there tell you which endpoints exist for new entries to target.

## Applying a config
`cc_vm.py` (next to this file) does all of this section's and "Adding files"' procedures, and the health numbers: copy
it to the VM and run `python3 cc_vm.py files <dir> | check <config> | apply <config> | restart <config> | health |
rmkey <key>` there (its docstring has the details). The rest of this section says what it does.

The ConfigMap is mounted at `/etc/prometheus/config/` in the pod. To apply a config without a restart, do what the
argmatch experiment's `reload_on_vm.sh` (`dump_ancestors/argmatch/experiment/`) does: merge-patch the ConfigMap's
`prometheus.yaml`, annotate the pod so the kubelet syncs the file promptly, wait until the file in the pod has the new
content (md5), then reload:
```
kubectl --context kind-prombench exec deploy/prometheus-meta -c prometheus -- \
  wget -qO- --post-data= localhost:9090/prometheus-meta/-/reload
```
and check `prometheus_config_last_reload_successful` in `localhost:9090/prometheus-meta/metrics` is 1.

**Checking a candidate** before applying it: put it in the ConfigMap under another key (e.g. `candidate.yaml`, synced
the same way), then run `promtool check config /etc/prometheus/config/candidate.yaml` in the pod.

**A restart:** scale the deployment to 0, wait for the pod to be deleted, then scale it back to 1 and wait for
`/prometheus-meta/-/ready` (as paramtrack's testbed does: `Component.stop` and `start` in `testbeds.py`). Not `rollout restart`: the
deployment uses the default rolling update, which starts the new pod while the old one still holds the TSDB lock.

## Adding files
Add a key to the `prometheus-meta` ConfigMap: it appears as `/etc/prometheus/config/<key>` (`scripts/value.py
--shape path` gives paths there). Put a copy of each in the output's `files/`.

paramtrack installs them too, when it runs the workflow with your config: its testbed takes the `files/` next to
`config.yaml` (paramtrack's README, "Running the testbed").

## Health
After each applied config, for a minute or so, through `kubectl exec ... wget -qO-` on prometheus-meta:
- `/prometheus-meta/metrics`: `prometheus_config_last_reload_successful` 1;
  `prometheus_remote_storage_samples_failed_total` not rising faster than before;
  `prometheus_notifications_sent_total` still rising, `prometheus_notifications_alertmanagers_discovered` > 0.
  `prometheus_notifications_errors_total` can't show whether the shipped alertmanagers still work: new entries meant
  to fail (e.g. OAuth2 alertmanagers whose token endpoint answers 404) send to the same Alertmanager URL, so its
  errors rise by design, and the metric has no label telling the sets apart. The message label check below is what
  shows nothing was lost;
  `prometheus_sd_discovered_targets` per job not lower than before.
- `/prometheus-meta/api/v1/query?query=up`: the shipped config's targets still up.
- `kubectl --context kind-prombench get pods -A`: no new restarts or failing pods anywhere.

## Message labels (constraint 2)
SKILL.md's constraint 2 says how to check them. On prombench:
- **The baseline run** is the testbed's shared one (paramtrack's README, "Use with /concreteconfig", step 1), and
  `scripts/check_labels.py` instruments the same modules. With Prometheus alone instrumented behind ingress-nginx (until 2026-11-15) a check
  took about 6 minutes, most of it the 240 s workload.
- **Labels that depend on timing,** not the config: none known among the five-module baselines: its labels changed
  between them only with the testbed (../README.md, "Labels that depend on timing"), and its 52 labels were identical
  in the three runs since. Seen in a check: `/-/ready` answering 503 while Prometheus starts.

paramtrack's testbed leaves the ConfigMap set to the config it ran, and prometheus-meta running the instrumented
image. When done, put back whatever the user wants the testbed left with (the shipped config, unless told otherwise);
a paramtrack run that doesn't instrument Prometheus puts back the shipped config and upstream's image.
