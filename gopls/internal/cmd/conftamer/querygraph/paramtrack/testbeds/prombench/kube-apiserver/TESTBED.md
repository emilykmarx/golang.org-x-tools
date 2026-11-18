# prombench-caddyICingress (prombench, module: the Kubernetes API server)

**Incomplete:** only "Message labels" below is written. The concreteconfig skill needs the other sections (as in
`../prometheus/TESTBED.md`), so a skill run for kube-apiserver must stop (SKILL.md, "Inputs").

The module is the cluster's own API server (`KubeAPIServer` in `testbeds.py`; its docstring says how it's
instrumented, configured and reloaded, and which fixtures it gets).

## Message labels (constraint 2)
- **The baseline run** is the testbed's shared one (paramtrack's README, "Use with /concreteconfig", step 1).
- **Labels that depend on timing:** the API server's labels vary between runs of the same configs; the other four
  modules' don't. Of seven runs on 2026-11-17 with every module instrumented (none changing the API server's config),
  the API server had 589-618 labels each, 651 in all, and 81 of them in only some runs; Alertmanager, Caddy, Grafana and
  Prometheus lost no label between them but what a config change explains (Alertmanager's first candidate lost its two
  `/hook` labels; its result.md). The runs, all on their shipped configs unless said: B8
  (`/tmp/paramtrack-b8-2026-11-17`, cluster reused), a check with Alertmanager's shipped config
  (`/tmp/paramtrack-chk-am-2026-11-17`, reused), B9 (`/tmp/paramtrack-b9-2026-11-17`, freshly deployed), Alertmanager's
  two skill-run checks (`gopls/results/alertmanager/concreteconfig-2026-11-17/work/labels-1` and `labels-2`,
  Alertmanager on its candidate configs, reused), Grafana's skill-run check
  (`gopls/results/grafana/concreteconfig-2026-11-17/work/labels-1`, Grafana on its concrete configs, reused) and the
  joint check (`/tmp/paramtrack-full-am-gf-2026-11-17`, Alertmanager and Grafana on theirs, freshly deployed). The 81,
  with the runs that have each: [varying-labels-2026-11-17.txt](varying-labels-2026-11-17.txt) (`scripts/count_labels.py
  --varying` on the seven runs' kube-apiserver events). A run lacks 2-38 of another's labels. They fall into these
  groups:
  - **403s** on reads, mostly cluster-wide lists (`/api/v1/namespaces`, `nodes`, `pods`, `services`,
    `persistentvolumes`, `/apis/storage.k8s.io/v1/csidrivers`, ...), the controller-manager's lease and a few named
    reads (`extension-apiserver-authentication`, `/apis/events.k8s.io/v1`): which ones a client without permission
    happens to try within the run.
  - **Requests the API server sends itself** to `kube-system`: `limitranges`, `resourcequotas`, and the `kindnet` and
    `kube-proxy` pods.
  - **Controllers' reads and writes by name:** `GET` of named nodes, csinodes, replicasets, statefulsets and daemonsets;
    deployments `GET` and `PUT`; a replicaset status `PUT` answering 409.
  - **Restart artifacts:** pod `DELETE` (the API server's mirror pod) and `pods/exec` answering 400; one run had an
    exec into Caddy's controller answering 500, and one a proxied `/prometheus-meta/-/ready` answering 503 (Prometheus
    still starting).
  - **Fresh cluster only** (B9 and the joint check): a ConfigMap `GET` answering 404 and `POST` answering 201 (the
    testbed creating a ConfigMap the cluster doesn't have yet, e.g. Grafana's plugins and alerting ones) and a pod
    `DELETE` answering 404; B9 also a pod `DELETE` answering 409. A check on a reused cluster never has these, so
    compare it with a baseline from a reused cluster.
