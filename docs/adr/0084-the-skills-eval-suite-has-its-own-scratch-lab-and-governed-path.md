# 0084 — The skills eval suite has its own scratch lab, accounts and governed device path

- **Status:** accepted (2026-10-24, when its last change converged; the owner approved the design 2026-10-22)
- **Date:** 2026-10-22
- **Related:** ADR 0034 (the topology and the Palo Alto deferral), ADR 0039 (InventoryBroker reads `lab` only), ADR 0046
  (agent twins), ADR 0065 (Vault aliases), ADR 0066 (evaluate every Gateway result), ADR 0067 (workflow names), ADR 0077
  (InteractiveHTML cards), ADR 0081 and 0082 (gbac as the boundary for automations), ADR 0083 (a service account per
  client)

## Context

A private skills workbench certifies agent skills (step-by-step network procedures) by running the same test cases
through three agent harnesses: Claude Code, Codex CLI and a FlowAI agent on this Platform, each with a hosted and a
local model. A run must touch only routers that nothing else depends on, every device change must wait for an
approval the agent cannot give itself, and the outcome must be judged by reading the routers independently of the
agent and of the Platform path it used.

Production had none of that: the show and push workflows are bound to the `lab` inventory; both approval cards carry
no groups, so the account that proposed a change could approve it; Push Configuration with Approval uses a ViewData
card that does not open on 6.6.0 and does not check its lines; there is no RESTCONF read; and there is no account scoped
to evaluation work.

## Decision

1. **A separate scratch lab.** `topology/skills-lab.yaml`: three C8000v 17.13.01a routers (`skl-r1..3`, OOB
   10.100.0.152-.154, confirmed free in `topology/ipam.yaml`, `docs/ip-plan.md` and NetBox before assignment) wired in
   a triangle, in their own EVE-NG lab (`/skills-lab.unl`). `eve/build.py --topology` builds it; `delete` removes it
   and refuses the enterprise lab. Generated files for any lab but the enterprise one go under
   `topology/generated/<lab name>/`, so the enterprise MAC table and reference configs are never overwritten.
   Every scratch node carries its own first MAC (`eve.firstmac`, locally administered `52:5c:00:0n:00:00`): EVE-NG
   derives `50:00:00:<node id>:00:<if>` otherwise, node ids restart at 1 in every lab, and `skl-r1..3` would
   duplicate the management MACs of isp-core01, dc1-wan01 and dc1-wan02 on pnet1. The builder refuses a non-enterprise
   node without one, checks EVE-NG kept it right after creating the node, and checks again before starting.
2. **Memory on loan.** The scratch lab borrows 18 GB of the 32 GB reserved for the four PA-VMs while `lab.firewalls`
   is false (`docs/resource-budget.md` 3a): 73 + 18 + 6 = 97 GB of the 115 GB ceiling. Turning the firewalls on means
   deleting the scratch lab first. `eve/build.py` enforces it on apply and start in both directions (a scratch
   build refuses while the enterprise firewalls are on; an enterprise build with firewalls on refuses while
   `topology/skills-lab.yaml` exists), and `tests/test_skills_lab.py` holds both.
3. **A scratch day-0.** `topology/configs/c8000v.j2` with `lab.scratch: true` renders management, the automation user
   (its own password, `SKILLS_LAB_AUTOMATION_PASSWORD`, never the production device password), NETCONF, RESTCONF, SSH
   and the config-change log to the lab syslog receiver, and nothing a skill configures itself (no loopback, no WAN
   VRF, no BGP, no data-port addressing). The enterprise configs render byte-identically.
