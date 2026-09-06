# 0030 — Home-LAN clients reach the lab via the router's static route; every reply takes the same path back

- **Status:** accepted
- **Date:** 2026-09-06

## Context

The owner chose a static route on the home router (10.100.0.0/14 via
`oob-gw` 192.168.68.120) over per-host routes. That makes client traffic
hairpin through the router: client -> router -> `oob-gw` -> lab. Anything in
the lab that replies straight to the client (because it also has a leg on the
home LAN, or because `oob-gw` itself did) produces an asymmetric flow, and the
router's stateful firewall drops the half it never saw. Symptoms were TCP
handshakes that completed and then stalled, intermittently. Two lab hosts are
dual-homed by design (NetBox VM 110 and EVE-NG VM 300), and NetBox serves
its API from a Docker container, whose replies are un-NATed *after* the
routing decision so a source-address policy rule cannot catch them.

## Decision

Every packet sourced from the lab towards the home LAN goes back through the
same hops it came in on:

- `oob-gw`: policy rule `from 10.100.0.0/14 to 192.168.68.0/22` uses a table
  whose only route is via the home router; ICMP redirects are neither accepted
  nor sent.
- EVE-NG: `from 10.100.0.2/32` uses a table with a default via `oob-gw`,
  persisted as `post-up` lines in the `pnet1` stanza.
- NetBox: the same `from 10.100.0.64/32` rule **plus** a conntrack mark set on
  connections that arrive on `oob0` and restored on their replies, with an
  `fwmark` policy rule to the same table (netplan `routing-policy`, nftables
  table `oob_return` loaded by a dedicated oneshot unit that never flushes
  Docker's tables).
- Every single-homed lab VM needs nothing: its default route is `oob-gw`.

## Consequences

- Client access works from any home-LAN device with no per-host route, and
  `verify/test-02-oob.sh` S1.8 proves it on every run.
- Any future dual-homed host (there should be none by design) must carry the
  same return-path rules; the PID tool audit gains this as a review item.
- Workstations running a VPN that captures 10.0.0.0/8 (NordVPN on the Mac
  Mini) must either disconnect it or add the more-specific route from
  `scripts/workstation-route.sh`; that is a client condition, not a lab one.
- Debian's `nftables.service` is left disabled on hosts that run Docker.
