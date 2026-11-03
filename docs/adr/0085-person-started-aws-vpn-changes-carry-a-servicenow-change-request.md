# 0085 — Person-started AWS VPN changes carry a ServiceNow change request

- **Status:** proposed (2026-10-27): R5 and A2 (plan review) built; A4 (Cloud Concierge) built 2026-10-31; A5 (Drift
  and Cost Watch) built 2026-11-02. Design approved by the owner 2026-10-26 (delivery
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

## Decision (A4: the Cloud Concierge)

11. **The Concierge holds two request tools, never Deploy or Tear Down.** `cloud-concierge` (Claude; its twin
    `cloud-concierge-local`) holds `Get AWS VPN Status`, `Request AWS VPN Deployment` and `Request AWS VPN Teardown`,
    all workflows. A request tool starts the real workflow through its endpoint trigger (`runEndpointTriggerWithPost`,
    the branded page's own route), so the run carries the trigger automation's groups and its card shows to the
    approver, and the tool answers at once: the chat never waits on a card (design D7, probes P1 and P1b). Nothing the
    agent holds approves. "Request" joins the naming rule's verbs: the tool asks for a run that a person still
    approves, and "Deploy" would say it deploys.
12. **The change request is found by the job id.** A run that a tool starts cannot be read back (probe P2b), so the tool
    finds the change request R5 opens by the job id in its short description (`listChangeRequests` with
    `short_descriptionLIKE<job>`, five reads over about a minute). Its answer names the job, the change request, the
    card the run waits on, the planned end and the next step (Hand Off on the branded page). The note it passes on is
    the person's words marked `Cloud Concierge: `, so the change request and the card name the Concierge (feasibility
    amendment A4); the mark fits inside Deploy's 280 characters.
13. **The on-prem address never reaches the agent** (D8, feasibility amendment A5). An agent reads every variable of a
    tool's job (measured 2026-10-31: a tool call's output is the job's `{_id, status, variables}`), so the address, the
    started run's document and ServiceNow's HTTP responses (headers, a session cookie among them) stay in task outputs.
    Each tool publishes only `started_job`, `change_number`, `outcome`, `error`, `input_check` and `change_wait`. The
    address `last` is the newest Deploy a person started: a run that an agent tool, an automation or a schedule started
    records `Pronghorn` as its `metrics.user` (measured 2026-10-31) and cannot be read. Without one, the agent asks the
    person and passes on what they type.
14. **The verify starts nothing.** `verify/test-06-flowai.sh` S4d.5s and S4d.5t run both agents on requests that start
    nothing (an approval asked for; another region and instance size): no request tool called, and a plain refusal.
    `verify/conciergecheck.py deploy` and `teardown` start a real run, reject its card as the administrator and read the
    change request Canceled in ServiceNow itself: they run by hand, with the owner's go.

15. **The request tools take the forms the models send** (2026-11-01, ADR 0082 amendment A1). The Platform describes
    every workflow input to a model as any JSON type, and both Concierges sent the lifetime as a number and the NAT
    setting as a boolean (sessions d0a02c96 and 102e1d23): `conciergecheck.py deploy` failed at the gate with nothing
    started. The request gate now takes Deploy's values as text or as that number or boolean, and the step that builds
    Deploy's inputs hands it text; a value in quote marks is still refused. NetClaw starts generated copies of the two
    workflows, marked `NetClaw: ` (ADR 0082 amendment A1). The deploy check rejects the card of every run any request call started before it judges
    the calls, so a retry can never leave a run waiting.

## Decision (A5: the Drift and Cost Watch)

16. **The workflow decides whether there is anything to propose, never the model** (D10). After the drift check, Check
    AWS Drift reads the deployment (`terraform-run outputs`, a state read) and `watch_gate` (runCode) routes the day.
    Drift found, or a deployment with no end time whose last Terraform apply is 24 h or more ago, is a proposal day.
    Anything else ends quietly with no agent call (criterion 13). An end time is Tear Down Expired AWS VPN's (R2b):
    an old deployment with an end time gets no proposal, and drift on an expired one opens the R3 drift task.
17. **At most one open card.** A proposal day first reads the running jobs (`getJobs` on `status` running; measured
    2026-11-02, the filter holds for complete, error and canceled). A second Check AWS Drift (an earlier day's card
    still waiting, its drift task included) ends the run with a note. A running change to the AWS VPN (Deploy, Hand
    Off, Tear Down, Tear Down Expired, either Rotate AWS VPN Key, Diagnose AWS VPN Outage) means no proposal that day,
    but drift still opens the R3 drift task: Tear Down Expired runs at the top of every hour, 07:00 included, so a
    change in flight must never hide drift. The change list goes past the design's "a card from an earlier day": an
    approved redeploy waiting at Deploy's own card would otherwise be proposed again the next day.
18. **One answer from a menu the situation fixes** (D6, D10). `drift-cost-watch` (Claude; twin
    `drift-cost-watch-local`) holds no tool and answers one JSON line, `{"proposal", "why"}`: on a drift day redeploy or
    tear-down, on a long-running day tear-down alone. The design also offered "nothing" there; it is gone (owner,
    2026-11-02), since the model could then hold back the cost card every day. `watch_proposal` (runCode) holds the
    answer to the menu. Its words
    reach the card and the started run's change note, so they are kept only when they name no resource outside the
    drift, no address of any kind and no cost (decision 10: the card shows the computed one); otherwise the card gives
    the workflow's own reason. On a drift day anything else (off the menu, a failed or canceled session, no JSON line)
    opens the plain R3 drift task, so the model can never hide drift (criterion 16). On a long-running day a failed
    watch still gets the Tear Down card with the workflow's reason: the rule found it, not the model.
19. **Approving starts the run; the run still asks** (D11). The card (InteractiveHTML, ADR 0077) shows the proposal and
    why, the drifted resources by attribute name, the deployment and the computed estimate (decision 10). Approve
    starts Deploy or Tear Down through its endpoint trigger, as the Concierge does (decision 11), with the note
    `Drift and Cost Watch: <why>`. Each opens its own change request and stops at its own card; Reject starts nothing.
    A redeploy keeps the end as close as Deploy's lifetimes allow (the choice nearest the hours left, the shorter on a
    tie; none stays none) and the NAT setting the Terraform state records. The design took the smallest choice at or
    above the hours left, which turns 30 hours into 72 (owner, 2026-11-02). It took NAT from the newest Deploy job,
    but a later Deploy that was rejected would then change it. The on-prem address is the newest person-started
    Deploy's, read into task outputs and never a job variable (decision 13); with none to read, the R3 task opens.
