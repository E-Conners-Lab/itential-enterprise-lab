# IP plan

Phase 1 deliverable. NetBox is the source of truth for every address below
(ADR 0002); this file is the human-readable intent that Phase 2 seeds into
NetBox. Nothing here is configured on any device yet.

## 1. What must not collide

| Prefix | Owner | Why it is off limits |
|---|---|---|
| 192.168.68.0/22 | Home LAN (`vmbr0`) | Proxmox host .161, NetBox .110, EVE-NG .240 live here. Never touched by this repo |
| 172.29.129.0/24 | EVE-NG `nat0` | EVE built-in NAT cloud |
| 172.29.130.0/24 | EVE-NG `wg0` | EVE Pro WireGuard |
| 172.17.0.0/16, 172.18.0.0/16 | Docker default bridges (EVE-NG, NetBox VM) | `docker0` and the first compose network |
| 172.20.20.0/24 | Containerlab default management network | Left at default on the Containerlab host; it never leaves that host |
| 10.42.0.0/16, 10.43.0.0/16 | k3s pod and service CIDRs (k3s defaults) | Kept at default; Cilium runs in tunnel mode so they never appear on the wire |

Everything this lab allocates lives inside **10.100.0.0/14** (10.100.0.0 -
10.103.255.255), which overlaps none of the above. Decision: ADR 0003.

## 2. Lab supernet layout

| Prefix | Role | Status |
|---|---|---|
| 10.100.0.0/24 | **OOB management** (`vmbr1` untagged, EVE-NG `pnet1`) | Allocated in Phase 1, seeded into NetBox in Phase 2 |
| 10.100.1.0/24 - 10.100.255.0/24 | Reserved for future management / service networks (e.g. a second OOB VLAN if the flat /24 ever fills) | Reserved |
| 10.101.0.0/16 | Data-centre in-band (spine/leaf underlay, server VLANs, firewall transit) | Detailed in the network-topology phase |
| 10.102.0.0/16 | Branch in-band (one /20 per branch) | Detailed in the network-topology phase |
| 10.103.0.0/16 | WAN / transit (simulated ISP, tunnels, loopbacks) | Detailed in the network-topology phase |

The in-band /16s are reserved now so that nothing else grabs them; their
sub-allocation is a topology decision and belongs to the phase that builds the
EVE-NG lab.

## 3. OOB management network: 10.100.0.0/24

- **Where it lives:** Proxmox `vmbr1` (VLAN-aware, untagged / PVID 1) and EVE-NG
  `pnet1` (via a second vNIC on VM 300, discovery assumption 6). No cable on
  `nic2` is required (assumption 2).
- **Gateway:** `10.100.0.1` on a dedicated `oob-gw` VM that also has a leg on
  `vmbr0`. It NATs OOB traffic to the home LAN / internet and is the only path
  between the two. The Proxmox host itself gets **no** IP on `vmbr1`. Decision:
  ADR 0004.
- **Workstation access:** a static host route for 10.100.0.0/14 via the
  `oob-gw` home-LAN address (the Mac only; nothing is changed on the home
  router).
- **DHCP:** only the pool in `.240-.254`, served by the DDI service, for
  first-boot / ZTP. Every named host has a static reservation in NetBox.

### 3.1 Allocation blocks

| Range | Block | Rule |
|---|---|---|
| .0 | network | |
| .1 - .15 | Infrastructure | gateway, hypervisor-adjacent things |
| .16 - .31 | k3s nodes and API VIP | |
| .32 - .63 | MetalLB LoadBalancer pool (32 VIPs) | one VIP per exposed k3s service |
| .64 - .95 | Service VMs on Proxmox | |
| .96 - .127 | Reserved (future service VMs) | |
| .128 - .143 | Network devices: firewalls (PA-VM) | EVE-NG nodes, `mgmt` interface |
| .144 - .159 | Network devices: WAN edge routers (C8000v) | |
| .160 - .191 | Network devices: switches (vEOS) | |
| .192 - .223 | Lab endpoints (Windows / Linux) | |
| .224 - .239 | Containerlab CI host and reserved | |
| .240 - .254 | DHCP pool (first boot / ZTP) | |
| .255 | broadcast | |

### 3.2 Static assignments (seeded into NetBox in Phase 2)

Hostnames are the left-hand label in the lab DNS zone (section 4). Placement
"Proxmox" means a VM managed by OpenTofu; "EVE-NG" means a node in the lab
topology; "k3s VIP" means a MetalLB address.

