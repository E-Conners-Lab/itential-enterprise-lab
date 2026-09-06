# itential-enterprise-lab

An enterprise services layer around a realistic multi-vendor lab network, built
so that [Itential Platform](https://www.itential.com/) can automate it end to
end. Everything is code: OpenTofu for Proxmox, the EVE-NG REST API for the
network topology, Helm/Kustomize for k3s, Ansible for guests, and NetBox as the
network source of truth.

> **Status: Phase 0 — repo bootstrap + read-only discovery.** No infrastructure
> has been changed by this repo yet.

## Architecture (target)

| Layer | Where | What |
|---|---|---|
| Hypervisor | Dell R640, Proxmox VE 9.2 | 72 threads, 320 GB RAM, single 1.92 TB SAS SSD |
| Network devices | EVE-NG Pro VM | PA-VM (DC HA pair + per-branch), Panorama, C8000v WAN edges, vEOS spine/leaf/access, Linux/Windows endpoints |
| OOB management | `vmbr1` on its own NIC, EVE-NG `pnet1` | Every service gets a leg on it |
| Services | Proxmox VMs + k3s | Itential Platform + IAG, DDI, AD/DNS, TACACS+, Keycloak, Zabbix, Prometheus/Grafana, gNMIc, Loki, Oxidized, Vault, Gitea, Panorama, Containerlab CI tier |
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

## Service status

| # | Service | Status |
|---|---|---|
| 1 | Out-of-band management network (vmbr1, pnet1) | planned |
| 2 | Itential Platform + Automation Gateway | planned |
| 3 | ServiceNow PDI (adapter config only) | planned |
| 4 | DDI: Infoblox NIOS eval (fallback Kea + BIND9) | planned |
| 5 | Identity/AAA: Windows Server AD/DNS, tac_plus, Keycloak | planned |
| 6 | Observability: Zabbix, Prometheus + Grafana, gNMIc, Loki | planned |
| 7 | Config/secrets/code: Oxidized, Vault, Gitea | planned |
| 8 | Panorama | planned |
| 9 | Containerlab CI/test tier | planned |

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
