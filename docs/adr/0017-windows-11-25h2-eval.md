# 0017 — Windows 11 Enterprise 25H2 evaluation for branch clients in EVE-NG, with the documented TPM bypass

- **Status:** accepted
- **Date:** 2026-09-06

## Context

Branch endpoints need a domain-joinable Windows client. The 25H2 Enterprise
eval is current (90 days). Windows 11 requires TPM 2.0 and Secure Boot; EVE-NG
has no swtpm/OVMF support in its templates through 7.2.0-4 and documents the
`LabConfig` registry bypass (manifest 2.8).

## Decision

**Windows 11 Enterprise 25H2 evaluation** as EVE-NG nodes (`win-11-25h2`), 2
vCPU / 6 GB / 64 GB, installed once with the bypass and cloned per branch.

## Consequences

The 90-day clock is a Zabbix item; the golden image is kept pre-activation so
clones restart the clock. Two clients cost 12 GB inside EVE-NG.
