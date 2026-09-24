# 06 — Platform applications

Golden Config and compliance, MOP command templates and nightly backups, Lifecycle Manager with a JSON form
as the approval, Integration Models as typed tools, and a fleet of FlowAI agents with tiered autonomy and
local twins.

Everything here is built through the API from documents in the repo, against the twelve devices chapter 04
built. Nothing is drawn in a UI, and every re-run updates in place.

> **Before reverse-engineering any FlowAI API, read the `itential-builder:flowagent` skill.** It documents
> the Agent Project Service, Model Registry Service, Tools Service and Agent Session Manager - request
> shapes, the create/update field asymmetries, and endpoints like
> `GET /model-registry-service/profiles/{id}/agent-impact` that are easy to miss. Probing these services by
> hand instead produced a confident, wrong, *written-down* conclusion here on 2026-09-11 ("profiles are
> create-only"), a needlessly destructive fix built on it, and six broken agents while correcting it. The
> skill is a map rather than a guarantee, so still verify against the live API - but start from the map.

---

## Before you start

### What must already be true

- Chapter 05 green: the Platform is up, Gateway 5 is connected with its runner registered, the inventory
  `lab` has twelve nodes, and the three base workflows run.
- Chapter 03's enrichment has run. Golden Config's device leaves render **from NetBox** — interface
  descriptions, VRFs and addresses — so an un-enriched NetBox gives you a tree with nothing in it.
- `ANTHROPIC_API_KEY` in `.env` if you want the hosted agents. The local Ollama twins need nothing.

### The order, and why it is fixed

```
itential.yml  →  platform.yml  →  flowai.yml
```

`itential.yml` re-imports every workflow, which changes their UUIDs. `platform.yml` builds the
applications that reference those workflows. `flowai.yml` re-resolves the agents' tool references, which
are UUID-bound. Run them out of order and the agents hold references to workflows that no longer exist —
and they fail at session time, not at build time. `make phase-flowai` runs all three in order.

### Six elements

| | Element | ADR |
|---|---|---|
| 1 | Device groups, Golden Config trees, the compliance plan and its nightly schedule | [0040](../adr/0040-golden-config-compliance-device-groups.md), [0041](../adr/0041-iosxe-hostname-defect-and-governed-fix.md) |
| 2 | MOP command and analytic templates, nightly backups | [0042](../adr/0042-command-templates-nightly-backups.md) |
| 3 | Lifecycle Manager model, its JSON form approval, and the governed delete | [0043](../adr/0043-lifecycle-manager-branch-vlan.md), [0044](../adr/0044-json-form-is-the-approval-task.md) |
| 4 | Integration Models for NetBox and ServiceNow as agent tools | [0045](../adr/0045-integration-models-netbox-servicenow.md) |
| 5 | The agent fleet with tiered autonomy and local twins | [0046](../adr/0046-agent-fleet-tiered-autonomy.md), [0037](../adr/0037-flowai-next-local-llm-alongside-claude.md) |
| 6 | The Ubuntu hosts as gateway inventory nodes, in their own inventory | [0047](../adr/0047-ubuntu-hosts-gateway5-inventory.md) |

### One write path

`Push Configuration with Approval` is the **only** workflow that writes to a device. It takes a device, the lines and a
reason, raises a Work Center approval card, pushes through the gateway and writes memory. Golden Config
never remediates on its own; the remediation agent's only write tool is this workflow; the lifecycle
delete runs it as a child job. That single choke point is what makes "an agent changed a device" a
sentence with an approval attached to it.

---

## The commands, in order

```
make phase-flowai
```

Three plays. `ansible/playbooks/platform.yml` is the substantial one and is organised as the six elements
above:

**Element 1 — Golden Config.** Device groups `site-*` and `role-*` derived from the NetBox tags on the
inventory nodes. One tree per OS (`lab-cisco-ios`, `lab-arista-eos`) with `base` → `<site>` → `<device>`:
the base node is the OS baseline, the site node carries the site group, and the device leaf is **NetBox
intent** rendered from `itential/golden-config/<deviceType>/device.j2`. All twelve devices attached. A
compliance plan `lab-baseline` with one node per device leaf, and an Operations Manager schedule trigger
at 03:00 UTC daily.

**Element 2 — MOP.** Command templates `lab-<os>-checks` (show version, interfaces up, BGP neighbours;
every rule an error) and analytic templates `lab-<os>-prepost` that compare version and management/loopback
address before and after a change. Plus `Back Up All Device Configs` — a filtered device list into a `forEach` that
backs each one up through the broker — on a nightly schedule at 02:30 UTC.

**Element 3 — Lifecycle Manager.** A `branch-vlan` resource model whose Create and Delete actions name
`Add Branch VLAN` and `Remove Branch VLAN`, a generated JSON form
`lab-branch-vlan-approval.json`, and one instance imported per NetBox branch VLAN. The form *is* the
approval task: `ShowJsonForm` shows the branch, VID, name, switch and NetBox reservation read-only, plus a
decision field defaulting to reject.

**Element 4 — Integration Models.** `lab-netbox` and `lab-servicenow`, generated by
`itential/integrations/build.py` from hand-selected operations rather than from the vendors' full OpenAPI
documents — NetBox's own schema is 322 paths and 14 MB, which would have produced hundreds of tools. The
play creates the models, the instances `netbox-api` and `servicenow-api`, writes their credentials from
`.env`, **re-syncs the roles the models register onto the admin group and the LDAP admin**, runs tool
discovery and asserts every operation is an authorized tool.

**Elements 5 and 6** are `ansible/playbooks/flowai.yml` and the host inventory in
`ansible/playbooks/itential.yml`. The fleet:

| Agent | Tools | Tier |
|---|---|---|
| `netbox-sot` | NetBox reads through the Integration Model | Read only |
| `device-ops` | The show-command workflows | Read only |
| `compliance` | `Summarize Compliance Results` | Read only |
| `diagnostics` | Reads, plus **one** write: a "Proposed fix" work note on an incident | Suggest |
| `remediation` | Reads, plus `Push Configuration with Approval` behind the Work Center card | Act, gated |

Each has an `ollama-lab` twin with one to three tools, and the twins are given **reducing workflows
rather than raw tools**, for two separately measured reasons: a small model handed the raw gateway tool
invented node names, and one handed the raw `dcim_devices_list` overran the Platform's inference timeout
on 52-field device objects (see Troubleshooting). So the twins read inventory through
`List Devices from NetBox`, which returns six fields per device.

---

## What "done" looks like

`platform.yml` is idempotent and a second run reports `changed=0` (element 4 is the exception: the
credential write always reports changed). A full run is tens of minutes, most of it the compliance plan
evaluating twelve devices.

- Golden Config: `lab-baseline` runs clean on all twelve devices. Introduce a hostname drift on two of them
  and the plan flags exactly those two, with no false positive; push the fix through `Push Configuration with Approval`
  and it is clean again.
- MOP: both command templates evaluate all six rules on a router and a switch; the analytic pre/post
  comparison is equal on both; a backup run produces twelve backups equal to the devices' running
  configurations.
- Lifecycle Manager: creating an instance raises a Work Center card showing the branch, VID, name, switch
  and NetBox reservation; approving it puts the VLAN on the switch and the instance active; rejecting it
  changes nothing and rolls the reservation back; deleting it removes the VLAN through an approval card
  and retires the instance.
- Integrations: eleven authorized tools across the two models, and a Claude agent answers a device
  question through `dcim_devices_list` and an incident question through `listIncidents` — **never**
  through an adapter method. The `ollama-lab` twins reach the same data through `List Devices from NetBox`,
  which calls that operation once on the runner and reduces the result.
- Agents: each agent in the fleet answers its own question, and the answer matches the second source. The
  compliance agent's run costs about 3.5k input tokens through the summary workflow.

---

## Verification

Two scripts, because the applications and the agents are separately provable:

```
verify/test-06b-platform.sh        # the applications
verify/test-06-flowai.sh           # the agents
```

| Criterion | Script | What a PASS means |
|---|---|---|
| S4d.1 | `verify/test-06b-platform.sh` | Golden Config plan clean on twelve devices; a deliberate hostname drift on two flagged with no false positive; restored through the governed push |
| S4d.2 | `verify/test-06b-platform.sh` | Command templates with every rule evaluated, analytic pre/post green, the nightly schedule present, and a run whose backups equal the running configurations |
| S4d.3 | `verify/test-06b-platform.sh` | The lifecycle create → form approval → instance → governed delete path, end to end, with direct SSH confirming the switch |
| S4d.4 | `verify/test-06b-platform.sh` | Both Integration Models with their declared operations, both instances, every operation an authorized tool, and an agent reading through them |
| S4d.6 | `verify/test-06b-platform.sh` | The three Ubuntu hosts are inventory nodes reachable through the gateway, **and unknown to Configuration Manager** |
| S4c.1 – S4c.7 | `verify/test-06-flowai.sh` | Provider profiles answer; the agent reads devices through the gateway tool; it makes a governed VLAN change with approval; it **refuses** a device not in the inventory without touching the gateway; the local twin answers; an MCP client drives a session; structured parsing works on both vendors |
| S4d.5, S4d.5a – S4d.5g | `verify/test-06-flowai.sh` | One criterion per fleet agent, each cross-checked against direct SSH, the NetBox API or the PDI |

Both scripts call `verify/tokens.sh --check` before starting an Anthropic session and **refuse to run**
once the week's budget is spent ([ADR 0049](../adr/0049-anthropic-key-budget.md)). `make tokens` prints the
current spend. `ANTHROPIC_VERIFY=force` overrides it deliberately.

---

## Troubleshooting

**An agent's tools resolve to nothing after a workflow change.** Tool references are UUID-bound and a
re-import gives a workflow a new UUID. Always run `itential.yml` → `platform.yml` → `flowai.yml` in that
order; `make phase-flowai` does. The failure appears at session time as a tool the model cannot call, not
at build time.

Know the symptom, because it does not look like a broken reference: the session ends `FAILED`
**within about two seconds**, having spent **zero tokens**, with one `inference-succeeded` message and
then a `tool-execution` that never returns. The agent said what it was about to do and then called a
tool that is not there. A zero-token failure is the tell — a session that genuinely tried and failed
costs tokens. This bites hardest when you import a single workflow **by hand** to test a change, which
is exactly when you are least likely to think about re-running the agent play afterwards.

**You changed only a prompt and do not want to re-run the play.** `make agents-push` (or
`CHECK=1 make agents-push` to diff first) PATCHes `prompt.instructions` on the agents that already
exist, from `itential/agents/*.yaml`, touching no tool binding, provider or operator. It reads the
prompt back afterwards, because the PATCH answering 200 is not proof the prompt stored. It cannot
create an agent - that needs the resolved tool ids - and after any workflow import the play is still
the path, for the uuid reason above. The MCP server is no help here: it can read agents but exposes
no agent-update tool.

**An Integration Model will not update.** `PUT /integration-models` answers 500 on 6.5.2, and the
documentation says to delete and re-import. The play compares the export's paths against the document and,
when they differ, removes the integration, deletes and re-creates the model, then re-creates the
integration. Do not try to patch one in place.

**An integration's tools are all "not authorized".** An Integration Model registers **roles**, and those
roles have to be re-synced onto the admin group and the LDAP admin. Without that the tools are simply
hidden from the agent, which then reports it has no such tool. The play re-syncs on every run for exactly
this reason.

**Starting an integration turns it `STOPPED` while it still works.** An integration is a *virtual* adapter,
not a process. `PUT /adapters/<name>/start` fails and flips its health to `STOPPED` while every operation
continues to run. Do not start one, and do not read that health field as truth.

**A workflow task cannot call an integration operation.** The task addresses it with `app` =
`<title>:<version>` and `adapter_id` = `<instance>`. That is different from the adapter contract in
chapter 05 (model by export, instance by `adapter_id`), and mixing them up gives a validation failure that
does not say which half is wrong. The Platform's own OpenAPI document is at
`GET /help/openapi?url=<base>`.

**Ollama rejects a request and the local twin never answers.** A tool schema carrying a bare `true`
literal is rejected outright. The integration documents drop it. Also set `OLLAMA_CONTEXT_LENGTH` high
enough for the payload — twelve parsed devices needs 16384 — or the model silently truncates its input and
answers about the devices it happened to see.

**A tool call fails `invalid_input` before it reaches the external system.** Two causes, both in the
prompt rather than the model. First, **a filter passed as a list**: S4f turned every NetBox filter from
an array into a single value, because a workflow's `$var` does not resolve *inside* an array (ADR 0054),
and the agent prompts went on teaching `name ["br1-sw01"]` for another day. Second, **a filter set to
`null`**: a 7B model fills in every property the schema declares, and `null` is not a string, so the
Platform rejects the call with `[invalid-tool-input] Input validation failed` and an `input` block full
of nulls. Say in the prompt that each filter is one plain value and that an unused filter is left out of
the call entirely. `tests/test_agent_fleet.py` now compares every prompt's filter examples against the
parameter types in `itential/integrations/*.json`, so the array form cannot come back.

**An agent session runs for ever and no job is running.** Look for a job that *errored* with
`Job has no available transitions. <task> could have led to the workflow end task, but did not.` A
workflow task with no failure edge dead-ends, and a dead-ended job never returns a result to the agent
that called it: the session stays `RUNNING` indefinitely. Measured 2026-09-11: `device-ops-local`
invented the device `R1`, Gateway 5 answered `404 Missing nodes - Inventory 'lab': [R1]`, the job
errored in 69 s and the session was still running eighteen minutes later. Because Ollama runs
`OLLAMA_NUM_PARALLEL=1`, that one session held the only local inference slot the whole time — every
other twin queues behind it. Distinguish it from the stale-UUID failure above: that one **ends** in ~2 s
having spent zero tokens; this one never ends at all. `Run Show Command on a Device` and `Push Configuration with Approval` now
publish `device_error` and reach their end (ADR 0059). **The transition state matters:** a Gateway task
that 404s lands in state `error`, and a `failure` edge does *not* fire for it — a correct-looking
`failure` edge still produced "5a could have led to the workflow end task, but did not". Use
`{"state": "error"}` for a Gateway task, and keep `failure` for a task that *completes* unsuccessfully
(an `evaluate`, or a rejected approval form). `tests/test_agent_fleet.py` fails a device-sending task
that has no `error` edge reaching `workflow_end`.

**A twin ignores an instruction in its prompt.** Check it *can* obey. `device-ops-local` was told "you
never invent a device name" while holding only the two show-command workflows — nothing that returns a
real name — so the instruction was unfollowable and the model invented one. `Run Show Command on a Device` takes
`device` as free text, so the workflow does not constrain the name either. Pair any such prohibition
with a tool that supplies the values: that twin now holds `List Devices from NetBox`. Note that a static
audit of all thirteen documents passed clean while this was broken — tools resolved, no prompt named a
tool its agent lacked, every name cited existed in NetBox. This class of defect is behavioural, and only
running the agent finds it, which is why every `ollama-lab` twin now runs in the verify (S4d.5h-k).

**A twin says something "is not in NetBox" when it simply has no tool for it.** Two different answers
wearing the same words, and the wrong one is stated with full confidence: measured 2026-09-12,
`netbox-sot-local` answered "Interfaces on br1-sw01: not in NetBox" when the interfaces are in NetBox
and it just had no `dcim_interfaces_list`. An operator reads that as "this switch has no interfaces
configured". Its prompt had offered exactly one way to say no, so it used that one. Every twin prompt
now reserves "not in NetBox" for a tool that ran and returned nothing, and otherwise names the agent
that does have the tool. When you see a confident absence, check the agent's tool list before believing
it.

**Do not trust the twins' tool cap as a law.** It was three, justified as "a 7B model loses its way with
more" - true of `qwen2.5:7b` on CPU, not of what runs now. Re-measured on `gemma4:26b` (ADR 0062): two
tools to four made tool *selection* better, not worse - the same six-clause question went from one
wasted call and a false answer to 6/6 with no waste. The cap is now six and exists for a different
reason: tool schemas cost input tokens on every request (that question went 10.5k to 18.6k), and an
unbounded list is how a twin ends up holding the wide NetBox reads that caused the timeout above. Give a
twin the tools its job needs, leave `dcim_devices_retrieve` and the other wide reads to the Claude
agent, and if you want the cap raised, measure first.

**A local twin invents node names.** That is why the twins do not get the raw gateway tool. A small model
handed an unconstrained `sendCommand` will confidently make up a hostname; handed a workflow that takes a
device from a fixed inventory, it cannot.

**A local twin fails with "ollama model invocation failed" while Ollama is working fine.** That
string is the *Platform's* inference timeout, not an error from Ollama. Check Ollama's own log before
believing it: measured 2026-09-11, `dcim_devices_list` handed straight to `netbox-sot-local` returned
five br1 devices of 52 fields each (17.9 kB), the prompt reached **7,823 tokens**, and qwen2.5:7b on
the four CPU cores of tools-01 ingests at **~22 tokens/sec** - about 350 s. The Platform gave up, marked
the session FAILED and **deleted the session record** (so `GET /sessions/<id>` 404s for a session the
list endpoint just showed you), while llama-server finished the same request successfully at 16:55:13,
`truncated = 0`. The fix is the same one as the entry below: reduce the data before it reaches the
model. `List Devices from NetBox` reads the list once and hands back six fields per device - br1 goes from
17.9 kB to 730 bytes - and the twins hold that workflow instead of the raw operation. The Claude agents
keep the raw operations: they need the full objects and ingest them in a second.

**A local twin is slow and its answer starts with its own reasoning.** The model is thinking, and you
are paying for every trace in wall-clock. Measured 2026-09-11 on the answer turn (model reads a tool
result, writes the reply): thinking costs **6-8x** - `gemma4:26b` goes 9.9 s/137 tokens to
1.6 s/39. There are three ways to switch it off and only one is available here: Ollama's API
`think: false` works but **the Platform does not send it**; `PARAMETER think false` is **not a
Modelfile parameter** (`Error: unknown parameter 'think'`); and `/no_think` in the prompt is obeyed by
**gemma only** - the Qwen family ignores it (`qwen3:8b` stayed at 703 thinking characters,
`qwen3:30b-a3b` at 1175). So the prompt decides the model, not the other way round: that is why the
twins run `gemma4:26b` with `/no_think` as the **first** line (ADR 0061). Re-take the decision by
re-running `scripts/model-bakeoff.py`, which scores candidates on the tool-call shapes that caused real
incidents here rather than on a leaderboard.

**A change to a Model Registry profile does not reach the Platform.** The profile *is* updatable, but
only through the documented shape: `PATCH /model-registry-service/profiles/<id>` with the body wrapped
in `{"update": {...}}` and every `models[]` entry carrying **both** `name` and `enabled` (create takes
`name` alone). Any other shape - `{"profile": {...}}`, which is the *create* wrapper, or a bare
`{"models": [...]}` - answers **200 and is silently ignored**, which reads exactly like "profiles cannot
be updated" (it did to me, 2026-09-11, and that wrong conclusion reached an ADR). **`itential-builder:flowagent`
documents this service - read it before probing the API by hand.**

**Updating a profile's models orphans every agent bound to it.** Rewriting `models[]` **reissues each
model's UUID**, and an agent's `provider.model` *is* that UUID: measured 2026-09-11, one profile PATCH
took `agentCount` from **6 to 0** and every twin stopped resolving until re-bound. Always re-resolve the
model ids and re-PATCH each agent's provider afterwards (the play does). Before deleting a profile
instead, call `GET /model-registry-service/profiles/{id}/agent-impact` - deletion is an irreversible
hard delete. So a base URL or a pinned model edited in
`itential/versions.yaml` stays whatever the profile was created with, and the symptom is not an error:
ADR 0060's move to `ollama.lab.internal` sat unapplied for a day while the profile held the raw address
it was born with, and the lab worked because that address happened to still be right. **Check the
profile document, not DNS resolution**, when you want to know what the Platform is using. The play now
compares each live profile against the repo and deletes the drifted ones so the create step rebuilds
them; before that it could not change a model at all - the failure was
`model_ids ... No first item, sequence was empty`, which names nothing useful.

**An agent burns an enormous number of tokens.** Measured: the raw compliance-report tools cost the
compliance agent **638k input tokens** in one session. `Summarize Compliance Results` reduces the reports on the
runner to one compact summary per device, and the same question then costs **3.5k**. When an agent is
expensive, the fix is almost always a workflow that reduces the data before it reaches the model, not a
better prompt.

**A compliance report search returns the wrong instance.** The plan-instance search pages at ten and is
unsorted. Sort by `started` descending with a limit of 100.

**A schedule trigger loses its inputs.** 6.5.2 schedule triggers do not persist `formData`, so a scheduled
workflow cannot be given parameters. `Run Nightly Compliance Check` finds its plan **by name** instead.

**A command template rule never matches.** Three measured facts about 6.5.2: RegEx rules are **bare
patterns** — a `/.../ ` wrapper never matches anything; `ignoreWarnings` is not stored; and analytic rules
need type `regex`, because `matches` does not compare captures. Also note that an analytic template create
wraps its body in `{template}`.

**The BGP rule fails on the branch switches.** They are L2-only and have no BGP. The rule passes them
through `% BGP inactive`; a genuinely stuck peer still fails everywhere. If you add an L3 switch, check
that this rule still means what you want.

**A lifecycle action stays in error and will not retry.** A job in error is retryable, so an errored action
waits until its **execution is cancelled**. Cancelling a create's execution retires its instance. The
lifecycle model runs its action workflow as a child of a `resource:action` wrapper job, so there are two
jobs to look at, not one. An instance import is the **bare document**, not a wrapper.

**Nothing appears in Work Center.** The `ShowJsonForm` task is at `/json-forms/task/ShowJsonForm`, the form
is addressed **by name**, and the decision comes back on `export`. The Work Center API returns the resolved
card fields, so that is the place to check what an approver is actually seeing.

**Five routers are running as `hostname Router`.** This happened here, from Phase 4, and nothing noticed
for two phases: the `config.iso` bootstrap never applied the hostname or the `ntp server vrf MGMT` line,
and the chapter 04 verification never checked either. Golden Config found it the first time it ran. It was
restored through `Push Configuration with Approval` with approvals — no wipe, no reboot. The lesson is the general one:
a verification only proves what it checks.

**An MCP client can read device passwords.** `describe_inventory` and Configuration Manager's
`get_devices` return `itential_password` in cleartext to any MCP client. The interim control is to exclude
both tools by tag (`ITENTIAL_MCP_SERVER_EXCLUDE_TAGS` in the Compose override); `run_command`,
`get_device_configuration`, `trigger_automation` and the workflows stay. This is checked by the tests and by
chapter 05's `verify/test-05-itential.sh` S4.7. It is an open exception until credential references land.

**A `runCode` task fails on its input.** Its `data` must be an **object**. And when a verify approves a
Work Center card raised by an agent session, it has to approve it **while the session is running** — an
approval after the session ends approves a card nobody is waiting on.

**An Ubuntu host shows up in Configuration Manager.** It should not. The hosts live in their own inventory
`lab-hosts` with no broker actions, and the `InventoryBroker` publishes only `lab`. If a host appears in
Configuration Manager, the broker's inventory list has grown. Note also that gateway node attributes are
`itential_host/port/driver/platform/user/password/become/become_password/driver_options` — there is **no
key attribute**, so those nodes need password login enabled for the automation user only.

---

## Tested versions

| Component | Version |
|---|---|
| Itential Platform | 6.5.2 |
| Gateway 5 | 5.5.2-amd64, distributed execution with an etcd v3.5.21 store |
| Gateway 5 runner | Built on the VM from `python:3.12.14-slim-trixie` + the pinned `iagctl` binary — the stock Alpine/Python 3.14 image cannot `pip install` Cisco pyATS |
| Genie / pyATS | 26.8 (Cisco parsing) |
| TextFSM / ntc-templates | 2.1.0 / 9.2.0 (Arista parsing) |
| Ollama | 0.33.3, `qwen2.5:7b` in-lab on CPU, 10 GB limit, loopback only |
| Anthropic model | `claude-sonnet-5`, budget-guarded at $15/week |
| `adapter-netbox` | v1.0.10 — the only npm adapter left, because the `InventoryBroker` consumes an adapter (ADR 0039) |
| Integration Models | `lab-netbox:1.0.0` (15 operations), `lab-servicenow:1.0.0` (8 operations) — both grew write operations when S4f converted the workflows |

> **Done since this chapter was written.** [ADR 0054](../adr/0054-integrations-over-adapters.md) made
> Integration Models built from OpenAPI the default and keeps an npm adapter only where the Platform itself
> requires one. That conversion has run: **no workflow holds an adapter task or a `genericAdapterRequest`
> any more**, `adapter-servicenow` is gone entirely, and `adapter-netbox` stays only because the
> `InventoryBroker` consumes an adapter rather than an integration (ADR 0039). The journal entries moved too
> — `extras_journal_entries_create` replaced the Python on the Gateway 5 runner that ADR 0048 needed only
> because the adapter dropped a trailing slash. Chapter 05 still installs `adapter-netbox`; it no longer
> installs `adapter-servicenow`.

---

**Previous:** [05 — The Itential dev stack](05-itential-dev-stack.md) · **Next:** [07 — Observability](07-observability.md)
