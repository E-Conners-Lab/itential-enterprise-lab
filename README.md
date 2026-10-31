# itential-enterprise-lab

An enterprise services layer around a realistic multi-vendor lab network, built
so that [Itential Platform](https://www.itential.com/) can automate it end to
end. Everything is code: OpenTofu for Proxmox, the EVE-NG REST API for the
network topology, Helm/Kustomize for k3s, Ansible for guests, and NetBox as the
network source of truth.

> **Status: Phases 0-8 and Phase 9a are complete.** Production is Itential's HA2 architecture at lab sizes
> (ADR 0053): Itential Platform 6.6.1 and Gateway 5.5.3 (ADR 0078) behind nginx, a three-member MongoDB replica set
> and Redis with Sentinels. The second Platform node is the Active/Standby standby and stays stopped (ADR 0055,
> 0078). Credentials live in Vault, and the Platform and Gateway read them through their built-in clients (ADR 0065).

## What it does

- **Governed change:** branch VLANs reserved in NetBox, configuration pushes behind a Work Center approval (one
  variant also under a revert timer), nightly compliance checks and backups.
- **AWS site-to-site VPN on demand** (ADR 0068, 0070): Terraform on the Gateway runs only an approved plan, Batfish
  proves the router change before anyone approves it (ADR 0076), and drift checks, key rotation and a timed
  teardown keep it honest.
- **Outage loops** (ADR 0072, 0073, 0075): a tunnel-down or BGP-session-down alert opens an incident, an agent
  diagnoses it and proposes one fix, a person approves, and Itential checks the result independently.
- **Agents:** FlowAI agents use the workflows as tools: `lab-netops`, a five-agent fleet (source of truth, device
  operations, compliance, diagnostics, gated remediation), `cloud-status`, `tunnel-diagnostics`,
  `fabric-diagnostics`, `platform-guide` and `cloud-concierge`, each with a local twin (ADR 0046) on qwen3.8 (ADR 0079).
  The diagnostics agents search a knowledge service through FlowMCP Gateway (ADR 0074); the Cloud Concierge starts
  Deploy and Tear Down AWS VPN runs that wait on their own Work Center cards (ADR 0085).
- **The Platform explains itself:** Describe Workflow and Describe Job (ADR 0080), the `platform-guide` agent with
  Ask Platform Guide (ADR 0081), and NetClaw as the lab's Slack interface (ADR 0082).

Every workflow is generated from `itential/workflows/build.py`, named in `itential/versions.yaml`, and described
step by step in the [workflow guide](docs/workflows/README.md).

## Architecture

| Layer | Where | What |
|---|---|---|
| Hypervisor | Dell R640, Proxmox VE 9.2 | 72 threads (2x Xeon Gold 6154), 314 GB usable RAM, `local-lvm` thin pool 1.6 TB; ceilings and current allocation in `docs/resource-budget.md` |
| Network devices | EVE-NG Pro VM | C8000v WAN edges, vEOS spine/leaf/access, Linux/Windows endpoints; PA-VM deferred behind `lab.firewalls` (ADR 0034) |
| OOB management | `vmbr1` on its own NIC, EVE-NG `pnet1` | Every service gets a leg on it |
| Services | Proxmox VMs + k3s | Itential Platform in the HA2 shape (11 VMs), Vault, Prometheus/Grafana, gNMIc, Loki, OpenLDAP, the knowledge service, Batfish; planned: Oxidized, Gitea, Keycloak, TACACS+, DDI (BIND9/Kea, Infoblox NIOS), Panorama |
| Prototyping | Proxmox VMs | The Copilot dev stack (`itential-dev`) and a Containerlab host (`clab`), never production (ADR 0063) |
| Off-lab host | Mac Mini | Ollama for the local twins (ADR 0060, 0079) and NetClaw's container (ADR 0082) |
| Sources of truth | NetBox (network), this repo (project), Vault (credentials) | See ADR 0002 and ADR 0065 |

## Bootstrap from zero

1. Tooling on the workstation: `tofu`, `ansible`, `ansible-lint`, `yamllint`, `gitleaks`, `pre-commit`, `gh`, `jq`, `helm` and `helm@3`, `kubectl`, `cilium`. On macOS: `brew install opentofu ansible ansible-lint yamllint gitleaks pre-commit gh jq helm helm@3 kubernetes-cli cilium-cli`.
2. `make bootstrap` (checks the tools, creates `.venv`, installs the pinned Ansible collections and the pre-commit hooks).
3. Copy `.env.example` to `.env` and fill it in (never committed).
4. Stage vendor images at `/srv/images/` on the Proxmox host per `docs/image-manifest.md`.
5. `make up` builds the lab in phase order. `make verify` runs every test in `verify/` and writes the results to `verify/results/`.
6. Production Vault: `make vault`, then the owner's steps in `docs/manual-steps.md` (initialise, unseal, the administrator login), then `make vault-config` and `make vault-cutover`. The runbooks in [`docs/runbooks/`](docs/runbooks/) walk through each track.

## Verification

`make verify` runs every `verify/test-*.sh` against production and writes a timestamped log to `verify/results/`;
a service is only "verified" once its test has passed and that log is committed. `make verify-dev` covers the dev
tier, and `make prod-snapshot` fingerprints production (GET-only) before and after any change.

## Phase and service status

Phases are delivered in the order fixed by ADR 0008. Service specs and acceptance criteria are in
`docs/PID.md`.

| Phase | Branch | Delivers | Status |
|---|---|---|---|
| 0 | `phase-0/discovery` | Repo scaffold, CI, read-only discovery | merged (PR #1) |
| 1 | `phase-1/pid` | PID, image manifest, IP plan, resource budget, ADRs, issues | merged (PR #13) |
| 2 | `phase-2/oob-network` | OOB network (`vmbr1`, `pnet1`, `oob-gw`), Proxmox API token, image staging, NetBox seeded | merged (PR #14) |
| 3 | `phase-3/platform` | 3-node k3s: Cilium, MetalLB, Longhorn, cert-manager + lab CA, CloudNativePG + Garage (ADR 0064) | merged (PR #15) |
| 4 | `phase-4/network-topology` | EVE-NG DC + 2 branches (C8000v, vEOS, endpoints) from `topology/` | merged (PR #16) |
| 5 | `phase-5/itential` | Itential Platform + Gateway 5, Inventory Manager from NetBox, generated workflows, MCP for Claude Code, ServiceNow PDI | merged (PR #17) |
| 6 | `phase-6/flowai` | FlowAI agents and the Platform applications: Configuration Manager, Golden Config, MOP, Lifecycle Manager, Integration Models | merged (PR #19, #20, #27) |
| 7 | `phase-7/observability` | kube-prometheus-stack, SNMP/blackbox exporters, gNMIc, Loki + Alloy, the Itential dashboard (Zabbix removed, ADR 0071) | merged (PR #22, #23, #29) |
| 8 | `phase-8/platform-ha2` | Production in Itential's HA2 shape (ADR 0053), phases 5-7 replayed onto it (ADR 0055), nightly MongoDB backups | merged (PR #24, #30) |
| 9a | `phase-9a/vault` | Vault on k3s; the Platform and Gateway 5 read their credentials through their built-in clients (ADR 0065) | done (PR #64, #65 and the cut-over) |
| 9b | - | Vault as the source of the credentials, `.env` reduced, certificates from Vault PKI | deferred (ADR 0065 amendment) |
| 9 | `phase-9/config-secrets-code` | Oxidized, Gitea | planned |
| 10 | `phase-10/identity` | Keycloak SSO (Grafana, Gitea), tac_plus; no Windows (ADR 0050) | planned |
| 11 | `phase-11/ddi` | BIND9 + Kea from NetBox (NIOS joins in the firewall track) | planned |
| 12 | `phase-12/containerlab` | Containerlab CI/test tier (cEOS mirror of the DC fabric) | planned |
| 13 | `phase-13/firewall-track` | NIOS grid master, the PA-VM firewalls (`lab.firewalls`), Panorama: once the images exist (ADR 0050) | planned |
| aws-vpn | `phase-aws-vpn/*` | The AWS site-to-site VPN run by Itential on production: deploy, hand off with the Batfish gate, verify, drift check, key rotation, timed teardown (ADR 0068, 0070, 0076) | on production since PR #112 (built from #71); the tunnel exists only between Deploy and Tear Down |
| outage loops | - | AWS tunnel and fabric BGP outage loops with the knowledge service (ADR 0072-0075) | running (PR #122-#145) |
| upgrade | - | Platform 6.6.1 and Gateway 5.5.3, with the upgrade runbook (ADR 0078) | done (PR #151; 6.6.1 in #211) |
| explain | - | Describe Workflow and Describe Job, the workflow guide, `platform-guide` and Ask Platform Guide (ADR 0080, 0081) | running (PR #162-#177) |
| netclaw | - | NetClaw, the lab's Slack interface (ADR 0082) | built (PR #178) |
| dev | - | The Copilot prototyping tier (ADR 0063): `itential-dev` with its own Vault and the local model, Containerlab topology `dev`, `svc-copilot` read-only on production; `make verify-dev` | running (PR #41-#57) |

## What is running

Every name resolves through the lab's own resolver on `10.100.0.1`; run
`sudo scripts/workstation-route.sh` once per workstation for the route and the resolver, and trust
`docs/lab-root-ca.crt` (manual step 6c) or every page reads *Not Secure*.

| Service | Address | Notes |
|---|---|---|
| Itential Platform | `https://itential.lab.internal` | nginx on `iap-lb`, in front of the Platform nodes |
| Vault | `https://vault.lab.internal:8200` | the lab's credentials; `make vault-status`, and the owner unseals it after a restart |
| MCP server | `https://mcp.lab.internal:8000/mcp` | on the tools VM; `.mcp.json` points Claude Code at it, with a Vault-signed token per machine (ADR 0083) |
| Dev stack (Copilot) | `https://itential-dev.lab.internal`, `http://mcp-dev.lab.internal:8000/mcp` | the sandbox of ADR 0063; nothing here is production |
| NetBox | `http://netbox.lab.internal:8080` | the network source of truth |
| Grafana | `https://grafana.lab.internal` | lab dashboards and the official Itential Platform Monitoring dashboard |
| Prometheus / Alertmanager | `https://prometheus.lab.internal`, `https://alertmanager.lab.internal` | metrics and the lab alert rules |
| Loki | `https://loki.lab.internal` | device and VM syslog through Alloy |
| gNMIc | `https://gnmic.lab.internal/metrics` | streaming telemetry from the vEOS fabric |
| EVE-NG | `https://eve.lab.internal` | presents its own certificate, so the browser warns |

## Repo layout

```
docs/           PID, ADRs, IP plan, resource budget, image manifest, manual steps, lessons learned
docs/runbooks/  how to build the lab, one chapter per track, parameterised so no real address is transcribed
docs/workflows/ one generated page per workflow
topology/       YAML that drives both EVE-NG and NetBox
images/         import scripts only (images themselves are never committed)
tofu/           Proxmox bridges, templates, VMs (bpg/proxmox); platform-ha2/ drives production
k8s/            Helm values and manifests: platform, observability, vault
ansible/        guest OS and application configuration; playbooks/ holds itential.yml, platform.yml, flowai.yml,
                vault*.yml and netops-knowledge.yml (the knowledge service on k3s)
eve/            EVE-NG REST API client and topology builder
clab/           the dev tier's Containerlab topology and device templates
itential/       the platform as documents: versions.yaml (the oracle), workflows/, golden-config/, command-templates/,
                lcm/, forms/, integrations/, agents/, vault/, ha2/ (production's own oracle and Compose templates)
observability/  expiry rules, Grafana dashboards, gNMIc, the Platform exporter, the alert relay
servicenow/     the stock ServiceNow PDI records the workflows rely on
scripts/        workstation helpers, including scripts/vault-prod.sh for the owner's Vault operations
tests/          pytest checks that keep the documents, the oracle and the generated files in step
verify/         one integration test per service; results/ holds committed evidence
```

## Working agreements

- `main` is protected: PR required, CI green, linear history. The merged PR is the approval record.
- Conventional Commits. Secrets never enter the repo (`gitleaks` runs pre-commit and in CI).
- Decisions are ADRs in `docs/adr/`. Manual steps are listed in `docs/manual-steps.md` with the reason.
- Problems met while building the lab, with their causes and fixes, are in `docs/lessons-learned.md`.
- Cost is a design input: `docs/resource-budget.md` tracks vCPU/RAM against a 1.5:1 CPU and 300 GB RAM ceiling (ADR 0063).
