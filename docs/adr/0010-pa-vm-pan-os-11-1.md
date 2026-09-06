# 0010 — PA-VM runs PAN-OS 11.1 (latest 11.1 KVM base image, upgraded in place to the current 11.1 maintenance release)

- **Status:** accepted
- **Date:** 2026-09-06

## Context

The DC HA pair and both branch firewalls are VM-Series nodes inside EVE-NG Pro
6.5. Candidate trains on 2026-09-06: 10.2 (extended support only, ends
2027-03-31), 11.1 (standard support to 2027-05-03, highest field adoption,
explicitly on Ubuntu 20.04 KVM in Palo Alto's matrix), 11.2 (~12 % adoption,
same EOL), 12.1 (supported to 2028-08-28, no public EVE-NG report, higher and
unpublished RAM needs, and a Panorama that requires a 224 GB disk migration).
Sources are in `docs/image-manifest.md` 2.1.

## Decision

PAN-OS **11.1**: download the newest 11.1 "PAN-OS for VM-Series KVM Base
Image" on the support portal and upgrade in place to the current 11.1
maintenance release (11.1.16-h1 on the decision date). Nodes run at 4 vCPU / 8
GB / 60 GB with QEMU 5.2 or newer in EVE-NG.

## Consequences

Known-good in EVE-NG and the smallest RAM footprint of the supported trains.
Standard support ends in eight months; a follow-up ADR moves to 12.1 once
someone publishes a working EVE-NG 12.1 deployment or the lab tests it in a
spare node. Unlicensed mode caps sessions at ~1,230 and provides no threat
content, which is acceptable for automation demos.
