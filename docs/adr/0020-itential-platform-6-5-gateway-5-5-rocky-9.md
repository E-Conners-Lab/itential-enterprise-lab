# 0020 — Itential Platform 6.5.2 + Gateway 5.5.2 + Gateway Manager 1.1.1 on Rocky Linux 9.8

- **Status:** amended 2026-09-07 (container path, see below and ADR 0035)
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

## Amendment 2026-09-07 — container images from Itential's private ECR replace the RPM path

The deployer/RPM install on Rocky is not built. Phase 5 runs the `itential-dev-stack`
containers on one Ubuntu VM (ADR 0035). Images and tags, listed with the owner's company
SSO profile on 2026-09-07 and pinned in `itential/versions.yaml` (digests recorded there):

| Image | Tag | Pushed | Why |
|---|---|---|---|
| `automation-platform-config-lcm-flowai` | `6.5.2` | 2026-09-03 | Platform 6.5.2 with the FlowAI bundle; same digest as the descriptive tag `6.5.2-ecm-6.5.2-fai-1.1.0-gm-1.2.3-lcm-6.5.2-2` (Gateway Manager 1.2.3 bundled) |
| `automation-gateway5` | `5.5.2-amd64` | 2026-09-02 | Gateway 5.5.2, the line Platform 6.5 needs |
| `automation-gateway` | `4.4.1` | 2026-09-02 | Gateway 4.4 is the patched line (4.3 gets no further security fixes); needed for Golden Config |
| `ghcr.io/itential/itential-mcp` | `v0.14.0` | 2026-08-13 | MCP server for Claude Code (S4.7) |
| `mongo` / `redis` | `7.0.40` / `7.4.11` | Docker Hub | Platform 6 fully supports MongoDB 7.0 and Redis 7 |

Gateway Manager is no longer a separate RPM: it ships inside the Platform image. Licence:
none required for the lab (owner decision 2026-09-07, manifest 3.5). Rocky template 9001 and
the `iag` VM are dropped.
