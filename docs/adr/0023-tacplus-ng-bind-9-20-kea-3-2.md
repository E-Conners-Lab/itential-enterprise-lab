# 0023 — tac_plus-ng built in-repo from a pinned commit; BIND 9.20 and Kea 3.2.0 official containers on the ddi-fallback VM

- **Status:** accepted
- **Date:** 2026-09-06

## Context

TACACS+ needs an actively maintained daemon; `tac_plus-ng` (event-driven-
servers) is RFC 8907 compliant with TACACS+ over TLS and LDAP, but has no
versioned releases or official image, and the community images are one to two
years stale. Ubuntu 24.04's packaged BIND is 9.18 (EOL 2026-06-17) and its Kea
is 2.4; ISC publishes official containers for BIND 9.20 (ESV) and Kea 3.2.0,
while Kea 3.0.4 LTS has no image yet (manifest 4.2).

## Decision

**tac_plus-ng** is built by `images/tacplus-ng/Dockerfile` from a commit
pinned in the manifest and runs in k3s behind MetalLB. **BIND 9.20**
(`internetsystemsconsortium/bind9:9.20`, pinned by digest) and **Kea 3.2.0**
(`docker.cloudsmith.io/isc/docker/kea-dhcp4:3.2.0`) run as Docker Compose
services with host networking on the `ddi-fallback` VM, not in k3s, so DHCP
broadcasts need no relay.

## Consequences

One in-repo image build (CI builds and scans it). BIND is digest-pinned
because ISC publishes only branch tags. Kea moves to 3.0.x LTS when ISC ships
that image.
