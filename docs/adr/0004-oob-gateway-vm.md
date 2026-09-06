# 0004 — A dedicated `oob-gw` VM routes and NATs the OOB network; the Proxmox host gets no IP on `vmbr1`

- **Status:** accepted
- **Date:** 2026-09-06

## Context

`vmbr1` is an isolated bridge with no host IP and no carrier (discovery §1).
Something must route 10.100.0.0/24 to the home LAN so that service VMs can
reach package mirrors and the workstation can reach the OOB network. Options:
(a) give the Proxmox host an IP on `vmbr1` and enable forwarding + NAT on the
host; (b) use EVE-NG's built-in `nat0`; (c) a small Ubuntu VM with one leg on
`vmbr0` and one on `vmbr1`.

## Decision

Option (c): an `oob-gw` VM (1 vCPU / 1 GB) built by OpenTofu from the Ubuntu
cloud image, configured by Ansible with `nftables` masquerade from `vmbr1` to
`vmbr0`, DHCP-free static addressing, and no inbound services except SSH from
the OOB side and from the workstation key.

## Consequences

- The Proxmox host's network config stays exactly as discovered; the only host
  changes in Phase 2 are additive (API token, `snippets`, staging LV).
- `oob-gw` is on the home LAN like NetBox and EVE-NG already are, which the
  kickoff rules allow (only `vmbr0` *configuration* is off limits).
- It is a single point of failure for OOB egress, which is acceptable: nothing
  inside the lab depends on the internet after images are staged, and the VM
  is rebuilt in under a minute from the repo.
- It needs one free static on the home LAN, which the owner must confirm
  (PID assumption A-19).
