# Handoff — NetBox enrichment (S4e) close-out (state after the 2026-09-08 session)

Paste the block below into a new Claude Code session opened in this repo (the `itential-builder`
plugin is installed at user scope).

---

Continue the itential-enterprise-lab project (repo https://github.com/E-Conners-Lab/itential-enterprise-lab,
local ~/PycharmProjects/itential-enterprise-lab). Branches: `phase-6/flowai` (draft PR #19, base `phase-5/itential`)
and, stacked on it, `phase-6/netbox-enrichment` (this work; `git log --oneline -8`). Read this file, then
`docs/PID.md` (S4c, S4d, S4e, amendments 1.8 to 1.13), ADR 0037-0048, `topology/README.md`, `topology/derive.py`,
`ansible/playbooks/netbox-topology.yml`, `netbox-enrich.yml`, `platform.yml` (+ `tasks/`, `templates/gc-interfaces.j2`),
`itential/workflows/build.py` (journal_chain), `itential/integrations/build.py`, `itential/agents/netbox-sot.yaml`,
`itential/golden-config/*/device.j2`, `verify/test-06c-netbox.sh` and `tests/test_netbox_enrich.py`. Memory notes in
`~/.claude/projects/-Users-elliotconner-PycharmProjects-itential-enterprise-lab/memory/` hold every API shape measured
(`itential-platform-lessons.md`, incl. the NetBox 4.7 / netbox.netbox 3.22 lessons of 2026-09-08).

## Rules (unchanged, see docs/handoffs/2026-09-09-phase-6b-close.md)
One branch and one PR per phase (this element is a stacked branch with its own PR, base `phase-6/flowai`); every
decision an ADR (next 0049); PID amendment before code (next 1.14); TDD; one element per commit with pytest,
ansible-lint and the verify log; everything through the APIs from documents in the repo, idempotent; device writes only
through `wf-config-push-v1`; post a summary and wait for a verbal "approve" before merging; never fabricate a "done";
`verify/devcmd.py` is the independent second source; measure any platform shape before trusting a skill.
`topology/enterprise.yaml` is the only oracle (ADR 0002/0048): the templates carry no address literal
(`tests/test_topology.py` proves the rendered configs equal `topology/generated/configs/` byte for byte).

## State
- Phase 6 draft PR #19 open (14/14 + 5/5 verified 2026-09-08, commit 9dd862b). PR #17 (Phase 5): the 24 h S4.6
  re-run of `verify/test-05-itential.sh` was armed for 19:12 UTC on 2026-09-08 (see the session log / results dir);
  its summary and Elliot's "approve" are still needed, then #19 is retargeted to main and rebased.
- S4e on `phase-6/netbox-enrichment` (ADR 0048, PID 1.13), all six criteria built:
  1. addressing lifted into the YAML, `derive.py` as the one derivation (ce0faae, bc46e36);
  2. VRFs + route targets, ASNs on sites, BGP neighbours and RDs in device contexts, FHRP group (73d70be);
  3./4. racks and locations, provider circuits with terminations and traced cables (963a638);
  5./6. journal entries (play per site on a commit's first or changing run; LCM create/delete through the NetBox
  adapter's genericAdapterRequest), the Integration Model with five more reads, netbox-sot's tools, the golden-config
  leaves with the interface intent from NetBox (last commit on the branch; logs 20260908T171855Z-06b-platform.log and 20260908T174750Z-06c-netbox.log).
- NetBox now holds: 112 interfaces with device names (Gi2 -> GigabitEthernet2), 64 addressed intent interfaces, VRFs
  MGMT/WAN/PROD, 7 ASNs, config contexts lab/site-*/platform-* + local per device, FHRP group dc1-vlan100-varp,
  4 locations + 4 racks with 21 devices placed, provider "Simulated ISP" with 4 circuits (37 cables), journal entries.
- Known warts (memory): `wf-show-all-v1` caps a device's parsed result at 1500 chars (verify falls back to
  `wf-show-command-v1`); `netbox-topology.yml` flips device status planned->active on every run (pre-existing);
  the collection cannot scope circuit terminations nor look up cables to them (API used); ip_address lookups move
  shared anycast addresses (API used); `docs/ip-plan.md` section 6 is not yet regenerated from the YAML (ADR 0048
  consequence, open).

## Next steps, in order
1. If not done: post the PR #17 summary (24 h S4.6 result), wait for "approve", merge; retarget #19 to main, rebase
   `phase-6/flowai` and `phase-6/netbox-enrichment`.
2. Open the S4e draft PR from `phase-6/netbox-enrichment` (base `phase-6/flowai`; description = built / verified /
   deferred with ADR 0048 and the test-06c logs), post the summary, wait for "approve".
3. Regenerate `docs/ip-plan.md` section 6 from the YAML with a test like `tests/test_ipam.py` (open ADR 0048 item).
4. Deferred, do not build: netbox-bgp plugin, console/power/front/rear ports, inventory items, tenants, custom
   fields, Windows client addresses, PA-VM firewalls, Gateway 4, observability (Phase 9), secrets (Phase 10).
