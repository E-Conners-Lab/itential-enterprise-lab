# 0009 — Infoblox NIOS is the DNS/DHCP primary; BIND9 + Kea are a live secondary that survives the eval expiry

- **Status:** accepted
- **Date:** 2026-09-06

## Context

The kickoff asks for "Infoblox NIOS with Kea + BIND9 fallback". The NIOS
evaluation license is time-limited (duration recorded in the image manifest)
and the lab is built over months, so the pre-mortem must assume NIOS will
expire at least once while the lab is in use. Two independent authoritative
servers for one zone drift; a primary/secondary pair does not.

## Decision

- `lab.internal` and the reverse zone are **primary on NIOS** and
  **secondary on BIND9** (`ddi-fallback` VM) via AXFR/IXFR with a long SOA
  expire (4 weeks). Every resolver lists NIOS first and BIND9 second.
- Zone content is *generated from NetBox* (a script/Itential workflow pushes
  records over WAPI). NIOS is a rendering of NetBox, never the source.
- Kea on `ddi-fallback` carries the same reservations, generated from the same
  NetBox data, and is started only when NIOS DHCP is down (manual or Itential
  failover). Two active DHCP servers on one flat OOB segment are never allowed.
- When the NIOS eval expires, the runbook is: redeploy NIOS from the staged
  image with `tofu`, re-apply the temp license, re-run the NetBox-to-WAPI
  sync, re-enable zone transfers. BIND9 keeps answering throughout.

## Consequences

- The DDI phase must deliver the NetBox-to-DNS generator, not just the
  appliances.
- The verification test for the phase includes "stop NIOS, resolve every
  service name via BIND9, start NIOS, confirm serial matches".