4. **Read accounts locked to reads.** `ansible/playbooks/skills-lab-devices.yml` creates `restconf-reader` (for the
   Gateway's `restconf-get` service) and `judge` (for the workbench's independent judge only) at privilege 1, applies
   Cisco's NACM read rules for privilege 1 (IOS XE 17.9.1+), proves each reader can read and is refused a write,
   records the HTTPS certificate and SSH host key fingerprints for pinning, and captures `bootflash:baseline.cfg`.
   The HTTPS pins live in `itential/versions.yaml` `skills_lab.nodes`, in the form `restconf-get` takes;
   `restconf-get` itself logs in only to these three names, each at its own address. The three
   passwords are seeded into production's Vault only (`prod_only`): the automation and reader passwords under
   `devices/*`, bound to the Gateway as `skills-lab-automation-password` and `skills-lab-reader-password`; the judge's
   under `eval/`, which no reader's policy reaches, so the Gateway can never resolve it.
5. **Outside NetBox's device records.** The scratch routers are not NetBox devices (owner decision 2026-10-22), so no
   inventory, compliance, backup, monitoring or MCP client picks them up. Their three addresses are reserved in
   `topology/ipam.yaml` (placement `eve-router`), which seeds NetBox IP address records and the oob-gw DNS names.
6. **Governed path (later changes of this ADR).** A separate inventory `skills-lab` (groups set at creation); four
   workflows with ADR 0067 names (Run Show Command on a Scratch Device, Get RESTCONF Data from a Scratch Device, Push
   Configuration to a Scratch Device with Approval, Restore Baseline on a Scratch Device: "Restore" joins the verb list
   on purpose); the push card's groups are `eval-approvers` and `admin_group`; a `restconf-get` Gateway 5 service with
   an allowlist; accounts `svc-eval` (runs the three agent tools, cannot finish tasks) and `svc-eval-approver`
   (releases or rejects cards and runs the restore); a new model profile for claude-sonnet-5-5 and agents `skill-eval` /
   `skill-eval-local` whose instructions are generic; skill text reaches the agents only in each session's request
   and is never stored in an agent definition or in this repository. Each session's record on the Platform (Agent
   Session Manager) keeps its inputs and its instructions with the request filled in (measured 2026-10-23), so the
   runner deletes each session once it is scored (owner decision 2026-10-23): `svc-eval` holds
   `AgentSessionManager/deleteSession` as an exact method of its custom role, leaving the deny list's `*:delete` in
   force. Neither session method is scoped to the eval agents: `session:read` lists every session on the Platform
   (measured with svc-netclaw, which holds the same role), so while a run lasts, every account with that role can
   read its procedure: svc-netclaw, svc-copilot, svc-eval and the production MCP account (`mcp-builders` holds every
   role but administration, ADR 0083, so it can delete any session already). How far svc-eval's delete reaches is
   measured at converge on a throwaway session (`verify/skillslabcheck.py agents`): it reaches every session, since
   svc-eval deleted one the administrator started (measured 2026-10-23).

   As built (production only; the dev tier imports, triggers and builds none of it): the `skills-lab` inventory is
   created once with `admin_group`, `eval` and `eval-approvers` and holds `skills_lab.nodes`, its password the Gateway
   alias; the RESTCONF workflow's gate is restconf-get's own rule (the allowlist in `skills_lab.restconf_allowlist`,
   `%` only as %2F, never `..`); the push refuses `skills_lab.forbidden_lines` on the runner before any card, its card
   names the two groups by `__GROUP_ID` markers the import fills in (and refuses to import when it cannot), and nothing
   is ever saved. The push also refuses `skills_lab.forbidden_commands` as IOS XE reads a line (abbreviated, behind
   `no` or `default`), and anything that reaches the management path a restore needs (owner decision 2026-10-23):
   GigabitEthernet1 and VRF MGMT by name, the vty and console lines, AAA, login, RESTCONF, NETCONF, the HTTPS server,
   SSH, the pinned trustpoint, control-plane policing, IP routing, and any alias; a pushed line that cuts the
   management path some other way leaves the router for the console. The restore proves itself with two hashes and
   never publishes the configuration. The scratch automations' gbac is `admin_group` plus one eval group and none of the base read groups, and the eval agents are
   operated by `admin_group` and `eval` only: the MCP server's group, which operates every other agent, is kept off
   them, and they sit in a project of their own, `skills_lab.project`, so the group's editor role on the agents'
   shared project cannot reach them (owner decision 2026-10-23). The approver holds no `cancelJobs`, which reaches
   every job on production: a push a run leaves waiting is rejected through its card (owner decision 2026-10-23).
   Work Center lets an account claim a card only when it is in the card's groups and also in its job's groups, which
   are the automation's gbac read plus write; it answers 404 otherwise. Either gbac list would let eval-approvers
   start the push itself (both measured at converge, 2026-10-23), so the push adds eval-approvers to its own job
   (`addJobGroup`) just before the card, and a push refused before its card never opens to the approver.
   The Platform's Anthropic provider sends `thinking: {type: disabled}`, which claude-sonnet-5-5 refuses (HTTP 400,
   "send between_tools instead"), so skill-eval failed at its first inference; the profile's Model Variables reach the
   API as-is and override it, so the profile carries `thinking: {type: between_tools}` and the flowai play keeps it in
   step on that profile alone, never on `anthropic` (measured 2026-10-24).
   `verify/tokens.sh` leaves their sessions out
   (`llm.budget.exclude_agents`). Probes: `verify/skillslabcheck.py`.
