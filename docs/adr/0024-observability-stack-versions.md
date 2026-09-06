# 0024 — Observability versions: kube-prometheus-stack 89.2.3, Loki chart 7.3.0, Alloy 1.19.2, gNMIc 0.47.0, Zabbix 7.0.30 LTS via the community chart

- **Status:** accepted
- **Date:** 2026-09-06

## Context

Prometheus, Grafana, Loki, gNMIc and Zabbix are required. Promtail is EOL
(2026-03-02), so Alloy ships logs. Zabbix's own Helm chart deploys only
agents/proxies; the community `zabbix-community/zabbix` chart deploys server
and web. Zabbix 8.0 LTS is not GA; 7.0 is the supported LTS to 2027-06-30
(manifest 4.3).

## Decision

Pin **kube-prometheus-stack 89.2.3** (Prometheus 3.14.0, Alertmanager 0.34.0,
Grafana 13.2.1 via the OCI subchart), **Loki chart 7.3.0** in single-binary
mode, **Alloy 1.19.2**, **gNMIc 0.47.0** by Kustomize, **Zabbix 7.0.30**
images through `zabbix-community/zabbix` 7.1.0 with the database on
CloudNativePG.

## Consequences

89.2.2 is the fallback for the same-day chart bump. Grafana and Alloy image
tags inferred from chart appVersions are confirmed in Phase 8.
