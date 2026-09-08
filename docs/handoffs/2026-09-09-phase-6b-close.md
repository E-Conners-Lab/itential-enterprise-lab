# Handoff — Phase 6b close-out (state after the 2026-09-08 session)

Paste the block below into a new Claude Code session opened in this repo (the `itential-builder`
plugin is installed at user scope).

---

Continue the itential-enterprise-lab project (repo https://github.com/E-Conners-Lab/itential-enterprise-lab,
local ~/PycharmProjects/itential-enterprise-lab, branch `phase-6/flowai`, at the branch head pushed on 2026-09-08: `git log --oneline -6`).
Read this file, then `docs/PID.md` (S4c, S4d, amendments 1.8 to 1.12), ADR 0037-0047, `itential/versions.yaml`,
`ansible/playbooks/itential.yml`, `platform.yml` (+ `tasks/`), `flowai.yml` (+ `tasks/flowai-agent.yml`),
`itential/workflows/build.py`, `itential/agents/*.yaml`, `verify/test-06-flowai.sh`, `verify/test-06b-platform.sh`
and `tests/`. Memory notes in `~/.claude/projects/-Users-elliotconner-PycharmProjects-itential-enterprise-lab/memory/`
hold every API shape measured on Platform 6.5.2 (`itential-platform-lessons.md`, incl. the owner's work-laptop lessons).

## Rules (unchanged)
- One branch and one PR per phase; the PR description is the phase report (built / verified / deferred).
  `phase-6/flowai` is rebased onto main after PR #17 (Phase 5) merges.
- Conventional Commits; secrets only in `.env`; every decision an ADR (next 0048); amend the PID (next 1.13) before
  code when the design changes. TDD: red tests and a red verify check first. One element per commit with pytest,
  ansible-lint (140-character lines) and the verify log.
- Everything is created through the Platform API from documents in the repo and re-runs idempotently. Play order
  after any workflow change: `itential.yml` (re-imports every `wf-*`, new uuids) -> `platform.yml` -> `flowai.yml`
  (`make phase-flowai`). Export `NETBOX_API=$NETBOX_URL` when running plays by hand.
- Device writes only through `wf-config-push-v1` with a Work Center approval; the verify scripts finish their own
  approvals; outside them, start the job, report the id and ask Elliot to approve the card. Ask before anything
  destructive or touching vmbr0. Post a detailed change summary and wait for a verbal "approve" before merging.
  Never fabricate a "done". `verify/devcmd.py <ip> "<cmd>"` is the independent second source (works on the Ubuntu
  hosts too since element 6). Measure any platform shape before trusting a skill; record what you measure in memory.

## State (elements 1 to 6 built on phase-6/flowai)
- Elements 1-4 committed and verified (ADR 0040-0045): Golden Config + nightly compliance, MOP templates + nightly
  backups, Lifecycle Manager `branch-vlan` with the JSON form approval (task 4a of `wf-branch-vlan-v1` is
  `ShowJsonForm`; API approvals send `variables.export.decision`), Integration Models `lab-netbox` / `lab-servicenow`
  (instances `netbox-api` / `servicenow-api`, roles re-synced, operations as `integration:` tools).
- Element 5 (ADR 0046): the fleet `netbox-sot`, `device-ops`, `compliance`, `diagnostics`, `remediation`, each with an
  `ollama-lab` twin (one to three tools). New workflows: `wf-compliance-report-v1` (run or read the plan, compact
  per-device summary; the raw report tools cost an agent 638k tokens) and `wf-show-all-v1` (one show command on
  every lab device in one call, parsed per device; a local model asked about "all devices" used to invent node
  names). Ollama runs with `OLLAMA_CONTEXT_LENGTH=16384`. Verify S4d.5a-g in test-06: PASS across 20260908T025400Z (a, b, d, f), 034041Z (c, g, S4c.5) and 040730Z (e); a single full run is next step 1.
- Element 6 (ADR 0047): the three Ubuntu hosts in the Gateway 5 inventory `lab-hosts` (platform `linux`; the
  automation account's password set and password login enabled for that user by `lab-endpoints.yml`); never in
  Configuration Manager. Verify S4d.6 PASS 20260908T032741Z.
- Lab facts: Platform https://itential.lab.internal (VM 205, 10.100.0.65), Gateway 5 cluster `lab` with the glibc
  runner, inventories `lab` (12 network nodes) and `lab-hosts` (3 hosts), NetBox 10.100.0.64:8080, PDI dev409097
  (user itential.integration; interactive login every 10 days), MCP http://mcp.lab.internal:8000/mcp.
- Known warts: the Tool Registry keeps inactive entries for deleted scratch workflows/models (discovery never
  prunes); a workflow tool whose job ends in error leaves the agent session RUNNING (cancel the job, then
  `POST /agent-session-manager/sessions/<id> {"action":"CANCEL"}`); `PUT /integration-models` is a 500 (the play
  deletes and re-creates a changed model); `getJSONComplianceReportsByBatch` returns empty lists.

## Next steps, in order
1. Full runs: `verify/test-06-flowai.sh` (S4c.1-7, S4d.5a-g; ~30 min, ~250k Anthropic input tokens) and
   `verify/test-06b-platform.sh` (S4d.1-4, S4d.6; ~15 min). Both logs go into a `test(phase-6)` commit.
2. PR #17 (Phase 5): re-run `verify/test-05-itential.sh` after 2026-09-08 19:10 UTC for the 24 h S4.6 memory check,
   post the change summary, wait for Elliot's "approve", merge.
3. Open the Phase 6 draft PR from `phase-6/flowai` (description = the phase report: built / verified / deferred,
   ADR 0037-0047, verify log paths); rebase onto main once #17 merges; post the summary; wait for "approve".
4. NetBox enrichment (owner request 2026-09-08, after the Phase 6 PR; ADR 0048, PID 1.13 before code). Measured
   on 2026-09-08: NetBox holds the skeleton only. Modeled: 5 sites, 5 manufacturers, 5 device types, 12 roles,
   5 platforms, 21 devices (17 active, 4 planned firewalls), 87 interfaces, 33 cables (one per topology link),
   19 prefixes, 2 DHCP ranges, 47 addresses with DNS names, primary IPv4 everywhere, 2 VLAN groups + 1 VLAN,
   23 tags, the Proxmox cluster with 5 VMs. Not modeled: locations/racks/positions, console/power/front/rear
   ports, inventory items (serials); the /30 link addresses, loopbacks, SVIs and anycast gateways on their
   interfaces and the "to <peer>" interface descriptions (what Golden Config interface intent needs, ADR 0040);
   VRFs (MGMT, tenant), route targets, ASNs (in the topology), FHRP groups (BGP sessions need the netbox-bgp
   plugin); providers/circuits/terminations for the four provider links; tenants, contacts, config contexts
   (site gateways, DNS, NTP live only in the YAML), custom fields, journal entries. Rule: `topology/*.yaml` stays
   the single oracle (ADR 0002): racks, circuits, VRFs and ASNs are added there and derived by a new
   `netbox-enrich.yml` play, never typed into NetBox. Order: interface addressing + descriptions, VRFs + ASNs,
   racks + locations, circuits, config contexts + journal entries (the plays and the LCM actions write them).
   Design first (ADR + PID), then red tests, then the play; netbox-sot's acceptance grows with each object type.
5. Deferred, do not build: PA-VM firewalls (image not staged), Gateway 4, observability/job metrics (Phase 9),
   node-credential secrets (Phase 10), Windows endpoints. Ideas noted, not scheduled: an eAPI python-script
   service for EOS structured data (TextFSM regex risk), Tool Registry pruning.
   Build-process gap closed 2026-09-08: `make phase-network-topology` now runs the Phase 4 sequence; the only
   unscripted step left is the EVE-NG Ubuntu golden image (docs/manual-steps.md step 15, a recipe is an
   `images/` idea, not scheduled).
