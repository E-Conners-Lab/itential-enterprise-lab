# 0022 — Keycloak 26.7.3 via the codecentric keycloakx chart; no Bitnami images anywhere

- **Status:** accepted
- **Date:** 2026-09-06

## Context

Keycloak is the SSO provider for NetBox, Grafana, Gitea and Zabbix. Bitnami
moved its versioned images to a frozen `bitnamilegacy` namespace on 2025-08-28
and pinned tags now require a paid tier, so Bitnami charts and images cannot
be pinned safely (manifest 4.2).

## Decision

**Keycloak 26.7.3** from `quay.io/keycloak/keycloak`, deployed with
**`codecentric/keycloakx` 7.3.1**, database on CloudNativePG, AD user
federation. Bitnami charts and images are banned repo-wide.

## Consequences

Slightly more Helm values than the Bitnami chart offered. Every future chart
choice checks for hidden Bitnami subcharts (Gitea's bundled DB is not used for
that reason).
