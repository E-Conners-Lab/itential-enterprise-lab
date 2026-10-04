# 0071 — Zabbix is removed from the lab; the expiries move to Prometheus rules

- **Status:** accepted (owner, 2026-10-04)
- **Date:** 2026-10-04
- **Amends:** ADR 0051 (what Zabbix owns: decisions 1, 3, 5, 6 and the S7.6 trigger), ADR 0057 (the refresh now sizes
  Prometheus only), ADR 0064 (`zabbix-db` is deleted, not kept unbacked), PID S7 (amendment 1.39)
- **Related:** ADR 0050 (phase order), ADR 0063 (the dev stack on VM 205), ADR 0070 (the AWS VPN on production)

## Context

On 2026-10-04 a full verify run, started by mistake during R4's production converge, found the Zabbix database dead.
`zabbix-db`'s 12 GiB volume was full. CloudNativePG refused to start postgres ("no free disk space for WALs"). The
instance pod had crash-looped 1689 times over 18 days, and nobody had noticed, so Zabbix had been down all that time.
The owner's answer: Zabbix serves no purpose in the lab. Prometheus already watches everything Zabbix did, except one
thing.

| Zabbix job (ADR 0051) | Already covered by |
|---|---|
| Device availability (ICMP), the S7.6 drill | blackbox ICMP probes, `LabDeviceDown` |
| SNMPv3 health of the routers and switches | snmp-exporter (`snmp-ios-xe`, `snmp-eos`) |
| HTTP checks for every UI | blackbox HTTP probes, `LabUiDown` |
| Ubuntu host metrics | node exporters on the k3s nodes and the two Platform VMs only |
| **The Expiries host** (licences, evaluations, tokens, the lab CA) | **nothing** |

## Decision

1. **Zabbix leaves the lab.** That covers the server and web (Helm release `zabbix`), its database (`zabbix-db`), the
   Traefik VIP and DNS name `zabbix.lab.internal` (10.100.0.35, released), the Grafana Zabbix app and datasource, Zabbix
   agent 2 on every Ubuntu machine, the `community.zabbix` collection, the generated templates, and the two
   `ZABBIX_*` secrets. Dropping a chart, manifest or template entry deletes nothing that is already running, so the
   plays name every leftover and delete it:
   - `observability.yml` uninstalls the release and deletes the Cluster (CloudNativePG deletes its PVC and secrets
     with it). It waits until no `zabbix-db` volume is left, then deletes the Ingress, the `traefik-vip-zabbix`
     Service and the `grafana-env` Secret.
   - Grafana's values `deleteDatasources` the Zabbix datasource it once provisioned.
   - `observability-hosts.yml` releases the agent's dpkg hold and purges `zabbix-agent2` and `zabbix-release`, which
     also removes the repository.
   - `netbox-seed.yml` deletes the seeded .35 reservation, and the resolver play stops answering for the name.
2. **The expiries move to Prometheus.** `observability/expiries.yaml` stays the one list.
   `observability/expiry_rules.py` turns it into the PrometheusRule `lab-expiries`:
   - a `lab:expiry_days_left` series per entry: `(<date at 00:00 UTC> - time()) / 86400`, evaluated hourly;
   - one alert, `LabExpirySoon`, under `warn_days` (14).

   The rule is plain arithmetic on the clock, so no exporter runs. A test holds the committed manifest to what the list
   renders. The Expiries dashboard reads the series and lists the alert. test-07's S7.1 checks each series against its
   YAML date, and the NetBox token and lab CA dates against their live sources, as the Zabbix check did.
3. **What stays, with its old name.** The devices' SNMPv3 user is still called `zabbix`. snmp-exporter polls as that
   user, and renaming it would mean a governed push to all twelve routers and switches for nothing. The Ubuntu
   machines keep forwarding syslog to Loki (`rsyslog`, unchanged).
4. **The verify cleanups the same run found ride along** (owner, 2026-10-04):
   - S7.2's NetBox host list skips, and names, an active device with no primary IPv4. The mock `dc1-asa01` (the ASA
     pack's NetBox record, 2026-09-29) broke it; it also goes to `planned` in NetBox.
   - S3.8 finds the EVE-NG internet cloud by its type (`nat0`). Its display name is free text and is now "AWS".
   - S11.8 ("VM 205 is retired") is retired itself: ADR 0063 brought VM 205 back as the Copilot dev stack.
   - dc1-wan01's Tunnel10 description follows NetBox ("IPsec VTI to AWS (Hand Off AWS VPN)"). lab-edge's render takes
     it from the pinned target (cloud-devops-pipeline), and the next Hand Off pushes it.

## Consequences

- One monitoring system, not two. Availability, health, HTTP, expiry and the S7.6 drill alerts all come from
  Prometheus and reach Alertmanager. The drill's independent second source is now a ping from the workstation.
- **Lost: host metrics on the Ubuntu machines Prometheus does not scrape.** These are oob-gw, netbox, eve, the EVE-NG
  endpoints, the MongoDB, Redis, Gateway, load-balancer and tools VMs (CPU, memory and disk through the agent). Adding node-exporter there is a
  separate decision. The PID's "Zabbix monitors `local-lvm` usage at 80 %" risk control goes with it, and the
  thin-pool risk is open again until that exporter exists.
- Grafana keeps the Zabbix app's files on its 1 GiB volume until a pod is rebuilt. They are not loaded: the plugin
  list is empty.
- The deletes cannot be undone: the Zabbix configuration, its 18 days of failed state and its metric history are gone.
  Nothing in the repo depended on them. ADR 0064 already called the database rebuildable.
- `.env` keeps the two `ZABBIX_*` lines until the owner removes them. Nothing reads them any more.

## Amendment 2026-10-04: what the first run found

The first run of `observability.yml` removed Zabbix and then stopped at the kube-prometheus-stack upgrade. Two
corrections followed, both in the same play:

- **The database volume was still on disk.** The `longhorn` StorageClass reclaims with `Retain`, so deleting the claim
  left `zabbix-db`'s PersistentVolume `Released` and its Longhorn volume holding about 21 GB. The play now deletes the
  PV that was bound to `zabbix-db-1`, then its Longhorn volume, and nothing else.
- **Grafana's rolling update deadlocked.** Its Longhorn volume attaches to one node at a time: the new pod waited for
  the volume and the old pod kept it, so the upgrade timed out at 15 minutes. Grafana now uses
  `deploymentStrategy: Recreate`, which costs about a minute of Grafana whenever its pod changes.
