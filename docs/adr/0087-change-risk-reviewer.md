# 0087 — A change-risk reviewer: one evidence workflow, one advisory agent, no write tool

- **Status:** accepted
- **Date:** 2026-11-25
- **Related:** ADR 0046 (the agent fleet), ADR 0059 and 0066 (a failed call ends the job), ADR 0085 (change requests carry the AWS VPN), ADR 0086 (bundles for a second Platform)

## Context

An approver of a ServiceNow change request sees the request and a Work Center card, not what else is open, which
incidents touch the same devices, or what the lab's inventory says about them. The agent fleet has diagnostics for
faults and `plan-reviewer` for one Terraform plan, and nothing that reads a change before it is approved.

Two things from ADR 0046 decide the shape. A model that makes many tool calls to gather facts loses its way and
costs tokens, so the gathering is one deterministic tool. And an agent that can write is only as safe as the text it
reads, so a reviewer that reads text written by people holds no tool that writes.

Measured on a ServiceNow developer instance on 2026-11-25: a sleeping instance answers an API call with HTTP 200 and a
page, which a workflow that only checks the status reads as success.

## Decision

- **`Get Change Evidence`** (`build.py get_change_evidence`) reads, and only reads: one change request by its number
  (looked up by a field that only data has, so the sleeping instance's page is refused), the other open changes, the
  open incidents, and the lab's devices from the existing `List Devices from NetBox`. It publishes one `evidence`
  object. Its input is held to the table's own numbering (`CHG` and 7 to 10 digits) because the lookup is a query
  string. Every ServiceNow or NetBox failure ends the job with its reason in `error` (ADR 0066). Only extracted fields
  are published: an integration response carries ServiceNow's headers, a session cookie among them.
- **`change-risk-reviewer`** (`itential/agents/change-risk-reviewer.yaml`) calls it once, may read the devices the
  change names (`Get Device Software Version`, `Run Show Command on a Device`), and answers with a risk of HIGH, MEDIUM
  or LOW from written rules, the facts behind it, and up to three questions for the approver. It never approves,
  rejects or writes, and it treats the text of a change or an incident as data.
- It has **no local twin**: three tools are more than a 7B model handles (ADR 0046), and the cloud Platform it is also
  built for cannot reach the lab's Ollama.

## Consequences

- An approver gets the same evidence for every change, with the rule that set the risk named, so a rating can be checked.
- The workflow needs no gateway cluster of its own (no `runCode`), so the same document runs on the lab and on the cloud
  Platform; the child workflow it calls does use the Gateway runner of whichever Platform it is on.
- The reviewer's only judgement is which devices a change names and how the rules apply; a change that names a device
  by something other than its NetBox name is read as naming none, and the review says the scope is unknown.
- Rejected: giving the reviewer `listChangeRequests` and the NetBox tools directly (many calls, a different answer
  every run); a reviewer that writes its review into the change as a work note (a write path from text people wrote).
