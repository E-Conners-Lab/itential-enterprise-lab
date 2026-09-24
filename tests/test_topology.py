"""topology/enterprise.yaml is the oracle for both the EVE-NG lab and NetBox (ADR 0002, 0034).
These tests hold it to topology/ipam.yaml (management addresses), docs/resource-budget.md
(node sizes and the EVE-NG RAM ceiling), and internal consistency (links, interfaces, images)."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
TOPO = ROOT / "topology" / "enterprise.yaml"
IPAM = ROOT / "topology" / "ipam.yaml"
BUDGET = ROOT / "docs" / "resource-budget.md"

# EVE-NG interface index 0 is management on every template; data ports start at 1.
IFACE_PATTERNS = {
    "c8000v": (re.compile(r"^Gi(\d+)$"), 1),  # Gi1 = index 0 (mgmt), Gi2 = index 1
    "veos": (re.compile(r"^(Mgmt1|Eth(\d+))$"), 0),
    "pa-vm": (re.compile(r"^(mgmt|eth1/(\d+))$"), 0),
    "ubuntu": (re.compile(r"^eth(\d+)$"), 0),
    "win11": (re.compile(r"^eth(\d+)$"), 0),
}
EVE_RAM_CEILING_MB = 115 * 1024


@pytest.fixture(scope="module")
def topo() -> dict:
    assert TOPO.exists(), f"{TOPO} is missing"
    return yaml.safe_load(TOPO.read_text())


@pytest.fixture(scope="module")
def ipam() -> dict:
    return yaml.safe_load(IPAM.read_text())


def test_shape(topo: dict) -> None:
    for key in ("version", "lab", "sites", "nodes", "links"):
        assert key in topo, key
    assert topo["lab"]["mgmt_network"] == "pnet1"
    assert topo["lab"]["domain"] == "lab.internal"


def test_every_node_has_its_ipam_management_address(topo: dict, ipam: dict) -> None:
    by_name = {a["hostname"]: a for a in ipam["addresses"]}
    for name, node in topo["nodes"].items():
        assert name in by_name, f"{name} not in ipam.yaml"
        assert node["mgmt_ip"] == by_name[name]["address"], (
            f"{name}: {node['mgmt_ip']} != ipam {by_name[name]['address']}"
        )
        assert by_name[name]["placement"].startswith("eve-"), (
            f"{name} is not an EVE-NG placement in ipam.yaml"
        )


def test_every_eve_placement_in_ipam_is_a_node(topo: dict, ipam: dict) -> None:
    eve_hosts = {
        a["hostname"] for a in ipam["addresses"] if a["placement"].startswith("eve-")
    }
    assert eve_hosts == set(topo["nodes"]), (
        f"missing: {eve_hosts - set(topo['nodes'])}; extra: {set(topo['nodes']) - eve_hosts}"
    )


def test_sites_and_roles(topo: dict) -> None:
    for name, node in topo["nodes"].items():
        assert node["site"] in topo["sites"], f"{name}: unknown site {node['site']}"
        assert name.startswith(node["site"] + "-") or node["site"] == "wan", (
            f"{name} does not carry its site prefix"
        )
        assert node["role"] in {
            "wan-edge",
            "isp-core",
            "firewall",
            "spine",
            "leaf",
            "access",
            "branch-switch",
            "server",
            "client",
        }


def test_interfaces_valid_for_platform_and_unique(topo: dict) -> None:
    used: Counter = Counter()
    for link in topo["links"]:
        for end in (link["a"], link["b"]):
            node, iface = end.split(":")
            assert node in topo["nodes"], f"link references unknown node {node}"
            pat, _ = IFACE_PATTERNS[topo["nodes"][node]["platform"]]
            assert pat.match(iface), (
                f"{node}: interface {iface} is not valid for {topo['nodes'][node]['platform']}"
            )
            assert iface not in ("Gi1", "Mgmt1", "mgmt", "eth0"), (
                f"{end}: management interface used for a data link"
            )
            used[end] += 1
    dupes = [k for k, v in used.items() if v > 1]
    assert not dupes, f"interface used twice: {dupes}"


def test_interface_count_covers_links(topo: dict) -> None:
    """Each node's 'ethernet' (EVE-NG NIC count) must fit its highest used data port + mgmt."""
    highest: dict[str, int] = {}
    for link in topo["links"]:
        for end in (link["a"], link["b"]):
            node, iface = end.split(":")
            n = int(
                (re.search(r"(\d+)$", iface) or re.search(r"(\d+)", iface)).group(1)
            )  # eth1/4 -> 4, Gi2 -> 2
            plat = topo["nodes"][node]["platform"]
            idx = (
                n - 1 if plat == "c8000v" else n
            )  # Gi2 -> index 1; Eth1/eth1/1 -> index 1
            highest[node] = max(highest.get(node, 0), idx)
    for node, idx in highest.items():
        assert topo["nodes"][node]["ethernet"] > idx, (
            f"{node}: ethernet={topo['nodes'][node]['ethernet']} but a link uses index {idx}"
        )


