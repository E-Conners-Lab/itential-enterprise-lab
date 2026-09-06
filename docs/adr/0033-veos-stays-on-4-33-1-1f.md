# 0033 — vEOS-lab stays on the loaded 4.33.1.1F; supersedes ADR 0013

- **Status:** accepted (supersedes 0013)
- **Date:** 2026-09-06

## Context

ADR 0013 chose EOS 4.35.6M for vEOS-lab with cEOS-lab at the same version.
The owner already holds `veos-4.33.1.1F` (`hda.qcow2` + Aboot `cdrom.iso`) on
EVE-NG and wants to start the topology from it, as with the C8000v (ADR 0032).
4.33 is a train that has since had maintenance rebuilds up to 4.33.10M; the
loaded build is an early feature release.

## Decision

Phase 4 builds the seven vEOS nodes from the existing
`/opt/unetlab/addons/qemu/veos-4.33.1.1F/` image. The manifest records
4.33.1.1F as running and 4.35.6M as the upgrade target. For Phase 11 the
Containerlab twin uses the newest cEOS-lab build of the **4.33 train**
available for download at that time so that the fabric configuration is
comparable; exact parity with 4.33.1.1F is not required and is recorded then.

## Consequences

- No vEOS download for Phase 4. `images/fetch.sh` keeps the `ardl` path for
  the upgrade and for cEOS, but does not run it by default.
- An early F build may carry bugs fixed in later M builds; if one blocks an
  EVPN/MLAG feature in Phase 4, the upgrade to 4.35.6M is one manifest row,
  one `ardl` download and a new ADR.
