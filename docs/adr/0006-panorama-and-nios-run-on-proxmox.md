# 0006 — Panorama and Infoblox NIOS run as Proxmox VMs, not EVE-NG nodes

- **Status:** accepted
- **Date:** 2026-09-06

## Context

Both products ship as KVM qcow2 images and could be EVE-NG nodes. But they are
long-lived *services* (management plane), not lab devices: they are never
rewired, they hold state that must survive lab rebuilds, and they are heavy
(Panorama 16 GB+, NIOS 8 GB+). EVE-NG already holds 128 GB of the 280 GB
budget, and nested virtualisation adds a boot-time penalty to PAN-OS that the
pre-mortem lists as a risk.

## Decision

Panorama and the Infoblox NIOS grid master are Proxmox VMs managed by
OpenTofu, attached to `vmbr1` (OOB) and, for NIOS, also to a lab VLAN when the
topology phase needs DHCP on in-band segments. They are still listed in the
image manifest with their EVE-NG folder names so they *can* be EVE nodes if
the decision is ever reversed.

## Consequences

- No nested-virt penalty; snapshots and rebuilds are `tofu` operations.
- EVE-NG's 128 GB stays for firewalls, routers, switches and endpoints.
- Images are imported with `qm importdisk` from `/srv/images` instead of the
  EVE-NG folder layout; the Phase 2 image-staging script handles both targets.
- Firewalls in EVE-NG reach Panorama over OOB (`10.100.0.70`), which is how a
  real deployment works anyway.