def test_link_and_node_counts_match_adr(topo: dict) -> None:
    assert len(topo["nodes"]) == 21
    design = [lk for lk in topo["links"] if not lk.get("bypass")]
    bypass = [lk for lk in topo["links"] if lk.get("bypass")]
    assert len(design) == 29
    assert len(bypass) == 4
    # a bypass link must not touch a firewall port and must be routed (prefix) or a VLAN access port
    for lk in bypass:
        assert lk.get("prefix") or lk.get("vlan"), lk
        for end in (lk["a"], lk["b"]):
            assert topo["nodes"][end.split(":")[0]]["role"] != "firewall", lk


def test_firewall_flag_present(topo: dict) -> None:
    assert isinstance(topo["lab"]["firewalls"], bool)


def _budget_eve_rows() -> dict[str, tuple[int, int]]:
    """image key -> (vcpu, ram GB) from docs/resource-budget.md section 3."""
    text = BUDGET.read_text()
    sec = text[text.index("## 3. EVE-NG internal budget") : text.index("## 4.")]
    rows = {}
    for line in sec.splitlines():
        m = re.match(
            r"\| `[^|]*` \| `([a-z0-9-]+)`[^|]*\| (\d+) \| (\d+) \| ([\d.]+) \|", line
        )
        if m:
            rows[m.group(1)] = (int(m.group(3)), float(m.group(4)))
    return rows


def test_node_sizes_match_budget(topo: dict) -> None:
    rows = _budget_eve_rows()
    assert rows, "could not parse the EVE-NG budget table"
    for name, node in topo["nodes"].items():
        vcpu, ram_gb = rows[node["platform"]]
        assert node["cpu"] == vcpu, f"{name}: cpu {node['cpu']} != budget {vcpu}"
        assert node["ram"] == int(ram_gb * 1024), (
            f"{name}: ram {node['ram']} != budget {int(ram_gb * 1024)} MB"
        )


def test_ram_within_eve_ceiling(topo: dict) -> None:
    total = sum(n["ram"] for n in topo["nodes"].values())
    assert total + 6 * 1024 <= EVE_RAM_CEILING_MB, (
        f"{total / 1024:.0f} GB of nodes + 6 GB host > 115 GB ceiling"
    )


def test_images_are_manifest_keys_and_eve_folders(topo: dict) -> None:
    allowed = {
        "c8000v": "c8000v-17.13.01a",
        "veos": "veos-4.33.1.1F",
        "pa-vm": "paloalto-11.1",
        "ubuntu": "linux-ubuntu-24.04-server",
        "win11": "win-11-25h2",
    }
    for name, node in topo["nodes"].items():
        assert node["image"].startswith(allowed[node["platform"]].split("-")[0]), (
            f"{name}: image {node['image']} does not match platform {node['platform']}"
        )


MAKEFILE = ROOT / "Makefile"
MANUAL_STEPS = ROOT / "docs" / "manual-steps.md"
ENDPOINTS_PLAY = ROOT / "ansible" / "playbooks" / "lab-endpoints.yml"


