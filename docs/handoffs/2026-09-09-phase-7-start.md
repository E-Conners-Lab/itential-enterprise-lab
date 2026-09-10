# Handoff — Phase 7 (observability) start, after the ADR 0050 reorder (2026-09-09)

Paste the block below into a new Claude Code session opened in this repo (the `itential-builder` plugin is
installed at user scope).

---

Continue the itential-enterprise-lab project (repo https://github.com/E-Conners-Lab/itential-enterprise-lab,
local ~/PycharmProjects/itential-enterprise-lab). Phases 0-6 and the S4e NetBox enrichment are merged on main
(PRs #17, #19, #20 on 2026-09-09). Branch `phase-7/observability` holds the reorder (ADR 0050, PID 1.15) as its
first commit: image-free phases first, Windows dropped. Read this file, then `docs/PID.md` (S7 with amendment 1.15,
Domain 7 with 1.14, section 3 plan), ADR 0049 and 0050, `docs/handoffs/2026-09-09-netbox-enrichment-close.md`
(the S4e state and warts), `itential/versions.yaml` (`llm.budget`), `verify/tokens.sh`, `docs/image-manifest.md`,
`docs/ip-plan.md` (VIPs .35-.39 for observability), `ansible/playbooks/k8s-platform.yml` (how Phase 3 deploys to
k3s), `verify/test-03-platform.sh` and `tests/`. Memory in
`~/.claude/projects/-Users-elliotconner-PycharmProjects-itential-enterprise-lab/memory/` holds every measured API
shape and the key budget rule.

## Rules (unchanged)
One branch and one PR per phase; the PR description is the phase report (built / verified / deferred). Conventional
Commits; secrets only in `.env`; every decision an ADR (next 0051); amend the PID (next 1.16) before code when the
design changes. TDD: red tests and a red verify check first. One element per commit with pytest, ansible-lint
(140-character lines) and the verify log. Everything created through APIs from documents in the repo, idempotent.
Device writes only through `wf-config-push-v1` with a Work Center approval; ask before anything destructive or
touching vmbr0. Post a detailed change summary and wait for Elliot's verbal "approve" before merging. Never
fabricate a "done". `verify/devcmd.py` is the independent second source. **Budget (ADR 0049): the Anthropic key is
the company key, $15 a week; run `make tokens` before any agent verify, iterate on `ONLY=` subsets or the `-local`
twins, one full Anthropic verify per PR.** Check disk space before building images; `kubectl get pods -A` after
k3s changes (kubeconfig `~/.kube/lab-k3s.yaml`).

## Phase 7 — Observability (S7), what to build
Zabbix server + web (CNPG backend; SNMPv3 templates for IOS XE and EOS, the PA-VM template waits for the firewall
track; agent on every Ubuntu VM; HTTP checks for every UI; an "Expiries" host with one item per manifest expiry,
trigger at 14 days), kube-prometheus-stack (Prometheus, Alertmanager, Grafana, node-exporters, SNMP and blackbox
exporters), gNMIc against the vEOS fabric, Loki for syslog from both vendors; VIPs 10.100.0.35-.39 from the IP plan;
Longhorn storage; lab-CA certificates from cert-manager. Also the Itential job metrics deferred from S4d (the
Platform exposes job/task metrics; the MCP tools `get_job_metrics*` show the shapes). Acceptance S7.1-4 and S7.6 as
written; S7.5 (Grafana via Keycloak) moves to phase 9. Verify `verify/test-07-observability.sh`; design first:
ADR 0051 (chart versions, exporters, what Zabbix vs Prometheus owns) and PID 1.16 if the design changes.

## Then
Phase 8 config/secrets/code (Vault takes the device credentials and the Anthropic key), 9 identity without Windows
(OpenLDAP + Keycloak + tac_plus), 10 DDI on BIND9 + Kea, 11 Containerlab (cEOS-lab after registering), 12 the
firewall track once NIOS, PA-VM 11.1 and Panorama exist.

## Open items carried
- `docs/ip-plan.md` section 6 regeneration from `topology/enterprise.yaml` (ADR 0048 consequence).
- The Gateway 5 runner's `NETBOX_URL` is the home-LAN value (works via oob-gw); the OOB address would be tidier.
- NetBox still holds the `dc01` reservation (10.100.0.69) the seed created before ADR 0050 released it: delete it
  by hand or with a small task in `netbox-seed.yml`.
- The seed play flips device status planned->active on every run; `wf-show-all-v1` caps parsed results at 1500
  characters (verifies fall back to `wf-show-command-v1`).
