# 0003 — Lab supernet 10.100.0.0/14; OOB management is 10.100.0.0/24

- **Status:** accepted
- **Date:** 2026-09-06

## Context

The lab needs an out-of-band management network shared by Proxmox VMs, k3s
services and every EVE-NG node, plus in-band ranges for the DC, branches and
WAN. Discovery (`docs/discovery.md` §1-2, assumption 3) lists what already
exists: the home LAN 192.168.68.0/22, EVE-NG's 172.29.129.0/24 and
172.29.130.0/24, Docker's 172.17/172.18, and k3s defaults 10.42/10.43.
Containerlab defaults to 172.20.20.0/24.

## Decision

Everything this lab allocates comes from **10.100.0.0/14**. The OOB management
network is **10.100.0.0/24**, untagged on `vmbr1` and on EVE-NG `pnet1`.
10.101/16, 10.102/16 and 10.103/16 are reserved for DC, branch and WAN in-band
use. Block layout and static assignments are in `docs/ip-plan.md` and are
seeded into NetBox before any host receives an address (ADR 0002).

## Consequences

- One flat /24 is enough for the committed topology (about 60 statics + a 32
  address MetalLB pool + a 15 address DHCP pool). If it fills, 10.100.1.0/24
  onward is already reserved for a second management VLAN.
- No VLAN tagging on OOB keeps EVE-NG `pnet1` and cloud-init simple.
- The workstation needs one static route (10.100.0.0/14 via `oob-gw`); the
  home router is never modified.
