# 0051 — Observability (S7): what Zabbix owns and what Prometheus owns, TLS at Traefik on the planned VIPs, exporters, the device telemetry lines, and what this phase leaves out

- **Status:** proposed (owner review; design before code per PID rule)
- **Date:** 2026-09-09
- **Related:** ADR 0024 (chart versions, kept), ADR 0034 (device design), ADR 0040/0041 (`wf-config-push-v1` is the only device write path), ADR 0048 (the topology YAML is the only oracle for device config), ADR 0050 (phase order, Windows dropped), PID S7 (amendment 1.16)

## Context

Phase 7 builds S7 on the Phase 3 cluster (k3s 1.36, Cilium, MetalLB pool `.32-.63`, Longhorn, cert-manager with
the lab CA, CloudNativePG, Traefik on `.32` with the wildcard `*.lab.internal` certificate). Measured on
2026-09-09 before any change:

- No device carries SNMP, syslog or gNMI configuration (`show running-config | include snmp|logging|gnxi` is
  empty on every C8000v, `show management api gnmi` on every vEOS says "no transports enabled").
- The C8000v image 17.13.01a does not know the `gnxi` commands (`show gnxi state` fails to parse); gNMI on IOS
  XE cannot be built on this image. vEOS 4.33.1.1F has `management api gnmi`.
- The Windows 11 clients (`br1-pc01`, `br2-pc01`) drop ICMP (100 % loss from the workstation): the Windows
  firewall, and ADR 0050 says nothing manages them.
