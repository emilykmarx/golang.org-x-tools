# prombench-caddyICingress (prombench, module: Grafana)

The module runs as the deployment `grafana-core` in namespace `default`, in prombench's kind cluster on the GCE VM
`paramtrack-e2e` (zone `us-central1-b`, project `conftamer`): `gcloud compute ssh --zone us-central1-b paramtrack-e2e
--project conftamer`. kubectl context `kind-prombench`. On the VM, the checkouts are under
`/home/emily/projects/config_tracing`.

paramtrack's testbed, with `--module grafana` (`Grafana` in `testbeds.py`; its docstring says what it installs and
where), builds Grafana under argmatch and contexttrack, deploys prombench if the VM has no cluster (or an older
variant's: `../upstream.py`), sets the deployment's image to it, and puts each config source's concrete config (a provisioning file
kind) in that source's ConfigMap, mounted at `/opt/grafana-provision/<source>/`. The other modules run stock unless the run
instruments them too.

- **Shipped concrete configs,** next to this file:
  - `datasources`: [shipped-datasources.yaml](shipped-datasources.yaml) (prometheus-meta and loki-meta);
  - `dashboards`: [shipped-dashboards.yaml](shipped-dashboards.yaml) (one file provider, loading the
    `grafana-dashboards` ConfigMap's dashboards);
  - `plugins` and `alerting`: none; prombench has neither. To set their keys, start from
    [empty-plugins.yaml](empty-plugins.yaml) and [empty-alerting.yaml](empty-alerting.yaml) (`apiVersion: 1` only). A
    check with one creates and mounts the source's ConfigMap; one without leaves it empty, as the baseline does.
- **Information about the testbed:** the deployed manifests, `$CT/paramtrack-prombench-caddy/prombench/manifests/` on
  the VM (`grafana_*.yaml`). Grafana's other settings are the deployment's `GF_*` environment variables (anonymous
  access, the admin password, serving from `/grafana`), plus paramtrack's `GF_PLUGINS_PREINSTALL_SYNC` (the four app
  plugins installed before startup: `testbeds.Grafana.PREINSTALL_SYNC` says why; since 2026-11-17, after B9). The workload's dashboard viewer loads the dashboards through
  Caddy's ingress (`/grafana`), which queries prometheus-meta through Grafana.

## Applying a config
`cc_vm.py` (next to this file) does this section's and "Adding files"' procedures, and the health numbers: copy it to
the VM and run `python3 cc_vm.py files <source> <dir> | apply <source> <config> | restart [<source> <config> ...] |
health | rmkey <source> <key>` there (its docstring has the details). What it does:

- **Apply (and check):** merge-patch the source's ConfigMap's file, annotate the pod so the kubelet syncs it promptly,
  wait until the file in the pod has the new content (md5), then `POST /grafana/api/admin/provisioning/<source>/reload`
  in the pod, as the admin (`wget` with `admin:$GF_SECURITY_ADMIN_PASSWORD`). Any answer but 200 is a failed check
  (MODULE.md, "Checking a config"); the pod log says why.
- **A restart:** scale the deployment to 0, wait for the pod to be deleted, set the files, scale it back to 1 and wait
  for `/grafana/api/health` (as paramtrack's testbed does: `Component.stop` and `start` in `testbeds.py`). Grafana's
  database is an emptyDir, so a restart starts from nothing but the provisioning files.
- `plugins`' and `alerting`'s ConfigMaps must be mounted. A paramtrack run that sets that config source mounts it
  (a run without it, such as the baseline, creates the ConfigMap empty but doesn't mount it), and the mount stays.
  `cc_vm.py apply` and `restart` mount it if it isn't mounted yet; the pod restarts.

## Adding files
Add a key to the source's ConfigMap (`cc_vm.py files <source> <dir>`): it appears as
`/opt/grafana-provision/<source>/<key>`.
Grafana reads every `.yaml`/`.yml` file there (and, for alerting, `.json`) as a provisioning file, so give added files
another extension. Put a copy of each in the output's `files/`; paramtrack installs them the same way when it runs the
workflow. Grafana can write under `/var/lib/grafana` and `/tmp`.

## Health
After each applied config, through `cc_vm.py health`:
- `/api/health` answers, with `"database": "ok"`;
- the datasources include `prometheus-meta` (type `prometheus`, uid `P86B77AEF8F12A5FA`, its shipped URL), and the
  dashboard count is the shipped config's;
- `kubectl --context kind-prombench get pods -A`: no new restarts or failing pods anywhere.

## Message labels (constraint 2)
SKILL.md's constraint 2 says how to check them. On prombench:
- **The baseline run** is the testbed's shared one (paramtrack's README, "Use with /concreteconfig", step 1), and
  `scripts/check_labels.py` instruments the same modules. Duration: 7.5 min on a reused cluster (2026-11-17); a fresh
  one: ../README.md.
- **Labels that depend on timing,** not the config: none seen in Grafana's labels so far (the B8 and B9 baselines and
  Alertmanager's check had the same Grafana labels). The checks also report kube-apiserver's varying labels (its
  TESTBED.md); they don't count for Grafana.

paramtrack's testbed leaves the ConfigMaps set to the configs it ran (the plugins and alerting ones mounted) and the
instrumented image. When done, put back whatever the user wants the testbed left with; a paramtrack run that doesn't
instrument Grafana puts back the shipped configs, empties the plugins and alerting ConfigMaps, and
puts back the stock image.
