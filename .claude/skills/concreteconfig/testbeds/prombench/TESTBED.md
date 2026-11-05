# prombench

The module runs as **prometheus-meta**, in prombench's kind cluster on the GCE VM `paramtrack-e2e` (zone
`us-central1-b`, project `conftamer`): `gcloud compute ssh --zone us-central1-b paramtrack-e2e --project conftamer`.
kubectl context `kind-prombench`. On the VM, the checkouts are under `/home/emily/projects/config_tracing`, with
prombench at `prometheus-test-infra/prombench` (locally `prom-test-infra/prombench`). Deploying it is described in
the argmatch experiment's README (`dump_ancestors/argmatch/experiment/README.md`) and paramtrack's README
("Testbeds").

- **Shipped concrete config:** the `prometheus.yaml` key of the `prometheus-meta` ConfigMap, in
  `prombench/manifests/cluster-infra/3b_prometheus-meta.yaml`; the same file locally as
  `prom-test-infra/prombench/fuzzed_prometheus.yaml`.
- **Information about the testbed:** `prombench/` (manifests, deploy.sh), and the other components' manifests there:
  what they are tells you which endpoints exist for new entries to target.

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
`/prometheus-meta/-/ready` (as paramtrack's `Prombench.restart` in `testbeds.py` does). Not `rollout restart`: the
deployment uses the default rolling update, which starts the new pod while the old one still holds the TSDB lock.

## Adding files
Add a key to the `prometheus-meta` ConfigMap: it appears as `/etc/prometheus/config/<key>` (`scripts/value.py
--shape path` gives paths there). Put a copy of each in the output's `files/`.

paramtrack installs them too, when it runs the workflow with your config: its testbeds take the `files/` next to
`config.yaml` (paramtrack's README, "Testbeds").

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
The authoritative check, at the end (each run takes about 6 minutes, most of it the 240 s workload):
1. **Baseline**, once per testbed deployment: paramtrack with the shipped config, `--testbed prombench --until 4`
   (its run 1: contexttrack records events, argmatch instruments nothing). The events are in
   `<out>/4-chains/events.jsonl`.
2. **Candidate:** the same, with `--config <candidate>` and another `--out`.
3. `scripts/count_labels.py <candidate events> --baseline <baseline events>`: it lists the labels missing from the
   candidate's run.

Some labels depend on timing, not the config (e.g. `/-/ready` answering 503 while Prometheus starts). If a label is
missing, check whether it plausibly depends on the config change before concluding: rerun the candidate, and if
needed the baseline, and compare the union of runs.

paramtrack's testbed leaves the ConfigMap set to the config it ran. When done, put back whatever the user wants the
testbed left with (the shipped config, unless told otherwise).
