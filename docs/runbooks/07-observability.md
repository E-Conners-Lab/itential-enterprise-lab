# 07 — Observability

Zabbix for availability, Prometheus for metrics, Loki for logs, gNMIc for streaming telemetry from the
fabric, and Grafana over all four — including Itential's own published Platform Monitoring dashboard.

Everything runs on the k3s cluster from chapter 02, is configured from documents in the repo, and takes its
list of hosts and targets from NetBox. The device-side lines are pushed through the governed workflow from
chapter 06, with an approval card per device.

This chapter needs no commercial licence except for the Platform metrics at the end.

---

## Before you start

### What must already be true

- Chapter 02 green: MetalLB, Longhorn, cert-manager and CloudNativePG. Zabbix's database is a CNPG cluster,
  and every UI is served by the chapter 02 Traefik under the wildcard lab-CA certificate.
- Chapter 04 green: the devices exist, and their startup configurations carry the observability snippet.
- Chapter 06 green if you want the Platform metrics — S7.7 and S7.8 read the Platform's own routes.
- `.env` carries `SNMPV3_AUTH_PASSWORD` and `SNMPV3_PRIV_PASSWORD`. They are used by the exporter from a
  Secret and by the device configurations, and they never appear in a rendered manifest.

### Who owns what

Nothing is monitored twice for the same purpose. That rule is the whole design
([ADR 0051](../adr/0051-observability-design.md)):

| | Owns | How |
|---|---|---|
| **Zabbix** | Availability, and what a NOC looks at | SNMPv3 for the network devices, agent 2 on every Ubuntu machine, HTTP checks for every UI, an "Expiries" host, and the failure-drill trigger |
| **Prometheus** | Metrics | The cluster via kube-prometheus-stack; device interface counters via the SNMP exporter; the vEOS fabric via gNMIc; VIPs and UIs via the blackbox exporter; the Platform's job and task metrics via a small exporter |
| **Loki** | Logs | Device syslog and VM syslog through **one** Alloy receiver; k3s pod logs through an Alloy DaemonSet |

Zabbix polls the devices for availability and a few health OIDs. Prometheus polls them for counters. They
do not overlap.

### Two things that are simply not possible on these images

- **gNMI on the C8000v.** The 17.13.01a image does not know the `gnxi` commands at all —
  `show gnxi state` fails to parse. gNMIc runs against the vEOS switches only. This is a property of the
  image, not a configuration gap.
- **ICMP to the Windows clients.** They drop it (Windows firewall), and nothing in this lab manages them.

### The addresses

Five MetalLB VIPs from the chapter 01 plan, resolved by name through the gateway's resolver: Zabbix
`10.100.0.35`, Grafana `.36`, Prometheus `.37` (with an `alertmanager` alias), Loki `.38`, gNMIc `.39`.
Two of them are **shared IPs**: Zabbix's VIP also carries the server's own port 10051, and Loki's also
carries Alloy's syslog receiver on 514.

---

## The commands, in order

```
make phase-observability
```

| | Step | What it does |
|---|---|---|
| 1 | `ansible/playbooks/netbox-seed.yml` | Re-seeds, which also **deletes** reservations the plan has released |
| 2 | `ansible/playbooks/oob-gw.yml --tags dns` | Re-renders the resolver so the five new names answer |
| 3 | `ansible/playbooks/observability.yml` | Secrets persisted first, then the Traefik VIP Services, then the charts, then the scrape objects generated from NetBox, then Zabbix's own configuration through its API |
| 4 | `ansible/playbooks/observability-hosts.yml` | Zabbix agent 2 and `rsyslog` on every Ubuntu machine — the NetBox inventory for the VMs and EVE-NG endpoints, plus a static inventory for the two pre-existing machines |
| 5 | `ansible/playbooks/observability-devices.yml` | One `wf-config-push-v1` job per router and switch for the device-side lines. **The owner approves each card in Work Center** |

Step 5 is the one that needs a person. Each device gets its own approval card, and the play skips a device
whose card is still waiting, so a re-run inside the approval window does not start a second job.

The device lines themselves are not typed here: they live in the topology templates as an observability
snippet, so a rebuilt node boots with them, and Golden Config's baselines carry the same lines, so a device
that loses them is flagged as drift.

