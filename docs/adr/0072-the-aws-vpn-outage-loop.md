# 0072 — The AWS VPN outage loop: an alert starts Itential, an agent proposes one fix, a person approves it

- **Status:** accepted (owner, 2026-10-05)
- **Date:** 2026-10-05
- **Amends:** PID S13 (R6 and A3 built together; R9 parked), ADR 0068 (the monitor Lambda's fixed documents), ADR 0071
  (Alertmanager's first receiver beyond lab-null)
- **Related:** ADR 0046 (the agent fleet and its tiers), ADR 0051 (observability), ADR 0054 (Integration Models),
  ADR 0070 (the AWS VPN on production)

## Context

The lab's infrastructure phase is done (owner, 2026-10-05). The next work is the scenarios real network teams run.
R6 (tunnel down, closed loop) and A3 (Hybrid Tunnel Diagnostics) make one story together: the tunnel drops at 2 a.m.,
monitoring notices, a ticket opens with the evidence, an agent diagnoses the cause and proposes a fix, a person
approves it, the fix runs, and the tunnel is proven back. R9 (`Rotate AWS Terraform Key`) is parked: the key lives
only in Vault behind a permission boundary, `make aws-key TIER=prod` rotates it by hand, and R4 already proves the
same pattern.

Five probes (2026-10-05) shaped the design:
- **P1:** Studio's `runAgent` task exists on 6.5.2.
- **P2:** an Operations Manager endpoint trigger takes a session token only as `?token=`. A Bearer header is refused,
  and Basic auth needs TLS at the Platform, which the load balancer terminates.
- **P3:** the PDI's incidents carry `correlation_id`, but the integration model could not create one.
- **P4:** Alertmanager is 0.34.0.
- **P5, on production:** `runAgent` takes the agent's UUID (a name is refused) and returns
  `result = {sessionId, sessionStatus, lastMessage}`. The engine adds its own callback signature, so the task must
  not name one.

## Decision (owner, 2026-10-05)

1. **The alert.** `LabAwsTunnelDown` fires when dc1-wan01's Tunnel10 (SNMP `ifOperStatus`, already scraped) is not up
   for 2 minutes. Alertmanager routes this alert, and only this one, to `itential-outage-relay`, without resolutions.
   It resends every 4 hours while the alert fires. Every other alert stays on lab-null.
2. **The relay.** `observability/alert-relay/relay.py` runs in the `observability` namespace, stdlib only, on the
   exporter's pinned image. It is the only way a static webhook can reach a trigger that wants a fresh token in its
   URL. It forwards only firing alerts named in `observability.yaml` `alert_relay.routes`, with each label value
   pattern-checked, and answers 502 when the Platform refuses, so Alertmanager retries. It logs in with the Platform
   exporter's existing Secret, the administrator (owner's choice over a dedicated directory account). The accepted
   cost: an admin credential sits behind one more in-cluster service.
3. **The workflow.** `Diagnose AWS VPN Outage` runs in this order:
   - its input gate holds the relay's fields;
   - it reads the deployment;
   - it finds an open incident by `correlation_id` (`aws-vpn-<device>`) and only notes it;
   - otherwise it gathers the evidence: the AWS alarm (`aws-vpn-monitor alarm`) and Verify's own checks, generated
     in as `verify_section`'s third copy;
   - a tunnel already up again ends quietly, with no incident;
   - it opens one incident (`createIncident`, added to `lab-servicenow`), then runs the agent.
4. **The agent (A3).** `tunnel-diagnostics` (Claude), with `tunnel-diagnostics-local` as its twin:
   - it reads three router commands, may read Verify and the status once, writes one work note, and ends with one
     line of JSON naming a fix;
   - the workflow holds that answer to the menu `repush-router-block`, `restart-strongswan`, `reset-ike`, `escalate`;
     anything else, or a failed session, is `escalate`;
   - the workflow names the agent by a marker, `__AGENT_ID:tunnel-diagnostics__`, which the workflow import fills
     with the UUID (`tasks/workflow-agent-ids.yml`); `tasks/outage-trigger.yml` refuses to wire a workflow that still
     holds the marker.
5. **The fix.** A Work Center card shows the evidence, the cause and the one fix. Only approval runs it, and exactly
   one service:
   - `lab-edge-push reset-sa`: clear the IKE SA and prove a fresh one, with no configuration change;
   - `aws-vpn-monitor restart`: the Lambda's new fixed document. It restarts `strongswan-starter`, which re-renders
     swanctl.conf on the way up and heals a stopped daemon; R4's reload cannot;
   - the router block re-pushed through Hand Off's own render and push.

   Then the tunnel is read again (`lab-edge verify`). Up resolves the incident (state 6, `Solution provided`).
   Still down notes it, and a Work Center task asks for a person. A rejection runs nothing and leaves the incident
   open.
6. **The drill.** `Break AWS VPN` injects named faults (Tunnel10 shut, IKE blocked in INET-IN, strongSwan stopped)
   behind its own approval card. It is its own PR. The strongSwan stop is already a fixed document (`drill-stop`).
   The router faults wait for a probe on dc1-wan01: how IOS-XE treats a timed change while another timed change is
   pending.

7. **The card is an HTML page (owner, 2026-10-05).** Work Center's `InteractiveHTML` task replaces `ViewData`, so the
   engineer sees the ticket laid out in the lab's portal design rather than raw JSON. The page is built per outage on the
   runner (`outage_card`, pure and tested) from the readings the loop already holds:
   - the drawing of the path (router, Tunnel10, strongSwan), coloured by Verify's signals;
   - one reading per source (router, traffic, AWS monitor, CloudWatch);
   - the handoffs so far (Prometheus, ServiceNow, the agent, you, Itential);
   - the agent's cause beside Verify's yes/no readings;
   - the one fix, with its scope and three promises;
   - a note box.

   Every outside value is escaped, the agent's words included. Three probes on production shaped it:
   - **P6:** the body renders in an iframe that keeps `<style>`, `@keyframes`, the form and its textarea, but strips
     `<svg>`.
   - **P6b:** an `<img>` with an SVG data URI survives, so each drawing travels as one.
   - **P6c:** the generated page renders as designed, and Work Center sizes the frame to it.

   Approve and Reject both export `{"decision": {"note": ...}}`. The note goes into the incident either way: in the
   resolution or the still-down note after an approval, and in the rejection note (`outage_rejected`). A card that
   cannot be drawn runs nothing and opens the Work Center task.

## Consequences

- One alert, one ticket, one approval per outage. The loop never changes the router or AWS without a person.
- The bootstrap role boundary allows `SendCommand` on the two new documents (applied by the owner, 2026-10-05). A
  same-inputs Deploy creates them and updates the Lambda.
- On a first install the agents must exist before the workflow import fills in the UUID. The trigger task says so
  and stops.
- The dev tier runs neither agent with ServiceNow tools (`agent_exclude`) nor the loop (ADR 0070: AWS work is
  production's).
