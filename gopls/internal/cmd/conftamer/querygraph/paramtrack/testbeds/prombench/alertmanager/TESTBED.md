# prombench-caddyICingress (prombench, module: Alertmanager)

The module runs as the deployment `alertmanager` in namespace `default`, in prombench's kind cluster on the GCE VM
`paramtrack-e2e` (zone `us-central1-b`, project `conftamer`): `gcloud compute ssh --zone us-central1-b paramtrack-e2e
--project conftamer`. kubectl context `kind-prombench`. On the VM, the checkouts are under
`/home/emily/projects/config_tracing`.

paramtrack's testbed, with `--module alertmanager` (`Alertmanager` in `testbeds.py`; its docstring says what it
installs), builds Alertmanager under argmatch and contexttrack, deploys prombench if the VM has no cluster (or an older
variant's: `../upstream.py`), sets the deployment's image to it, and puts the concrete configs in the
`alertmanager-config` ConfigMap. The other modules run stock unless the run instruments them too.

- **Shipped concrete configs,** next to this file:
  - `config` config source: [shipped-config.yml](shipped-config.yml), upstream's ConfigMap with this testbed's patches (patch
    0004 adds the always-firing alert's route; MODULE.md, "Values other modules refer to").
  - `web` config source: none; prombench runs Alertmanager without a web config. To set web keys, start from
    [empty-web.yml](empty-web.yml) (`{}`: plain HTTP, no auth, no headers). A check with a web config adds
    `--web.config.file` to the deployment; one without leaves it off, as the baseline does.
- **Information about the testbed:** the deployed manifests, `$CT/paramtrack-prombench-caddy/prombench/manifests/` on
  the VM (`5a_alertmanager_deployment.yaml`, `5b_amGithubNotifier_deployment.yaml`). Prometheus (prometheus-meta and
  the benchmark instances' alerting) posts alerts to it; it notifies `amgithubnotifier`. In-cluster services new
  receivers can target: `amgithubnotifier` (`http://amgithubnotifier/hook`, answers 500). A receiver that gets alerts
  but fails (e.g. a URL nothing listens on) only adds failed notifications, as long as the existing route still
  notifies `amgithubnotifier`.

## Applying a config
`cc_vm.py` (next to this file) does this section's and "Adding files"' procedures, and the health numbers: copy it to
the VM and run `python3 cc_vm.py files <dir> | check <config> | apply <config> | web <web config> | restart <config>
[<web config>] | health | rmkey <key>` there (its docstring has the details). What it does:

- **`config` config source:** merge-patch the ConfigMap's `alertmanager.yml`, annotate the pod so the kubelet syncs the file
  promptly, wait until the file in the pod has the new content (md5), then `POST /alertmanager/-/reload` in the pod
  (`wget --post-data=`), and check `alertmanager_config_last_reload_successful` is 1 (`/alertmanager/metrics`).
- **Checking a candidate** before applying it: put it in the ConfigMap as `candidate.yml` (synced the same way), then
  `amtool check-config /etc/alertmanager/candidate.yml` in the pod; then remove `candidate.yml` again (a ConfigMap
  holds at most 1 MiB, and a large candidate left there can make the next apply fail).
- **`web` config source:** the same for `web.yml`; there's no reload, since exporter-toolkit reads it on every request, so check
  `/alertmanager/-/ready` answers. `--web.config.file=/etc/alertmanager/web.yml` must be set: a paramtrack run with
  the web config source leaves it set, and `cc_vm.py restart <config> <web config>` sets it.
- **A restart:** scale the deployment to 0, wait for the pod to be deleted, set the files, scale it back to 1 and wait
  for `/alertmanager/-/ready` (as paramtrack's testbed does: `Component.stop` and `start` in `testbeds.py`).

## Adding files
Add a key to the `alertmanager-config` ConfigMap: it appears as `/etc/alertmanager/<key>` (`scripts/value.py --shape
path --dir /etc/alertmanager` gives such paths). Don't use the names `alertmanager.yml` and `web.yml`. Put a copy of
each in the output's `files/`; paramtrack installs them the same way when it runs the workflow. Alertmanager can
write under `/alertmanager` (its storage).

## Health
After each applied config, through `cc_vm.py health`:
- `alertmanager_config_last_reload_successful` 1;
- `alertmanager_alerts_received_total` still rising (Prometheus' alerts arrive), and
  `alertmanager_notifications_total{integration="webhook"}` still rising (the always-firing alert's notifications;
  `alertmanager_notifications_failed_total{integration="webhook"}` rises with it, by design: the notifier answers 500);
- `kubectl --context kind-prombench get pods -A`: no new restarts or failing pods anywhere.

## Message labels (constraint 2)
SKILL.md's constraint 2 says how to check them. On prombench:
- **The baseline run** is the testbed's shared one (paramtrack's README, "Use with /concreteconfig", step 1), and
  `scripts/check_labels.py` instruments the same modules. A check takes about 8 minutes on a reused cluster
  (2026-11-17, with 92 integrations in the candidate), most of it the 240 s workload; `score.py` takes seconds.
- **Labels that depend on timing,** not the config: none seen. Alertmanager's 10 labels were identical in three
  runs of the shipped configs and two checks (2026-11-17: B8, B9 and the runs in kube-apiserver's TESTBED.md).

paramtrack's testbed leaves the ConfigMap set to the configs it ran, `--web.config.file` set if it ran a web config,
and the instrumented image. When done, put back whatever the user wants the testbed left with; a paramtrack run that
doesn't instrument Alertmanager puts back the shipped config, no web config, and upstream's image.
