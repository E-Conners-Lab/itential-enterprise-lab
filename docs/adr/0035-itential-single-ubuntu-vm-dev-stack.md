# 0035 — Itential runs as the vendored `itential-dev-stack` containers on one Ubuntu VM with Docker CE

- **Status:** accepted — placement amended by ADR 0053 (VM 205 retired at S11.8, `mcp.lab.internal` moved to `tools-01`) and ADR 0063 (the VM returns as `itential-dev` with the alias `mcp-dev`)
- **Date:** 2026-09-07
- **Amends:** ADR 0020 (RPM/deployer on two Rocky VMs), PID S4 (v1.4)

## Context

ADR 0020 planned two Rocky 9 VMs installed by `itential.deployer` from Itential's
RPM repository, gated on repository credentials and licence terms that were the
PID's largest schedule risk. On 2026-09-06 the owner proved a working stack on a
work laptop from Itential's private ECR using the public
[`itential-dev-stack`](https://github.com/itential/itential-dev-stack) Compose
file (manifest 3.5), and on 2026-09-07 confirmed that no licence is needed for
the lab and that ECR access is through the company AWS SSO. Itential's Ubuntu
non-support applies to RPM installs; a container host's distribution is
irrelevant to the images, and the lab already has an Ubuntu template and no
Docker host.

## Decision

- **One VM** `itential` (VM 205, 10.100.0.65, 8 vCPU / 24 GB / 160 GB, Ubuntu
  24.04 from template 9000, vmbr1 only) replaces the `itential` + `iag` pair.
  10.100.0.66 is released. `mcp.lab.internal` is an alias A record for .65.
- **Docker CE 29.8.0** from download.docker.com (pinned in
  `itential/versions.yaml`), Compose v2 plugin.
- The upstream `docker-compose.yml` is **vendored unchanged** at the pinned
  commit (sha256 in `versions.yaml`, checked by `tests/test_itential.py`); every
  lab change is in `itential/compose.override.yml`. Profiles `platform` + `gateway5`
  + `mcp` + `ldap`: Platform, MongoDB 7.0 (cache capped at 4 GB), Redis 7, Gateway 5,
  MCP, plus the upstream OpenLDAP (`osixia/openldap:1.4.0`, profile `ldap`) because
  Gateway Manager only honours group membership for AAA-provisioned users: the
  built-in local `admin` is never in a group (observed 2026-09-07, even with the
  membership written to Mongo), so the gateway cluster is created as the LDAP user
  `admin@itential`, which is also the login the owner asked for. Phase 7 points the
  LDAP adapter at Active Directory and drops OpenLDAP. OpenBao is off (Vault in 9).
- **Exposure:** upstream mappings bind to 127.0.0.1; 10.100.0.65 gets 443
  (Platform, lab-CA certificate from ClusterIssuer `lab-ca`), 8083 (Gateway 4),
  50051 (Gateway 5) and 8000 (MCP over streamable HTTP, plain HTTP on the OOB
  segment: accepted SEC exception for the lab, recorded in the Phase 5 PR).
- **Images:** pulled on the VM after a `docker login` with a 12-hour ECR token
  minted on the workstation from the SSO profile (`ECR_AWS_PROFILE`) or pasted
  temporary keys (`ECR_AWS_*`); long-lived keys never reach the VM. Each ECR
  image is `docker save`d to `/srv/images/itential/` with its sha256 in
  `MANIFEST.sha256` so the lab can be rebuilt offline (`images/fetch.sh
  itential` loads from there when ECR is unreachable).
- **Secrets:** `ITENTIAL_ENCRYPTION_KEY`, the rotated admin password and the
  MCP account password live in the repo `.env` (persisted before first use,
  lab-build-lessons) and are rendered into the stack `.env` on the VM (mode
  0600).

- **Gateway 5 is the gateway.** Gateway 4 (Golden Config / Configuration Manager)
  is not needed by any S4 criterion; the owner chose Gateway 5 as the main gateway
  on 2026-09-07. The 4.4.1 image stays staged and pinned so the profile can be
  enabled later without a new pull.

## Consequences

- Budget: 73 vCPU / 263 GB, headroom 17 GB (was 9). The Rocky template 9001
  stays built but unused; it is removed in a later cleanup PR if nothing else
  claims it.
- Manual step 6 becomes "SSO login before a pull". Manifest 3.2/3.3 RPM rows
  are historical; 3.5 is authoritative.
- The dev stack is documented by Itential as development-only; the lab accepts
  that (single host, no HA), which is what the PID's all-in-one sizing already
  assumed.
