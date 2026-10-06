# 0073 — The fabric BGP outage loop: a session down starts Itential, an agent proposes one fix on one end, a person approves it

- **Status:** accepted (owner, 2026-10-05; amended 2026-10-06 for PR C: decisions 7 and 8)
- **Date:** 2026-10-05
- **Amends:** PID (new S14, R10 + A6), ADR 0051 (the gNMIc BGP rule and the SNMP modules), ADR 0072 (the alert relay's
  route table and the outage card, now shared)
- **Related:** ADR 0046 (the agent fleet and its tiers), ADR 0048 (the topology as the intent NetBox is seeded from),
  ADR 0065 (Vault aliases on the Gateway), ADR 0068 (cloud-devops-pipeline's services and their pin)

## Context

R6 (ADR 0072) proved the loop on the AWS VPN: alert, incident, agent diagnosis, an approved fix, proof. R10 applies it
to the on-prem network's BGP: the vEOS fabric (spines, leaves, EVPN) and the C8000v WAN routers, 38 declared session
ends in all. Owner decisions, 2026-10-05: coverage is the EOS fabric through gNMIc plus the C8000v routers through SNMP;
the fix menu is `clear-session`, `no-shut-neighbor`, `no-shut-interface` or `escalate`, each proved by the session
reaching Established (no configuration re-push from NetBox); a new `fabric-diagnostics` agent with a local twin; both
approval cards are HTML cards in R6's design; and the drill (`Break Fabric BGP`, R10 PR C) injects a neighbor shutdown
or an interface shutdown behind its own approval card with an automatic revert.

Probes on production (2026-10-05, `itential-deliveries/bgp-outage-loop/probes-2026-10-05.md`):
- **P1:** every C8000v answers BGP4-MIB `bgpPeerState` for every peer, VRF WAN's too (20/20). Neither BGP MIB carries
  the VRF.
- **P2:** `Run Show Command on a Device` reads BGP on both platforms, but on EOS it still runs TextFSM on `| json` (an
  empty `parsed` that looks like "no neighbours"). IOS-XE 17.13 refuses `show bgp vrf X neighbors`.
- **P3 (an approved neighbor shutdown on dc1-spine01, undone):** SNMP saw it within 15 s; gNMIc after ~35 s, and gNMIc
  keeps the previous state's series for its 2 m expiry, so ESTABLISHED and IDLE both read 1 for up to 2 minutes on each
  transition. The old rule fired ~2m40s after the fault, without a neighbor label, once per end, and stayed firing 2
  minutes after recovery. EOS names the cause: `idleReason: "Administratively shut down"` on the shut end, `Active` on
  the other. vEOS answers BGP4-MIB for its default VRF only.
- **P4:** the undo re-established in about a second. The `clear ip bgp` probe was not run (owner: prove it in the
  service's tests and the first drill).
- **P5:** the Gateway already binds `lab-automation-password`, the inventory's shared device account.
- The leaf <-> WAN edge sessions flap on BFD for 1-2 s dozens of times a day.

## Decision (owner, 2026-10-05)

1. **The signal (PR A, #125, #126).** `observability/bgp_rules.py` renders `bgp-rules.yaml` from the topology: an
   intent series per session end (device, neighbor, vrf, peer, remote_as, pair), `lab:bgp_session_up` from SNMP
   BGP4-MIB on IOS-XE and gNMIc on vEOS for declared sessions only, and `LabBgpSessionDown` after 2 minutes. On gNMIc an
   ESTABLISHED series wins over a stale one, so BFD flaps never fire and a recovered session reads up at once; the cost
   is that an EOS outage fires ~1.5 minutes later than SNMP would. The snmp-exporter rolls its pod when its modules
   change (a checksum annotation; #125's converge left the old config running until a manual restart).
2. **The relay and Alertmanager.** The relay's route table maps each alert to its trigger and to the labels it may
   forward, each checked against its field's pattern. `LabBgpSessionDown` goes to `diagnose-fabric-bgp-outage`,
   grouped by device `pair`: both ends of a broken session fire at once (P3), and one call per pair starts the loop
   once; another alert from the same pair comes no sooner than `group_interval` (5 m), when the open incident notes it.
3. **The service.** `fabric-bgp` (cloud-devops-pipeline #37, pinned at 60839ab) reads one session end, or runs one
   approved fix and proves it. It refuses - nothing sent - when the device differs from the intent, the session is
   already Established, or the fix does not fit the fault; it sends one change, treats any `%` line as a refusal, waits
   a bounded time for Established, and saves only then. It logs in as the shared `automation` account (owner choice
   over a scoped account per router) and only to the host key pinned in `versions.yaml` `fabric_bgp.host_keys` (owner:
   no trust on first use). Each key was taken only when the device's own report (`show ip ssh`, `show management ssh
   hostkey ed25519 public`, through Run Show Command) equalled `ssh-keyscan` from the workstation: 9/9 on 2026-10-05.
4. **The workflow.** `Diagnose Fabric BGP Outage`, R6's shape: its input gate holds the relay's fields; the plan holds
   the alerting end to a declared session and finds its far end (build-time tables from `topology/derive.py`); an open
   incident for the pair (`correlation_id=bgp-<pair>`, open = `stateIN1,2,3`) is only noted; otherwise fabric-bgp reads
   both ends (a far end that cannot be read is evidence too), a session already Established ends quietly, one incident
   opens, the agent runs, its answer is held to the menu AND to the session's two ends, the card shows it, approval
   runs exactly one fabric-bgp call for that end, and up to four reads over ~2.5 minutes confirm it before the
   incident is resolved, or noted with a Work Center task.
5. **The agent (A6).** `fabric-diagnostics` (Claude) and `fabric-diagnostics-local`: the reads the request lists,
   through Run Show Command, one work note, one line of JSON naming the fix and the end. Causes: `neighbor-shut`,
   `interface-shut`, `stuck-session` (fixes) and `config-drift`, `link-down`, `unknown` (escalate).
6. **The card.** An InteractiveHTML page in R6's design: the two devices with their AS and each end's reading, the
   links, intent against the device, the agent's cause and evidence, the one fix with its scope and promises, and the
   decision with the engineer's note into the incident. R6's page frame, clock, mark and decision form are shared
   helpers now (R6's card byte-identical before and after); `Break Fabric BGP` (PR C) uses them too.

7. **The exact lines on the outage card (PR C, owner 2026-10-06).** After the agent's answer passes, the workflow asks
   fabric-bgp's `plan` action (cloud-devops-pipeline #38, pin 02b62f1; no device contacted) for the exact lines the
   approved fix will send, and the card shows them in a block, with the mode they run in. A plan that does not answer
   fails before the card: an engineer never approves a change they cannot read.
8. **The drill (PR C, owner 2026-10-06).** `Break Fabric BGP` (inputs: device, neighbor, fault) breaks one declared
   session on vEOS only - the IOS-XE equivalent of a commit timer needs `archive`, which the routers lack - and only
   while both ends read Established. Its HTML card (the shared helpers) shows the session healthy, the steps, the exact
   lines from `plan` and that EOS rolls it back 20 minutes after the approval (the timer starts at the inject, so the
   card names no clock time: drill 3, 2026-10-06). Approved, fabric-bgp `inject-neighbor-shutdown` or
   `inject-interface-shutdown` commits the one change in a configuration session `r10-drill-<epoch>` with `commit
   timer` 20 minutes, never saved. The workflow never fixes anything: it reads the session four times over 18 minutes
   while the outage loop does its work, and `confirm-drill` (cancel the timer) runs only after a read says
   Established. A session still down at the last read is left to the timer and read again after it, to prove the
   rollback. A rejection, an unhealthy session, an undeclared session or an IOS-XE device runs nothing.

## Consequences

- A session over a Tunnel interface carries no interface fabric-bgp may change (its name check takes Ethernet and
  GigabitEthernet only): a shut tunnel is escalated, never a card. Widening the check is a cdp follow-up.
- The single-neighbor `clear ip bgp` syntax (EOS: VRF last; IOS-XE: `clear ip bgp vrf X <ip>`) is proved by the first
  drill; until then a wrong form fails safe.
- A rebuilt device has a new host key and is refused until its key is pinned again, by the same two-source check.
- The loop and its trigger exist on production only; the dev tier imports the workflow without a trigger.
- A fix the outage loop runs during a drill goes in through `configure terminal` while the drill's configuration
  session waits on its timer; EOS accepting that, and the confirm that follows, is proved by the first drill run. If
  EOS refuses it the loop's fix fails safe (nothing saved) and the timer still rolls the drill back.
- The drill starts by API or from Operations Manager by hand; it has no trigger, so nothing outside a person starts it.
- The leaf <-> WAN edge BFD flapping is real and unexplained (likely timers too tight for EVE-NG's virtual CPUs); it is
  not this loop's to fix.
