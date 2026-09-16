# Resource budget

Phase 1 deliverable. Cost is a design input: every VM and every EVE-NG node is
counted here before it exists, and `verify/` compares `qm config` on the host
against this file (PID eval E10). Amend this file in the same PR as any
sizing change.

## 1. Ceilings

| Resource | Host | Ceiling | Rule |
|---|---|---|---|
| vCPU | 72 threads (2x Xeon Gold 6154) | **108 vCPU allocated** | 1.5:1 oversubscription (kickoff) |
| RAM | 314 GB usable | **300 GB allocated** | leaves ~14 GB for the Proxmox host, ZFS-free page cache and KSM churn; raised from 280 GB by the owner when the dev stack returned (ADR 0063), and to 300 GB for 4 GB vEOS on `clab` (2026-09-17) |
| Disk | `local-lvm` thin pool 1.6 TB, `pve-root` 96 GB | **thin allocation <= 1.5 TB; alert at 80 % *data* usage** | thin-provisioned, so allocation may exceed usage; usage is what pauses VMs. Raised from 1.4 TB by ADR 0063: the usage alert is the real guard, the allocation ceiling is bookkeeping |
| EVE-NG internal | 24 vCPU / 128 GB given to VM 300 | nodes sized so that summed RAM <= 115 GB | EVE-NG oversubscribes CPU freely; RAM is the real limit (KSM helps but is not counted) |

Already allocated before this project (discovery §1): 28 vCPU, 136 GB, 264 GB
disk (VM 110 NetBox and VM 300 EVE-NG).

## 2. Proxmox VMs

| VM | Phase | OS / image | vCPU | RAM (GB) | Disk (GB) | Notes |
|---|---|---|---|---|---|---|
| 300 `eve-ng` (existing) | - | EVE-NG Pro 6.5 | 24 | 128 | 200 | unchanged; internal budget in section 3 |
| 110 `netbox` (existing) | 2 | Ubuntu 24.04 | 4 | 8 | 64 | unchanged (assumption 17); gains a `vmbr1` NIC |
| `oob-gw` | 2 | Ubuntu 24.04 cloud | 1 | 1 | 16 | NAT + forwarding resolver + NTP; 16 GB because clones inherit the template disk |
| `k3s-01` | 3 | Ubuntu 24.04 cloud | 4 | 12 | 80 | 30 GB OS + 50 GB Longhorn |
| `k3s-02` | 3 | Ubuntu 24.04 cloud | 4 | 12 | 80 | |
| `k3s-03` | 3 | Ubuntu 24.04 cloud | 4 | 12 | 80 | |
| 205 `itential-dev` | 5 | Ubuntu 24.04 cloud | 8 | 24 | 160 | dev stack for Copilot prototyping, returned by ADR 0063 (2026-09-16) under a new name. The VM `itential` was retired 2026-09-10 at S11.8 (ADR 0053); its names `itential` and `mcp` stay with production |
| `nios` | 13 | NIOS 9.0.8 IB-V825 | 2 | 16 | 150 | vendor minimum for IB-V825 with the resizable image |
| `ddi-fallback` | 11 | Ubuntu 24.04 cloud | 2 | 2 | 20 | BIND9 + Kea containers, host networking |
| `panorama` | 13 | Panorama 11.1 | 8 | 16 | 141 | 81 system + 60 log disk; Management Only mode accepted; **lever 2 pulled with ADR 0053 (24 -> 16 GB)**. Vendor floor is 16/64, EVE-NG and community run 8/16 (manifest 2.2) |
| `iap-lb` | 8 | Ubuntu 24.04 cloud | 1 | 1 | 16 | nginx in front of the two Platform nodes (ADR 0053) |
| `iap-01` | 8 | Ubuntu 24.04 cloud | 4 | 8 | 60 | Platform 6.5.2 container, node 1 |
| `iap-02` | 8 | Ubuntu 24.04 cloud | 4 | 8 | 60 | Platform 6.5.2 container, node 2 |
| `mongo-01` | 8 | Ubuntu 24.04 cloud | 2 | 4 | 40 | MongoDB 7.0 replica set rs0 |
| `mongo-02` | 8 | Ubuntu 24.04 cloud | 2 | 4 | 40 | MongoDB 7.0 replica set rs0 |
| `mongo-03` | 8 | Ubuntu 24.04 cloud | 2 | 4 | 40 | MongoDB 7.0 replica set rs0 |
| `redis-01` | 8 | Ubuntu 24.04 cloud | 1 | 2 | 16 | Redis 7.4 + Sentinel |
| `redis-02` | 8 | Ubuntu 24.04 cloud | 1 | 2 | 16 | Redis 7.4 + Sentinel |
| `redis-03` | 8 | Ubuntu 24.04 cloud | 1 | 2 | 16 | Redis 7.4 + Sentinel |
| `iag-01` | 8 | Ubuntu 24.04 cloud | 4 | 6 | 60 | Gateway 5 cluster (gateway5, etcd, runner) |
| `tools-01` | 8 | Ubuntu 24.04 cloud | 4 | 8 | 40 | MCP server, Ollama (in-lab model) |
| `clab` | 12 | Ubuntu 24.04 cloud | 8 | 20 | 60 | VM 230, CPU type `host` for nested KVM. Docker + Containerlab, built now for the dev topology (ADR 0063): 2 C8000v (~4 GB each) + 2 vEOS-lab (4 GB each, as in EVE-NG; 16 -> 20 GB on 2026-09-17 after the switches ran out of memory at 2 GB), all vrnetlab in nested KVM (ADR 0063 amendment 2026-09-16); the S10 cEOS CI twin later |
| **Total** | | | **95** | **300** | **1,455** | dc01 removed (ADR 0050), Panorama 16 GB (lever 2), the eleven S11 VMs added and VM 205 retired at S11.8 (ADR 0053), VM 205 back as `itential-dev` (ADR 0063): +8 vCPU / 24 GB / 160 GB |
| Ceiling | | | 108 | 300 | 1,500 | VMs only; the 200 GB `/srv/images` thin LV takes the pool's allocation to 1,655, which only thin provisioning allows: disk is thin, usage decides (section 1) |
| **Headroom** | | | **13 vCPU** | **0 GB** | plan; the dev stack's return took the 8 GB that S11.8 gave back plus the 16 GB the ceiling was raised by (ADR 0063), and the vEOS fix the 4 GB it was raised by again (2026-09-17) |

