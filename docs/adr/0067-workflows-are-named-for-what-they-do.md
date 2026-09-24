# 0067 — Workflows are named for what they do

- **Status:** accepted (owner decision, 2026-09-24)
- **Date:** 2026-09-24
- **Amends:** PID 1.33; every workflow name set since Phase 5 (ADR 0035 onward)
- **Related:** ADR 0043 (Lifecycle Manager actions name their workflows), ADR 0046 (the agent fleet and its
  workflow tools), ADR 0060/0061 (the local model), ADR 0066 (the failure paths of the same workflows)

## Context

The lab's eleven workflows were named like code identifiers: `wf-branch-vlan-v1`, `wf-config-push-v1`,
`wf-compliance-report-v1`. That is the name an operator sees in Operations Manager, the name on every job, and the
name an agent calls as a tool. An operator reading `wf-branch-vlan-v1` in a job list has to know the lab's
conventions to learn that it adds a VLAN to a branch, and the `-v1` suffix promised a versioning scheme the lab
never used: every change replaced the document in place, and git holds the history.

The asset packs built to Itential's conventions name workflows for what they do: the owner's NX-OS pack for itential/assets ships
"Port Turn Up", "NX-OS Upgrade" and "Run Compliance".

One risk had to be measured before names with spaces were adopted. A lab note from the owner's earlier build
records that the local model could not call a workflow tool whose name contained spaces, `@` or `:`; the model
reasoned in circles and never made the call. That note concerned Studio *project* workflows, whose tool names the
Platform builds as `@<projectId>: <name>`. This lab's workflows are global imports.

**Measured on the dev tier (2026-09-24, Platform 6.5.2, `gemma4:26b` on the Mac Mini):** a copy of the show-version
workflow imported as "Get Device Software Version" and given to a scratch agent on the `ollama-mac` profile. The
model called the tool by that name with the right device, the job completed, and the agent answered `4.33.1.1F`
(the running version of `clab-sw1`) in every run that the network let finish. The session records the tool by the
workflow's name, spaces included. (The runs that did not finish were cut by a home-network fault found the same
day and fixed on the Proxmox bridge; the control agent with the old names failed the same way.)

The agents already reference workflows by id (`workflow:<uuid>`, the name kept as `lastKnownName`), and the play
re-resolves every agent tool by name on each replay, removing tools that no longer resolve.

## Decision

1. **A workflow's name is a verb and its object, in Title Case**, in the words an operator would use:
   "Add Branch VLAN", "Push Configuration with Approval". Words are letters and digits separated by single spaces;
   at most 64 characters, since the name becomes an agent's tool name.
2. **No `wf-` prefix and no version.** Git and the replay carry the version; the name says what the workflow does.
3. **The name is written once**, in `itential/versions.yaml` under `workflows:`; the generator, the plays, the
   verify scripts and the tests read it from there. The key (`branch_vlan`, `config_push`, …) stays the stable
   handle in code.
4. **The file is the name in lowercase with dashes** (`add-branch-vlan.json`). The play reads every `*.json` in
   `itential/workflows/` and deletes a previous copy by the name inside the document, URL-encoded.
5. **A description says what the workflow does**, not a restatement of the name.
6. **Retired names are recorded, deleted and never reused.** `retired_workflows` in `versions.yaml` lists the old
   names; the play deletes each one it still finds after importing the successors. The old names remain only in
   the record: ADRs, handoffs, verify logs, and the PID's amendment history.
7. **`tests/test_workflow_names.py` enforces 1-6.** The first word must be one of a listed set of verbs; a new
   workflow with a new verb adds it to that set on purpose, in review.

The rename:

| Before | After |
|---|---|
| `wf-netbox-device-count-v1` | Count Devices in NetBox |
| `wf-show-version-v1` | Get Device Software Version |
| `wf-show-command-v1` | Run Show Command on a Device |
| `wf-show-all-v1` | Run Show Command on All Devices |
| `wf-config-push-v1` | Push Configuration with Approval |
| `wf-branch-vlan-v1` | Add Branch VLAN |
| `wf-branch-vlan-delete-v1` | Remove Branch VLAN |
| `wf-compliance-run-v1` | Run Nightly Compliance Check |
| `wf-compliance-report-v1` | Summarize Compliance Results |
| `wf-backup-all-v1` | Back Up All Device Configs |
| `wf-netbox-devices-v1` | List Devices from NetBox |

## Consequences

- The agents' instructions name their tools by the new names, quoted where the model reads them as prose.
- Job history before the rename stays under the old names; Operations Manager does not rename past jobs.
- Shell code that passes a name must quote it: `run_job "Add Branch VLAN" …`. The verify scripts do, and
  `verify/test-08-platform-ha2.sh` reads the names one per line.
- Checks that matched a tool list against a fragment of an old name (`config-push`, `wf-show-command`, `wf-`)
  match the new names; the S4c.4 refusal check builds its pattern from `versions.yaml`.
- The Lifecycle Manager model's actions follow the new names: the play replaces the model when an action's
  workflow changes and validates that every action is runnable.
- The first replay on each tier deletes the eleven old workflows. On the dev tier that was approved on
  2026-09-24; production waits for this change's review and its own approval.
