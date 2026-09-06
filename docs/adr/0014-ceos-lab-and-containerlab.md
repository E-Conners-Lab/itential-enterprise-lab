# 0014 — cEOS-lab 4.35.6M and Containerlab 0.79.0 for the CI tier

- **Status:** accepted
- **Date:** 2026-09-06

## Context

The Containerlab twin (Phase 11) validates fabric changes before EVE-NG.
Fidelity requires the same EOS version as ADR 0013. Containerlab 0.79.0
(2026-08-21) is current; cEOS 4.32.0F+ auto-detects cgroups and supports up to
50 nodes per host (manifest 2.5, 4.5).

## Decision

**cEOS64-lab 4.35.6M** imported as `ceos:4.35.6M`, on **Containerlab 0.79.0**
installed with the pinned installer flag, on the `clab` Ubuntu VM (8 vCPU / 16
GB).

## Consequences

Version parity is a verify criterion (S10 criterion 4). Any EOS bump is a
paired change to ADR 0013 and this ADR.
