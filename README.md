# itential-enterprise-lab

An enterprise services layer around a realistic multi-vendor lab network, built
so that [Itential Platform](https://www.itential.com/) can automate it end to
end. Everything is code: OpenTofu for Proxmox, the EVE-NG REST API for the
network topology, Helm/Kustomize for k3s, Ansible for guests, and NetBox as the
network source of truth.

> **Status: Phases 0-8 and Phase 9a are complete.** The lab runs a production Itential environment whose
> credentials live in HashiCorp Vault.
>
> `https://itential.lab.internal` is served by nginx on `iap-lb` in front of two Itential Platform 6.5.2 nodes,
> backed by a three-member MongoDB replica set (keyFile, SCRAM, TLS) and Redis replication with three Sentinels,
> with Gateway 5 on its own VM and the MCP server and OpenLDAP on a tools VM: Itential's **HA2** architecture at lab
> sizes (ADR 0053). The load-balancing policy is Active/Standby by design, because Gateway Manager accepts one
> connection per gateway cluster (ADR 0055 decision 9).
>
> **Vault (Phase 9a, ADR 0065).** Vault 2.0.4 runs on k3s behind `vault.lab.internal`. The device, NetBox and
> ServiceNow credentials are in Vault, and the Platform and Gateway 5 read them through their own built-in AppRole
> clients, read-only and bound to their hosts' addresses. Inventory nodes, the NetBox adapter and the integrations
> hold Vault references, not credentials. Vault is unsealed by hand after a restart, administered through a
> least-privilege login, snapshotted off-host, and watched by a `VaultSealed` alert.
>
> **Since Phase 8.**
> - The Platform reaches NetBox and ServiceNow through Integration Models built from OpenAPI, with no adapter task
>   or generic request left in any workflow (ADR 0054).
> - Observability follows the estate, with a test that fails a phase which adds a host without refreshing monitoring
>   (ADR 0057). The production MongoDB is backed up nightly and the verify restores the archive (ADR 0058).
> - The local agent twins run on an off-lab Ollama host with a pinned model (ADR 0060, 0061).
> - A Copilot prototyping tier (ADR 0063): a dev stack on its own VM with a Containerlab topology of two C8000v and
>   two vEOS-lab nodes, and a read-only account on production.
> - Every device task's result is checked and every failure ends the job with a reason (ADR 0066).
> - Workflows are named for what they do, and their Studio canvases are laid out top to bottom from their
>   transitions (ADR 0067).
> - A Cisco NX-OS asset pack with a NetBox-driven inventory, and an eleven-chapter runbook series (ADR 0056).
>
> **Next.** The rest of Phase 9 (Oxidized, Gitea) and a Batfish configuration-analysis twin on the Containerlab host.
> Phase 9b (Vault as the source, a smaller `.env`, Vault PKI) is deferred: `.env` stays the source and Vault a
> read-only copy of it (ADR 0065 amendment).

## Architecture

| Layer | Where | What |
|---|---|---|
| Hypervisor | Dell R640, Proxmox VE 9.2 | 72 threads (2x Xeon Gold 6154), 314 GB usable RAM, `local-lvm` thin pool 1.6 TB; ceilings and current allocation in `docs/resource-budget.md` |
| Network devices | EVE-NG Pro VM | C8000v WAN edges, vEOS spine/leaf/access, Linux/Windows endpoints; PA-VM deferred behind `lab.firewalls` (ADR 0034) |
| OOB management | `vmbr1` on its own NIC, EVE-NG `pnet1` | Every service gets a leg on it |
| Services | Proxmox VMs + k3s | Itential Platform in the HA2 shape (11 VMs: nginx, 2 Platform, 3 MongoDB, 3 Redis, Gateway 5, tools), Vault, Zabbix, Prometheus/Grafana, gNMIc, Loki, OpenLDAP; planned: Oxidized, Gitea, Keycloak, TACACS+, DDI (BIND9/Kea, Infoblox NIOS), Panorama |
| Prototyping | Proxmox VMs | The Copilot dev stack (`itential-dev`) and a Containerlab host (`clab`), never production (ADR 0063) |
| Local inference | Off-lab host | Ollama for the local agent twins (ADR 0060, 0061) |
| Sources of truth | NetBox (network), this repo (project), Vault (credentials) | See ADR 0002 and ADR 0065 |

## What is automated

The workflows are generated from `itential/workflows/build.py` and named in `itential/versions.yaml`:

| Workflow | What it does |
|---|---|
| Add Branch VLAN / Remove Branch VLAN | Reserve a VLAN in NetBox, approve it, configure the branch switch; the Lifecycle Manager `branch-vlan` actions |
| Push Configuration with Approval | The one governed write path: a Work Center approval, then the push, checked line by line |
| Get Device Software Version | `show version` on one device through Gateway 5 |
| Run Show Command on a Device / on All Devices | A show command parsed per vendor (Genie, TextFSM) |
| Run Nightly Compliance Check / Summarize Compliance Results | The Golden Config plan and a compact result for the agents |
| Back Up All Device Configs | Every Configuration Manager device backed up, nightly |
| List Devices from NetBox / Count Devices in NetBox | Inventory reads for the agents |

FlowAI agents in `itential/agents/` use them as tools: `lab-netops`, a five-agent fleet (NetBox source of truth,
device operations, compliance, diagnostics, gated remediation) and a local twin of each.

## Bootstrap from zero

1. Tooling on the workstation: `tofu`, `ansible`, `ansible-lint`, `yamllint`, `gitleaks`, `pre-commit`, `gh`, `jq`, `helm` and `helm@3`, `kubectl`, `cilium`. On macOS: `brew install opentofu ansible ansible-lint yamllint gitleaks pre-commit gh jq helm helm@3 kubernetes-cli cilium-cli`.
2. `make bootstrap` (checks the tools, creates `.venv`, installs the pinned Ansible collections and the pre-commit hooks).
3. Copy `.env.example` to `.env` and fill it in (never committed).
4. Stage vendor images at `/srv/images/` on the Proxmox host per `docs/image-manifest.md`.
5. `make up` builds the lab in phase order. `make verify` runs every test in `verify/` and commits the results.
6. Production Vault: `make vault`, then the owner's steps in `docs/manual-steps.md` (initialise, unseal, the administrator login), then `make vault-config` and `make vault-cutover`. The runbooks in [`docs/runbooks/`](docs/runbooks/) walk through each track.

## Verification

`make verify` runs `verify/run.sh`, which executes every `verify/test-*.sh` against production and writes a
timestamped log to `verify/results/`. A service is only "verified" when its integration test has passed and the
result is committed. The dev tier has its own `make verify-dev`. `make prod-snapshot` fingerprints production
(GET-only) before and after any change, and `make tokens` reports the week's model spend.

## Phase and service status

Phases are delivered one PR each in the order fixed by ADR 0008. Service specs and acceptance criteria are in
`docs/PID.md`.

| Phase | Branch | Delivers | Status |
|---|---|---|---|
| 0 | `phase-0/discovery` | Repo scaffold, CI, read-only discovery | merged (PR #1) |
| 1 | `phase-1/pid` | PID, image manifest, IP plan, resource budget, ADRs, issues | merged (PR #13) |
| 2 | `phase-2/oob-network` | OOB network (`vmbr1`, `pnet1`, `oob-gw`), Proxmox API token, image staging, NetBox seeded | merged (PR #14) |
| 3 | `phase-3/platform` | 3-node k3s: Cilium, MetalLB, Longhorn, cert-manager + lab CA, CloudNativePG + Garage (lab databases rebuildable, ADR 0064) | merged (PR #15) |
| 4 | `phase-4/network-topology` | EVE-NG DC + 2 branches (C8000v, vEOS, endpoints; PA-VM deferred behind `lab.firewalls`) from `topology/` | merged (PR #16) |
| 5 | `phase-5/itential` | Itential Platform 6.5.2 + Gateway 5.5.2, Inventory Manager from NetBox, generated workflows, MCP for Claude Code, ServiceNow PDI | merged (PR #17) |
| 6 | `phase-6/flowai` | FlowAI agents (Anthropic and local profiles, `lab-netops`, Genie/TextFSM on a Gateway 5 runner) and Platform coverage of the lab: Configuration Manager, Golden Config + nightly compliance, MOP + nightly backups, Lifecycle Manager `branch-vlan` + JSON form approval, NetBox/ServiceNow Integration Models, the five-agent fleet with local twins | merged (PR #19, #20, #27) |
| 7 | `phase-7/observability` | Zabbix, kube-prometheus-stack + SNMP/blackbox exporters, gNMIc (vEOS), Loki + Alloy syslog, the official Itential dashboard (ADR 0051, 0052) | merged (PR #22, #23, #29) |
| 8 | `phase-8/platform-ha2` | Production Itential environment in Itential's HA2 shape (ADR 0053): 11 VMs with TLS and auth between every component and OpenLDAP as the directory; phases 5-7 replayed onto it (ADR 0055); nightly MongoDB backups (ADR 0058) | merged (PR #24, #30) |
| 9a | `phase-9a/vault` | Vault on k3s; the Platform and Gateway 5 read the device, NetBox and ServiceNow credentials through their built-in clients (ADR 0065) | done (PR #64, #65 and the cut-over) |
| 9b | - | Vault as the source of the credentials, `.env` reduced, certificates from Vault PKI | deferred (ADR 0065 amendment) |
| 9 | `phase-9/config-secrets-code` | Oxidized, Gitea | planned |
| 10 | `phase-10/identity` | Keycloak SSO (Grafana, Gitea), tac_plus; no Windows (ADR 0050) | planned |
| 11 | `phase-11/ddi` | BIND9 + Kea from NetBox (NIOS joins in the firewall track) | planned |
| 12 | `phase-12/containerlab` | Containerlab CI/test tier (cEOS mirror of the DC fabric) | planned |
| 13 | `phase-13/firewall-track` | NIOS grid master, the PA-VM firewalls (`lab.firewalls`), Panorama: once the images exist (ADR 0050) | planned |
| dev | - | The Copilot prototyping tier (PID S12, ADR 0063): `itential-dev` with its own dev Vault, a view-only NetBox token and the local model only; Containerlab topology `dev` (2 C8000v + 2 vEOS-lab); `svc-copilot` read-only on production. Verified by `make verify-dev` | running (PR #41-#57) |

## What is running

Every name resolves through the lab's own resolver on `10.100.0.1`; run
`sudo scripts/workstation-route.sh` once per workstation for the route and the resolver, and trust
`docs/lab-root-ca.crt` (manual step 6c) or every page reads *Not Secure*.

| Service | Address | Notes |
|---|---|---|
| Itential Platform | `https://itential.lab.internal` | nginx on `iap-lb`, in front of the Platform nodes |
| Vault | `https://vault.lab.internal:8200` | the lab's credentials; `make vault-status`, and the owner unseals it after a restart |
| MCP server | `http://mcp.lab.internal:8000/mcp` | on the tools VM; `.mcp.json` points Claude Code at it |
| Dev stack (Copilot) | `https://itential-dev.lab.internal`, `http://mcp-dev.lab.internal:8000/mcp` | the sandbox of ADR 0063; nothing here is production |
| NetBox | `http://netbox.lab.internal:8080` | the network source of truth |
| Grafana | `https://grafana.lab.internal` | lab dashboards and the official Itential Platform Monitoring dashboard |
| Zabbix | `https://zabbix.lab.internal` | availability: SNMPv3 devices, agent 2 on every Ubuntu machine, HTTP checks |
| Prometheus / Alertmanager | `https://prometheus.lab.internal`, `https://alertmanager.lab.internal` | metrics and the lab alert rules |
| Loki | `https://loki.lab.internal` | device and VM syslog through Alloy |
| gNMIc | `https://gnmic.lab.internal/metrics` | streaming telemetry from the vEOS fabric |
| EVE-NG | `https://eve.lab.internal` | presents its own certificate, so the browser warns |

## Repo layout

```
docs/        PID, discovery, IP plan, resource budget, image manifest, manual steps, lessons learned, ADRs
topology/    YAML that drives both EVE-NG and NetBox
images/      import scripts only (images themselves are never committed)
tofu/        Proxmox bridges, templates, VMs (bpg/proxmox); platform-ha2/ drives the production environment from itential/ha2/versions.yaml
k8s/         Helm values and manifests: platform, observability, vault
ansible/     guest OS and application configuration (itential.yml, platform.yml, flowai.yml build the platform from itential/;
             vault*.yml build and configure Vault)
eve/         EVE-NG REST API client and topology builder
clab/        the dev tier's Containerlab topology and device templates
itential/    the platform as documents: versions.yaml (oracle), workflows/ (build.py -> one <name>.json per workflow), golden-config/,
             command-templates/, lcm/, forms/, integrations/, agents/, vault/ (the dev tier's Vault), the vendored dev-stack Compose
             files, ha2/ (the production environment's own oracle and its Compose templates)
observability/  Zabbix templates and expiries, Grafana dashboards, gNMIc and the Platform exporter
scripts/     workstation helpers, including scripts/vault-prod.sh for the owner's Vault operations
verify/      one integration test per service; results/ holds committed evidence
docs/runbooks/  how to build the lab, one chapter per track, parameterised so no real address is transcribed
```

## Working agreements

- `main` is protected: PR required, CI green, linear history. One branch and one PR per phase; the merged PR is the approval record.
- Conventional Commits. Secrets never enter the repo (`gitleaks` runs pre-commit and in CI).
- Decisions are ADRs in `docs/adr/`. Manual steps are listed in `docs/manual-steps.md` with the reason.
- Problems met while building the lab, with their causes and fixes, are in `docs/lessons-learned.md`.
- Cost is a design input: `docs/resource-budget.md` tracks vCPU/RAM against a 1.5:1 CPU and 300 GB RAM ceiling (ADR 0063).
