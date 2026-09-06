# 0011 — Panorama runs 11.1 on Proxmox in Management Only mode at 8 vCPU / 24 GB

- **Status:** accepted
- **Date:** 2026-09-06

## Context

Panorama must be at least the firewalls' version. Palo Alto's current
documented floor for the virtual appliance is 16 vCPU / 64 GB (Management
Only) and 12.1.2+ needs a 224 GB system disk; EVE-NG's own table and community
reports run 10.x-11.x at 8 vCPU / 16 GB. The RAM ceiling is 280 GB and EVE-NG
already holds 128 GB. Licensing for a fresh unlicensed Panorama is unverified
(manifest 2.2).

## Decision

Panorama **11.1**, same maintenance release as ADR 0010, as a Proxmox VM (ADR
0006) with 8 vCPU / 24 GB, an 81 GB system disk and a 60 GB log disk,
accepting Management Only mode. The owner requests an evaluation Panorama
licence through the support portal before Phase 10.

## Consequences

Fits the budget with the lever of 16 GB if it runs fine, or 16 vCPU / 64 GB by
trimming EVE-NG if it falls into Maintenance mode. If no licence can be
obtained, Phase 10 degrades to "firewalls onboarded, policy still pushed per-
device by Itential" and this ADR is superseded.
