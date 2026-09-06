# tofu/ — Proxmox infrastructure as code

OpenTofu with the `bpg/proxmox` provider. Owns: bridges, VM templates
(built from cloud images via a scripted `qm importdisk`), VMs, and cloud-init.

Nothing is applied here until Phase 2. CI only runs `tofu fmt -check` and
`tofu validate` (no Proxmox access from GitHub Actions).

Credentials come from environment variables (`PROXMOX_VE_*`), never from
files in this repo. See `docs/manual-steps.md` for the one-time API token step.
