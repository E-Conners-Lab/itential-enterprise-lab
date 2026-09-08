# itential-enterprise-lab

An enterprise services layer around a realistic multi-vendor lab network, built
so that [Itential Platform](https://www.itential.com/) can automate it end to
end. Everything is code: OpenTofu for Proxmox, the EVE-NG REST API for the
network topology, Helm/Kustomize for k3s, Ansible for guests, and NetBox as the
network source of truth.

> **Status: Phase 6 — FlowAI agents and Platform coverage of the lab (in progress, branch
> `phase-6/flowai`; Phase 5 is draft PR #17).** Itential Platform 6.5.2, Gateway 5 (server plus a
> glibc runner for Genie/TextFSM, ADR 0038), the MCP server and an in-lab Ollama run as the
> `itential-dev-stack` containers on one Ubuntu VM (`itential.lab.internal`, ADR 0035). Everything on
> the platform is created through its API from documents in `itential/` by three plays
> (`itential.yml` -> `platform.yml` -> `flowai.yml`): ten generated workflows (`wf-*`, the only device
> write path is `wf-config-push-v1` behind a Work Center approval), Golden Config trees and a nightly
> compliance plan, MOP pre/post templates and nightly backups, the Lifecycle Manager service
> `branch-vlan` with its JSON form approval, NetBox and ServiceNow Integration Models as agent tools,
> and the agent fleet (`lab-netops`, `netbox-sot`, `device-ops`, `compliance`, `diagnostics`,
> `remediation`, each on Claude and on a local model). The three Ubuntu hosts are Gateway 5 inventory
> nodes; the four PA-VM firewalls wait on the Customer Support Portal download (`lab.firewalls`, ADR 0034).

## Architecture (target)

| Layer | Where | What |
|---|---|---|
| Hypervisor | Dell R640, Proxmox VE 9.2 | 72 threads, 320 GB RAM, single 1.92 TB SAS SSD |
| Network devices | EVE-NG Pro VM | PA-VM (DC HA pair + per-branch), C8000v WAN edges, vEOS spine/leaf/access, Linux/Windows endpoints |
| OOB management | `vmbr1` on its own NIC, EVE-NG `pnet1` | Every service gets a leg on it |
| Services | Proxmox VMs + k3s | Itential Platform + IAG, DDI (Infoblox NIOS, BIND9/Kea), AD/DNS, TACACS+, Keycloak, Zabbix, Prometheus/Grafana, gNMIc, Loki, Oxidized, Vault, Gitea, Panorama (Proxmox VM, ADR 0006), Containerlab CI tier |
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
| 5 | `phase-5/itential` | Itential Platform 6.5.2 + Gateway 5.5.2 as the dev-stack containers on VM 205 (ADR 0035), NetBox adapter, Inventory Manager from NetBox, generated workflows incl. `wf-branch-vlan-v1`, MCP for Claude Code; ServiceNow PDI adapter | draft PR #17 (verify 11/11; the 24 h memory check pending) |
| 6 | `phase-6/flowai` | FlowAI agents over the topology (S4c, ADR 0037/0038: Anthropic + in-lab Ollama profiles, `lab-netops`, Genie/TextFSM on a Gateway 5 runner) and Platform coverage of the lab (S4d, ADR 0039-0047: Configuration Manager through the InventoryBroker, Golden Config + nightly compliance, MOP templates + nightly backups, Lifecycle Manager `branch-vlan` + JSON form approval, NetBox/ServiceNow Integration Models, the five-agent fleet with local twins, Ubuntu hosts in Gateway 5) | in progress (S4c 7/7, S4d 1-6 built; draft PR after the full verify) |
| 7 | `phase-7/ddi` | Infoblox NIOS primary, BIND9 + Kea secondary, zone generated from NetBox | planned |
| 8 | `phase-8/identity` | Windows Server AD DS/DNS, tac_plus, Keycloak SSO | planned |
| 9 | `phase-9/observability` | Zabbix, Prometheus + Grafana, gNMIc, Loki | planned |
| 10 | `phase-10/config-secrets-code` | Oxidized, Vault, Gitea | planned |
| 11 | `phase-11/panorama` | Panorama, firewall onboarding | planned |
| 12 | `phase-12/containerlab` | Containerlab CI/test tier (cEOS mirror of the DC fabric) | planned |

## Repo layout

```
docs/        PID, discovery, IP plan, resource budget, image manifest, manual steps, ADRs
topology/    YAML that drives both EVE-NG and NetBox
images/      import scripts only (images themselves are never committed)
tofu/        Proxmox bridges, templates, VMs (bpg/proxmox)
k8s/         Helm values, Kustomize overlays
ansible/     guest OS and application configuration (itential.yml, platform.yml, flowai.yml build the platform from itential/)
eve/         EVE-NG REST API client and topology builder
itential/    the platform as documents: versions.yaml (oracle), workflows/ (build.py -> wf-*.json), golden-config/,
             command-templates/, lcm/, forms/, integrations/, agents/, the vendored dev-stack Compose files
verify/      one integration test per service; results/ holds committed evidence
```

## Working agreements

- `main` is protected: PR required, CI green, linear history. One branch and one PR per phase; the merged PR is the approval record.
- Conventional Commits. Secrets never enter the repo (`gitleaks` runs pre-commit and in CI).
- Decisions are ADRs in `docs/adr/`. Manual steps are listed in `docs/manual-steps.md` with the reason.
- Cost is a design input: `docs/resource-budget.md` tracks vCPU/RAM against a 1.5:1 CPU and 280 GB RAM ceiling.
