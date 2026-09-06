# 0018 — Ubuntu 24.04 LTS cloud image (serial 20260826) is the base for every non-Itential Linux VM

- **Status:** accepted
- **Date:** 2026-09-06

## Context

The host already has an older noble cloud image. Every service VM except the
two Itential nodes runs Ubuntu 24.04 (supported to 2029-05-31). The image is
public with a published SHA256 (manifest 2.9).

## Decision

**`noble-server-cloudimg-amd64.img` serial 20260826** (SHA256 `d0fe84bb…0e30`)
becomes the Proxmox cloud-init template in Phase 2; the EVE-NG `linux-
ubuntu-24.04-server` image stays for the DC endpoint.

## Consequences

Template rebuilds are a `tofu` change with a new serial and a new ADR only if
the major version changes; serial bumps are recorded in the manifest.
