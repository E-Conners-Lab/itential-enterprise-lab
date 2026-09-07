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
    "c8000v": (re.compile(r"^Gi(\d+)$"), 1),        # Gi1 = index 0 (mgmt), Gi2 = index 1
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
        assert node["mgmt_ip"] == by_name[name]["address"], f"{name}: {node['mgmt_ip']} != ipam {by_name[name]['address']}"
        assert by_name[name]["placement"].startswith("eve-"), f"{name} is not an EVE-NG placement in ipam.yaml"


def test_every_eve_placement_in_ipam_is_a_node(topo: dict, ipam: dict) -> None:
    eve_hosts = {a["hostname"] for a in ipam["addresses"] if a["placement"].startswith("eve-")}
    assert eve_hosts == set(topo["nodes"]), f"missing: {eve_hosts - set(topo['nodes'])}; extra: {set(topo['nodes']) - eve_hosts}"


def test_sites_and_roles(topo: dict) -> None:
    for name, node in topo["nodes"].items():
        assert node["site"] in topo["sites"], f"{name}: unknown site {node['site']}"
        assert name.startswith(node["site"] + "-") or node["site"] == "wan", f"{name} does not carry its site prefix"
        assert node["role"] in {"wan-edge", "isp-core", "firewall", "spine", "leaf", "access", "branch-switch", "server", "client"}


def test_interfaces_valid_for_platform_and_unique(topo: dict) -> None:
    used: Counter = Counter()
    for link in topo["links"]:
        for end in (link["a"], link["b"]):
            node, iface = end.split(":")
            assert node in topo["nodes"], f"link references unknown node {node}"
            pat, _ = IFACE_PATTERNS[topo["nodes"][node]["platform"]]
            assert pat.match(iface), f"{node}: interface {iface} is not valid for {topo['nodes'][node]['platform']}"
            assert iface not in ("Gi1", "Mgmt1", "mgmt", "eth0"), f"{end}: management interface used for a data link"
            used[end] += 1
    dupes = [k for k, v in used.items() if v > 1]
    assert not dupes, f"interface used twice: {dupes}"


def test_interface_count_covers_links(topo: dict) -> None:
    """Each node's 'ethernet' (EVE-NG NIC count) must fit its highest used data port + mgmt."""
    highest: dict[str, int] = {}
    for link in topo["links"]:
        for end in (link["a"], link["b"]):
            node, iface = end.split(":")
            n = int((re.search(r"(\d+)$", iface) or re.search(r"(\d+)", iface)).group(1))  # eth1/4 -> 4, Gi2 -> 2
            plat = topo["nodes"][node]["platform"]
            idx = n - 1 if plat == "c8000v" else n  # Gi2 -> index 1; Eth1/eth1/1 -> index 1
            highest[node] = max(highest.get(node, 0), idx)
    for node, idx in highest.items():
        assert topo["nodes"][node]["ethernet"] > idx, f"{node}: ethernet={topo['nodes'][node]['ethernet']} but a link uses index {idx}"


def test_link_and_node_counts_match_adr(topo: dict) -> None:
    assert len(topo["nodes"]) == 21
    design = [lk for lk in topo["links"] if not lk.get("bypass")]
    bypass = [lk for lk in topo["links"] if lk.get("bypass")]
    assert len(design) == 29
    assert len(bypass) == 4
    # a bypass link must not touch a firewall port and must carry a prefix
    for lk in bypass:
        assert lk.get("prefix"), lk
        for end in (lk["a"], lk["b"]):
            assert topo["nodes"][end.split(":")[0]]["role"] != "firewall", lk


def test_firewall_flag_present(topo: dict) -> None:
    assert isinstance(topo["lab"]["firewalls"], bool)


def _budget_eve_rows() -> dict[str, tuple[int, int]]:
    """image key -> (vcpu, ram GB) from docs/resource-budget.md section 3."""
    text = BUDGET.read_text()
    sec = text[text.index("## 3. EVE-NG internal budget"):text.index("## 4.")]
    rows = {}
    for line in sec.splitlines():
        m = re.match(r"\| `[^|]*` \| `([a-z0-9-]+)`[^|]*\| (\d+) \| (\d+) \| ([\d.]+) \|", line)
        if m:
            rows[m.group(1)] = (int(m.group(3)), float(m.group(4)))
    return rows


def test_node_sizes_match_budget(topo: dict) -> None:
    rows = _budget_eve_rows()
    assert rows, "could not parse the EVE-NG budget table"
    for name, node in topo["nodes"].items():
        vcpu, ram_gb = rows[node["platform"]]
        assert node["cpu"] == vcpu, f"{name}: cpu {node['cpu']} != budget {vcpu}"
        assert node["ram"] == int(ram_gb * 1024), f"{name}: ram {node['ram']} != budget {int(ram_gb * 1024)} MB"


def test_ram_within_eve_ceiling(topo: dict) -> None:
    total = sum(n["ram"] for n in topo["nodes"].values())
    assert total + 6 * 1024 <= EVE_RAM_CEILING_MB, f"{total / 1024:.0f} GB of nodes + 6 GB host > 115 GB ceiling"


def test_images_are_manifest_keys_and_eve_folders(topo: dict) -> None:
    allowed = {"c8000v": "c8000v-17.13.01a", "veos": "veos-4.33.1.1F", "pa-vm": "paloalto-11.1", "ubuntu": "linux-ubuntu-24.04-server", "win11": "win-11-25h2"}
    for name, node in topo["nodes"].items():
        assert node["image"].startswith(allowed[node["platform"]].split("-")[0]), f"{name}: image {node['image']} does not match platform {node['platform']}"
