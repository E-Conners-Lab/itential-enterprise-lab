# Resource budget

Phase 1 deliverable. Cost is a design input: every VM and every EVE-NG node is
counted here before it exists, and `verify/` compares `qm config` on the host
against this file (PID eval E10). Amend this file in the same PR as any
sizing change.

## 1. Ceilings

| Resource | Host | Ceiling | Rule |
|---|---|---|---|
| vCPU | 72 threads (2x Xeon Gold 6154) | **108 vCPU allocated** | 1.5:1 oversubscription (kickoff) |
| RAM | 314 GB usable | **280 GB allocated** | leaves ~34 GB for the Proxmox host, ZFS-free page cache and KSM churn |
| Disk | `local-lvm` thin pool 1.6 TB, `pve-root` 96 GB | **thin allocation <= 1.4 TB; alert at 80 % *data* usage** | thin-provisioned, so allocation may exceed usage; usage is what pauses VMs |
| EVE-NG internal | 24 vCPU / 128 GB given to VM 300 | nodes sized so that summed RAM <= 115 GB | EVE-NG oversubscribes CPU freely; RAM is the real limit (KSM helps but is not counted) |

Already allocated before this project (discovery §1): 28 vCPU, 136 GB, 264 GB
disk (VM 110 NetBox and VM 300 EVE-NG).

## 2. Proxmox VMs

| VM | Phase | OS / image | vCPU | RAM (GB) | Disk (GB) | Notes |
|---|---|---|---|---|---|---|
| 300 `eve-ng` (existing) | - | EVE-NG Pro 6.5 | 24 | 128 | 200 | unchanged; internal budget in section 3 |
| 110 `netbox` (existing) | 2 | Ubuntu 24.04 | 4 | 8 | 64 | unchanged (assumption 17); gains a `vmbr1` NIC |
| `oob-gw` | 2 | Ubuntu 24.04 cloud | 1 | 1 | 8 | NAT + forwarding resolver + NTP |
| `k3s-01` | 3 | Ubuntu 24.04 cloud | 4 | 12 | 80 | 30 GB OS + 50 GB Longhorn |
| `k3s-02` | 3 | Ubuntu 24.04 cloud | 4 | 12 | 80 | |
| `k3s-03` | 3 | Ubuntu 24.04 cloud | 4 | 12 | 80 | |
| `itential` | 5 | Rocky 9.8 cloud | 8 | 24 | 160 | Platform + MongoDB 7 + Redis 7 all-in-one; 40 root, 60 `/opt/itential`, 60 `/var/lib/mongo`. Sizing **unverified** (manifest 3.2); first lever if short is section 5 |
| `iag` | 5 | Rocky 9.8 cloud | 4 | 8 | 40 | Gateway 5 server + runner |
| `nios` | 6 | NIOS 9.0.8 IB-V825 | 2 | 16 | 150 | vendor minimum for IB-V825 with the resizable image |
| `ddi-fallback` | 6 | Ubuntu 24.04 cloud | 2 | 2 | 20 | BIND9 + Kea containers, host networking |
| `dc01` | 7 | Windows Server 2025 eval | 4 | 8 | 80 | AD DS + DNS |
| `panorama` | 10 | Panorama 11.1 | 8 | 24 | 141 | 81 system + 60 log disk; Management Only mode accepted. Vendor floor is 16/64, EVE-NG and community run 8/16 (manifest 2.2) |
| `clab` | 11 | Ubuntu 24.04 cloud | 8 | 16 | 60 | Docker + Containerlab, 5 cEOS nodes at ~1.5 GB + runner |
| **Total** | | | **77** | **271** | **1,163** | |
| Ceiling | | | 108 | 280 | 1,400 (with the 200 GB `/srv/images` LV: 1,363) | |
| **Headroom** | | | **31 vCPU** | **9 GB** | ~37 GB | |

RAM is the binding constraint. The 9 GB of headroom is deliberately not
pre-assigned; section 5 lists the levers in the order they are pulled.

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
| `dc1-srv01` | `ubuntu` 24.04 | 1 | 2 | 2 | 2 | 2 |
| `br1-host01`, `br2-host01` | `alpine` 3.24 | 2 | 1 | 0.5 | 2 | 1 |
| EVE-NG host OS, Docker, KSM | | | | | | ~6 |
| **Total** | | **21 nodes** | | | **48** (2:1 on 24 vCPU) | **111** |
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
| Keycloak | 7 | 1.5 | |
| tac_plus-ng | 7 | 0.1 | |
| kube-prometheus-stack (Prometheus 15-day retention, Alertmanager, Grafana, exporters) | 8 | 3.0 | |
| Loki (single binary) + Alloy (3 nodes) | 8 | 2.5 | |
| gNMIc | 8 | 0.3 | |
| Zabbix server + web | 8 | 1.5 | |
| Oxidized | 9 | 0.5 | |
| Vault (Raft, 1 replica) | 9 | 0.5 | |
| Gitea + Actions runner controller | 9 | 0.7 | |
| **Total requests** | | **22.1** | of 36 GB; survives one node loss with ~2 GB to spare |

Longhorn volumes (2 replicas each): Prometheus 30 GB, Loki 20 GB, three
Postgres 10 GB each, Gitea 10 GB, Vault 2 GB, Oxidized 2 GB, backups bucket
20 GB = 114 GB logical, 228 GB physical across the 150 GB of Longhorn disk on
three nodes. **This does not fit at 2 replicas**; Phase 3 sets Prometheus and
Loki to 1 replica (they are rebuildable telemetry) which brings physical usage
to 128 GB. Recorded here so that Phase 3 does not discover it.

## 5. Levers, in the order they are pulled

1. **Trim EVE-NG from 128 GB to 120 GB** (frees 8 GB; requires an EVE-NG
   restart, an approval-gated action). Internal budget is 111 GB so this is
   safe.
2. **Panorama 24 -> 16 GB** (frees 8 GB) if it runs in Management Only mode
   without complaint; verified by `show system resources` in Phase 10.
3. **Containerlab 16 -> 12 GB** (frees 4 GB) if only five cEOS nodes are used.
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