- The Platform exposes its own Prometheus route, `GET /prometheus_metrics` (documented at
  [docs.itential.com, "Prometheus metrics and configuration"](https://docs.itential.com/itential-platform/monitor/prometheus-metrics)):
  nine `iap_*` families (sessions, API calls, active jobs, heap, CPU), served without a session on 6.5.2
  (measured: 200 unauthenticated; the doc's basic-auth and client-certificate options are properties the lab
  does not set). The job and task metrics are **not** in that route; they live behind the session-authenticated
  API: `GET /workflow_engine/jobs/metrics` (`results[].workflow.name`, `jobsComplete`, `totalRunTime`,
  `slaTargetsMissed`, paged with `skip`/`limit`/`total`; the concepts are documented under
  [Operations Manager, "Jobs and metrics"](https://docs.itential.com/itential-platform/operations-manager/jobs-and-job-metrics)),
  `GET /workflow_engine/tasks/metrics` (`results[].app`, `name`, `metrics[].totalSuccesses`, `totalErrors`,
  `totalSuccessRunTime`), and `/health/applications`, `/health/adapters` (`results[].id`, `state`,
  `connection.state`; the health routes are the documented monitoring routes: an application whose `state` is not
  `RUNNING` is unhealthy).
- `GET /configuration_manager/devices/<name>/configuration` returns the live running config (`config`), so a
  play can tell which device already carries the observability lines without a workflow job.
- The Zabbix community chart 7.1.0 ships appVersion 7.0.23; the 7.0.30 images of ADR 0024 exist on Docker Hub
  (`alpine-7.0.30`); the chart takes an external PostgreSQL through `postgresAccess.existingSecretName` with
  configurable key names, which fits the CloudNativePG `<cluster>-app` secret (`host`, `port`, `username`,
  `password`, `dbname`). The chart's own PostgreSQL is a plain StatefulSet without backups.
- Zabbix 7.0 agent 2 packages exist for Ubuntu 24.04 (the VMs) and 22.04 (the EVE-NG host) at
  `repo.zabbix.com/zabbix/7.0/ubuntu` (7.0.30-1); the zabbix-release packages' SHA-256 are pinned in
  `k8s/observability/versions.yaml`.
- Alloy's `loki.source.syslog` accepts `rfc5424`, `rfc3164` and `raw`; IOS XE emits its own BSD-like format
  (sequence number, then timestamp) that no strict parser accepts, so device lines are taken raw with the
  sender address as the label.
- snmp_exporter 0.30 expands `${VAR}` in `username`/`password`/`priv_password` with
  `--config.expand-environment-variables`, so the SNMPv3 passphrases stay in a Secret.
- `rsyslog` is installed and running on the Ubuntu 24.04 VMs (8.2312).
- Every k3s node uses ~2.5 GB of its 12 GB; Longhorn has 150 GB across the three nodes at 200 %
  over-provisioning (ADR 0031).
- The Grafana Zabbix plugin (`alexanderzobnin-zabbix-app`) is at 6.6.0 (2026-07-30, Grafana >= 11.6).
- NetBox still holds the released reservations `dc01` (10.100.0.69) and `iag` (10.100.0.66); `topology/ipam.yaml`
  and `docs/ip-plan.md` still label the VIPs with the pre-ADR-0050 phase numbers.

## Decision

1. **Ownership.** *Zabbix* owns availability and the things a NOC looks at: every managed host (SNMPv3 for the
   network devices, agent 2 for every Ubuntu machine), HTTP checks for every UI, the "Expiries" host, and the
   S7.6 trigger. *Prometheus* owns metrics: the cluster (kube-prometheus-stack), the devices' interface
   counters through the SNMP exporter, the vEOS fabric through gNMIc, the VIPs and UIs through the blackbox
   exporter, the Platform's job and task metrics through a small exporter, and the S7.6 alert. *Loki* owns
   logs: device syslog and VM syslog through one Alloy receiver on `.38`, k3s pod logs through an Alloy
   DaemonSet. Nothing is monitored twice for the same purpose: Zabbix polls the devices for availability and
   a few health OIDs, Prometheus polls them for counters.
2. **Versions stay as ADR 0024 pinned them** (kube-prometheus-stack 89.2.3 with Prometheus 3.14.0,
   Alertmanager 0.34.0, Grafana 13.2.1; Loki chart 7.3.0 with 3.6.12; Alloy chart 1.12.1 with 1.19.2; gNMIc
   0.47.0; Zabbix 7.0.30 through the community chart 7.1.0). Added: `prometheus-community/prometheus-snmp-exporter`
   9.17.1 (0.30.1), `prometheus-community/prometheus-blackbox-exporter` 11.18.0 (0.28.0), Zabbix agent 2
   7.0.30-1, Grafana Zabbix plugin 6.6.0, and the Platform exporter on the Python image already pinned as
   `runner_base` in `itential/versions.yaml` (no image build: the script is a ConfigMap, stdlib only).
   `k8s/observability/versions.yaml` is the single oracle for all of it plus the VIPs;
   `tests/test_observability.py` holds it to `docs/image-manifest.md` 4.3 and `topology/ipam.yaml`.
   89.2.4 and 90.0.0 exist (2026-09) and are not taken: the manifest was verified against 89.2.3.
3. **One namespace `observability`**, a CloudNativePG cluster `zabbix-db` (PostgreSQL 18.6, 10 Gi Longhorn,
   WAL archiving and a nightly base backup to Garage through the Barman Cloud plugin exactly like
   `platform-db`; the Garage credentials are copied into the namespace by the play), Longhorn volumes for
   Prometheus (10 Gi, 10 days / 8 GB retention), Loki (10 Gi, 7 days), Grafana and Alertmanager (1 Gi each).
   RAM requests stay inside the `docs/resource-budget.md` section 4 rows (Zabbix 1.5 GB, kube-prometheus-stack
   3 GB, Loki + Alloy 2.5 GB, gNMIc 0.3 GB).
4. **TLS terminates at Traefik; every UI keeps its planned VIP.** Each service VIP of the IP plan is an extra
   `LoadBalancer` Service in `kube-system` that selects the Traefik pods (`metallb.io/loadBalancerIPs`), and an
   `Ingress` per UI (`zabbix`, `grafana`, `prometheus`, `alertmanager` as an alias of `.37`, `loki`, `gnmic`)
   routes by host name to the plain-HTTP pod behind it under the wildcard lab-CA certificate that Traefik already
   holds; HTTP redirects to HTTPS as configured in Phase 3. The two VIPs that also carry a non-HTTP port share
   the address with `metallb.io/allow-shared-ip`: `.35` carries Zabbix server 10051 (active agents, trapper) and
   `.38` carries the Alloy syslog receiver 514/tcp+udp. Rejected: native TLS per application (five different
   certificate mechanisms, HTTPS on every in-cluster scrape and datasource) and routing everything through `.32`
   (the IP plan and the resolver already name a VIP per service).
5. **Zabbix configuration is documents in `observability/zabbix/`**, applied by `community.zabbix` 4.2.0
   (added to `requirements.yml`) over the API (`httpapi` with the lab CA): lab templates in the 7.0 export
   format (`lab-network-device`: ICMP every 15 s, unreachable after four misses, so S7.6 fires in about a
   minute; `lab-ios-xe` and `lab-eos` link it with the stock `Cisco IOS by SNMP` and `Network Generic Device by
   SNMP`; `lab-linux` links `Linux by Zabbix agent`), and two generated templates (`observability/zabbix/build.py`,
   committed output, a test proves it matches its source): `lab-web-ui` with one HTTP agent item per UI from
   `observability/observability.yaml` and `lab-expiries` with one Script item per entry of
   `observability/expiries.yaml` (days left, computed in the item's JavaScript from the date, trigger at
   `< 14`). Hosts come from NetBox (the inventory oracle, PIS-15): active devices with platform `ios-xe`/`eos`
   as SNMPv3 hosts, active Ubuntu devices and VMs as agent hosts, plus the two pre-existing VMs NetBox holds
   only as active addresses (`eve`, `netbox`); host groups `lab/<site>`, `lab/vms`. SNMPv3 passphrases and the
   Grafana read-only user's password are secret global macros / secrets from `.env`. The Zabbix `Admin`
   password is generated once and persisted to `.env` before use (lab-build-lessons), same for
   `GRAFANA_ADMIN_PASSWORD`, `ZABBIX_GRAFANA_PASSWORD`, `SNMPV3_AUTH_PASSWORD`, `SNMPV3_PRIV_PASSWORD`.
6. **Every Ubuntu machine gets Zabbix agent 2 (passive, `Server=10.100.0.0/24`) and an rsyslog forward** (RFC 5424
   over TCP to `.38`) from `ansible/playbooks/observability-hosts.yml`, driven by the NetBox inventory for the VMs
   and the EVE-NG endpoints and by `inventory/phase2.yml` for the two pre-existing machines (`eve-ng`, `netbox-vm`).
   No Alloy on the VMs: rsyslog is already there.
7. **The device lines live in the topology templates** (`topology/configs/c8000v-observability.j2` and
   `veos-observability.j2`, included by the startup-config templates, values from a new `lab.observability`
   block in `topology/enterprise.yaml`: syslog host, SNMPv3 view/group/user names, gNMI): SNMPv3 view/group,
   syslog to `.38` in VRF MGMT, `management api gnmi` in VRF MGMT on EOS (plaintext gRPC, the automation account).
   The SNMPv3 *user* line carries passphrases and is rendered only at push time from `.env`; the committed
   startup configs and Golden Config never hold a secret. `topology/generated/configs/` is regenerated and
   `itential/golden-config/*/base.gc` gains the same lines, so the nightly compliance plan checks them. The
   running devices receive the delta through `ansible/playbooks/observability-devices.yml`: it reads each
   running config from Configuration Manager, skips devices that already carry the lines, and starts one
   `wf-config-push-v1` job per device that still needs them, then waits for the Work Center approvals (the owner
   approves the cards; the play never approves). Rejected: `eve/build.py push-configs` (wipes and restarts every
   node) and a direct SSH push (ADR 0040).
8. **gNMIc subscribes to the seven vEOS only** (interface counters every 30 s, BGP neighbour session state), from
   a ConfigMap the play renders from NetBox; Prometheus output with `strings-as-labels`, so the S7.3 count is the
   number of `ESTABLISHED` series for `dc1-spine01`. IOS XE telemetry is the SNMP exporter (`if_mib` walk, one
   `ScrapeConfig` per platform with the SNMPv3 auth) until an image that knows `gnxi` is loaded.
9. **Prometheus targets are declared in git.** `observability/observability.yaml` lists every scrape job with
   the rule that gives its target count (a number, or `devices`, `eos`, `nodes`, `web_checks`); the play creates
   exactly those objects (ServiceMonitors, `Probe`s for blackbox, `ScrapeConfig`s for SNMP) and the verify
   compares `/api/v1/targets` per job with the document. The k3s control-plane components that k3s binds to
   localhost (scheduler, controller manager, etcd) and kube-proxy (replaced by Cilium) are disabled in the chart,
   so nothing is declared that cannot be up. A `PrometheusRule` carries the lab alerts (`LabDeviceDown` on the
   blackbox ICMP probe after 1 minute, `LabTargetDown`, `LabUiDown`, `LabBgpSessionDown` on the gNMIc state).
10. **The Platform's own `/prometheus_metrics` route is scraped as job `itential-platform`** (a `ScrapeConfig`
    over HTTPS with the lab CA, per the documented scrape example), and **the Platform exporter**
    (`observability/itential-exporter/exporter.py`, stdlib only, served by `http.server`) adds what the native
    route lacks: it logs in with the admin account (the Accounts API cannot create a local read-only user, memory
    2026-09-07; accepted exception until the identity phase) and exposes gauges/counters:
    `itential_workflow_jobs_complete_total{workflow}`, `itential_workflow_run_time_ms_total{workflow}`,
    `itential_workflow_sla_missed_total{workflow}`, `itential_task_successes_total{app,task}`,
    `itential_task_errors_total{app,task}`, `itential_application_running{app}`, `itential_adapter_online{adapter}`,
    `itential_up`. This closes the S4d deferral.
11. **Grafana** provisions the Prometheus, Alertmanager, Loki and Zabbix datasources (the Zabbix plugin, with the
    lab CA) and the dashboards from `observability/grafana/dashboards/*.json` as sidecar ConfigMaps: "Lab health",
    "Fabric", "WAN", "Expiries"; "Firewalls" waits for the firewall track. S7.5 (login through Keycloak, every
    dashboard renders with data) is asserted in phase 9 (ADR 0050); this phase asserts only that the dashboards
    are provisioned.
12. **What S7.1 means now.** "Every host in the IP plan" = every NetBox device or VM with status `active` plus the
    two pre-existing machines, except the two Windows 11 clients (ADR 0050: unmanaged; their firewall drops
    ICMP). Green = enabled, every interface available, no problem from a `lab-*` trigger. The verify's second
    sources: `snmpget` from the workstation with the same SNMPv3 credentials, `systemctl is-active zabbix-agent2`
    over SSH, and `verify/devcmd.py` for the BGP table and the device log.
13. **S7.6 is a drill** (PIS-09): the verify stops `br1-wan01` through the EVE-NG API only with `VERIFY_DRILLS=1`,
    expects the Zabbix problem and the Alertmanager alert within 3 minutes, starts the node and expects both to
    clear; one drill log is part of the PR.
14. **Housekeeping carried in the same PR:** the IP plan's phase labels follow ADR 0050 (`topology/ipam.yaml`,
    `docs/ip-plan.md`, the NetBox `phase-*` tags on the next seed run), `prometheus` gains the alias
    `alertmanager`, and `netbox-seed.yml` deletes seeded addresses the YAML no longer lists (`dc01`, `iag`).

## Consequences

- Twelve governed pushes (one per router and switch) need the owner's Work Center approvals before S7.1
  (devices), S7.3 and S7.4 can pass; the k3s side, the hosts and the Expiries/UI checks do not wait on them.
- The Windows clients are the only IP-plan hosts without a Zabbix host; the PA-VM and Panorama entries wait for
  the firewall track (ADR 0050), NIOS/ddi-fallback/clab for their phases.
- The Prometheus and Alertmanager UIs are reachable without a login (as upstream); they sit on the OOB network
  behind the lab CA. A Keycloak front (oauth2-proxy) is a phase 9 candidate with S7.5.
- SNMP over VRF MGMT: IOS XE answers polls arriving on a VRF interface without extra configuration; EOS needs
  `snmp-server vrf MGMT`. Both are in the snippets and in Golden Config.
- Loki stores device lines unparsed (label `host_ip` from the sender); Grafana/LogQL filters do the parsing.
  If the raw listener drops the sender label, the fallback is `rfc3164` with `logging origin-id hostname`.
- The Grafana admin, the Zabbix admin and the Grafana read-only Zabbix user are three more secrets in `.env`
  until Vault (phase 8).
