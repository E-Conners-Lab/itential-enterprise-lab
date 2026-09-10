# 0054 — Integration Models built from OpenAPI specifications are the default; an adapter only where the Platform itself requires one

- **Status:** proposed (owner instruction 2026-09-10: "going forward I would like to use the integrations over the adapters when possible. I want to use the openapi specs as much as possible also")
- **Date:** 2026-09-10
- **Related:** ADR 0039 (Configuration Manager through the InventoryBroker adapter), ADR 0045 (Integration Models for NetBox and ServiceNow as agent tools), ADR 0048 (NetBox enrichment; journal entries through the NetBox adapter's generic request), ADR 0053 (the production environment), PID S4b/S4d.4/S11 (amendment 1.19)

## Context

The lab reaches its two external systems twice today, for historical reasons:

| System | Adapter (npm, `itential/versions.yaml` `adapters`) | Integration Model (OpenAPI, `itential/integrations/`) |
|---|---|---|
| NetBox | `adapter-netbox` v1.0.10, instance `NetBox` | `lab-netbox:1.0.0`, instance `netbox-api`, 11 read operations |
| ServiceNow | `adapter-servicenow` v3.0.11, instance `servicenow-api`… no: instance `ServiceNow` | `lab-servicenow:1.0.0`, instance `servicenow-api`, 5 operations |

The adapters came first (Phase 5, ADR 0020/0035) because the Inventory Manager and Configuration Manager
brokers needed them. The Integration Models came in Phase 6 (ADR 0045) so the FlowAI agents had typed tools.
The result is two code paths to the same API, two credentials to rotate, and two things to keep pinned.

Measured limits that pushed work onto the adapters, and what they cost:

- `adapter-netbox` strips trailing slashes, so NetBox endpoints that require one (`available-vlans/`) are
  unreachable through it, and the journal entries of `wf-branch-vlan-v1` go out through
  `genericAdapterRequest` with a hand-built path (ADR 0048).
- `adapter-servicenow`'s own change methods normalise the response, so `wf-config-push-v1`'s ServiceNow calls
  already use `genericAdapterRequest` rather than the adapter's typed tasks (`itential/workflows/build.py`).
- Both adapters are npm packages installed into every Platform node's custom-services directory, `npm install`
  and all: in the HA2 environment of ADR 0053 that is the same install on two nodes, and it will be the same
  install on every node added later.

In other words, most of what the lab actually asks of these adapters is already a generic HTTP request, which
is exactly what an Integration Model does from a specification, with typed inputs and no npm.

## Decision

1. **The default is an Integration Model generated from an OpenAPI specification.** Any new external system the
   lab integrates gets a document under `itential/integrations/` produced by `itential/integrations/build.py`
   from the vendor's published specification (trimmed to the operations the lab uses, as ADR 0045 established
   because NetBox's live schema is 322 paths and 14 MB). No new npm adapter is installed without an entry in
   the exceptions list below.
2. **An adapter is used only where the Platform itself requires one**, which today means:
   - `InventoryBroker` (Itential's own): Configuration Manager and Inventory Manager consume devices through
     the broker, and the broker consumes an adapter, not an integration (ADR 0039). Unchanged.
   - `LDAP` (Itential's own): AAA for the Platform, needed before any integration exists. Unchanged.
   - Gateway Manager's connection: internal to the Platform. Unchanged.
3. **NetBox and ServiceNow convert to their Integration Models**, in that order, as their own element with its
   own red tests: every workflow task that today names an adapter export or `genericAdapterRequest` moves to
   the integration's typed operation; `itential/integrations/*.json` gains the operations those workflows need
   (NetBox: the VLAN create/delete and journal writes; ServiceNow: the change transitions `wf-config-push-v1`
   uses); the `adapters` block of `itential/versions.yaml` shrinks to what item 2 lists, and the npm install
   disappears from the Platform play. The InventoryBroker keeps its NetBox adapter until Itential's broker can
   consume an integration, so `adapter-netbox` stays installed while `adapter-servicenow` goes entirely.
4. **Specifications are documents in the repo, pinned like every other version.** Each Integration Model
   records the source of its specification (URL and the date or release it was taken from) next to the
   operation list, so a regeneration is reproducible and a drift is visible; `tests/test_integrations.py`
   grows a check that every operation named in a workflow exists in the model.
5. **Order.** This conversion is *not* part of the Phase 8 migration: that migration must replay what phases
   5-7 built so the existing verifies prove the new infrastructure, not new content. It runs as the first
   element after the cut-over, before the dev-stack is retired, so both environments can be compared.

## Consequences

- One credential per external system instead of two, one place to pin, and nothing to `npm install` on a
  Platform node - which is what makes adding a third node cheap in the HA2 shape.
- The conversion touches Phase 5 and Phase 6 assets, so `verify/test-05`, `test-06`, `test-06b` and `test-06c`
  all re-run against it; `wf-branch-vlan-v1`'s journal entries and `wf-config-push-v1`'s ServiceNow calls are
  the two places where the generic request disappears.
- Where a vendor publishes no usable OpenAPI specification, the choice is an adapter or a hand-written model;
  that decision gets its own ADR at the time, naming which of the two and why.
- Until the conversion runs, the production environment installs both adapters exactly as the dev-stack does,
  because the replayed workflows call them (ADR 0053 decision 4).
