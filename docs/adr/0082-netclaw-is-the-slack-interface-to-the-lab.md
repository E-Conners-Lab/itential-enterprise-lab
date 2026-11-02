# 0082 — NetClaw is the Slack interface to the lab: open source in front, the Itential Platform as the only path to the network

- **Status:** proposed (2026-10-11); built spec-driven with the itential-builder skills (delivery
  `itential-deliveries/netclaw/`: customer-spec, feasibility, solution-design approved by the owner 2026-10-11); dev
  converged and the first Slack round trip made on dev 2026-10-12; production and the phase 1 proofs follow the merge
- **Date:** 2026-10-11
- **Related:** ADR 0046 (the agent fleet and its twins), ADR 0063 (svc-copilot, the service-account pattern reused here),
  ADR 0079 (the local model), ADR 0080 (the describer), ADR 0081 (platform-guide and its Slack front door, which stays the
  department bot's door), the department-agents delivery (it measured that gbac is the only boundary for trigger runs)

## Context

The lab's work is done by the Platform: workflows read and change the devices, FlowAI agents explain and diagnose, Work
Center holds every approval. Each is reached through a different screen or a purpose-built bot. The owner wants one
conversational interface from the phone, in the personal Slack workspace where the department bot already answers: ask a
device's state, start a workflow, talk to the lab's agents. It is also the showcase of open-source software monitoring,
managing and maintaining infrastructure with the Itential Platform as the orchestrator, and a story for the owner's AutoCon 6
session with NetClaw's author.

NetClaw (Apache-2.0) is an open-source network agent on the OpenClaw runtime. Out of the box it is the opposite of what the
lab needs: its installer deploys all 237 of its skills and persona files that tell the agent it owns the network, to push
changes through pyATS and approve emergencies afterwards; a heartbeat checks devices and posts every 10-60 minutes; OpenClaw
offers shell, file and web tools; the persona alone is about 40k tokens, more than the local model's whole context.

## Decision

1. **Itential is the only path to the network.** NetClaw holds no device credentials and runs no device MCP; a device's state
   comes from the `Run Show Command on a Device` workflow, whose input gate admits only `show` commands. Anything that
   changes a device is a workflow that stops on a Work Center approval: NetClaw posts the card's link, never a Slack button,
   so the approver's identity stays in Itential's audit (owner, 2026-10-03).
2. **Its own account, its own group, a short role.** `svc-netclaw` in the LDAP group `netclaw` (made by
   `tasks/netclaw-access.yml` from platform.yml, through the shared `tasks/ldap-service-account.yml`, which now writes a
   closed set of two kinds: copilot and netclaw). The role oracle `itential/netclaw/roles.yaml`: a custom role
   `netclaw-run` with exactly `runEndpointTriggerWithPost`, `getJob`, `getJobs` and Work Center's `getContextHistory` (the
   card's id for its link; the only built-in Work Center role can also claim, complete and cancel cards), plus
   AgentSessionManager `session:read` and `operator`. Never the platform admin, never the lab MCP server.
3. **gbac is the boundary, set by the plays.** On 6.6.0 an automation whose gbac is empty can be started by any account that
   may run triggers. `tasks/automation-access.yml` (end of platform.yml and of flowai-assets.yml) sets every automation's gbac
   from versions.yaml `automation_access`: read = the tier's admin and copilot groups, plus `netclaw` on exactly NetClaw's
   automations; write = admin_group; the department delivery's Ask automations are never touched. platform-guide and its
   twin are agent automations (`expose_route`) whose operators add the `netclaw` group (`operator_groups`).
4. **Seven tools, custom, not bindings.** itential-mcp's bindings look the automation up by name (hidden from a non-admin
   without every workflow document's read), turn the job's user into a name through every account (refused, so the call
   fails after the job started: a retry is a second change) and return the whole task map (94 tasks for Add Branch VLAN).
   The delivery's `itential-tools/lab_tools.py` loads as itential-mcp custom tools: four starters, each posting to one fixed
   route and returning only the job or session id (`run_show_command`, `describe_job_card`, `add_branch_vlan`,
   `ask_platform_guide`), `job_result` (status, the workflow's own outputs, the waiting card's Work Center link),
   `latest_job` and `guide_answer` (platform-guide's final text, waiting inside one call). Include tags and OpenClaw's tool
   filter both name exactly these seven.
5. **NetClaw's installer, hardened, in a container on the Mac Mini.** NetClaw 1.10.0's own installer with only its itential
   component, on OpenClaw pinned to 2026.8.35 (`@openclaw/slack` the same), in a Docker Desktop container beside Ollama:
   non-root, no home-directory mount, no published port. The overlay: our six persona files (3 KB), no skills (OpenClaw shows
   skills only through the file `read` tool, which NetClaw does not get; the lab's guidance is `AGENTS.md`), tool profile
   `minimal` plus the MCP tools with every other group denied, heartbeat and automations off, update check, telemetry and
   catalog refresh off. The config is rendered over OpenClaw's own at each start (OpenClaw restores its last-good copy when
   a write drops its `meta`).
6. **Slack: one private channel, one sender.** A second app `netclaw` (Socket Mode, outbound only; scopes for a private
   channel only, no interactivity, no DMs) in private `#lab-ask`; OpenClaw's allow-list names the channel id and the owner's
   member id; join message and native approvals off. Tokens and ids in Vault `lab/netclaw/slack`; svc-netclaw's password
   from the lab .env (a Vault copy would stop every Vault converge until platform.yml has generated it).
7. **Models.** qwen3.8:27b on the Mac Mini day to day (context 32768, the same runner the Platform's twins use; warmed
   first), and with it platform-guide's twin; Claude (`anthropic/claude-sonnet-5` and `platform-guide`) only for the
   recorded demo, after `verify/tokens.sh --check` (`netclaw.sh start <tier> claude`).

## Consequences

- One Slack thread can read a device, describe a job, explain a workflow and request a change, and the Platform still decides
  everything: what the account may start (gbac), what each workflow accepts (its gate), who approves (Work Center).
- svc-netclaw reads every job and every agent session on the Platform (6.6.0 scopes neither per account): accepted for an IT
  front door used only by the owner; the department accounts do not hold these reads.
- Every lab automation now carries gbac from the plays; a replay no longer opens a new automation to any trigger-runner. A new
  group that should start a lab automation (the department bot's own channel for Ask Platform Guide, later) is a versions.yaml
  change.
- The approvals stay where they were: the phone sees the link; approving needs a browser that reaches the Platform (the Mac).
- NetClaw stops when the Mac sleeps (restart policy covers reboots); k3s is the home once the configuration settles.
- Add Branch VLAN cannot start on dev (no ServiceNow adapter there): the change path is proven on production only.
- Two upstream findings are the owner's to report: itential-mcp's bindings fail for a least-privilege account (the account
  lookup after the start, the task map in the answer), and NetClaw's installer deploys every skill whatever the components.

## Measured (dev, 2026-10-11/12)

- The plays on dev (platform.yml, flowai.yml: failed=0): svc-netclaw holds exactly `netclaw`; every automation carries its
  gbac; the four routes start as svc-netclaw; another automation's route (401), a direct workflow start, MOP, Configuration
  Manager, Studio's documents and the Work Center search (403) refuse it; the show gate refuses `configure terminal` (400, no
  job). `verify/test-16-netclaw-dev.sh`: 6 passed, S17.7 skipped (no ServiceNow on dev).
- The container: config valid; `openclaw mcp probe` lists exactly the seven tools; no eligible skill; four plugins load
  (anthropic, memory-core, ollama, slack); every transcript shows exactly the seven tools offered.
- Turns on the twin without Slack: (a) 2 tool calls, 107 s, 13.1k tokens; (b) 2 calls, 53 s, 13.2k; (c) 3 calls, 29 s, 17.9k
  with the card link; (d) one call, dev's refusal reported, no retry, 8.6k; (e) refused with no Itential tool, 8.1k.
- The first Slack round trip (the owner's phone, dev, twin): 2 tool calls, the answer in the thread after 138 s, 15.4k in /
  428 out tokens; no top-level post. A public channel delivers nothing (the app's scopes are private-channel only).

## Amendment A1 (2026-11-01): the AWS VPN from the phone

Owner-approved (delivery `customer-spec.md` and `solution-design.md`, amendment A1): for AutoCon 6, NetClaw may ask for
the AWS VPN from Slack. Approvals stay a person's, in Work Center.

1. **Five more automations, never Deploy's or Tear Down's own.** `automation_access.netclaw` adds `Get AWS VPN Status`,
   `Request AWS VPN Deployment from NetClaw` and `Request AWS VPN Teardown from NetClaw` (decision 2), and
   `Hand Off AWS VPN` and `Verify AWS VPN` through their branded-page endpoints. The status and the two requests get
   endpoints of their own (`get-aws-vpn-status`, `request-aws-vpn-deployment`, `request-aws-vpn-teardown`), on the AWS
   VPN's tier only, removed with the rest by `aws-vpn-retire.yml`. Deploy's and Tear Down's routes stay closed to
   svc-netclaw, so NetClaw never sends or receives the on-prem address: the request workflow reuses it (`last`) inside
   Itential. svc-netclaw can still read the job documents that hold it, as any account can (job reads are platform-wide
   on 6.6.0, measured 2026-10-11 and accepted above), which is why decision 4 withholds it in NetClaw's tools.
2. **NetClaw's request workflows are generated copies of the Cloud Concierge's, marked `NetClaw: `** (owner,
   2026-11-01). One builder makes both pairs (ADR 0085 decision 11); a copy differs only in its name, its note's mark,
   its answer's next step (NetClaw's hand-off, not the branded page) and its description, and a test holds the rest
   equal. The mark is a constant of the copy, never the job's initiator: the import turns every `$var.job.X` a workflow
   reads and never writes into a required input of the agent's tool, which the model would then fill in itself
   (tests/test_skills_lab_workflows.py), so a run could claim any caller.
3. **NetClaw never decides a card, and sees only some.** Its role holds no Work Center claim or complete; S17.7 tries
   both on a real Add Branch VLAN card it can see. A card shows to the groups of the automation whose trigger started
   the run: Hand Off's (netclaw reads it), but not Deploy's or Tear Down's, which the request copies start through those
   workflows' own endpoints. So for those NetClaw names the card and links Work Center and the job; the approver finds
   the card in their own queue.
4. **Nothing a tool returns carries an address.** The AWS VPN outputs hold the on-prem and AWS addresses (Deploy's
   `outcome`, the status `summary`, Hand Off's and Verify's plans; measured on the newest eight jobs of each), so
   NetClaw's tools return named fields only and withhold every global IPv4 address in them.
5. **Reaching the cards from the phone is not this repository's.** The delivery serves the Platform to the owner's
   tailnet from the Mac, switched on only for rehearsals and the demo; no lab route, DNS or certificate authority is
   added, because the lab root CA carries no name constraints and a phone that trusted it would trust it for any site.

S17.8 runs the status and Verify as svc-netclaw and proves each request route's gate refuses a bad input before a job;
the phone proofs (a run rejected at its card, then one full approved cycle) are the delivery's.

## Alternatives considered

- **NetClaw's pyATS (or any device MCP) for device state.** A second copy of the device credentials on NetClaw's host and a
  path to the network that bypasses the orchestrator; NetClaw's own skills steer it toward pushes.
- **itential-mcp bindings** (one endpoint trigger = one named tool): see decision 4; the custom tools keep the same shape
  (one route, one tool, include tags) without the three defects.
- **The lab MCP server (mcp.lab.internal:8000).** 74 tools, logs in as the platform admin, no client authentication.
- **`Ask Platform Guide` for platform-guide.** It ends on a card that waits for Seen; itential-mcp has no Work Center tool, so
  every question would leave a card open. It stays the department bot's door.
- **Plain OpenClaw with our own workspace.** Less to strip, but no longer NetClaw for the AutoCon story; the hardening overlay
  is a candidate upstream "orchestrator-only" profile.
- **tools-01 or k3s for the container.** tools-01 is production's identity host; k3s needs the image, registry and Ansible
  that a first phase does not; the Mac keeps the model one hop away (no oob-gw return path, ADR 0079's measurements).
