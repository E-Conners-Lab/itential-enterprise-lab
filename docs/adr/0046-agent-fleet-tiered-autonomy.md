# 0046 — The agent fleet: five scoped agents with tiered autonomy, a local twin each, one governed write path

- **Status:** accepted
- **Date:** 2026-09-08
- **Related:** ADR 0037 (agents as code), ADR 0040 (`wf-config-push-v1`), ADR 0045 (integration tools), PID S4d.5, memory `work-laptop-agent-fleet`

## Context

PID S4d.5 asks for the fleet the owner built on the work laptop (NetBox source of truth, device ops,
compliance, diagnostics, gated remediation), each on Claude and on a local model, with one acceptance
per agent and every device write behind an approval. After elements 1 to 4 every tool the fleet needs
exists as a governed platform asset: the NetBox and ServiceNow integration operations, the Gateway 5
show commands with parsers, the Configuration Manager compliance plan and its reports, and
`wf-config-push-v1` with its Work Center card. The Tool Registry offers the Configuration Manager
methods as `application:ConfigurationManager:<method>`; `searchCompliancePlanInstances` takes
`searchParams {instanceId | planId | planName}` and `getJSONComplianceReportsByBatch` a `batchId`
(measured). The PDI's `itential.integration` user can write an incident work note and read it back only
through the incident with `sysparm_display_value=true` (measured on INC0010001, closed).

## Decision

- **Five agents in the project `lab-netops`**, one document each under `itential/agents/`, tiered by what
  they may touch; `lab-netops` stays the generalist exposed to MCP clients:
  1. `netbox-sot`: the six NetBox integration reads, nothing else.
  2. `device-ops`: NetBox device lookup, `wf-show-command-v1`, raw `send-command`; confirms the node in
     NetBox before reading it; no configuration tool.
  3. `compliance`: `wf-compliance-run-v1` and the Configuration Manager plan/instance/report tools;
     detection only, no write tool.
  4. `diagnostics`: reads the incident, confirms the devices in NetBox, reads them with
     `wf-show-command-v1`, matches the known root causes written into its instructions (hostname drift,
     missing NTP, missing branch VLAN, a stuck BGP neighbour, an interface shut) and writes **one** work
     note "Proposed fix: ..." with `updateIncident`. The note is the fleet's only ungated write: a ticket,
     not a device, and the pattern the owner built before. It never changes the incident state.
  5. `remediation`: the compliance report tools, the NetBox lookup and `wf-config-push-v1` as its only
     write; it proposes a fix only for a single-line drift (hostname, the NTP line) and starts the push
     once; the Work Center card gates the device.
- **A local twin per agent** (`<name>-local`, profile `ollama-lab`, qwen2.5:7b on CPU) with a trimmed
  tool set of one to three tools, because the 7B model loses its way with more.
- **`tasks/flowai-agent.yml`** gains `kind: application` with `app`, resolved to
  `application:<App>:<method>`.
