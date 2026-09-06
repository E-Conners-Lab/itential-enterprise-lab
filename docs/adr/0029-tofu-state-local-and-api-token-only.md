# 0029 — OpenTofu state is a local file on the workstation; the provider uses only the API token

- **Status:** accepted
- **Date:** 2026-09-06

## Context

There is no object store or state backend in the lab until at least Phase 9
(Gitea) or a later MinIO on Longhorn. State must live somewhere now. The
`bpg/proxmox` provider needs SSH to the node only for snippets upload and
`source_file` imports; Phase 2 uses neither (cloud-init through the built-in
`initialization` block, images through `local:import/` staged by Ansible).

## Decision

- `tofu/<module>/terraform.tfstate` stays on the workstation, gitignored,
  with a copy taken to the owner's backup location after every apply
  (manual step until Phase 9 provides an in-lab backend).
- The provider block is empty: endpoint and token come from
  `PROXMOX_VE_ENDPOINT` / `PROXMOX_VE_API_TOKEN`; no SSH block, no password.
- The token belongs to `tofu@pve` with role `TofuLab`: the bpg documented
  privilege list minus user, permission, realm, group and mapping management.

## Consequences

- Losing the workstation loses state; recovery is `tofu import` of the few
  VMs this repo created, which is acceptable at this scale.
- Snippets are enabled on `local` for Phase 6 (NIOS needs user-data before
  first boot), at which point an SSH block or the `proxmox_virtual_environment_file`
  upload path gets its own ADR.
- A state backend migration is a Phase 9 deliverable.
