# 0007 — `/srv/images` is a 200 GB thin LV, not a directory on `pve-root`

- **Status:** accepted
- **Date:** 2026-09-06

## Context

Discovery assumption 7: `pve-root` has ~73 GB free. The image manifest sums to
roughly 40 GB of vendor images today, but every version bump keeps the old
file until verified, and Windows/NIOS/Panorama images are 4-10 GB each. Filling
the root filesystem of the hypervisor would be a self-inflicted outage.

## Decision

Phase 2 creates a 200 GB thin logical volume `pve/images` from the `local-lvm`
thin pool, formats it ext4, and mounts it at `/srv/images` via `/etc/fstab`.
The manifest checksums are verified in place before any import.

## Consequences

- Thin allocation means the 200 GB is only consumed as files land; the thin
  pool is 3.4 % used.
- `pve-root` is protected from image sprawl.
- This is a host change and is scripted (idempotent) in the Phase 2 Ansible
  play, with the mount recorded in `docs/manual-steps.md` only if it cannot be
  automated.
