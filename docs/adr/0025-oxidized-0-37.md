# 0025 — Oxidized 0.37.0 by Kustomize with a NetBox source and Gitea output

- **Status:** accepted
- **Date:** 2026-09-06

## Context

Device config backup is required and Oxidized has no Helm chart (upstream
issue open since 2020). 0.37.0 is the current release; `latest` tracks master
and is never used (manifest 4.4).

## Decision

**`oxidized/oxidized:0.37.0`** as a Kustomize Deployment with a Longhorn PVC,
NetBox as the device source (tag `oxidized`), git output pushed to Gitea.

## Consequences

Models for PAN-OS, IOS XE, EOS and NIOS are upstream; any custom model lives
in `k8s/oxidized/`.
