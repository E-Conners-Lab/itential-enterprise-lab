# topology/ — single YAML that drives EVE-NG and NetBox

The YAML here is the source for both the EVE-NG lab (nodes, networks, links,
startup configs, built via the EVE-NG REST API) and the NetBox objects (sites,
devices, VMs, prefixes, IPs, VLANs). Both consumers read the same file so they
cannot drift from each other. Schema lands in Phase 1 with the PID.

## Addressing keys (ADR 0048)

Every address the devices run is in `enterprise.yaml`; the startup-config templates in `configs/`
carry no address literal (`tests/test_topology.py` enforces it and proves the rendered configs equal
`generated/configs/` byte for byte). `derive.py` turns the YAML into the template variables
(`render_context`) and into the NetBox interface intent (`interfaces`; `python topology/derive.py`
prints it as JSON for `ansible/playbooks/netbox-enrich.yml`).

| Key | Meaning |
|---|---|
| `links[].prefix` | the link's /30 or /31; a-side takes the first usable address, b-side the second (a /31: `.0` and `.1`) |
| `nodes.<n>.addressing.loopbacks[]` | `name`, `address` (with mask), `description`, `anycast: true` for the shared VTEP |
| `nodes.<n>.addressing.svis[]` | `name`, `vrf`, `address` (per device) and/or `virtual` (shared anycast / virtual-router address), `description` |
| `nodes.<n>.addressing.lan` | the branch users VLAN: `vlan`, `prefix`, `gateway`; the gateway sits on the router's bypass port while `lab.firewalls` is false |
| `routing.tunnels[]` | IKEv2 tunnels in the link syntax (`dc1-wan01:Tunnel1` <-> `br1-wan01:Tunnel1`, `prefix`); the interface number is the tunnel id on that router |
| `routing.firewall_transit_gateway`, `routing.loopback_prefixes` | the active firewall on the VLAN 100 transit; what the fabric advertises as loopbacks |
| `sites.<s>.aggregate` | the site prefix the edge advertises and null-routes |
| `vrfs` | `MGMT`, `WAN`, `PROD` with description, platforms, RD pattern and route targets |
| `inband_prefixes[]` | docs/ip-plan.md section 6 (site, role, description, `vlan`, `vrf`); seeded by `netbox-topology.yml` |
| `racks`, `rack_order` | one location and one 42U rack per site; nodes are racked top down in `rack_order` (one U each, position derived) |
| `providers`, `circuits`, `links[].circuit` | the four provider links as circuits: termination Z on the provider port (a-side, WAN site), A on the customer edge (b-side); the seed skips their direct cables |
| `addressing.svis[].virtual_router` | a shared EOS virtual-router address (VARP): an FHRP group in NetBox with the member SVIs, never a per-SVI address |

`derive.py` also emits, for `netbox-enrich.yml`: `contexts` (lab, per site, per platform, per device with BGP
neighbours and RDs), `asns`, `fhrp_groups`, `racks`, `circuits` and `renames` (short YAML port names the seed once
created, renamed in place to the device's names).