---

## What "done" looks like

The charts take a while — kube-prometheus-stack is large and Loki plus Alloy add several more workloads.
Budget most of an hour for a first run, plus however long the approvals take.

- Zabbix monitors every NetBox-active host and is green, using the lab templates, with SNMPv3 and the
  agents confirmed from the workstation.
- The **Expiries** host has one item per expiry in the manifest — days left computed from the date, with
  the NetBox token and the lab CA read back live — and a trigger at 14 days.
- Prometheus's target count per job equals the declared numbers, no target is down, and the lab rules are
  loaded.
- Grafana has the Prometheus, Alertmanager, Loki and Zabbix datasources and the lab dashboards.
- gNMIc's BGP session count for a spine equals what the switch says, and Prometheus agrees.
- Loki returns a syslog line **from each vendor** within 60 seconds of a governed no-op push, labelled with
  the device's own hostname.
- Prometheus holds the Platform's job metrics and every application and adapter reads as up.
- The official Itential Platform Monitoring dashboard renders with data in every metric family it queries.

---

## Verification

`verify/test-07-observability.sh` is this chapter's script.

```
verify/test-07-observability.sh
```

| Criterion | What a PASS means |
|---|---|
| S7.1 | Zabbix monitors every NetBox-active host, green, with the lab templates; SNMPv3 and the agents confirmed from the workstation. Plus the Expiries host and its 14-day trigger |
| S7.2 | Prometheus's target count per job equals the declared configuration; no target down; lab rules loaded |
| S7.3 | gNMIc's BGP session count for a spine equals `show bgp summary` — and Prometheus agrees |
| S7.4 | Loki returns a syslog line from **each vendor** within 60 s of a governed no-op push, and the device's own log agrees |
| S7.5 | Grafana login through Keycloak — **deferred to the identity phase**. `S7.5-prep` proves the dashboards and datasources are provisioned |
| S7.6 | The drill: stopping a router raises a Zabbix trigger *and* an Alertmanager alert within 3 minutes, and starting it clears both |
| S7.7 | Prometheus holds the Platform job metrics (per-workflow completions equal to the API) and every application and adapter is up |
| S7.8 | The official Itential dashboard is provisioned and **every metric family it queries has data** |

The drill is worth running once:

```
VERIFY_DRILLS=1 verify/test-07-observability.sh
```

It stops a branch router for a few minutes. Measured here: the Zabbix trigger and the Alertmanager alert
both fired **68 seconds** after the stop, and both cleared 257 seconds after it.

---

## Troubleshooting

**A Helm chart renders empty or Ansible fails inside a template.** Two collisions, both measured: chart
values passed in a variable named `values`, and a play variable named `namespace`, both break Jinja
rendering in ways that produce empty output rather than an error. Rename them.

**A rendered manifest does not take effect.** Use `apply: true` on rendered manifests. A patch merges,
which is not what you want when a key has been *removed* — a stale Loki selector key survived a patch here
and the Service kept selecting nothing.

**A Grafana plugin will not install.** Plugins are `id@version`, not a bare id. The Zabbix plugin needs
Grafana 11.6 or newer.

**Alloy's syslog receiver accepts nothing.** IOS XE emits a BSD-like format — a sequence number, *then* the
timestamp — that no strict parser accepts, so the receiver must take lines `raw`. On current Alloy, `raw`
requires the experimental stability level to be enabled explicitly.

**Every log line arrives with the same host label, or none at all.** Three separate traps stacked here:

- The **sender address cannot be the label**. A `Cluster`-policy LoadBalancer SNATs it, and `Local` breaks
  MetalLB's shared VIPs. The host has to come out of the line itself.
- Helm's `tpl` rendered a `stage.template` body containing `{{ if .host }}` to an **empty string**, so no
  label was ever set and nothing errored. Use one `stage.regex` per vendor format instead — no templating
  needed.
- EOS prints its hostname in the line; IOS XE does not until you add `logging origin-id hostname`, which is
  in the snippet, in the Golden Config baseline and in the reference configurations.

