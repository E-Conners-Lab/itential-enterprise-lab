# itential-enterprise-lab

An enterprise services layer around a realistic multi-vendor lab network, built
so that [Itential Platform](https://www.itential.com/) can automate it end to
end. Everything is code: OpenTofu for Proxmox, the EVE-NG REST API for the
network topology, Helm/Kustomize for k3s, Ansible for guests, and NetBox as the
network source of truth.

> **Status: Phase 1 — PID, image manifest, IP plan, resource budget.** No
> infrastructure has been changed by this repo yet. Phase 2 is the first phase
> that touches the hypervisor.

## Architecture (target)

| Layer | Where | What |
|---|---|---|
| Hypervisor | Dell R640, Proxmox VE 9.2 | 72 threads, 320 GB RAM, single 1.92 TB SAS SSD |
| Network devices | EVE-NG Pro VM | PA-VM (DC HA pair + per-branch), C8000v WAN edges, vEOS spine/leaf/access, Linux/Windows endpoints |
| OOB management | `vmbr1` on its own NIC, EVE-NG `pnet1` | Every service gets a leg on it |
| Services | Proxmox VMs + k3s | Itential Platform + IAG, DDI (Infoblox NIOS, BIND9/Kea), AD/DNS, TACACS+, Keycloak, Zabbix, Prometheus/Grafana, gNMIc, Loki, Oxidized, Vault, Gitea, Panorama (Proxmox VM, ADR 0006), Containerlab CI tier |
| Sources of truth | NetBox (network), this repo (project) | See ADR 0002 |

## Bootstrap from zero

1. Tooling on the workstation: `tofu`, `ansible`, `ansible-lint`, `yamllint`, `gitleaks`, `pre-commit`, `gh`, `jq`. On macOS: `brew install opentofu ansible ansible-lint yamllint gitleaks pre-commit gh jq`.
2. `make bootstrap` (installs the pre-commit hooks and checks the tools).
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
| 1 | `phase-1/pid` | PID, image manifest, IP plan, resource budget, ADRs, issues | in review |
| 2 | `phase-2/oob-network` | OOB network (`vmbr1`, `pnet1`, `oob-gw`), Proxmox API token, image staging, NetBox seeded | planned |
| 3 | `phase-3/platform` | 3-node k3s: Cilium, MetalLB, Longhorn, cert-manager, CloudNativePG | planned |
| 4 | `phase-4/network-topology` | EVE-NG DC + 2 branches (PA-VM, C8000v, vEOS, endpoints) from `topology/` | planned |
| 5 | `phase-5/itential` | Itential Platform + Automation Gateway, ServiceNow PDI adapter | planned |
| 6 | `phase-6/ddi` | Infoblox NIOS primary, BIND9 + Kea secondary, zone generated from NetBox | planned |
| 7 | `phase-7/identity` | Windows Server AD DS/DNS, tac_plus, Keycloak SSO | planned |
| 8 | `phase-8/observability` | Zabbix, Prometheus + Grafana, gNMIc, Loki | planned |
| 9 | `phase-9/config-secrets-code` | Oxidized, Vault, Gitea | planned |
| 10 | `phase-10/panorama` | Panorama, firewall onboarding | planned |
| 11 | `phase-11/containerlab` | Containerlab CI/test tier (cEOS mirror of the DC fabric) | planned |

## Repo layout

```
docs/        PID, discovery, IP plan, resource budget, image manifest, manual steps, ADRs
topology/    YAML that drives both EVE-NG and NetBox
images/      import scripts only (images themselves are never committed)
tofu/        Proxmox bridges, templates, VMs (bpg/proxmox)
k8s/         Helm values, Kustomize overlays
ansible/     guest OS and application configuration
eve/         EVE-NG REST API client and topology builder
verify/      one integration test per service; results/ holds committed evidence
```

## Working agreements

- `main` is protected: PR required, CI green, linear history. One branch and one PR per phase; the merged PR is the approval record.
- Conventional Commits. Secrets never enter the repo (`gitleaks` runs pre-commit and in CI).
- Decisions are ADRs in `docs/adr/`. Manual steps are listed in `docs/manual-steps.md` with the reason.
- Cost is a design input: `docs/resource-budget.md` tracks vCPU/RAM against a 1.5:1 CPU and 280 GB RAM ceiling.
