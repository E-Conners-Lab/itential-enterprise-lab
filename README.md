# itential-enterprise-lab

An enterprise services layer around a realistic multi-vendor lab network, built
so that [Itential Platform](https://www.itential.com/) can automate it end to
end. Everything is code: OpenTofu for Proxmox, the EVE-NG REST API for the
network topology, Helm/Kustomize for k3s, Ansible for guests, and NetBox as the
network source of truth.

> **Status: Phase 8 complete — the lab runs a production Itential environment.** Phases 0-8 are merged.
>
> `https://itential.lab.internal` is served by nginx on `iap-lb` in front of two Itential Platform 6.5.2
> nodes, backed by a three-member MongoDB replica set (keyFile, SCRAM, TLS) and Redis replication with three
> Sentinels, with Gateway 5 on its own VM and the MCP server, Ollama and OpenLDAP on a tools VM — Itential's
> **HA2** architecture at lab sizes (ADR 0053). Everything phases 5-7 built was replayed onto it and verified
> there, the service name was cut over, and the single-VM dev stack was deleted (ADR 0055).
>
> The load-balancing policy is **Active/Standby**, not active/active, and deliberately so: Gateway Manager
> accepts one connection per gateway cluster, and only the Platform node holding it can reach a device, so the
> standby is built, attached to the same databases and parked until a failover. The measurement and the trade
> are in ADR 0055 decision 9.
>
> **Since Phase 8 closed.** The nine-chapter runbook series in [`docs/runbooks/`](docs/runbooks/) (ADR 0056);
> NetBox and ServiceNow moved off their npm adapters onto Integration Models built from OpenAPI, leaving no
> adapter task or generic request in any workflow (ADR 0054); observability was made to follow the estate
> after Phase 8's eleven VMs had left it behind, with a test that fails any later phase which adds a host
> without refreshing monitoring (ADR 0057); and the production MongoDB is backed up nightly with the verify
> **restoring** the archive rather than asserting a filename (ADR 0058). Every verification script in
> `verify/` is green.
>
> Next: testing Itential marketplace integrations and assets on this environment — rollback now covers both
> halves, the repo's own assets by `git revert` plus a replay and the database by the nightly dump.

## Architecture (target)

| Layer | Where | What |
|---|---|---|
| Hypervisor | Dell R640, Proxmox VE 9.2 | 72 threads (2x Xeon Gold 6154), 314 GB usable RAM, `local-lvm` thin pool 1.6 TB; ceilings and current allocation in `docs/resource-budget.md` |
| Network devices | EVE-NG Pro VM | PA-VM (DC HA pair + per-branch), C8000v WAN edges, vEOS spine/leaf/access, Linux/Windows endpoints |
| OOB management | `vmbr1` on its own NIC, EVE-NG `pnet1` | Every service gets a leg on it |
| Services | Proxmox VMs + k3s | Itential Platform in the HA2 shape (11 VMs: nginx, 2 Platform, 3 MongoDB, 3 Redis, Gateway 5, tools), DDI (Infoblox NIOS, BIND9/Kea), OpenLDAP, TACACS+, Keycloak, Zabbix, Prometheus/Grafana, gNMIc, Loki, Oxidized, Vault, Gitea, Panorama (Proxmox VM, ADR 0006), Containerlab CI tier |
| Sources of truth | NetBox (network), this repo (project) | See ADR 0002 |

## Bootstrap from zero

1. Tooling on the workstation: `tofu`, `ansible`, `ansible-lint`, `yamllint`, `gitleaks`, `pre-commit`, `gh`, `jq`, `helm` and `helm@3`, `kubectl`, `cilium`. On macOS: `brew install opentofu ansible ansible-lint yamllint gitleaks pre-commit gh jq helm helm@3 kubernetes-cli cilium-cli`.
2. `make bootstrap` (checks the tools, creates `.venv`, installs the pinned Ansible collections and the pre-commit hooks).
3. Copy `.env.example` to `.env` and fill it in (never committed).
4. Stage vendor images at `/srv/images/` on the Proxmox host per `docs/image-manifest.md`.
5. `make up` builds the lab in phase order. `make verify` runs every test in `verify/` and commits the results.

## Verification

`make verify` runs `verify/run.sh`, which executes every `verify/test-*.sh` and
writes a timestamped log to `verify/results/`. A service is only "verified" when
its integration test has passed and the result is committed.

## Phase and service status

Phases are delivered one PR each in the order fixed by ADR 0008. A phase is
"verified" only when its test in `verify/` has passed and the result is
committed. Service specs and acceptance criteria are in `docs/PID.md`.

| Phase | Branch | Delivers | Status |
|---|---|---|---|
| 0 | `phase-0/discovery` | Repo scaffold, CI, read-only discovery | merged (PR #1) |
| 1 | `phase-1/pid` | PID, image manifest, IP plan, resource budget, ADRs, issues | merged (PR #13) |
| 2 | `phase-2/oob-network` | OOB network (`vmbr1`, `pnet1`, `oob-gw`), Proxmox API token, image staging, NetBox seeded | merged (PR #14) |
| 3 | `phase-3/platform` | 3-node k3s: Cilium, MetalLB, Longhorn, cert-manager + lab CA, CloudNativePG + Garage backups | merged (PR #15) |
| 4 | `phase-4/network-topology` | EVE-NG DC + 2 branches (C8000v, vEOS, endpoints; PA-VM deferred behind `lab.firewalls`) from `topology/` | merged (PR #16) |
| 5 | `phase-5/itential` | Itential Platform 6.5.2 + Gateway 5.5.2 as the dev-stack containers on VM 205 (ADR 0035), NetBox adapter, Inventory Manager from NetBox, generated workflows incl. `wf-branch-vlan-v1`, MCP for Claude Code; ServiceNow PDI adapter | merged (PR #17) |
| 6 | `phase-6/flowai` | FlowAI agents over the topology (S4c, ADR 0037/0038: Anthropic + in-lab Ollama profiles, `lab-netops`, Genie/TextFSM on a Gateway 5 runner) and Platform coverage of the lab (S4d, ADR 0039-0047: Configuration Manager through the InventoryBroker, Golden Config + nightly compliance, MOP templates + nightly backups, Lifecycle Manager `branch-vlan` + JSON form approval, NetBox/ServiceNow Integration Models, the five-agent fleet with local twins, Ubuntu hosts in Gateway 5) | merged (PR #19; S4e NetBox enrichment PR #20) |
| 7 | `phase-7/observability` | Zabbix (CNPG), kube-prometheus-stack + SNMP/blackbox exporters, gNMIc (vEOS), Loki + Alloy syslog, Platform metrics; VIPs .35-.39 behind Traefik (ADR 0051); the official Itential dashboard (ADR 0052) | merged (PR #22, #23) |
| 8 | `phase-8/platform-ha2` | Production Itential environment in Itential's HA2 shape at lab sizes (ADR 0053): 11 VMs — 2 Platform nodes behind nginx, a MongoDB replica set, Redis + Sentinel, Gateway 5 and a tools VM — with TLS and auth between every component and OpenLDAP as the directory. Everything phases 5-7 built replayed onto it from shared task files and verified there (ADR 0055), `itential.lab.internal` cut over to the load balancer, VM 205 deleted and 10.100.0.65 released | merged (PR #24) |
| 9 | `phase-9/config-secrets-code` | Oxidized, Vault (takes the device, database and Sentinel credentials and the company key), Gitea | planned |
| 10 | `phase-10/identity` | OpenLDAP, Keycloak SSO (Grafana, Gitea), tac_plus; no Windows (ADR 0050) | planned |
| 11 | `phase-11/ddi` | BIND9 + Kea from NetBox (NIOS joins in the firewall track) | planned |
| 12 | `phase-12/containerlab` | Containerlab CI/test tier (cEOS mirror of the DC fabric) | planned |
| 13 | `phase-13/firewall-track` | NIOS grid master, the PA-VM firewalls (`lab.firewalls`), Panorama: once the images exist (ADR 0050) | planned |

## What is running

Every name resolves through the lab's own resolver on `10.100.0.1`; run
`sudo scripts/workstation-route.sh` once per workstation for the route and the resolver, and trust
`docs/lab-root-ca.crt` (manual step 6c) or every page reads *Not Secure*.

| Service | Address | Notes |
|---|---|---|
| Itential Platform | `https://itential.lab.internal` | nginx on `iap-lb`, in front of the Platform nodes |
| MCP server | `http://mcp.lab.internal:8000/mcp` | on the tools VM; `.mcp.json` points Claude Code at it |
| NetBox | `http://netbox.lab.internal:8080` | the network source of truth |
| Grafana | `https://grafana.lab.internal` | lab dashboards and the official Itential Platform Monitoring dashboard |
| Zabbix | `https://zabbix.lab.internal` | availability: SNMPv3 devices, agent 2 on every Ubuntu machine, HTTP checks |
| Prometheus / Alertmanager | `https://prometheus.lab.internal`, `https://alertmanager.lab.internal` | metrics and the lab alert rules |
| Loki | `https://loki.lab.internal` | device and VM syslog through Alloy |
| gNMIc | `https://gnmic.lab.internal/metrics` | streaming telemetry from the vEOS fabric |
| EVE-NG | `https://eve.lab.internal` | presents its own certificate, so the browser warns |

## Repo layout

```
docs/        PID, discovery, IP plan, resource budget, image manifest, manual steps, ADRs
topology/    YAML that drives both EVE-NG and NetBox
images/      import scripts only (images themselves are never committed)
tofu/        Proxmox bridges, templates, VMs (bpg/proxmox); platform-ha2/ drives the production environment from itential/ha2/versions.yaml
k8s/         Helm values, Kustomize overlays
ansible/     guest OS and application configuration (itential.yml, platform.yml, flowai.yml build the platform from itential/)
eve/         EVE-NG REST API client and topology builder
itential/    the platform as documents: versions.yaml (oracle), workflows/ (build.py -> wf-*.json), golden-config/,
             command-templates/, lcm/, forms/, integrations/, agents/, the vendored dev-stack Compose files,
             ha2/ (the production environment's own oracle and its Compose templates)
observability/  Zabbix templates and expiries, Grafana dashboards, gNMIc and the Platform exporter (phase 7)
verify/      one integration test per service; results/ holds committed evidence
docs/runbooks/  how to build the lab, one chapter per track, parameterised so no real address is transcribed
```

## Working agreements

- `main` is protected: PR required, CI green, linear history. One branch and one PR per phase; the merged PR is the approval record.
- Conventional Commits. Secrets never enter the repo (`gitleaks` runs pre-commit and in CI).
- Decisions are ADRs in `docs/adr/`. Manual steps are listed in `docs/manual-steps.md` with the reason.
- Cost is a design input: `docs/resource-budget.md` tracks vCPU/RAM against a 1.5:1 CPU and 280 GB RAM ceiling.