RAM is the binding constraint. The plan sums to **300 GB against the 300 GB ceiling**, exactly on it: the
eleven production VMs of ADR 0053 are in, VM 205 came out when S11.8 retired the dev-stack (2026-09-10), and it
came back as `itential-dev` (ADR 0063, 2026-09-16) because Copilot needs a sandbox that is not production; the
owner raised the ceiling from 280 to 296 GB to hold it, and to 300 GB when the `clab` VM grew 16 -> 20 GB for
4 GB vEOS switches (2026-09-17). Measured on the hypervisor
**2026-09-11: 17 VMs running, 67 vCPU, 222 GB** — `eve-ng`, `netbox-prod`, `oob-gw`, the three k3s nodes and
the eleven production VMs; `itential-dev` and `clab` add 44 GB, about 266 GB of 314. The firewall-track VMs
(`nios`, `panorama`, 32 GB) are inside the total, but with no plan headroom left the firewall track now
depends on the levers in section 5: they are pulled, in order, before those VMs are built.

## 3. EVE-NG internal budget (inside VM 300: 24 vCPU / 128 GB)

Node sizes are the manifest's "Lab" values. CPU inside EVE-NG is
oversubscribed on purpose (idle lab nodes use little CPU); the PAN-OS boot-storm
drill (PID E3) is what proves the CPU figure is survivable.

| Node | Image | Count | vCPU each | RAM each (GB) | vCPU total | RAM total (GB) |
|---|---|---|---|---|---|---|
| `dc1-fw01/02`, `br1-fw01`, `br2-fw01` | `pa-vm` 11.1 | 4 | 4 | 8 | 16 | 32 |
| `dc1-wan01/02`, `br1-wan01`, `br2-wan01`, `isp-core01` | `c8000v` 17.18.4 | 5 | 2 | 6 | 10 | 30 |
| `dc1-spine01/02`, `dc1-leaf01/02`, `dc1-acc01`, `br1-sw01`, `br2-sw01` | `veos` 4.35.6M | 7 | 2 | 4 | 14 | 28 |
| `br1-pc01`, `br2-pc01` | `win11` 25H2 | 2 | 2 | 6 | 4 | 12 |
| `dc1-srv01` | `ubuntu` 24.04 | 1 | 1 | 1 | 1 | 1 |
| `br1-host01`, `br2-host01` | `ubuntu` 24.04 (Alpine deferred, ADR 0034) | 2 | 1 | 1 | 2 | 2 |
| EVE-NG host OS, Docker, KSM | | | | | | ~6 |
| **Total** | | **21 nodes** | | | **47** (2:1 on 24 vCPU) | **111** |
| Ceiling | | | | | | 115 |

