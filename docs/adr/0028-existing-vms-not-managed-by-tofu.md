# 0028 — VMs 110 (NetBox) and 300 (EVE-NG) stay outside OpenTofu; NIC additions use a guarded `qm set`

- **Status:** accepted
- **Date:** 2026-09-06

## Context

The PID's tool audit gives OpenTofu ownership of Proxmox VMs. Two VMs predate
this repo and hold state that must not be disturbed: NetBox (VM 110) and
EVE-NG (VM 300, 128 GB, running labs). Importing them into tofu state means
the provider reconciles every attribute it knows about, and a single default
that differs from the live config (CPU flags, cache mode, agent options)
produces a plan that reboots or re-creates the VM. Phase 2 only needs one
extra vNIC on each.

## Decision

VM 110 and VM 300 are **not** tofu resources. Their `net1` on `vmbr1` is added
by the Phase 2 Ansible host play with `qm set`, guarded so the command runs only
when `net1` is absent and fails if `net0` changed. Every VM created by this
repo (templates, `oob-gw`, and later service VMs) is a tofu resource.

## Consequences

- No risk of an accidental plan against the two stateful VMs.
- Their configuration lives in `docs/discovery.md` plus the guarded tasks; a
  rebuild from zero re-creates them by hand or by a later, separate ADR.
- This is the documented exception to the "never `qm` by hand" rule; the
  command is in a play, idempotent, and recorded in the PR.