20. **A watch that cannot run is R3 as it was.** A watch step that errors hands back to R3's own `found?`: drift opens
    the drift task, no drift ends quietly.

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
- A4: a run the Concierge starts belongs to the Pronghorn account, and Work Center files its card under the agent
  session (probe P1), not under the job; the person is the session's creator. The request tools sit in the leak sweep's
  job list (`verify/awsvpncheck.py`) beside the workflows they start. On the dev tier, which has no AWS endpoints, the
  tools answer that the run could not be started.
- A4 is proven to its cards only after the converge: `conciergecheck.py deploy` and `teardown`, each rejected
  (criteria 10-12); the whole cycle waits for the AWS window.
- A5: a proposal day costs one watch session (Claude on production); a quiet day costs none, and every day reads the
  state once more. Check AWS Drift has two manual tasks now, the proposal card and the R3 drift task. A Check AWS Drift
  imported before the agent exists keeps the marker until the next converge fills it (two passes on the first install,
  as A2); until then a drift day opens the R3 task and a long-running day gets the Tear Down card with the workflow's
  reason.
- The local twin `drift-cost-watch-local` runs in verify/test-06-flowai.sh (S4d.5u) on a fixed drift day, held by the
  workflow's own check. No agent session (A2's or A5's) is in the leak sweep yet; the gate never hands the model an
  address (tests/test_drift_cost_watch.py).
- A5 is proven to its card only after the converge: with AWS empty the daily run ends quietly (criterion 13); the drift
  and long-running cards (criteria 14-16) wait for the AWS window.
