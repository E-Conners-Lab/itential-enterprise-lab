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
    assert "ports[item]" in task, "the seed must map the short port name through the derive.py port map"
    assert "topology/derive.py" in text
