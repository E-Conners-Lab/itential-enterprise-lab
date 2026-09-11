# 0048 — NetBox enrichment derived from the topology YAML: addressing on interfaces, VRFs and ASNs, racks, circuits, config contexts, journal entries

- **Status:** accepted (PR #20, 2026-09-11) — the enrichment is built and S4e.1-S4e.6 pass
- **Date:** 2026-09-08
- **Related:** ADR 0002 (NetBox = network source of truth, `topology/` = the one input), ADR 0034 (the design the addresses implement), ADR 0040 (Golden Config: interface intent waits for NetBox), ADR 0045/0046 (netbox-sot reads through the Integration Model), PID S4e (amendment 1.13)

## Context

Measured on 2026-09-08 (NetBox 4.7.0 on VM 110, netbox-docker, no plugins): NetBox holds the skeleton that
`netbox-topology.yml` seeds from `topology/enterprise.yaml`: 5 sites, 21 devices (17 active, 4 planned
firewalls), 87 interfaces, 33 cables, 19 prefixes, 47 addresses of which 26 sit on an interface (all of them
management), 21 interface descriptions (all management), 2 VLAN groups, 1 VLAN. Zero VRFs, route targets,
ASNs, locations, racks, circuits, providers, tenants, config contexts, custom fields, journal entries, FHRP
groups.

Everything the devices actually run beyond management lives only in `topology/configs/c8000v.j2` and
`veos.j2` as hard-coded dictionaries: router loopbacks (`10.103.255.x`), fabric loopbacks and the anycast
VTEP, the ISP peering and WAN addresses, the tunnel plan (`10.103.100.0/30` ...), the branch LAN gateways,
the MLAG peer addresses, the transit SVI addresses and the virtual router address, the VRFs `MGMT`, `WAN`
(rd `<asn>:1`) and `PROD` (L3VNI 50001). The link /30 and /31 addresses are derived from `links[].prefix`
(a-side first, b-side second). So today NetBox cannot answer "what address does br1-wan01 Gi2 carry, in
which VRF, to which peer", netbox-sot cannot either, and Golden Config's interface intent (ADR 0040) has
nothing to render from.

Owner request 2026-09-08: enrich NetBox with what the lab really runs, in this order: interface addressing
and descriptions, VRFs and ASNs, racks and locations, circuits, config contexts and journal entries.

## Decision

1. **The YAML stays the only oracle; the templates stop owning addresses.** `topology/enterprise.yaml`
   gains one `addressing` block per node (`loopbacks`, `svis`, `tunnels`, `lan`, `wan`), plus top-level
   `vrfs` (name, rd, route targets, description), `racks` (one rack per site, positions per node) and
   `circuits` (the four provider links: circuit id, provider, the two terminations). Link addresses keep
   the existing rule (a-side first usable, b-side second; /31 a-side `.0`). `eve/build.py` passes the
   block to the templates and the templates read it instead of their dictionaries. **The rendered startup
   configs are byte-identical before and after the lift**: `tests/test_topology.py` renders every node from
   the committed reference (`topology/generated/configs/<node>.cfg`, added by this change from the
   pre-lift templates) and compares. The running devices are not touched.
2. **One derivation play, `ansible/playbooks/netbox-enrich.yml`**, runs after `netbox-topology.yml`
   (`make phase-network-topology`, also `make netbox-enrich`) and writes, idempotently (create or update,
   `changed=0` on the second run, tagged `phase-6`):
   - every addressed interface with its address (`ipam/ip-addresses` assigned to the interface, the
     right VRF, role `loopback` / `anycast` / `vip` where NetBox has one), the `to <peer>:<port>` description
     the template writes, `mgmt_only` on the management port, and virtual interfaces (loopbacks, SVIs,
     tunnels, port-channels) created where the seeding play made only the physical ones;
   - VRFs `MGMT`, `WAN`, `PROD` with RDs and import/export route targets; prefixes and addresses moved into
     their VRF; the anycast gateway `10.101.10.1` and the virtual router `10.101.1.254` as FHRP groups
     (protocol `other`, the leaf SVIs as members) with the virtual address assigned to the group;
   - the seven ASNs from `routing.asn` (RIR `private`, `ipam/asns`) assigned to their sites; BGP neighbours
     per device in the device's local config context (name, address, remote AS, VRF, description) rendered
     from the same rules the templates use. **No netbox-bgp plugin**: netbox-docker on VM 110 would need a
     custom image; the owner can revisit once the plugin is wanted for its own sake;
   - one location and one rack per site (`<site>-rack01`, 42U), every device with a position, the EVE-NG
     VMs marked `airflow` unset and role-ordered top down (edges, firewalls, spines, leaves, access, hosts);
   - providers `Simulated ISP` and four circuits (`ISP-<site>-<n>`, type `transit`), termination A on the
     site and Z on the WAN site, the edge port and the `isp-core01` port cabled to the terminations so the
     cable trace crosses the circuit; the existing direct cables of those four links are replaced;
   - config contexts: `lab` (domain, DNS, NTP, management gateway) for every device, one per site with the
     site gateway and prefix roles, one per platform with the automation account name (never the password),
     and the local context above; the golden-config leaf reads rendered context through the integration;
   - journal entries: the play writes one entry per run on each site (what it reconciled, the git commit),
     and `wf-branch-vlan-v1` / `wf-branch-vlan-delete-v1` write one on the switch device when the push
     completes (through the NetBox adapter, the same governed job).
3. **Golden Config consumes it** (closes the ADR 0040 deferral): the device leaf template renders the
   interface descriptions and addresses NetBox holds for the device; the compliance plan stays clean on
   the 12 devices, which is the data-plane proof that NetBox now equals what runs.
4. **Verification** `verify/test-06c-netbox.sh` (PID S4e), second source `verify/devcmd.py`: per device the
   parsed `show ip interface brief` / `show ip vrf` / `show bgp summary` (Genie and TextFSM through
   `wf-show-command-v1`, direct SSH as the second source) equal NetBox; the play's second run is
   `changed=0`; netbox-sot's acceptance grows one question per object type (address+VRF+peer of a port,
   ASN of a site, rack position, circuit of a provider link, the site's NTP from the context), each answer
   equal to the NetBox API; the compliance plan clean with the interface intent in the tree.

## Consequences

- The topology YAML grows by the addressing block (~150 lines); the templates shrink by their
  dictionaries; `docs/ip-plan.md` §6 is regenerated from the YAML the way `tests/test_ipam.py` proves
  the OOB table today.
- Not modelled, on purpose: console, power, front and rear ports and inventory items (virtual devices,
  no serials), tenants and contacts (one tenant lab), custom fields (nothing needs one yet), the netbox-bgp
  plugin (above), the Windows clients' addresses (DHCP, not intent).
- The `lab.firewalls: false` bypass links keep their `bypass` tag so the enrichment retires their
  addresses when the firewalls arrive, the same flip ADR 0034 defines.
- Branch `phase-6/netbox-enrichment` stacked on `phase-6/flowai` (its own PR with base `phase-6/flowai`,
  the Phase 6 report gains the element); rebased with it after PR #17 and #19 merge.
