# 0057 — Every phase that adds a host refreshes observability, and a unit test enforces it

- **Status:** accepted (PR #29, 2026-09-11) — make observability-refresh is wired in and the enforcing test is negative-tested
- **Date:** 2026-09-11
- **Related:** ADR 0051 (observability design), ADR 0053/0055 (the production environment), PID S7, amendment 1.23

## Context

`verify/test-07-observability.sh` was red from the moment Phase 8 merged until 2026-09-11, and the reason was
structural rather than a missed step.

`Makefile` runs the phases in a fixed order:

```
PHASES := oob-network platform network-topology itential flowai observability platform-ha2 \
          config-secrets-code identity ddi containerlab firewall-track
```

**`observability` is phase 7 and `platform-ha2` is phase 8.** So a clean `make up` from nothing builds the
monitoring stack and *then* builds eleven more VMs it has never heard of. `phase-platform-ha2` contains no
observability step at all. Nothing was misconfigured: `observability-hosts.yml` takes its hosts from the
NetBox inventory and all eleven production VMs were already in the `ubuntu-24-04` group it targets, and
`observability.yml` sizes Zabbix and Prometheus from NetBox too. **Nothing ever re-ran them.**

The gap was also invisible to the phase that created it. `verify/test-08-platform-ha2.sh` checks that the
eleven VMs exist at the budgeted sizes and that NetBox holds each one. Nothing in it asks whether they are
monitored, because "the estate is monitored" is S7's criterion and S7 belongs to an earlier phase. A phase
cannot notice that it invalidated an earlier phase's acceptance criterion.

Five phases still to come — config/secrets/code, identity, DDI, Containerlab and the firewall track — every
one of which adds VMs. Each would recreate this exactly.

## Decision

1. **`make observability-refresh` is the supported way to make monitoring catch up.** It runs
   `ansible/playbooks/observability.yml` (the stack, the Zabbix configuration and the scrape objects, all
   derived from NetBox) and `ansible/playbooks/observability-hosts.yml` (the agent and rsyslog on every
   Ubuntu machine). It deliberately does **not** run `observability-devices.yml`: those are governed config
   pushes that raise a Work Center card per device and need a person, which is not something a later phase
   should trigger as a side effect.

2. **Every phase target after `observability` in `PHASES` that registers a host ends with that refresh.**
   Today that is `phase-platform-ha2`. The refresh is idempotent, so a phase that happens to add nothing
   costs one no-op play run.

3. **A unit test enforces it, because a rule nobody can forget is the only kind that survives.**
   `tests/test_observability.py` reads the `Makefile`, finds every implemented `phase-*` target whose position
   in `PHASES` is after `observability`, and fails if one registers a host (`netbox-vms.yml`, or a `tofu apply`
   that creates VMs) without also refreshing observability. A phase added in six months fails CI rather than
   silently reopening this.

4. **Monitoring is not moved later in the order.** It could be, but it should not: the earlier phases deserve
   to be monitored while they are built, and any position in a linear order is wrong for whatever is built
   after it. The defect is not *where* observability sits; it is that the estate changes after it and nothing
   tells it. A refresh at the end of each host-adding phase fixes the general case; reordering fixes one
   instance of it.

## Consequences

- A clean `make up` ends with monitoring that matches the estate, and so does any single phase run.
- The cost is one idempotent play run per later phase, a few minutes.
- The test is a document test: it reads the `Makefile` and needs no lab access.
- It does not cover a host added **outside** a phase target — a VM built by hand still needs a manual
  `make observability-refresh`. That is the same contract as every other NetBox-derived thing in this repo:
  register it in NetBox and re-run the play that reads NetBox.
- The deeper lesson is recorded rather than fixed: **a phase can invalidate an earlier phase's acceptance
  criterion, and nothing in the verification order notices.** `verify/run.sh` runs every phase's script and
  would have caught it on any full run — which is the argument for running the whole suite at a phase close
  rather than only the phase's own script.
