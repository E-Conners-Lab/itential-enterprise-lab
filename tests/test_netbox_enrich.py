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


# --- S4e.3: locations and racks ------------------------------------------------------------------------------
def test_every_node_is_racked_once_with_a_unique_position(topo: dict) -> None:
    rows = derive.racks(topo)
    assert {r["site"] for r in rows} == set(topo["racks"]) == set(topo["sites"]), (
        "one rack per site"
    )
    racked = [d["name"] for r in rows for d in r["devices"]]
    assert sorted(racked) == sorted(topo["nodes"]), "every node is racked exactly once"
    for r in rows:
        positions = [d["position"] for d in r["devices"]]
        assert len(set(positions)) == len(positions) and all(
            1 <= p <= r["u_height"] for p in positions
        ), r["rack"]
        assert r["devices"][0]["position"] == r["u_height"], (
            f"{r['rack']}: the first device sits at the top"
        )
    dc1 = {
        d["name"]: d["position"]
        for r in rows
        if r["site"] == "dc1"
        for d in r["devices"]
    }
    assert (
        dc1["dc1-wan01"]
        > dc1["dc1-fw01"]
        > dc1["dc1-spine01"]
        > dc1["dc1-leaf01"]
        > dc1["dc1-acc01"]
        > dc1["dc1-srv01"]
    )
    assert set(topo["rack_order"]) >= {n["role"] for n in topo["nodes"].values()}


def test_enrich_play_builds_element_3() -> None:
    text = PLAY.read_text()
    for needle in ("netbox_location", "netbox_rack", "position:"):
        assert needle in text, f"netbox-enrich.yml lacks {needle}"


def test_seed_device_types_take_one_u() -> None:
    """A 0U device type cannot hold a rack position (NetBox), so every type is 1U."""
    assert "u_height: 1" in SEED.read_text()


# --- S4e.4: provider circuits on the four provider links ------------------------------------------------------
def test_circuits_cover_every_provider_link(topo: dict) -> None:
    rows = derive.circuits(topo)
    isp_links = [
        lk
        for lk in topo["links"]
        if lk["a"].startswith("isp-core01:") or lk["b"].startswith("isp-core01:")
    ]
    assert len(rows) == len(isp_links) == 4
    assert {r["cid"] for r in rows} == set(topo["circuits"]), (
        "every circuit in the YAML sits on exactly one link"
    )
    for r in rows:
        assert (
            r["z"]["device"] == "isp-core01"
            and r["z"]["site"] == "wan"
            and r["z"]["interface"].startswith("GigabitEthernet")
        )
        assert (
            r["a"]["site"] == topo["nodes"][r["a"]["device"]]["site"]
            and r["a"]["site"] != "wan"
        )
        assert r["provider"] == "Simulated ISP" and r["type"] == "transit"
    assert {r["cid"]: r["a"]["device"] for r in rows}["ISP-BR1-01"] == "br1-wan01"


def test_seed_cables_skip_circuit_links() -> None:
    text = SEED.read_text()
    task = text[text.index("- name: Cables (one per link)") :]
    task = task[: task.index("- name:", 10)]
    assert "rejectattr('circuit', 'defined')" in task


def test_enrich_play_builds_element_4() -> None:
    text = PLAY.read_text()
    for needle in (
        "netbox_provider",
        "netbox_circuit_type",
        "netbox_circuit:",
        "netbox_circuit_termination",
        "circuits.circuittermination",
    ):
        assert needle in text, f"netbox-enrich.yml lacks {needle}"


# --- S4e.5: journal entries ---------------------------------------------------------------------------------------
def test_enrich_play_journals_only_when_the_changelog_moved() -> None:
    text = PLAY.read_text()
    assert "core/object-changes" in text and "netbox_journal_entry" in text
    task = text[text.index("- name: Journal entry per site") :]
    assert (
        "when: journal_wanted" in task and "journal_this_commit.json.count == 0" in text
    ), (
        "an entry when the run changed NetBox or the first time a commit runs; a repeat run of the same commit writes none"
    )


def test_branch_vlan_workflows_write_a_journal_entry_on_the_switch() -> None:
    import json

    for name, marker in (
        ("wf-branch-vlan-v1", "branch-vlan create"),
        ("wf-branch-vlan-delete-v1", "branch-vlan delete"),
    ):
        wf = json.loads((ROOT / "itential" / "workflows" / f"{name}.json").read_text())
        posts = [
            t
            for t in wf["tasks"].values()
            if t.get("name") == "runCode"
            and "extras/journal-entries/" in t["variables"]["incoming"].get("code", "")
        ]
        assert len(posts) == 1, f"{name}: one runCode task posts the journal entry"
        assert "NETBOX_TOKEN" in posts[0]["variables"]["incoming"]["code"], (
            "the token comes from the runner's environment, never from job variables"
        )
        assert marker in json.dumps(wf), f"{name}: the journal comment names the action"


# --- S4e.6: the agent reads the new objects; Golden Config renders the interface intent ---------------------------
INTEGRATION = ROOT / "itential" / "integrations" / "lab-netbox.json"
AGENT = ROOT / "itential" / "agents" / "netbox-sot.yaml"
GC = ROOT / "itential" / "golden-config"
NEW_READS = {
    "/api/dcim/racks/": "dcim_racks_list",
    "/api/circuits/circuits/": "circuits_circuits_list",
    "/api/ipam/asns/": "ipam_asns_list",
    "/api/ipam/vrfs/": "ipam_vrfs_list",
    "/api/ipam/prefixes/": "ipam_prefixes_list",
}


def test_integration_model_reads_the_enriched_objects() -> None:
    import json

    doc = json.loads(INTEGRATION.read_text())
    for path, op_id in NEW_READS.items():
        assert (
            path in doc["paths"] and doc["paths"][path]["get"]["operationId"] == op_id
        ), f"lab-netbox lacks {op_id}"
    # ADR 0048 asserted the model was read-only. ADR 0054 (S4f) moved the governed workflows' VLAN and
    # journal writes onto it, so the guardrail narrows rather than disappears: every write the document
    # declares must be one the oracle names, so an accidental one is still caught here.
    import yaml as _yaml

    declared = set(
        _yaml.safe_load((ROOT / "itential" / "versions.yaml").read_text())["integrations"]["models"]["netbox"]["write_operations"]
    )
    written = {o["operationId"] for item in doc["paths"].values() for verb, o in item.items() if verb != "get"}
    assert written <= declared, (
        f"lab-netbox declares undeclared write operations {sorted(written - declared)}"
    )


def test_netbox_sot_holds_the_new_reads() -> None:
    import yaml

    agent = yaml.safe_load(AGENT.read_text())
    refs = {t["reference"] for t in agent["tools"]}
    assert set(NEW_READS.values()) <= refs, f"netbox-sot tools: {sorted(refs)}"
    assert "config_context" in agent["instructions"], (
        "the agent is told the device retrieve carries the rendered config context"
    )


def test_golden_config_leaf_renders_interface_intent() -> None:
    for os_dir in ("cisco-ios", "arista-eos"):
        text = (GC / os_dir / "device.j2").read_text()
        assert (
            "for i in interfaces" in text
            and "i.description" in text
            and "ip address" in text
        ), f"{os_dir}/device.j2 renders no interface intent from NetBox"
    assert (
        "'virtual ' if a.role == 'anycast'"
        in (GC / "arista-eos" / "device.j2").read_text()
    ), "EOS anycast addresses render as ip address virtual"
