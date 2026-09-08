"""S4e (ADR 0048): NetBox is enriched from topology/enterprise.yaml by ansible/playbooks/netbox-enrich.yml.
These tests hold the derivation (topology/derive.py) to the YAML and the wiring of the play."""

from __future__ import annotations

import ipaddress
import re
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from topology import derive

PLAY = ROOT / "ansible" / "playbooks" / "netbox-enrich.yml"
SEED = ROOT / "ansible" / "playbooks" / "netbox-topology.yml"
MAKEFILE = ROOT / "Makefile"


@pytest.fixture(scope="module")
def topo() -> dict:
    return derive.load_topology()


@pytest.fixture(scope="module")
def intent(topo: dict) -> dict[str, list[dict]]:
    return derive.interfaces(topo)


@pytest.mark.parametrize(
    "platform,short,long",
    [
        ("c8000v", "Gi2", "GigabitEthernet2"),
        ("c8000v", "GigabitEthernet2", "GigabitEthernet2"),
        ("c8000v", "Tunnel1", "Tunnel1"),
        ("veos", "Eth7", "Ethernet7"),
        ("veos", "Mgmt1", "Management1"),
        ("veos", "Vlan10", "Vlan10"),
        ("pa-vm", "eth1/1", "ethernet1/1"),
        ("ubuntu", "eth1", "eth1"),
    ],
)
def test_device_interface_names(platform: str, short: str, long: str) -> None:
    assert derive.device_name(platform, short) == long


def test_link_sides_take_the_first_two_usable_addresses(topo: dict) -> None:
    for link in topo["links"]:
        if "prefix" not in link:
            continue
        ends = derive.link_ends(link)
        net = ipaddress.ip_network(link["prefix"])
        a, b = (
            ipaddress.ip_address(ends[link["a"].split(":")[0]]["ip"]),
            ipaddress.ip_address(ends[link["b"].split(":")[0]]["ip"]),
        )
        hosts = list(net.hosts()) if net.prefixlen != 31 else [net[0], net[1]]
        assert (a, b) == (hosts[0], hosts[1]), link


def test_every_intent_row_has_a_device_name_and_a_description(intent: dict) -> None:
    for device, rows in intent.items():
        for row in rows:
            assert not re.match(r"^(Gi|Eth|Mgmt)\d", row["name"]), (
                f"{device}: {row['name']} is the YAML short form"
            )
            assert "description" in row, (device, row)
            if row["kind"] in ("link", "tunnel"):
                assert row["description"], (device, row)
                assert row.get("peer"), (device, row)


def test_addresses_unique_except_the_shared_virtual_ones(intent: dict) -> None:
    per_device: dict[str, Counter] = {
        d: Counter(r["address"] for r in rows if r.get("address"))
        for d, rows in intent.items()
    }
    for device, counts in per_device.items():
        assert all(c == 1 for c in counts.values()), (
            f"{device}: address used twice {counts}"
        )
    shared = Counter()
    for rows in intent.values():
        for r in rows:
            if r.get("address") and not r.get("anycast"):
                shared[r["address"]] += 1
    assert all(c == 1 for c in shared.values()), (
        f"non-anycast address on two devices: {[a for a, c in shared.items() if c > 1]}"
    )
    virtual = Counter(
        r["virtual"] for rows in intent.values() for r in rows if r.get("virtual")
    )
    assert set(virtual.values()) == {2}, (
        f"shared virtual addresses must sit on both leaves: {virtual}"
    )


def test_every_address_lies_in_an_inband_prefix_of_its_site(
    topo: dict, intent: dict
) -> None:
    prefixes = [
        (ipaddress.ip_network(p["prefix"]), p["site"]) for p in topo["inband_prefixes"]
    ]
    for device, rows in intent.items():
        site = topo["nodes"][device]["site"]
        for row in rows:
            for key in ("address", "virtual"):
                if not row.get(key):
                    continue
                ip = ipaddress.ip_interface(row[key]).ip
                homes = {s for net, s in prefixes if ip in net}
                assert homes, (
                    f"{device} {row['name']} {row[key]} is in no in-band prefix"
                )
                assert site in homes or "wan" in homes, (
                    f"{device} {row['name']} {row[key]} sits in another site's prefix ({homes})"
                )


def test_vrfs_referenced_exist(topo: dict, intent: dict) -> None:
    for rows in intent.values():
        for row in rows:
            if row.get("vrf"):
                assert row["vrf"] in topo["vrfs"], row


def test_intent_covers_the_running_interfaces(intent: dict) -> None:
    """Measured on the devices 2026-09-08 (show ip interface brief): the addressed interfaces per device."""
    names = {
        d: {r["name"] for r in rows if r.get("address") or r.get("virtual")}
        for d, rows in intent.items()
    }
    assert names["br1-wan01"] == {
        "GigabitEthernet2",
        "GigabitEthernet4",
        "Loopback0",
        "Tunnel1",
        "Tunnel2",
    }
    assert names["dc1-leaf01"] == {
        "Ethernet1",
        "Ethernet2",
        "Ethernet7",
        "Loopback0",
        "Loopback1",
        "Vlan10",
        "Vlan100",
        "Vlan4094",
    }
    assert names["br1-sw01"] == set() and names["dc1-acc01"] == set()


def test_enrich_play_exists_and_is_wired() -> None:
    assert PLAY.exists(), "ansible/playbooks/netbox-enrich.yml is missing"
    text = PLAY.read_text()
    assert "topology/derive.py" in text, (
        "the play must take its intent from topology/derive.py"
    )
    mk = MAKEFILE.read_text()
    assert re.search(r"^netbox-enrich:", mk, re.MULTILINE), "make netbox-enrich target"
    phase = mk[mk.index("phase-network-topology:") :]
    assert phase.index("netbox-topology.yml") < phase.index("netbox-enrich.yml"), (
        "enrich runs right after the seed"
    )