**Zabbix cannot reach its database, or TLS fails.** The Zabbix image pins `SSLCALocation` to
`/var/lib/zabbix/ssl/ssl_ca`, so the lab CA has to be mounted there. The chart takes an external PostgreSQL
through `postgresAccess.existingSecretName`, which lines up with CloudNativePG's `<cluster>-app` secret;
the chart's own PostgreSQL is a plain StatefulSet with no backups, which is why it is not used.

**The Zabbix agent package will not install at the pinned version.** Zabbix `.deb` packages carry **epoch
1**. A version string without it does not match.

**A Zabbix agent on a k3s node is unreachable.** A server pod polling its own node arrives with its **pod**
address, not the node's. The agent's `Server=` list has to include the pod CIDR as well.

**Prometheus reports duplicate timestamps and drops samples.** `/workflow_engine/tasks/metrics` returns one
row **per workflow task** alongside the global rows with the same app and name — 448 duplicate samples per
scrape here. The exporter exports only the global rows.

**A metric route needs a session and a health route does not.** The Platform's own
`GET /prometheus_metrics` serves nine `iap_*` families without a session on 6.5.2. The job and task metrics
are **not** in that route: they are behind the session-authenticated API. And the Platform's login route
answers `text/html`, so a check that assumes JSON reads a login page as a valid response.

**A device gets a no-op push job every run.** The play compares the snippet against the running
configuration line by line, so any line that is a **default** never appears in the running config and
therefore always looks missing. Two here: the EOS gNMI `port 6030` (removed from the snippet) and
`logging trap informational` on both vendors (dropped — a default that never shows). Seven no-op jobs had
to be cancelled before this was understood.

**A re-run starts a duplicate job per device.** The guard that skips a device with a waiting card has to
compare the **real job status**, because the jobs API ignores its status filter. An early version matched
the job description without stripping it — one doubled backslash in a folded scalar — and one re-run
started twelve duplicate jobs. They pushed the same idempotent lines, so no harm, but it is a good example
of a guard that looks right and does nothing.

**Traefik's shared VIPs do not come up.** Create the VIP `Service` objects **before** the charts. MetalLB's
shared-IP policy has to match first; a Service that arrives later asking to share an address that is
already allocated under a different policy just stays pending.

**S7.7 fails immediately after S7.4.** The exporter refreshes on an interval, and the S7.4 pushes complete
seconds before the comparison. The verify waits for the next refresh. If you are comparing Platform
metrics by hand, give the exporter a cycle.

**The verification's Zabbix session breaks partway through the drill.** The drill re-logs in, so the login
helper has to clear the bearer token first.

---

## Tested versions

| Component | Version |
|---|---|
| Zabbix | chart 7.1.0, images `alpine-7.0.30`, agent 2 `7.0.30-1` (Ubuntu 24.04 and 22.04) |
| Zabbix database | CloudNativePG, PostgreSQL 18.6, 10 Gi |
| kube-prometheus-stack | chart 89.2.3 — operator `v0.93.1`, Prometheus `v3.14.0`, Alertmanager `v0.34.0`, Grafana 13.2.1 |
| Grafana Zabbix plugin | `alexanderzobnin-zabbix-app@6.6.0` |
| SNMP exporter | chart 9.17.1, `v0.30.1` (expands `${VAR}` so passphrases stay in a Secret) |
| Blackbox exporter | chart 11.18.0, `v0.28.0` |
| Loki | chart 7.3.0, 3.6.12, single binary, 168 h retention |
| Grafana Alloy | chart 1.12.1, `v1.19.2` |
| gNMIc | `ghcr.io/openconfig/gnmic` 0.47.0 — **vEOS only** |
| Itential dashboard | grafana.com **25527**, revision 3, vendored byte for byte and held to its SHA-256 |
| Storage (Longhorn) | Prometheus 10 Gi / 10 d / 8 GB, Loki 10 Gi, Grafana 1 Gi, Alertmanager 1 Gi |

`k8s/observability/versions.yaml` is the oracle and `tests/test_observability.py` fails CI if it drifts
from the image manifest or the IP plan. Nothing is `latest`.

---

**Previous:** [06 — Platform applications](06-platform-applications.md) · **Next:** [08 — Production HA and migration](08-production-ha2-and-migration.md)