def test_makefile_wires_phase_4_in_build_order() -> None:
    """`make up` must build Phase 4 the way PR #16 did: NetBox first (ADR 0002), then the EVE-NG lab
    (plan gates on missing images), start, the node MAC export the oob-gw DHCP reservations read,
    the endpoint play, verify. push-configs wipes and restarts nodes, so it stays out of the chain."""
    mk = MAKEFILE.read_text()
    assert re.search(r"^phase-network-topology:.*##", mk, re.MULTILINE), (
        "Makefile phase-network-topology target still the stub"
    )
    stub = re.search(r"filter-out ([^,]+),\$\(PHASES\)", mk)
    assert stub and "network-topology" in stub.group(1).split(), (
        "network-topology still listed as unimplemented"
    )
    body = mk.split("phase-network-topology:", 1)[1].split("\n\n", 1)[0]
    steps = [
        "playbooks/netbox-topology.yml",
        "eve/build.py plan",
        "eve/build.py apply",
        "eve/build.py start",
        "eve/build.py export",
        "playbooks/oob-gw.yml",
        "-i inventory/netbox.yml playbooks/lab-endpoints.yml",
        "verify/run.sh",
    ]
    positions = [body.find(s) for s in steps]
    assert all(p >= 0 for p in positions), [
        s for s, p in zip(steps, positions) if p < 0
    ]
    assert positions == sorted(positions), "Phase 4 steps out of order"
    assert "push-configs" not in body


def test_endpoint_play_waits_for_the_guests_to_boot() -> None:
    """Right after `eve/build.py start` the endpoints are still booting; the play must wait for SSH
    instead of failing the first `make up`."""
    play = yaml.safe_load(ENDPOINTS_PLAY.read_text())[0]
    names = [t.get("name", "") for t in play.get("pre_tasks", [])]
    waits = [
        t
        for t in play.get("pre_tasks", [])
        if "ansible.builtin.wait_for_connection" in t
    ]
    assert waits, f"no wait_for_connection in pre_tasks: {names}"
    assert int(waits[0]["ansible.builtin.wait_for_connection"].get("timeout", 0)) >= 600


def test_manual_steps_record_the_ubuntu_golden_image() -> None:
    """The EVE-NG Ubuntu golden image (automation user, key, netplan) was prepared by hand in
    Phase 4 and has no recipe in images/; the gap must be on the manual-steps list."""
    text = MANUAL_STEPS.read_text()
    row = [ln for ln in text.splitlines() if "linux-ubuntu-24.04-server" in ln]
    assert row, "no manual-steps row for the EVE-NG Ubuntu golden image"
    assert "automation" in row[0] and "| 4 |" in row[0]


# --- S4e (ADR 0048): the YAML owns every address; the templates render it byte-identically --------------------
CONFIGS = ROOT / "topology" / "configs"
REFERENCE = ROOT / "topology" / "generated" / "configs"
IPV4 = re.compile(r"(?<![\w.])(\d{1,3}\.){3}\d{1,3}(?![\w.])")
ADDRESSED_PLATFORMS = ("c8000v", "veos")


def _render_all(topo: dict) -> dict[str, str]:
    import os
    import sys

    sys.path.insert(0, str(ROOT / "eve"))
    import build

    os.environ["AUTOMATION_PASSWORD"] = "__AUTOMATION_PASSWORD__"
    return {
        name: build.render_config(node["platform"], name, node, topo)
        for name, node in topo["nodes"].items()
        if node["platform"] in ADDRESSED_PLATFORMS
    }


def test_rendered_configs_match_reference(topo: dict) -> None:
    """The lift of the addresses into the YAML (ADR 0048) must not change one byte of any startup config.
    The reference was rendered from the pre-lift templates with the placeholder password."""
    rendered = _render_all(topo)
    assert {p.stem for p in REFERENCE.glob("*.cfg")} == set(rendered), (
        "reference set != rendered set"
    )
    for name, text in rendered.items():
        assert text == (REFERENCE / f"{name}.cfg").read_text(), (
            f"{name}: rendered config differs from the reference"
        )