def test_seed_play_creates_interfaces_with_the_device_names() -> None:
    """The data interfaces a link names (Gi2, Eth1) reach NetBox as the device's own names (ADR 0048)."""
    text = SEED.read_text()
    task = text[text.index("Data interfaces used by links") :]
    task = task[: task.index("- name:", 10)]
    assert "item.split(':')[1] }}\"" not in task.replace(
        'name: "{{ item.split', "name: X"
    ), task
    assert "ports[item]" in task, (
        "the seed must map the short port name through the derive.py port map"
    )
    assert "topology/derive.py" in text


# --- S4e.2: VRFs, ASNs, BGP neighbours in config contexts, FHRP group ---------------------------------------
REFERENCE = ROOT / "topology" / "generated" / "configs"


def configured_neighbors(text: str) -> set[tuple[str, int]]:
    """neighbor <ip> remote-as <as>, and on EOS neighbor <ip> peer group <G> with the remote-as on the group."""
    group_as = {
        g: int(a)
        for g, a in re.findall(r"^\s*neighbor (\S+) remote-as (\d+)", text, re.M)
        if not g[0].isdigit()
    }
    out = {
        (ip, int(a))
        for ip, a in re.findall(
            r"^\s*neighbor (\d+\.\d+\.\d+\.\d+) remote-as (\d+)", text, re.M
        )
    }
    out |= {
        (ip, group_as[g])
        for ip, g in re.findall(
            r"^\s*neighbor (\d+\.\d+\.\d+\.\d+) peer group (\S+)", text, re.M
        )
    }
    return out


def test_bgp_neighbors_equal_the_reference_configs_both_ways(topo: dict) -> None:
    """Every neighbour the derivation lists is configured on the device, and every configured one is listed."""
    for name, node in topo["nodes"].items():
        if node["platform"] not in ("c8000v", "veos"):
            continue
        derived = {
            (n["neighbor"], n["remote_as"]) for n in derive.bgp_neighbors(topo, name)
        }
        configured = configured_neighbors((REFERENCE / f"{name}.cfg").read_text())
        assert derived == configured, (
            f"{name}: derived {sorted(derived)} != configured {sorted(configured)}"
        )


def test_bgp_neighbor_vrfs_and_afis(topo: dict) -> None:
    n = {
        (x["neighbor"], x["vrf"], x["afi"])
        for x in derive.bgp_neighbors(topo, "br1-wan01")
    }
    assert ("10.103.0.9", "WAN", "ipv4") in n and ("10.103.100.1", None, "ipv4") in n
    n = {
        (x["neighbor"], x["vrf"], x["afi"])
        for x in derive.bgp_neighbors(topo, "dc1-leaf01")
    }
    assert ("10.101.254.1", None, "evpn") in n and ("10.101.3.1", "PROD", "ipv4") in n
    assert (
        derive.bgp_neighbors(topo, "br1-sw01") == []
        and derive.bgp_neighbors(topo, "dc1-acc01") == []
    )


def test_asns_cover_routing_and_sit_on_their_sites(topo: dict) -> None:
    rows = derive.asns(topo)
    assert {r["asn"] for r in rows} == set(topo["routing"]["asn"].values())
    assert {r["asn"]: r["site"] for r in rows}[65201] == "br1" and {
        r["asn"]: r["site"] for r in rows
    }[65000] == "wan"
    assert all(r["site"] in topo["sites"] for r in rows)


def test_device_contexts_carry_asn_router_id_and_rds(topo: dict) -> None:
    c = derive.contexts(topo)
    assert (
        c["devices"]["br1-wan01"]["bgp"]["asn"] == 65201
        and c["devices"]["br1-wan01"]["bgp"]["router_id"] == "10.103.255.21"
    )
    assert c["devices"]["br1-wan01"]["vrfs"]["WAN"]["rd"] == "65201:1"
    assert c["devices"]["dc1-leaf01"]["vrfs"]["PROD"]["rd"] == "10.101.254.11:50001"
    assert "bgp" not in c["devices"]["br1-sw01"]
    assert (
        c["lab"]["ntp"] == topo["lab"]["ntp"]
        and c["sites"]["br1"]["gateways"][0]["gateway"] == "10.102.17.1"
    )
    assert set(c["platforms"]) == {"ios-xe", "eos"}


def test_fhrp_group_for_the_transit_virtual_router(topo: dict) -> None:
    groups = derive.fhrp_groups(topo)
    assert (
        len(groups) == 1
        and groups[0]["address"] == "10.101.1.254/24"
        and groups[0]["vrf"] == "PROD"
    )
    assert {m["device"] for m in groups[0]["members"]} == {"dc1-leaf01", "dc1-leaf02"}


def test_enrich_play_builds_element_2() -> None:
    text = PLAY.read_text()
    for needle in (
        "netbox_asn",
        "netbox_rir",
        "netbox_config_context",
        "local_context_data",
        "netbox_fhrp_group",
        "netbox_route_target",
    ):
        assert needle in text, f"netbox-enrich.yml lacks {needle}"


def test_seed_play_creates_vrfs_before_prefixes() -> None:
    text = SEED.read_text()
    assert "netbox_vrf" in text and text.index("netbox_vrf") < text.index(
        "In-band prefixes"
    ), "VRFs must exist before prefixes are placed in them"
    assert 'vrf: "{{ item.vrf | default(omit) }}"' in text
