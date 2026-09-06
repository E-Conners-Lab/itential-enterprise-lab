# 0002 — NetBox is the network source of truth; the GitHub repo is the project source of truth

- **Status:** accepted
- **Date:** 2026-09-06

## Context

The lab has both network state (sites, devices, VMs, prefixes, IPs, VLANs) and
project state (code, docs, decisions, verification results). Mixing the two
leads to IPs living in YAML that nobody reserved, or decisions living in a
NetBox journal entry that nobody reads.

## Decision

- **NetBox** owns network facts. Nothing receives an IP that is not reserved in
  NetBox first. Automation reads intent from NetBox; it never edits NetBox to
  match the network.
- **The GitHub repo** owns everything else. If it is not in the repo, it did
  not happen. No work product lives only in a chat session.
- `topology/` YAML is the single input that populates both the EVE-NG lab and
  the NetBox objects, so the two cannot drift from each other.

## Consequences

Every phase must include the NetBox population step before VMs get addresses.
Verification tests read from NetBox to decide what the network *should* look
like. Discovery and results are committed under `docs/` and `verify/results/`.
