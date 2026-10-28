# 0085 — Person-started AWS VPN changes carry a ServiceNow change request

- **Status:** proposed (2026-10-27): R5 and A2 (plan review) built; A4 (Cloud Concierge) and A5 (drift and cost
  watch) add their decisions here in the PRs that build them. Design approved by the owner 2026-10-26 (delivery
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
   Implement with a work note carrying the card's note (decision 3); ServiceNow refusing either move
   applies nothing (Deploy discards the approved plan) and the change ends Canceled with why. Rejected -> Canceled
   with the decision. A run that ends before any approval -> Canceled with its reason. After an approval -> Review ->
   Closed `successful` or `unsuccessful` with the run's own outcome or error and the job link. Tear Down's second card
   (the AWS destroy) adds a work note; rejecting it closes the change `successful` (router removed, AWS kept).
3. **The notes name no one; the card records who and when.** A run started through any trigger - the page's, the
   form's, an automation's - cannot read its own record (measured below), so a decision's work note says what was
   decided, carries the card's note and links the job whose Work Center card records who and when. The run does not
   try to read itself back (amended 2026-10-27: the reads only ever failed and left errored tasks on every job).
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
   gate rule (`CHANGE_NOTE_RULE`: at most 280 characters, no markup, one line), and shows it on both of its cards, so
   the requester's words reach the approver and the change request.

## Decision (A2: the plan review on Deploy's card)

7. **Deploy reviews its own plan before the card.** Between the plan and the card, `plan_facts` (runCode) turns the
   plan's review extract (cloud-devops-pipeline #48: address, actions, type, allow-listed facts; the on-prem address
   never) into the counts, one line per change and the estimate, and `runAgent` hands that to `plan-reviewer`.
   `plan_review` (runCode) holds the answer to the plan and builds the card's body: the review first, the estimate,
   then every change by address. The raw extract stays in the job's `plan` variable, off the card.
8. **The reviewer holds no tool and answers one JSON line** (`{"review": [{"text", "addresses"}]}`): no MCP route, no
   extra operators, only its workflow runs it (`WORKFLOW_ONLY_AGENTS` in tests/test_flowai.py). The check keeps at most
   8 lines, replacements and deletions first. The card shows only each line's text, so a line is left out when its
   text names a resource not in the plan (by address or the end of one) or a global IPv4 address, or when none of the
   addresses it lists is in the plan; one mistyped address among good ones only loses that address (measured on the
   twin 2026-10-28).
9. **Advisory, never in the way.** A session that errors, is canceled or answers anything but the line leaves the card
   saying "Review unavailable" with why; a review step that cannot run goes straight on to the card, which then shows
   the plan as it is. The approval works the same either way.
10. **The cost is computed, never written by a model** (D5): one pure `estimate_per_day` from `aws_vpn.prices`, shared
    with Get AWS VPN Status. It counts each billed public IPv4 address, so with the NAT gateway on both workflows now
    add its address too (Status said $1.48 a day for t3.micro with NAT, now $1.60). The card's message drops its
    written "about $1 a day" for the computed figure.

## Consequences

- Every ending after the open meets at one of four close entries: more arrows cross tasks on the canvas (Deploy 5,
  Hand Off 14, Tear Down 11; the owner's 2026-10-03 rule: canvas only, the job runs the same).
- Each run makes four to seven ServiceNow calls; a slow or asleep PDI makes Deploy, Hand Off and Tear Down slower or,
  at the create, stops them. That is the intended trade (no unrecorded change).
- No CR field is checked for secrets yet: the window's leak sweep (verify S13.2j) gains the change requests with A2-A5.
- Measured live 2026-10-27 (Deploy job 1a5507b8, CHG0030030, rejected): a run started through an endpoint or manual
  trigger - the page's and the form's too, not only an automation's - cannot read its own record (`getJob` on its own
  id answers 401, though `created_by` is the person). Probe P2's readable run was started by the admin, how was not
  recorded. So the notes cannot name the approver from inside the run: they say the card on the job records who and
  when, and the first note says only that the run started in Itential. The reads were dropped the same day.
- Tear Down's second card (the AWS destroy) adds a work note on a rejection as on an approval: the change still closes
  `successful` (the router change happened), and the record says the destroy was rejected, with the card's note.
- A2: every Deploy spends one reviewer session (Claude on production) before its card, and the card waits for it:
  runAgent has no timeout, so the reviewer gets no tools and one turn. A Deploy imported before the agent exists
  keeps the agent marker and its review says "unavailable" until the next converge fills it (two passes on the first
  install). Deploy's canvas has three more arrows through tasks (8; the owner's 2026-10-03 rule).
- The local twin `plan-reviewer-local` runs in verify/test-06-flowai.sh (S4d.5r) on a fixed plan, held by the
  workflow's own check.