7. **Two owner decisions (2026-10-22).** The owner approves scratch changes with the existing administrator login
   (`admin_group`) alongside the runner's approver account, instead of a new LDAP membership. Only eval accounts reach
   the scratch workflows: their automations carry `admin_group` plus `eval` or `eval-approvers` and none of the base
   read groups, so Copilot, the production MCP account and other agents cannot touch the scratch routers during a run.
8. **Budget.** Eval agent sessions are metered by the workbench (an alert at each $100), not by the $15-a-week verify
   budget; `verify/tokens.sh` leaves them out.

## Consequences

- Production gains a lab-only router set the eval suite can break and restore at will, at the cost of the Palo Alto
  reservation while the firewalls stay deferred.
- The scratch routers keep the config-change log the lab edge deliberately lacks; they hold no production secret, and
  the log is what the eval suite compares a run's changes with.
- The scratch routers boot with an empty licence level, like the enterprise routers before their first push-configs
  reload; a skill that needs a feature behind the licence level needs that one reload first.
- The first SSH and TLS contact with a freshly built router cannot be pinned; the devices play records both
  fingerprints and every later step pins them.

## Rollback

`make skills-lab-down` deletes the EVE-NG lab; reverting this ADR's changes removes the topology, the IPAM rows, the
budget loan and the builder flags. The later Platform changes roll back as recorded in the workbench's design (retired
workflow names, then the inventory, accounts, agents and profile by hand).

## Verification

`tests/test_skills_lab.py` (topology, IPAM, budget loan, firewall guard, day-0, builder refusal),
`tests/test_eval_access.py` (the two role oracles) and `tests/test_skills_lab_workflows.py` (gates, forbidden lines,
card groups, restore proof, inventory, agents); then a probe per acceptance criterion after converge,
`verify/skillslabcheck.py`: accounts, inventory, gbac, forbidden, restconf, hold, restore, agents (with the session
delete and its measured reach), profile and project, and the production snapshot compare.

## As built (2026-10-24, production)

Eight lab changes converged it: #189 (topology, builder, day-0, devices play, budget loan, this ADR), #190 (the NACM
read rules retried on first boot, read-only proven with a real write), #191 (the `restconf-get` pin, the scratch Vault
secrets and the HTTPS pins), #192 (accounts, inventory, the four workflows, the profile, the agents and the probes),
then four fixes the converge found: #193, #194, #195 and #196. The Vault and Gateway plays ran once, after #191; every
later change was a replay from main behind a production snapshot, which showed only the intended additions after #192
and only workflow re-import timestamps after #194 to #196.

Measured at converge, each now held by a test:

- **A grouped manual task does not import in bulk.** 6.6.0's bulk workflow import refuses a workflow whose manual task
  carries groups, and the first replay left 15 automations pointing at no workflow. Workflows with grouped cards are
  created one at a time (#193).
- **A card's readers are its job's groups.** Work Center lets an account claim a card only when it is in the card's
  groups and in its job's, and a job's groups are the automation's gbac read plus write; anyone else gets 404. Either
  gbac list also lets the account start the automation: the approver started a push, which the gate refused before any
  card. So the push adds `eval-approvers` to its own job with Operations Manager's `addJobGroup` just before the card,
  and a push refused before its card never opens to the approver (#194).
- **An unmapped task output imports but cannot start.** `addJobGroup` declares the output `result`; with nothing
  mapped, the workflow imported cleanly and every start answered HTTP 400 (`Output: "undefined" does not match model
  output: "result"`). `POST /automation-studio/workflows/validate` reports it and the import does not (#195, with a test
  that every automatic task maps an output).
- **Session messages are not in eventId order.** Agent Session Manager's eventIds are UUIDs, so `sortBy=eventId`
  returns a session's messages out of order; the probes take the last answer by timestamp (#194).
- **Sonnet 5.5 refuses the Platform's thinking setting.** The Anthropic provider sends `thinking: {type: disabled}`;
  claude-sonnet-5-5 answers HTTP 400 and asks for `between_tools`. The profile's Model Variables override it (#196). A
  profile `models[]` PATCH reissues the model id, so the play re-binds the agent after it, as it does for Ollama.

The design's unverified items:

| Item | As built |
|---|---|
| `configure replace` output returns through `sendCommand` | Settled: the restore runs `configure replace bootflash:baseline.cfg force` and reads both hashes through `sendCommand` |
| `bootflash:baseline.cfg` equals the running configuration after normalization | Settled: the restore probe's baseline and running hashes match |
| The Anthropic key lists `claude-sonnet-5-5` | Yes; serving it needed the thinking override above |
| Inventory access for jobs run by `svc-eval` and `svc-eval-approver` | Settled: every scratch workflow ran as its account |
| The session field for the budget exclusion | `agentSnapshot.name` (`verify/tokens.sh`, measured 2026-10-23) |
| An Operations Manager finish by a non-member is refused | Not relied on and not probed: the hold probe tries the Work Center claim, which `svc-eval` is refused (403) |
| Session messages page with `page` | Left to the workbench runner's first live run |

Probes after the last replay (2026-10-24 04:06-04:10Z, `verify/skillslabcheck.py all` then `agents --claude`): all passed. `accounts`: each account's session holds exactly its one eval group. `inventory`: exactly `skl-r1..3`,
none in `lab`, Configuration Manager or NetBox's devices. `gbac`: `svc-eval` runs the show and RESTCONF routes and
is refused the restore route, a production route and a direct workflow start; the approver is refused the runner's
routes. `forbidden`: `write memory`, `do copy running-config startup-config` and `int gi1` / ` shutdown` refused
before any card. `restconf`: JSON for the allowlisted path, the username path refused before any job, no password in
the record. `hold`: held on its card, `svc-eval`'s claim refused (403), released by the approver and read on the
router as `judge`; a rejected push never reached it. `restore`: baseline and running hashes equal, the probe change
gone. `profile`: claude-sonnet-5-5 with `between_tools`, all 22 agent documents operable. `project`: exactly the
pair, no group among its members. `agents`: `skill-eval` and `skill-eval-local` each answered from the RESTCONF
tool alone with the count `judge` reads, and `svc-eval` deleted both sessions; its delete also removed a session the
administrator started. The snapshot compare after that replay showed only workflow re-import timestamps.

Still open:

- Acceptance criterion 3 asks for the router's config-change log as the proof; the hold probe reads the router through
  RESTCONF as `judge` instead, which is independent of the agent and of the Platform path, but is not that log.
- The flowai play creates a profile without its Model Variables and corrects them only on its next run (Ollama
  profiles too).
- The replay imports a workflow that the validate endpoint rejects; validating before import would have stopped #195's
  fault before production.
- The workbench runner must read session messages in time order, find cards as the approver, reject rather than cancel
  a card a run leaves waiting, and delete each session once it is scored.