- **Verification** in `verify/test-06-flowai.sh` (S4d.5, one check per agent on Claude, tokens printed):
  netbox-sot counts the active devices of a site against NetBox; device-ops reports br2-sw01's version
  against direct SSH; compliance runs lab-baseline and reports it clean against the batch reports read
  directly; diagnostics takes an incident created by the verify about a deliberate hostname drift on
  br2-sw01 (pushed and later restored through `wf-config-push-v1` with the verify's approvals), and its work
  note read back from the PDI names the drift and proposes `hostname br2-sw01`; remediation, asked to fix
  that drift, starts `wf-config-push-v1` with exactly that line and the verify approves the card, direct SSH
  confirming the restore; then one local twin (netbox-sot-local) answers a site question, with its response
  time and the VM memory recorded as in S4c.5. The verify's incident is closed at the end.

## Lessons carried from the owner's work-laptop build (memory `itential-platform-lessons`)

- Ollama's tool grammar rejects names with `@`, `:` or spaces, which is what a Studio-project workflow tool
  gets; every workflow the local twins call stays a global import (`wf-*`, tool `workflow:<uuid>`).
- A schema property whose value is the bare JSON literal `true` makes Ollama reject the whole request:
  the generated Integration Model documents carry none (`itential/integrations/build.py`).
- Small models fatigue after roughly fifteen tool calls, and a local generalist asked about "all
  devices" invented node names (`node-1`, `node-2`) and gave up (measured 2026-09-08): a local twin gets
  one to three tools, and multi-device work is one deterministic tool, `wf-show-all-v1` (the lab device
  list, one multi-node Gateway 5 `send-command`, one parse on the runner keyed by device, 1500 characters
  per device), never a loop inside the model. The local Ollama runs with a 16k context so that result fits.
- Session message reads truncate at about eleven messages by default: the verify scripts pass an
  explicit limit before judging a session's tools or tokens.
- The compliance agent's first version read the raw report tools and spent 638k input tokens polling;
  `wf-compliance-report-v1` (run or read the plan, wait on the platform, one compact summary per device
  reduced on the Gateway 5 runner) replaces them for the compliance and remediation agents. The
  `getJSONComplianceReportsByBatch` tool returned empty lists on 6.5.2 while `getComplianceReportsByBatch`
  returns the reports, and the instance search pages at ten unsorted instances unless `searchParams` carries
  `sort {started: -1}` and a `limit` (both measured in the first fleet runs). A workflow tool whose job ends
  in error leaves the agent session running until the job and the session are cancelled.

## Consequences

- Six more agents (five plus the generalist already present) and five local twins in one project; every
  session records its token usage; the Claude acceptance run spends roughly 100k input tokens.
- A device is changed by the fleet only through `wf-config-push-v1` and a human approval; the compliance
  and diagnostics agents hold no device-writing tool at all, so a prompt injection through a ticket or a
  device banner can at most produce a wrong note or a wrong proposal.
- Rejected: one agent with every tool (no tiering, larger prompts, weaker refusals); Lifecycle Manager
  `resourceAction` tools for the fleet (the branch VLAN service stays with `lab-netops` through
  `wf-branch-vlan-v1`); a knowledge base as a separate tool (five root causes fit the instructions).

## Lessons carried over from the owner's work-laptop fleet (IAG_DEMO, snapshot 2026-09-07)

The laptop build of the same five agents (Arista cEOS, Platform 6.5.1, Gateway 4 + 5, Claude and
Ollama qwen3:30b-a3b) found constraints this lab has not hit yet. They bind the fleet and its local twins:

- **Workflows used as tools stay global.** A Studio-*project* workflow is registered under the name
  `@{projectId}: {workflow name}`; Ollama's tool grammar rejects `@`, `:` and spaces and the model ends the
  turn with no tool call and no error. Decorators change a tool's description and schema, never its name.
  The `wf-*` workflows are global Automation Studio imports (tool id `workflow:<uuid>`) and the twins call
  them (S4c.5 measured); no workflow a `-local` agent references may move into a Studio project.
- **Every tool schema an Ollama profile sees has object-shaped properties.** One property whose value is
  the bare literal `true` makes Ollama reject the whole request, taking every other tool with it. The
  generated Integration Model documents (`itential/integrations/build.py`) and any decorator are checked
  for value shape, not only for property count.
- **Fleet-wide questions fan out outside the model on the local profiles.** qwen3:30b-a3b abandoned a
  five-device compliance check after ~15 tool calls (16 minutes, no report, 20k of 262k context used);
  Claude ran the equivalent in one session in 19 seconds. The twins keep one to three tools and one device
  per question; a fleet result for a local model is a script calling the single-device twin per device.
  Multi-device tolerance is measured per provider, never assumed.
- **A session is read with `limit=100` before it is judged empty.** `describe_session` and the messages
  route return about eleven messages by default with no continuation marker; on the laptop this
  masqueraded as a silent-turn-drop bug twice. `verify/test-06-flowai.sh` reads sessions this way.
- **Integration Model tools that never appear are a role gap first.** The laptop abandoned an OpenAPI
  model after six fixes because Tool Registry never listed its operations; this lab's role re-sync
  (ADR 0045) is the missing step there. Only after the roles are proven is a Gateway 5 Python Script
  Service the fallback.
- **Arista structured output has no parser-free path except eAPI.** Genie has no EOS parsers;
  ntc-templates breaks on the current `show ip bgp summary` (new `PfxAdv` column) and omits interface
  error counters. The laptop enabled eAPI on every switch and served `/command-api` JSON-RPC from one
  Gateway 5 Python script registered as several purpose-named services (an env var per service selects
  the command). The vEOS side of `wf-show-command-v1` (TextFSM, ADR 0038) carries the regex risk; eAPI is
  the candidate replacement when a twin needs interface or BGP data.
- **Risky tools are scoped by construction.** Generic runners (`send-command`, `set-config`) are absent
  from an agent, or their service name is pinned by a decorator enum; instructions alone never scope a tool.
