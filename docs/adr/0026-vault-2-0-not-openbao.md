# 0026 — HashiCorp Vault 2.0.4 (chart 0.34.1) rather than OpenBao

- **Status:** accepted
- **Date:** 2026-09-06

## Context

A secrets store must replace `.env`. Vault 2.1.0 was five days old on the
decision date; the chart default is 2.0.4. Vault is BUSL 1.1 (fine for a
personal lab; only competing hosted offerings are restricted). OpenBao 2.6.2
is the MPL-2.0 fork with a near-identical chart, but Itential's `adapter-
hashicorp_vault` and Platform's native secrets integration target Vault and
Gateway 5.5's external secrets feature names Vault KV v2 (manifest 4.4).

## Decision

**Vault 2.0.4** via `hashicorp/vault` chart 0.34.1, Raft storage on Longhorn,
manual unseal (keys held by the owner), Kubernetes auth for workloads, AppRole
for Itential and Gateway, PKI secondary CA. OpenBao is the recorded
alternative if Vault's licence ever becomes a problem for publishing the lab.

## Consequences

Itential integration follows the documented path. Auto-unseal is out of scope;
a Vault restart needs the owner (documented in the runbook).