def test_templates_carry_no_ipv4_literal() -> None:
    """Every address comes from topology/enterprise.yaml; masks and the any-address are the only literals allowed."""
    for tpl in (CONFIGS / "c8000v.j2", CONFIGS / "veos.j2"):
        found = [m.group(0) for m in IPV4.finditer(tpl.read_text())]
        literals = [ip for ip in found if not ip.startswith("255.") and ip != "0.0.0.0"]
        assert not literals, f"{tpl.name} hard-codes addresses: {sorted(set(literals))}"


def test_addressing_block_per_network_node(topo: dict) -> None:
    for name, node in topo["nodes"].items():
        if node["platform"] not in ADDRESSED_PLATFORMS:
            continue
        addr = node.get("addressing")
        if node["role"] in ("access", "branch-switch"):
            assert addr is None, f"{name}: a layer-2 switch carries no addressing"
            continue
        assert addr, f"{name}: addressing block missing"
        lo = [x for x in addr["loopbacks"] if x["name"] == "Loopback0"]
        assert len(lo) == 1 and lo[0]["address"].endswith("/32"), (
            f"{name}: Loopback0 /32 missing"
        )
        for entry in addr["loopbacks"] + addr.get("svis", []):
            for key in ("address", "virtual"):
                if key in entry:
                    assert re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}/\d{1,2}", entry[key]), (
                        f"{name}: {entry['name']} {key} needs a mask"
                    )
        if node["role"] == "wan-edge" and node["site"] != "dc1":
            assert (
                addr["lan"]["gateway"].endswith("/24") and addr["lan"]["vlan"] == 10
            ), f"{name}: branch LAN gateway"


def test_routing_tunnels_vrfs_and_prefixes_in_the_yaml(topo: dict) -> None:
    tunnels = topo["routing"]["tunnels"]
    assert len(tunnels) == 4
    for t in tunnels:
        for end in (t["a"], t["b"]):
            node, iface = end.split(":")
            assert topo["nodes"][node]["role"] == "wan-edge" and iface.startswith(
                "Tunnel"
            ), end
        assert t["prefix"].endswith("/30"), t
    assert len({(t["a"]) for t in tunnels} | {t["b"] for t in tunnels}) == 8, (
        "one tunnel interface per end"
    )
    assert set(topo["vrfs"]) == {"MGMT", "WAN", "PROD"}
    assert {p["prefix"] for p in topo["inband_prefixes"]} >= {
        "10.103.100.0/24",
        "10.101.254.0/24",
        "10.102.17.0/24",
    }
    for site in ("dc1", "br1", "br2"):
        assert topo["sites"][site]["aggregate"], f"{site}: aggregate prefix"
    assert IPV4.fullmatch(topo["routing"]["firewall_transit_gateway"])
    assert topo["routing"]["loopback_prefixes"] == [
        "10.101.254.0/24",
        "10.101.253.0/24",
    ]


def test_c8000v_ntp_server_follows_its_vrf_definition_and_the_verify_checks_it(topo: dict) -> None:
    """Issue #18: IOS-XE dropped `ntp server vrf MGMT` placed before `vrf definition MGMT` on all five routers' first
    boot. The rendered config keeps NTP after the VRF, and test-04 S3.2b compares the live lines with the rendering."""
    rendered = _render_all(topo)
    routers = [n for n, node in topo["nodes"].items() if node["platform"] == "c8000v"]
    assert routers
    for name in routers:
        lines = rendered[name].splitlines()
        vrf = lines.index("vrf definition MGMT")
        ntp = next(i for i, ln in enumerate(lines) if ln.startswith("ntp server vrf MGMT"))
        assert ntp > vrf, f"{name}: ntp server before its VRF is defined"
    verify = (ROOT / "verify" / "test-04-topology.sh").read_text()
    assert 'check "S3.2b ' in verify and "topology/generated/configs/${n_name}.cfg" in verify