| IP | Hostname | Placement | Service / phase |
|---|---|---|---|
| 10.100.0.1 | oob-gw | Proxmox | OOB gateway + NAT (phase 2) |
| 10.100.0.2 | eve | EVE-NG `pnet1` | EVE-NG management on OOB (phase 2) |
| 10.100.0.3 | *(reserved)* | | Proxmox host, only if a host IP on `vmbr1` is ever approved |
| 10.100.0.4 - .15 | *(reserved)* | | |
| 10.100.0.16 | k3s-01 | Proxmox | k3s server (phase 3) |
| 10.100.0.17 | k3s-02 | Proxmox | k3s server (phase 3) |
| 10.100.0.18 | k3s-03 | Proxmox | k3s server (phase 3) |
| 10.100.0.19 | k3s-api | VIP (kube-vip) | Kubernetes API (phase 3) |
| 10.100.0.20 - .31 | *(reserved)* | | future k3s workers |
| 10.100.0.32 | ingress | k3s VIP | cluster ingress controller (phase 3) |
| 10.100.0.33 | keycloak | k3s VIP | identity (phase 7) |
| 10.100.0.34 | tacacs | k3s VIP | tac_plus (phase 7) |
| 10.100.0.35 | zabbix | k3s VIP | Zabbix server + web (phase 8) |
| 10.100.0.36 | grafana | k3s VIP | Grafana (phase 8) |
| 10.100.0.37 | prometheus | k3s VIP | Prometheus (phase 8) |
| 10.100.0.38 | loki | k3s VIP | Loki push endpoint (phase 8) |
| 10.100.0.39 | gnmic | k3s VIP | gNMIc collector (phase 8) |
| 10.100.0.40 | oxidized | k3s VIP | Oxidized (phase 9) |
| 10.100.0.41 | vault | k3s VIP | Vault (phase 9) |
| 10.100.0.42 | gitea | k3s VIP | Gitea HTTP + SSH (phase 9) |
| 10.100.0.43 - .63 | *(pool)* | k3s VIP | unassigned MetalLB pool |
| 10.100.0.64 | netbox | Proxmox (VM 110, second NIC) | NetBox OOB leg (phase 2) |
| 10.100.0.65 | itential | Proxmox | Itential Platform (phase 5) |
| 10.100.0.66 | iag | Proxmox | Itential Automation Gateway (phase 5) |
| 10.100.0.67 | nios | Proxmox | Infoblox NIOS grid master, LAN1 (phase 6) |
| 10.100.0.68 | ddi-fallback | Proxmox | BIND9 secondary + Kea standby (phase 6) |
| 10.100.0.69 | dc01 | Proxmox | Windows Server AD DS / DNS (phase 7) |
| 10.100.0.70 | panorama | Proxmox | Panorama (phase 10) |
| 10.100.0.71 - .95 | *(reserved)* | | |
| 10.100.0.128 | dc1-fw01 | EVE-NG | PA-VM, DC HA pair member A |
| 10.100.0.129 | dc1-fw02 | EVE-NG | PA-VM, DC HA pair member B |
| 10.100.0.130 | br1-fw01 | EVE-NG | PA-VM, branch 1 |
| 10.100.0.131 | br2-fw01 | EVE-NG | PA-VM, branch 2 |
| 10.100.0.144 | dc1-wan01 | EVE-NG | C8000v DC WAN edge A |
| 10.100.0.145 | dc1-wan02 | EVE-NG | C8000v DC WAN edge B |
| 10.100.0.146 | br1-wan01 | EVE-NG | C8000v branch 1 WAN edge |
| 10.100.0.147 | br2-wan01 | EVE-NG | C8000v branch 2 WAN edge |
| 10.100.0.148 | isp-core01 | EVE-NG | C8000v simulated provider core |
| 10.100.0.160 | dc1-spine01 | EVE-NG | vEOS spine |
| 10.100.0.161 | dc1-spine02 | EVE-NG | vEOS spine |
| 10.100.0.162 | dc1-leaf01 | EVE-NG | vEOS leaf |
| 10.100.0.163 | dc1-leaf02 | EVE-NG | vEOS leaf |
| 10.100.0.164 | dc1-acc01 | EVE-NG | vEOS access / campus |
| 10.100.0.165 | br1-sw01 | EVE-NG | vEOS branch 1 switch |
| 10.100.0.166 | br2-sw01 | EVE-NG | vEOS branch 2 switch |
| 10.100.0.192 | dc1-srv01 | EVE-NG | Ubuntu server endpoint (DC) |
| 10.100.0.193 | br1-pc01 | EVE-NG | Windows 11 client (branch 1) |
| 10.100.0.194 | br2-pc01 | EVE-NG | Windows 11 client (branch 2) |
| 10.100.0.195 | br1-host01 | EVE-NG | Alpine/Ubuntu endpoint (branch 1) |
| 10.100.0.196 | br2-host01 | EVE-NG | Alpine/Ubuntu endpoint (branch 2) |
| 10.100.0.224 | clab | Proxmox | Containerlab CI host (phase 11) |
| 10.100.0.240 - .254 | *(DHCP pool)* | DDI | first-boot / ZTP |

The EVE-NG node list is the *minimum* topology the PID commits to; the
network-topology phase may add nodes inside the blocks above without changing
this plan.

## 4. Lab DNS

| Item | Value | Why |
|---|---|---|
| Lab zone | `lab.internal` | `.internal` is reserved by ICANN (2024) for exactly this use. `.local` (which EVE-NG uses by default) is mDNS and breaks resolvers; a public domain would need real registration for no gain. Decision: ADR 0005 |
| Service records | `<hostname>.lab.internal` | every row in 3.2 gets an A record and a PTR |
| Active Directory forest | `ad.lab.internal`, NetBIOS `LAB` | AD-integrated DNS, delegated from the lab zone. Keeps AD's DNS-owning behaviour out of the main zone |
| Reverse zone | `0.100.10.in-addr.arpa` | |
| Authoritative | Infoblox NIOS (primary), BIND9 on `ddi-fallback` (secondary via AXFR) | Infoblox eval expiry cannot take DNS down (PID pre-mortem) |
| Resolver for every VM / node | `10.100.0.67`, `10.100.0.68` in that order | |
| Home-LAN name for the gateway | none | the home router's DNS is not touched |
| Client resolver for `lab.internal` | 192.168.68.120 (`oob-gw` LAN leg) | macOS: `/etc/resolver/lab.internal`; Windows: NRPT rule or a conditional forwarder on the router if it supports one |

## 5. Open items for Phase 2

- `oob-gw` needs one address on the home LAN. Discovery did not record the home
  DHCP scope, so the owner must pick a free static (proposal: `192.168.68.245`).
  Recorded as PID assumption A-19.
- A VLAN ID for OOB is deliberately **not** used (untagged on `vmbr1`); if the
  topology phase needs tagged lab VLANs on `vmbr1`, they come from 10.101 -
  10.103 and never from the OOB /24.
