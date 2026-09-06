# 0016 — Windows Server 2025 Standard evaluation for the domain controller, with virtio-win 0.1.302

- **Status:** accepted
- **Date:** 2026-09-06

## Context

AD DS/DNS needs Windows Server. The 2025 eval is current (180 days, must
activate within 10 days); 2022 is still offered. Microsoft's answers conflict
on the rearm count and a known bug expires some 2025 evals early. Fedora
virtio-win 0.1.302 is the current stable driver ISO; Proxmox's wiki still
names 0.1.271 because of a vioscsi race in 0.1.285/0.1.292 (manifest 2.7).

## Decision

**Windows Server 2025 Standard (Desktop Experience) evaluation** on Proxmox, 4
vCPU / 8 GB / 80 GB, virtio-blk and virtio-net drivers from **virtio-win
0.1.302**, unattended install from `autounattend.xml`.

## Consequences

Expiry is a Zabbix item; the rearm count is read with `slmgr /dlv` in Phase 7
and stored as evidence. The DC is rebuilt from the Ansible play if the eval
cannot be extended; virtio-blk sidesteps the vioscsi issue.
