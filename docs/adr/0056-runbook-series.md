# 0056 — A nine-chapter runbook series in `docs/runbooks/`, parameterised and adaptable, with the blog posts drafted on top later

- **Status:** proposed (owner request 2026-09-10, after Phase 8 closed: "when we're done with phase 8 I want to create some runbooks")
- **Date:** 2026-09-10
- **Related:** every phase ADR (each chapter is one track); PID amendment 1.21

## Context

The repo can build the lab from nothing, and `verify/` proves it did. What it cannot do is tell a person
*how*, or why any of it is shaped the way it is. That knowledge is spread across 56 ADRs, a 700-line PID, 28
plays and eleven verify scripts, and the parts a newcomer needs most - the traps - are in commit messages.

Phase 8 made the case concrete. Eleven separate faults in that phase failed **silently**: a Platform that
reported a task `complete` while returning nothing, Sentinels that could never reach quorum so no failover
ever ran, an image without `curl` so every health check lied, an MCP server publishing device passwords. Each
cost real time, each is now a comment in the file that caused it, and none of them is findable by someone who
does not already know it happened.

The owner also wants blog posts. Those are a different artifact with a different voice, and writing both at
once tends to produce a runbook that reads like a blog post and a blog post that reads like documentation.

## Decision

1. **Nine chapters in `docs/runbooks/`**, one per track, numbered to match the order a person builds in:

   | | Chapter | Covers |
   |---|---|---|
   | 00 | prerequisites | hardware, accounts you must supply, the `.env` contract, the fill-in table |
   | 01 | oob-network-and-addressing | the OOB gateway, unbound, the address plan, why `vmbr0` is never touched |
   | 02 | k3s-platform-services | k3s, MetalLB, Traefik, cert-manager and the lab CA, Longhorn, CloudNativePG |
   | 03 | netbox-source-of-truth | NetBox as the inventory source, the seed, the enrichment, `nb_inventory` |
   | 04 | eve-ng-topology | the C8000v and vEOS nodes, golden images, topology YAML to device config |
   | 05 | itential-dev-stack | one VM, Compose, ECR, LDAP, Gateway 5, adapters, inventories, workflows |
   | 06 | platform-applications | Golden Config, compliance, MOP, LCM, forms, Integration Models, FlowAI |
   | 07 | observability | Zabbix on CNPG, kube-prometheus-stack, Loki + Alloy, gNMIc, the Itential dashboard |
   | 08 | production-ha2-and-migration | the HA2 build, the replay, the cut-over, the retirement |

2. **Every chapter has the same five sections**, so a reader learns the shape once: *Before you start* ·
   *The commands, in order* · *What "done" looks like* (elapsed time and the observable result) ·
   *Verification* (the `verify/test-NN-*.sh` line and what a PASS means) · *Troubleshooting*.

3. **Troubleshooting is the point.** Each entry names the symptom as it actually appears - the log line, the
   HTTP code, the silence - then the cause and the fix. A trap that failed silently is worth more than a
   command that worked first time, and this repo has banked dozens.

4. **Parameterised, never transcribed** (owner decision). Every value specific to one environment - the home
   LAN, the router address, the ECR account, the ServiceNow PDI, tokens, the CA's subject - appears once in
   chapter 00's fill-in table and is referenced as `${PLACEHOLDER}` everywhere else. The lab's own
   `10.100.0.0/24` addresses stay concrete: they are private, invented here, and readability depends on them.
   `tests/test_runbooks.py` fails if a real home-LAN address, account id or instance name appears in any
   chapter, so publishing stays safe by construction rather than by memory.

5. **Adaptable, with the tested versions pinned** (owner decision). A chapter describes the shape and lets a
   reader substitute their own hardware; a version table records exactly what this lab ran, so "tested" and
   "should work" are never confused.

6. **Runbooks first; the blog posts are a later, separate pass** (owner decision). The runbooks are the
   durable artifact in the repo and carry the commands. The posts carry the narrative and the war stories,
   cite the chapters, and get their own ADR when they start.

## Consequences

- One place to send a person, and one place a trap has to be written down for it to count as learned.
- The chapters are derived from documents that already exist and are already tested, so they can be checked
  against them: `tests/test_runbooks.py` holds every command a chapter names to a real `make` target or play,
  and every verify it cites to a real script.
- A chapter goes stale when its phase changes. The tests catch a renamed target or a deleted play; they
  cannot catch prose that has quietly stopped being true, so a phase that changes shape re-reads its chapter.
- Writing them will find gaps - manual step 6c (trusting the lab root CA) was already one, found by asking
  why a browser said "Not Secure" and never written down since Phase 3. That is the series working before it
  is written.
