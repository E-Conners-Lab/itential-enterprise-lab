# 0008 — Delivery phases and their order

- **Status:** accepted
- **Date:** 2026-09-06

## Context

The kickoff lists nine services and requires a k3s platform phase before
observability. Several services (Keycloak, tac_plus, Vault, Gitea, the whole
observability stack) are Helm/Kustomize workloads and therefore need the
cluster first. Itential can only be *verified* against a network, so the
EVE-NG topology must exist before the Itential phase is testable. DNS should
exist before most services, but Infoblox's evaluation clock argues for
deploying it as late as practical (see ADR 0009).

## Decision

One branch and one PR per phase, in this order:

| # | Branch | Delivers |
|---|---|---|
| 0 | `phase-0/discovery` | done |
| 1 | `phase-1/pid` | this document set |
| 2 | `phase-2/oob-network` | `oob-gw`, EVE-NG `net1`/`pnet1`, Proxmox API token, `snippets`, `/srv/images` LV, cloud-init template, NetBox seeded with the IP plan, NetBox OOB leg + guest agent + backup, EVE password rotation |
| 3 | `phase-3/platform` | 3-node k3s, Cilium, MetalLB, Longhorn, cert-manager + lab CA, CloudNativePG, ingress |
| 4 | `phase-4/network-topology` | EVE-NG DC + 2 branches from `topology/`, NetBox devices/interfaces/IPs, baseline configs, routing up |
| 5 | `phase-5/itential` | Itential Platform + IAG, NetBox/ServiceNow/device adapters, first end-to-end workflow |
| 6 | `phase-6/ddi` | BIND9 secondary + Kea standby, Infoblox NIOS primary, zone generated from NetBox |
| 7 | `phase-7/identity` | Windows Server AD DS/DNS, tac_plus, Keycloak SSO for NetBox/Grafana/Gitea/Itential |
| 8 | `phase-8/observability` | Zabbix, kube-prometheus-stack, gNMIc, Loki + Alloy |
| 9 | `phase-9/config-secrets-code` | Oxidized, Vault (secrets migrate out of `.env`), Gitea |
| 10 | `phase-10/panorama` | Panorama, firewall onboarding, template/device-group push via Itential |
| 11 | `phase-11/containerlab` | Containerlab host, cEOS mirror of the DC fabric, CI job that pre-validates changes |

Until Phase 6, hosts use the `oob-gw` as a forwarding resolver and static
`/etc/hosts` entries generated from NetBox; Phase 6 replaces that with real
records. Until Phase 9, secrets live only in the gitignored `.env`.

## Consequences

- Two phases (3 and 4) are added to the kickoff's service list; the `Makefile`
  `PHASES` variable and README status table are updated to match.
- Every phase PR must include its verification test from the PID and the
  committed result in `verify/results/`.

## Amendment 2026-09-07 (ADR 0037)

Phase 6 becomes FlowAI agents; DDI, identity, observability, config/secrets/code,
Panorama and Containerlab shift to Phases 7-12 unchanged in content. Branch names of
the shifted phases keep their original numbers in `docs/PID.md` section 3 to preserve
history.
