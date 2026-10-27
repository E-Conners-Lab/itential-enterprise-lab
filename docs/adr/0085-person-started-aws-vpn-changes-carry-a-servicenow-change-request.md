# 0085 — Person-started AWS VPN changes carry a ServiceNow change request

- **Status:** proposed (2026-10-27): R5 built; A2 (plan review), A4 (Cloud Concierge) and A5 (drift and cost watch)
  add their decisions here in the PRs that build them. Design approved by the owner 2026-10-26 (delivery
  `aws-vpn-roadmap`: customer-spec, feasibility amendments A1-A5, solution-design section C, probes P1-P5)
- **Date:** 2026-10-27
- **Related:** ADR 0054 (the `lab-servicenow` integration), ADR 0066 (evaluate every Gateway result), ADR 0068 (the AWS
  VPN through Terraform on the Gateway), ADR 0070 (the AWS VPN runs on production), ADR 0077 (InteractiveHTML cards)

## Context

Deploy, Hand Off and Tear Down AWS VPN change production (AWS and the edge router) after a person approves a Work
Center card, but nothing outside the Platform records that the change happened, who approved it or how it ended. Add
Branch VLAN already walks a ServiceNow standard change; the AWS VPN workflows did not.

## Decision (R5)

1. **One standard change per person-started run**, from the PDI template "Lab AWS VPN change" (`versions.yaml`
   `aws_vpn.change_request.template`), opened right after the input check and before any plan, output read or device
   read. Its short description is `<Workflow>: <target> (itential-enterprise-lab, job <id>)`; the first work note
   carries the workflow, target, the requester's note, the planned window, how the run was started and the job link.
   ServiceNow refusing the create ends the run with nothing planned, read or changed (fail closed). Tear Down Expired,
   Verify and the read-only workflows open none.
2. **Work Center stays the gate; the change mirrors it.** Every card shows the change number. Approved -> Scheduled ->
   Implement with a work note naming the approver, the time and the card's note; ServiceNow refusing either move
   applies nothing (Deploy discards the approved plan) and the change ends Canceled with why. Rejected -> Canceled
   with the decision. A run that ends before any approval -> Canceled with its reason. After an approval -> Review ->
   Closed `successful` or `unsuccessful` with the run's own outcome or error and the job link. Tear Down's second card
   (the AWS destroy) adds a work note; rejecting it closes the change `successful` (router removed, AWS kept).
3. **The approver by role, never by name** (this repo is public): the card task's finisher (`metrics.user`, read from
   the job's own record) through `aws_vpn.change_request.approver_roles`; an unmapped account reads "Platform account
   ...<last 6>". A run an automation starts through a trigger cannot read its own record (probe P2b), so its note says
   the decision is on the job's card.
4. **One generator, no child jobs.** `with_change_request` writes the same tasks into all three workflows (`ce*` open,
   `c8*`-`cb*` per card, `cd*` close, `cc*` fix by hand; a test holds the copies identical). Each workflow names how
   each of its endings closes the change (canceled, rejected, failed, successful); the build refuses an ending it
   was not told about. Approve and Reject each read the decision on their own chain, so no rejected card can reach an
   apply or a push in the graph, not only at run time.
5. **A refused update after the change** leaves the change done and puts a "Fix change request by hand" card in
   Work Center (InteractiveHTML: ViewData does not open there on 6.6.0) listing the state moves and notes to write.
   The build checks each ending's kind against the graph: nothing reached after the first card's change is in
   Implement may close it canceled or rejected.
6. **Tear Down takes a required `change_note`** on the page, the form and the endpoint trigger, under Deploy's own
   gate rule (`CHANGE_NOTE_RULE`: at most 280 characters, no markup, one line), so the requester's words reach the
   change request.

## Consequences

- Every ending after the open meets at one of four close entries: more arrows cross tasks on the canvas (Deploy 5,
  Hand Off 14, Tear Down 11; the owner's 2026-10-03 rule: canvas only, the job runs the same).
- Each run makes four to seven ServiceNow calls; a slow or asleep PDI makes Deploy, Hand Off and Tear Down slower or,
  at the create, stops them. That is the intended trade (no unrecorded change).
- No CR field is checked for secrets yet: the window's leak sweep (verify S13.2j) gains the change requests with A2-A5.
