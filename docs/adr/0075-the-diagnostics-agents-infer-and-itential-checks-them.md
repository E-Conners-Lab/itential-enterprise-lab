# 0075 — The diagnostics agents infer the cause; Itential checks their answer independently

- **Status:** accepted (owner, 2026-10-06)
- **Date:** 2026-10-06
- **Amends:** ADR 0073 (the fabric BGP outage loop: the agent's request, prompt and card), ADR 0072 (the AWS VPN outage
  loop: the same change follows in its own PR)
- **Related:** ADR 0074 (the knowledge search), ADR 0046 (the agent fleet)

## Context

The owner asked whether the drills hand the agent its answer. They did not - the agent never sees a drill - but the
loop itself nearly did: `fabric_summary` wrote its conclusions into the request ("the neighbor is administratively
shut down", "the device runs AS 65101 -> 64999, NetBox intends 65101 -> 65102"), and the prompt was a cause -> fix
table. The agent confirmed a few reads and copied a line. That is safe, but it does not show an agent reasoning, and
the owner wants the agent to have to infer its answer (2026-10-06).

## Decision (owner, 2026-10-06)

1. **Facts, not conclusions.** The fabric agent's request carries the incident, the alert and NetBox's intent for both
   ends (AS numbers, the neighbor, the interface toward the peer) - nothing the workflow's reads concluded
   (`fabric_summary` `facts`; `outage_request` sends it). The incident keeps the readings, for people; so the agent
   loses `getIncident`, which would have handed it those readings.
2. **A goal, tools and a menu - no table.** The prompt gives the fix menu as capabilities, each with the condition that
   makes it safe, and the rule never to propose a fix whose condition the agent has not seen in its own reads. The agent
   chooses its own reads (any `show` command: the workflow's input gate allows nothing else; at most 8; never `show
   archive log config all` without `| count`), searches the lab's knowledge (ADR 0074) and answers with one JSON line
   that now also carries `findings` (at most 6), `ruled_out` (at most 4) and `kb`. No self-rated confidence (owner).
   The local twin gets the same prompt (owner), behind its `/no_think`.
3. **Itential checks the answer independently.** The workflow still reads both ends with fabric-bgp and never shows the
   agent those reads; `fabric_agreement` holds the agent's answer against them (a fix needs its own condition on the
   end it names; escalate is backed unless an end shows a condition a menu fix covers). The card shows the agent's
   account (labelled as the agent's, escaped, capped), the knowledge it cited, the check as a badge - a disagreement
   is red but never blocks the approval (owner): fabric-bgp's own precondition check still refuses a wrong fix - and a
   link to the agent's session for every command it ran and the raw output (owner: a link, no session-fetch service;
   no workflow task returns a session's messages, measured). An escalation's work note records the check too.
4. **The guards are unchanged.** The workflow accepts only a menu fix on one of the session's two ends, and fabric-bgp
   re-reads the session and refuses a fix whose condition does not hold, then proves it Established.
5. **The drills test inference.** Besides the shutdowns, `Break Fabric BGP` can now inject a wrong peer AS (config
   drift: the right answer is escalate) and, next, an MD5 password on one end only. An eval matrix - every fault plus
   a healthy session, the twin three times and Claude once each - scores the right fix on the right end, nothing off
   the menu, a KB line and the read budget.

## Consequences

- More tool calls and tokens per run than a confirm-and-copy agent; the twin may score lower without the table - a
  finding, not a failure; the live drills then run on Claude.
- A model's account can be wrong or fluent nonsense: it is labelled, capped and escaped, and the badge and fabric-bgp's
  refusal are what an approver relies on.
- The AWS VPN loop (`tunnel-diagnostics`) still receives evidence and matches a table until its own PR applies this
  ADR; `outage_request` sends facts only when the loop provides them.
