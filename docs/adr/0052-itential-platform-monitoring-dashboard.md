# 0052 — The official "Itential Platform Monitoring" dashboard (grafana.com 25527) and the exporters it expects

- **Status:** accepted (owner request and approval 2026-09-10, PR #23)
- **Date:** 2026-09-10
- **Related:** ADR 0035 (the dev-stack on VM 205; `compose.override.yml` is the lab's only change to the vendored Compose file), ADR 0051 (observability design; the Platform exporter and the native `/prometheus_metrics` route), PID S7 (amendment 1.17)

## Context

Searched on 2026-09-10: docs.itential.com, itential.com, the `itentialopensource` GitLab group and grafana.com.
Itential publishes exactly one Grafana dashboard: **"Itential Platform Monitoring"**, grafana.com dashboard
**25527**, organisation *Itential*, revision 3 of 2026-08-14, announced in the blog post "Itential Platform
Monitoring Is Now in the Grafana Marketplace". Nothing for Gateway, and no dashboard JSON on docs.itential.com
(the "Prometheus metrics and configuration" page documents only the nine `iap_*` gauges). The dashboard has five
rows (Overview, Workflow Engine, Platform Process, Redis, MongoDB), 70 queries, a `datasource` variable and a
`node` variable (`label_values(node_cpu_seconds_total, instance)`), and reads these sources (measured from the
JSON):

| Source | Job label the queries use | Series |
|---|---|---|
| Platform `/prometheus_metrics` | `iap_exporter` | `iap_active_sessions`, heap, CPU; `up{job="iap_exporter"}` |
| "wfe-metrics-exporter" (Itential's, **not published anywhere found**) | any | `itential_job_status_total{status}`, `itential_job_start/complete/error/cancel`, `itential_task_start/complete/error`, `itential_up`, `itential_watcher_reconnects_total` |
| node_exporter on the Platform host | `node_exporter` | CPU, memory, filesystem |
| process_exporter on the Platform host | (any) | `namedprocess_namegroup_*{groupname=~"Pronghorn.* Application|Adapter"}` |
| redis_exporter | `redis_exporter` | `redis_*` incl. `redis_instance_info{role}` |
| mongodb_exporter | `mongo_exporter` | `mongodb_rs_members_*`, `mongodb_ss_*` |

Measured on VM 205: the Platform's Node processes are titled exactly `Pronghorn <Name> Application` /
`Pronghorn <Name> Adapter`; the dev-stack's MongoDB (7.0.40) and Redis (7.4.11) run without authentication on
the Docker network and bind 127.0.0.1 on the host; MongoDB is standalone (no replica set), so the dashboard's
replica-set panels can never show a healthy state there. The Operations Manager jobs API returns light
documents with `include=status` (79 bytes each; 387 jobs today), so job counts per status cost four requests.

## Decision

1. **Vendor the dashboard as published**: `observability/grafana/dashboards/itential-platform-monitoring.json` is
   revision 3 of grafana.com 25527 byte for byte (`k8s/observability/versions.yaml` records id, revision and
   SHA-256; `tests/test_observability.py` holds the file to them). The Grafana sidecar provisions it like the lab
   dashboards; its `datasource` variable selects the Prometheus datasource. Nothing in the JSON is edited: an
   upstream revision bump is a file replacement plus a hash change.
2. **The Platform host's exporters run beside the dev-stack** as services of `itential/compose.override.yml`
   under a new Compose profile `monitoring` (added to `stack.profiles` in `itential/versions.yaml`): node_exporter
   1.12.1, process_exporter 0.8.7 (host PID namespace, one group per Pronghorn process title), redis_exporter
   1.91.1 and mongodb_exporter 0.53.0 (both on the Docker network, no credentials because the dev-stack has none).
   Each listens on the OOB address only (ports 9100, 9256, 9121, 9216); nothing else changes in the stack.
3. **Job names follow the dashboard.** ScrapeConfigs in the observability play name the jobs `node_exporter`,
   `process_exporter`, `redis_exporter`, `mongo_exporter`, and the native route job becomes `iap_exporter`
   scraped at `10.100.0.65:443` (TLS server name `itential.lab.internal`), so its `instance` matches the
   dashboard's `node` variable. `observability/observability.yaml` declares them and the S7.2 count follows.
4. **The lab's Platform exporter stands in for the wfe-metrics-exporter.** It already exports `itential_up` and the
   per-workflow/task series (ADR 0051); it gains the dashboard's series from the same APIs: `itential_job_status_total{status}`
   (jobs per status from `/operations-manager/jobs?include=status`, paged), `itential_job_start/complete/error/cancel`
   (cumulative counts by status: start = all jobs), `itential_task_start/complete/error` (from the global task
   metrics) and `itential_watcher_reconnects_total` (always 0: the lab exporter polls, it has no change-stream
   watcher). If Itential publishes its exporter, it replaces this block and the series names stay.
5. **S7 gains criterion 8** (PID 1.17): the official dashboard is provisioned and every metric family it queries
   has data in Prometheus, except the MongoDB replica-set family, which is documented as absent on the standalone
   dev-stack database.

## Consequences

- The dashboard's MongoDB health tile shows DOWN and the replica-set panels stay empty (standalone MongoDB in
  the dev-stack); operations, connections and WiredTiger cache panels work. A replica set is not a lab goal.
- Four more listeners on `10.100.0.65` (read-only metrics; the OOB network is the trust boundary, as for every
  exporter in `observability`). Vault (phase 8) is the place to add TLS or auth if the boundary changes.
- The exporter's cumulative job counters are derived from the jobs collection: deleting jobs would make them go
  backwards for one scrape (Prometheus `rate()` treats that as a reset). Acceptable for a lab.
- Rejected: editing the dashboard JSON (job names, MongoDB panels) — it would drift from upstream; writing a
  second exporter for the same APIs; running node_exporter as a systemd package on the VM (the stack's
  Compose file is the one place the VM is configured, ADR 0035).