Adding a node inside EVE-NG is a change to this table and to `topology/`;
the builder refuses a topology whose RAM sum exceeds the ceiling.

## 4. k3s workload budget (inside the three 4 vCPU / 12 GB nodes)

Requests are what the Helm values set; limits are 2x requests unless noted.
Sum of requests must stay under ~24 GB so that one node can fail (PID E5).

| Workload | Phase | RAM request (GB) | Note |
|---|---|---|---|
| Cilium + Hubble (3 nodes) | 3 | 1.5 | 0.5 per node |
| Longhorn (3 nodes) | 3 | 3.0 | 1 per node |
| kube-system, Traefik, MetalLB, cert-manager, kube-vip, CNPG operator | 3 | 2.5 | |
| CloudNativePG clusters x3 (Zabbix, Keycloak, Gitea) | 3/7/8/9 | 4.5 | 1.5 each, single instance |
| Keycloak | 9 | 1.5 | |
| tac_plus-ng | 9 | 0.1 | |
| kube-prometheus-stack (Prometheus 10-day retention, Alertmanager, Grafana, exporters) | 7 | 3.0 | |
| Loki (single binary) + Alloy (3 nodes + the syslog receiver) | 7 | 2.5 | |
| gNMIc | 7 | 0.3 | |
| Zabbix server + web | 7 | 1.5 | |
| Oxidized | 8 | 0.5 | |
| Vault (Raft, 1 replica) | 8 | 0.5 | |
| Gitea + Actions runner controller | 8 | 0.7 | |
| **Total requests** | | **22.1** | of 36 GB; survives one node loss with ~2 GB to spare |

Longhorn volumes (2 replicas each): Prometheus 10 GB and Loki 10 GB as built in phase 7 (ADR 0051; the plan said 30 + 20), three
Postgres 10 GB each (Zabbix's is 12 GB since ADR 0064, +2 GB logical), Gitea 10 GB, Vault 2 GB, Oxidized 2 GB, Garage backups
bucket 20 GB (+2 GB metadata) = 116 GB logical, 232 GB physical across the
150 GB of Longhorn disk on three nodes (over-provisioning 200 %, ADR 0031). **This does not fit at 2 replicas**; Phase 3 sets Prometheus and
Loki to 1 replica (they are rebuildable telemetry) which brings physical usage
to 128 GB. Recorded here so that Phase 3 does not discover it.

## 5. Levers, in the order they are pulled

1. **Trim EVE-NG from 128 GB to 120 GB** (frees 8 GB; requires an EVE-NG
   restart, an approval-gated action). Internal budget is 111 GB so this is
   safe.
2. ~~**Panorama 24 -> 16 GB** (frees 8 GB) if it runs in Management Only mode
   without complaint; verified by `show system resources` in Phase 10.~~ Pulled with ADR 0053.
3. ~~**Containerlab 16 -> 12 GB** (frees 4 GB) if only five cEOS nodes are used.~~ No longer holds: the dev
   topology runs two C8000v at ~4 GB each in nested KVM (ADR 0063), with two vEOS at 4 GB, so 20 GB is its floor. What replaces it
   is operational: **stop the dev topology, or the dev stack's containers,** while the firewall track needs
   the RAM (frees up to 40 GB of use, not of allocation) - both rebuild from the repo.
4. **k3s nodes 12 -> 10 GB** (frees 6 GB) only if section 4 requests stay under
   18 GB.

Levers are pulled in a PR that amends this file, never ad hoc.

## 6. Non-compute costs

| Item | Estimate | Basis |
|---|---|---|
| Electricity | ~216 kWh/month, ~$30/month | 300 W average draw, $0.14/kWh (owner to confirm the tariff) |
| Licences | $0 incremental | EVE-NG Pro owned to 2027-04-15; all images eval or free lab editions (manifest) |
| Cloud | $0 | ServiceNow PDI is free; no cloud resources |
| Claude Code tokens | Existing subscription | PID Domain 7 |
