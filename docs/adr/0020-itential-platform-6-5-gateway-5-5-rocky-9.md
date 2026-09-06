# 0020 — Itential Platform 6.5.2 + Gateway 5.5.2 + Gateway Manager 1.1.1 on Rocky Linux 9.8

- **Status:** accepted
- **Date:** 2026-09-06

## Context

Platform 6 is the only Extended Support line (end of support 2028-02-27);
2023.2 ends support 2026-10-02. Itential supports only RHEL/Rocky 8/9 and
states Ubuntu is not supported for any component. Gateway 5 is the current
gateway line and Platform 6.5 features need Gateway 5.5 / Gateway Manager
1.1.1. Itential's development sizing is far above a home lab and no smaller
minimum is published; licensing and repository access are only available
through the account representative (manifest 3).

## Decision

**Itential Platform 6.5.2**, **Gateway 5.5.2**, **Gateway Manager 1.1.1**,
installed by `itential.deployer` 4.2.0 and `itential.iag5` 1.1.1 on two
**Rocky 9.8** GenericCloud VMs (all-in-one Platform+MongoDB 7+Redis 7 at 8
vCPU / 24 GB / 160 GB; Gateway at 4 vCPU / 8 GB / 40 GB). Users authenticate
with LDAP to Active Directory.

## Consequences

A second cloud-image template (Rocky) is added in Phase 2. The owner must
confirm repository credentials and licence terms before Phase 5; without them
the phase is blocked. Sizing is measured in Phase 5 and the budget levers are
applied by PR if needed.
