# 0034 — Lab topology: one EVE-NG lab, EVPN-VXLAN DC fabric, IPsec/GRE WAN, two branches; built from `topology/*.yaml`

- **Status:** accepted
- **Date:** 2026-09-06

## Context

PID S3 commits to a DC with an HA firewall pair and a two-spine/two-leaf
fabric, a simulated provider core, and two branches, all manageable over OOB
from Phase 2 and automatable by Itential from Phase 5. ADR 0002 requires one
YAML to drive both EVE-NG and NetBox. The images available are the ones on
EVE-NG today (ADR 0032, 0033), PA-VM 11.1 (manual download) and the Windows
11 eval ISO (staged). Every node's first interface is management on `pnet1`.

## Decision

- **One lab** `/enterprise.unl` on EVE-NG. Management is the `pnet1` cloud;
  every point-to-point link is a hidden EVE-NG bridge network created by the
  builder. Nodes carry the OOB address from `topology/ipam.yaml`.
- **Provider**: `isp-core01` (C8000v, AS 65000) hands each site a /30 from
  10.103.0.0/24 and a default route over eBGP.
- **DC1 WAN edge**: `dc1-wan01/02` (C8000v, AS 65100) with eBGP to the ISP,
  iBGP between them, GRE-over-IPsec tunnels to each branch (10.103.100.0/24).
- **DC1 firewalls**: `dc1-fw01/02` (PA-VM 11.1) active/passive: HA1 over
  management, HA2 on `eth1/3`; untrust `eth1/1`+`eth1/2` towards the WAN
  routers (10.101.2.0/24), trust `eth1/4` into the fabric (10.101.1.0/24).
- **DC1 fabric**: `dc1-spine01/02` (AS 65101) and `dc1-leaf01/02` (MLAG pair,
  AS 65102) run eBGP underlay on /31s from 10.101.255.0/24 with loopbacks in
  10.101.254.0/24, iBGP EVPN overlay with VTEPs in 10.101.253.0/24;
  `dc1-acc01` is an L2 access switch on an MLAG port-channel; server VLAN 10 =
  10.101.10.0/24 (`dc1-srv01`), firewall transit VLAN 100 = 10.101.1.0/24.
- **Branches** `br1`, `br2`: `brN-wan01` (C8000v, AS 6520N) with tunnels to
  both DC edges, `brN-fw01` (PA-VM, NAT + policy), `brN-sw01` (vEOS L2),
  `brN-pc01` (Windows 11) and `brN-host01` (Ubuntu). Each branch owns a /20
  from 10.102.0.0/16 (br1 10.102.16.0/20, br2 10.102.32.0/20); user VLAN 10 is
  the first /24 of it.
- **Endpoints**: Linux endpoints use the existing `linux-ubuntu-24.04-server`
  image at 1 GB (the Alpine entry in the budget is replaced; +1 GB total).
  Windows 11 clients are built once, unattended, on the Proxmox host from the
  staged ISO into an EVE-NG image (`win-11-25h2`); if that automation fails the
  clients are deferred to Phase 7 and the PID is amended.
- **Startup configs** are Jinja templates per platform rendered by the
  builder and uploaded through EVE-NG's config API (`config: 1`), so a node
  boots with management, AAA and its routing baseline.
- **NetBox** receives sites, device types, roles, devices, interfaces,
  cables, the in-band prefixes and every address from the same YAML before
  the EVE-NG lab is created.

## Consequences

- 21 nodes, 29 links, 106 GB inside EVE-NG's 115 GB ceiling
  (`docs/resource-budget.md` §3 amended for the Linux endpoints).
- The topology YAML is the oracle for both systems; `tests/test_topology.py`
  holds it to the IP plan and the budget, and `verify/test-04-topology.sh`
  compares NetBox, the EVE-NG API and the devices themselves.
- EVE-NG templates default C8000v to 4 vCPU / 8 GB and vEOS to 6 GB; the
  builder passes the budget's smaller values per node.
