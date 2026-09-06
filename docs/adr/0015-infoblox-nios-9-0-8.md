# 0015 — Infoblox NIOS 9.0.8 as vNIOS IB-V825 on Proxmox with the 150 GB resizable image

- **Status:** accepted
- **Date:** 2026-09-06

## Context

Infoblox qualifies NIOS 9.0.8 and 9.1.1 on Proxmox VE 9.2.3, exactly the lab's
hypervisor; EVE-NG lists only NIOS 8.x. 9.0.6 opened the LTS programme.
IB-V825 is the smallest grid-master-capable model (2 vCPU / 16 GB). The
temporary licence lasts 60 days and the public evaluation sign-up path is
currently broken (manifest 2.6).

## Decision

**NIOS 9.0.8** (WAPI v2.13.8) as **IB-V825**, 2 vCPU / 16 GB / 150 GB
resizable image, Proxmox VM per ADR 0006, cloud-init user-data attached before
first boot, temp licence applied on the console, primary/secondary design per
ADR 0009.

## Consequences

16 GB is double the Phase 0 guess and is counted in the budget. The owner
obtains the image and eval through the support portal (manual step). 9.1.x is
the upgrade path when OAuth on WAPI matters.
