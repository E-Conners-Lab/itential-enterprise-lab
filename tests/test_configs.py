"""Rendered startup configs follow the approved design (ADR 0034 amendment): the templates are the
source of truth for the routers/switches, so assert the design-defining lines, not the whole text."""

import copy
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eve.build import load_topology, render_config  # noqa: E402


@pytest.fixture(scope="module")
def rendered() -> dict[str, str]:
    os.environ["AUTOMATION_PASSWORD"] = "unit-test-only"
    topo = load_topology()
    assert topo["lab"]["firewalls"] is False
    return {n: render_config(v["platform"], n, v, topo) for n, v in topo["nodes"].items() if v["platform"] in ("c8000v", "veos")}


def test_dc_edge_bypass_and_no_firewall_config(rendered: dict[str, str]) -> None:
    cfg = rendered["dc1-wan01"]
    assert "neighbor 10.101.3.2 remote-as 65102" in cfg  # eBGP into the leaf tenant VRF
    assert "dc1-fw" not in cfg and "standby" not in cfg  # no HSRP, no deferred-firewall ports
    assert "vrf definition WAN" in cfg and "tunnel vrf WAN" in cfg  # front-door VRF transport
    assert "encryption aes-gcm-256" in cfg and "esp-gcm 256" in cfg and "group 19" in cfg  # NGE
    assert "tunnel mode ipsec ipv4" not in cfg  # GRE-over-IPsec by default (wan_tunnel_mode: gre)
    assert "neighbor 10.101.2.250 next-hop-self" in cfg


def test_branch_router_is_the_gateway_while_bypassed(rendered: dict[str, str]) -> None:
    cfg = rendered["br1-wan01"]
    assert "ip address 10.102.17.1 255.255.255.0" in cfg and "ip dhcp pool USERS" in cfg
    assert "network 10.102.16.0 mask 255.255.240.0" in cfg
    assert "neighbor 10.103.100.1 remote-as 65100" in cfg and "neighbor 10.103.100.9 remote-as 65100" in cfg


def test_fabric_follows_avd(rendered: dict[str, str]) -> None:
    spine = rendered["dc1-spine01"]
    assert "neighbor EVPN next-hop-unchanged" in spine and "neighbor EVPN remote-as 65102" in spine
    leaf = rendered["dc1-leaf01"]
    assert "vxlan vrf PROD vni 50001" in leaf and "ip address virtual 10.101.10.1/24" in leaf
    assert "ip address 10.101.1.252/24" in leaf and "ip virtual-router address 10.101.1.254" in leaf
    assert "neighbor 10.101.3.1 remote-as 65100" in leaf  # bypass peer inside VRF PROD
    assert "dual-primary detection delay 5 action errdisable all-interfaces" in leaf
    assert "10.101.1.1" not in leaf  # firewall peer only when firewalls are built
    assert "switchport access vlan 10" in rendered["br1-sw01"].split("interface Ethernet4")[1]


def test_firewalls_true_swaps_bypass_for_firewall_peers() -> None:
    topo = copy.deepcopy(load_topology())
    topo["lab"]["firewalls"] = True
    wan = render_config("c8000v", "dc1-wan01", topo["nodes"]["dc1-wan01"], topo)
    assert "neighbor 10.101.2.2 remote-as 65103" in wan and "10.101.3." not in wan
    leaf = render_config("veos", "dc1-leaf01", topo["nodes"]["dc1-leaf01"], topo)
    assert "neighbor 10.101.1.1 remote-as 65103" in leaf and "Ethernet7" not in leaf
    br = render_config("c8000v", "br1-wan01", topo["nodes"]["br1-wan01"], topo)
    assert "ip dhcp pool" not in br and "GigabitEthernet4" not in br
