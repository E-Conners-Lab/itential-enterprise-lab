# 0050 — Image-free phases first; one firewall track for everything behind a paid image; Windows dropped

- **Status:** accepted (owner approval 2026-09-09)
- **Date:** 2026-09-09
- **Related:** ADR 0009 (DDI eval-proof fallback), ADR 0034 (firewalls behind `lab.firewalls`), ADR 0037 (the first reorder), ADR 0049 (the company key budget), PID amendment 1.15

## Context

After Phase 6 merged, four of the six remaining phases waited on an image the owner cannot download without his
employer's help: Infoblox NIOS (Phase 7), Windows Server (Phase 8), PA-VM (the Phase 4 deferral) and Panorama
(Phase 11). Observability and the config/secrets/code phase need nothing, identity needs Windows only for AD DS,
DDI has an eval-proof half (BIND9 + Kea) by design, and cEOS-lab needs an arista.com account rather than a
purchase. The owner also decided to leave Windows out of the lab altogether.

## Decision

1. **Order, image-free first**: 7 Observability (S7), 8 Config/secrets/code (S8), 9 Identity without Windows (S6
   rewritten), 10 DDI on BIND9 + Kea (S5 first half), 11 Containerlab (S10), 12 the **firewall track**: NIOS grid
   master (S5 second half), the PA-VM firewalls (`lab.firewalls: true`) and Panorama (S9), started only once the
   images exist.
2. **Windows is out.** Windows Server, AD DS and `ad.lab.internal` leave S6; the `dc01` VM and 10.100.0.69 are
   released in the IP plan; the deferred "Windows endpoints as inventory nodes" item is dropped. The two Windows 11
   client VMs already in the topology stay as plain endpoints; nothing joins or manages them.
3. **Identity without a domain controller**: OpenLDAP in k3s is the one directory (groups NetAdmins, NetOps,
   ReadOnly, ServiceAccounts), Keycloak federates it for SSO, tac_plus uses its LDAP backend, Itential's LDAP
   adapter repoints from the dev-stack OpenLDAP to it. The S6 criteria keep their substance with LDAP groups in
   place of AD groups; criterion 2 (Windows eval expiry) is dropped.
4. **Criteria that move with the order**: Grafana via Keycloak (S7.5) and Gitea SSO (S8) are asserted in the
   identity phase; the PA-VM SNMP templates (S7) and the NIOS Oxidized model (S8) belong to the firewall track;
   S5 criteria 1 to 3 run against one server in phase 10, criteria 4 to 6 in the firewall track.
5. **The company key moves into Vault** with the device credentials in phase 8 (ADR 0049), so `.env` shrinks
   earlier than the original plan.

## Consequences

- Nothing waits on the employer's images except the firewall track; the owner's only near-term downloads are
  cEOS-lab (registration) and, for the track, NIOS, PA-VM 11.1 and Panorama.
- Phase 10 replaces a working DNS (oob-gw's unbound from ipam.yaml) with NetBox-driven BIND9 + Kea, hence its
  place after the phases that add capability.
- The PID's branch names align with the phase numbers again from phase 7 (`phase-7/observability`, ...).
