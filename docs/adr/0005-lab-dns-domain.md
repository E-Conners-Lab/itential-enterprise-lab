# 0005 — Lab DNS zone is `lab.internal`; the AD forest is `ad.lab.internal`

- **Status:** accepted
- **Date:** 2026-09-06

## Context

Discovery assumption 13: EVE-NG defaults to `lab.local`, and the PID must pick
one lab domain served by the DDI service. `.local` is reserved for mDNS
(RFC 6762) and causes resolver stalls on macOS and systemd-resolved. A public
domain would need registration and DNSSEC/ACME considerations the lab does not
need. ICANN reserved `.internal` for private use in 2024.

## Decision

- Lab zone: **`lab.internal`**. Every service and network device gets an A and
  PTR record here, generated from NetBox.
- Active Directory forest: **`ad.lab.internal`** (NetBIOS `LAB`), delegated
  from `lab.internal` so AD-integrated DNS owns only its own subtree.
- Keycloak realm name: `lab`.

## Consequences

- No TLS certificate from a public CA can be issued for these names; the lab
  runs its own CA (cert-manager, later backed by Vault PKI) and distributes the
  root to lab clients. This is recorded as a capability in the PID.
- The EVE-NG lab must set its DNS domain to `lab.internal` in the topology
  build rather than keeping `lab.local`.
