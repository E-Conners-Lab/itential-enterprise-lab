# Handoff prompt — Phase 6b: wire every Platform application to the EVE-NG lab

Paste the block below into a new Claude Code session opened in this repo (the
`itential-builder` plugin is installed at user scope and loads at session start).

---

Continue the itential-enterprise-lab project (repo https://github.com/E-Conners-Lab/itential-enterprise-lab,
local ~/PycharmProjects/itential-enterprise-lab, branch `phase-6/flowai`, last commit aed839f).
Read `docs/PID.md` (S4, S4b, S4c), ADR 0035-0039, `itential/versions.yaml`, `ansible/playbooks/itential.yml`,
`ansible/playbooks/flowai.yml`, `itential/workflows/build.py`, `itential/agents/*.yaml`,
`verify/test-06-flowai.sh` and `tests/test_parsers.py` before writing anything. Memory notes in
`~/.claude/projects/-Users-elliotconner-PycharmProjects-itential-enterprise-lab/memory/` hold the API lessons.

## Rules (unchanged)
- One branch and one PR per phase; the PR description is the phase report (built / verified / deferred).
  This work continues on `phase-6/flowai` (rebase onto main after PR #17 merges).
- Conventional Commits; secrets only in `.env` (gitignored); every decision and image version is an ADR
  in `docs/adr/`; the PID is amended (next amendment 1.8, new section S4d) before code.
- TDD: red unit tests and a red verify check first, then the implementation, then green.
- Everything is created through the Platform API from documents in the repo and re-runs idempotently
  (pattern: `ansible/playbooks/flowai.yml` + `tasks/flowai-agent.yml`). Nothing is clicked into the UI.
- Run infrastructure changes one step at a time and report after each. Ask in chat before anything
  destructive or anything touching vmbr0. Post a detailed change summary and wait for a verbal
  "approve" before merging. Never fabricate a "done".
- After `itential.yml` (it re-imports every `wf-*`, which changes workflow uuids) always run
  `flowai.yml` so the agents' Tool Registry references are re-resolved.
- Use the `itential-builder` plugin skills (`/itential-builder:*`: itential-golden-config,
  itential-devices, itential-mop, itential-lcm, itential-json-forms, itential-inventory, flowagent)
  as the reference for object shapes; keep the repo's Ansible/API pattern as the delivery vehicle.

## Lab facts
- Platform https://itential.lab.internal (VM 205, 10.100.0.65; lab CA `docs/lab-root-ca.crt`; login
  `admin@itential` / `ITENTIAL_ADMIN_PASSWORD` from `.env`; approvals in Work Center).
- Gateway 5 cluster `lab` = stock server + etcd + glibc runner (`gateway5-runner`, ADR 0038);
  Inventory Manager inventory `lab` = the 12 network nodes from NetBox (5 IOS-XE `cisco_ios`,
  7 EOS `arista_eos`), broker actions get-config/set-config/run-command/is-alive.
- Configuration Manager reads devices through the `InventoryBroker` adapter (ADR 0039); backups and
  `run_command` work; Gateway 4 stays staged, never deployed.
- Workflows: wf-netbox-device-count-v1, wf-show-version-v1, wf-show-command-v1 (Genie/TextFSM
  structured output), wf-branch-vlan-v1 (NetBox reservation, Work Center approval, optional
  ServiceNow standard change). Agents: lab-netops (Claude), lab-netops-local (in-lab qwen2.5:7b),
  lab-netops-mac (qwen3:30b-a3b on the Mac, optional). MCP server http://mcp.lab.internal:8000/mcp.
- NetBox (adapter `NetBox`, export `Netbox`), ServiceNow PDI dev409097 (adapter `ServiceNow`).
- Devices: `verify/devcmd.py <ip> "<cmd>"` gives the second, independent source for every check.

## Scope: PID S4d "Platform coverage of the EVE-NG lab", build in this order, one element per commit
1. **Golden Config + compliance + device groups**: device groups by site (dc1, br1, br2, wan) and role;
   one CLI Golden Config tree per OS (`cisco_ios`, `arista_eos`) whose baseline nodes come from
   documents in `itential/golden-config/` (management, AAA/users, NTP/logging, interfaces to
   NetBox intent); every node attached; one compliance plan on a schedule. Acceptance: a deliberate
   hostname drift on two devices is flagged with no false positives elsewhere, then restored.
2. **Command templates (MOP) + backup schedule**: pre/post check templates per vendor (version,
   interfaces up, BGP/OSPF neighbours) with pass/fail rules; an Operations Manager schedule trigger
   that backs up every device nightly. Acceptance: a run of each template against one device per
   vendor; a scheduled backup exists for every device with the config equal to direct SSH.
3. **Lifecycle Manager + JSON Forms**: resource model `branch-vlan` (create/delete actions bound to
   wf-branch-vlan-v1 and a new delete workflow), instances for the VLANs that exist; a JSON Form on
   the approval task showing branch, VLAN, switch and the NetBox reservation. Acceptance: create ->
   instance -> delete round trip with the approval in Work Center; instance history recorded.
4. **Integration Models**: ServiceNow (OpenAPI upload + instance with `itential.integration`) and
   NetBox, registered as agent tools. Acceptance: an agent reads an incident and a NetBox device
   through the integration models, not the adapters.
5. **Agent fleet** (the owner's LinkedIn pattern, memory `work-laptop-agent-fleet`): netbox-sot,
   device-ops, compliance, diagnostics, remediation; each with a Claude profile and a local-model
   twin; tiered autonomy, every write behind a Work Center approval. Acceptance per agent in
   `verify/test-06-flowai.sh`, token usage recorded.
6. **Hosts and firewalls**: the three Ubuntu hosts as inventory nodes (`linux` platform,
   reachability and uptime checks); PA-VM firewalls when the image is on EVE-NG (PID S3 item).

Deferred (do not build): Gateway 4, observability/job metrics (Phase 8), node-credential
secrets (Phase 10), Windows endpoints.

## First step
Confirm the gate (git status clean on `phase-6/flowai`, `pytest -q` 83 passing, Platform health
answers, `verify/test-06-flowai.sh` was 7/7 on 2026-09-07), then propose the PID 1.8 / S4d text and
the ADR list for element 1 before writing code.
