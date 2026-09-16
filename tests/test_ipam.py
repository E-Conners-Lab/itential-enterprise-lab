"""Unit tests for topology/ipam.yaml, the machine-readable IP plan.

The YAML is the oracle that seeds NetBox (ADR 0002). These tests hold it to
the rules in docs/ip-plan.md and ADR 0003, and cross-check it against the
human-readable table in docs/ip-plan.md so the two sources cannot drift.
They run in CI with no lab access.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
IPAM_PATH = ROOT / "topology" / "ipam.yaml"
IP_PLAN_PATH = ROOT / "docs" / "ip-plan.md"

FORBIDDEN = [
    ipaddress.ip_network(p)
    for p in (
        "192.168.68.0/22",
        "172.29.129.0/24",
        "172.29.130.0/24",
        "172.17.0.0/16",
        "172.18.0.0/16",
        "172.20.20.0/24",
        "10.42.0.0/16",
        "10.43.0.0/16",
    )
]
SUPERNET = ipaddress.ip_network("10.100.0.0/14")
OOB = ipaddress.ip_network("10.100.0.0/24")

# Placement -> allowed last-octet blocks, straight from docs/ip-plan.md §3.1.
BLOCKS = {
    "infrastructure": [(1, 15)],
    "k3s-node": [(16, 31)],
    "k3s-vip": [(32, 63)],
    "proxmox-vm": [(64, 95)],
    "eve-firewall": [(128, 143)],
    "eve-router": [(144, 159)],
    "eve-switch": [(160, 191)],
    "eve-endpoint": [(192, 223)],
    "clab": [(224, 239)],
}


@pytest.fixture(scope="module")
def ipam() -> dict:
    assert IPAM_PATH.exists(), f"{IPAM_PATH} is missing"
    return yaml.safe_load(IPAM_PATH.read_text())


def _addr_rows(ipam: dict) -> list[dict]:
    return ipam["addresses"]


def test_top_level_shape(ipam: dict) -> None:
    for key in ("version", "domain", "supernet", "prefixes", "ranges", "addresses"):
        assert key in ipam, f"missing top-level key {key}"
    assert ipam["domain"] == "lab.internal"
    assert ipaddress.ip_network(ipam["supernet"]) == SUPERNET


def test_prefixes_inside_supernet_and_not_forbidden(ipam: dict) -> None:
    for row in ipam["prefixes"]:
        net = ipaddress.ip_network(row["prefix"])
        assert net.subnet_of(SUPERNET), f"{net} is outside {SUPERNET}"
        for bad in FORBIDDEN:
            assert not net.overlaps(bad), f"{net} overlaps forbidden {bad}"
        assert row["status"] in ("container", "active", "reserved")


def test_oob_prefix_present(ipam: dict) -> None:
    nets = {ipaddress.ip_network(r["prefix"]) for r in ipam["prefixes"]}
    assert OOB in nets and SUPERNET in nets


def test_clab_prefixes(ipam: dict) -> None:
    """ADR 0063: the Containerlab dev topology has a routed management /24 and a host-internal in-band /24.
    Neither may be 10.100.1.0/24, which ADR 0003 keeps for a second OOB VLAN, and neither may overlap the OOB
    /24 or each other - an overlap would make the static route on oob-gw swallow live lab addresses."""
    by_role = {r["role"]: r for r in ipam["prefixes"]}
    assert "clab-management" in by_role and "clab-inband" in by_role, "the clab prefixes are missing (ADR 0063)"
    mgmt = ipaddress.ip_network(by_role["clab-management"]["prefix"])
    inband = ipaddress.ip_network(by_role["clab-inband"]["prefix"])
    assert mgmt == ipaddress.ip_network("10.100.2.0/24") and inband == ipaddress.ip_network("10.100.3.0/24")
    assert by_role["clab-management"]["status"] == "active", "the mgmt prefix is routed and in use"
    second_oob = ipaddress.ip_network("10.100.1.0/24")
    for net in (mgmt, inband):
        assert net.subnet_of(SUPERNET), f"{net} is outside {SUPERNET}"
        assert not net.overlaps(OOB), f"{net} overlaps the OOB /24"
        assert not net.overlaps(second_oob), f"{net} takes the second-OOB reservation of ADR 0003"
    assert not mgmt.overlaps(inband), "the clab mgmt and in-band prefixes overlap"
    clab = next(r for r in _addr_rows(ipam) if r["hostname"] == "clab")
    assert clab["address"] == "10.100.0.224", "the mgmt prefix is routed via clab at .224"


def test_every_name_and_alias_is_unique_across_the_plan(ipam: dict) -> None:
    """Hostnames and aliases all become A records in one zone (unbound on oob-gw). A name used twice resolves to
    two addresses and a client picks one at random - the DNS collision ADR 0063 must never cause by giving the
    dev stack `itential` or `mcp`, which production has carried since the S11 cut-over."""
    names = []
    for row in _addr_rows(ipam):
        names.append(row["hostname"])
        names.extend(row.get("aliases", []))
    inference = ipam["home_lan"].get("inference_host")
    if inference:
        names.append(inference["hostname"])
        names.extend(inference.get("aliases", []))
    duplicates = sorted({n for n in names if names.count(n) > 1})
    assert not duplicates, f"names resolve to more than one address: {duplicates}"


def test_addresses_unique_and_in_oob(ipam: dict) -> None:
    rows = _addr_rows(ipam)
    addrs = [row["address"] for row in rows]
    names = [row["hostname"] for row in rows]
    assert len(addrs) == len(set(addrs)), "duplicate address"
    assert len(names) == len(set(names)), "duplicate hostname"
    for a in addrs:
        assert ipaddress.ip_address(a) in OOB, f"{a} not in {OOB}"
    for n in names:
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]*[a-z0-9]", n), f"bad hostname {n}"


def test_addresses_sit_in_their_placement_block(ipam: dict) -> None:
    for row in _addr_rows(ipam):
        last = int(row["address"].rsplit(".", 1)[1])
        blocks = BLOCKS[row["placement"]]
        assert any(lo <= last <= hi for lo, hi in blocks), f"{row['hostname']} {row['address']} is outside the {row['placement']} block {blocks}"


def test_ranges_do_not_collide_with_statics(ipam: dict) -> None:
    statics = {ipaddress.ip_address(r["address"]) for r in _addr_rows(ipam)}
    for rng in ipam["ranges"]:
        start, end = ipaddress.ip_address(rng["start"]), ipaddress.ip_address(rng["end"])
        assert start < end and start in OOB and end in OOB
        # k3s VIPs are carved from the MetalLB range on purpose; nothing else may sit in a range.
        for s in statics:
            if start <= s <= end:
                row = next(r for r in _addr_rows(ipam) if ipaddress.ip_address(r["address"]) == s)
                assert rng["role"] == "metallb-pool" and row["placement"] == "k3s-vip", f"{row['hostname']} {s} collides with range {rng['role']}"


def test_gateway_and_eve_and_verify_scratch(ipam: dict) -> None:
    by_name = {r["hostname"]: r for r in _addr_rows(ipam)}
    assert by_name["oob-gw"]["address"] == "10.100.0.1"
    assert by_name["eve"]["address"] == "10.100.0.2"
    assert by_name["netbox"]["address"] == "10.100.0.64"
    scratch = by_name["verify-scratch"]
    assert scratch["status"] == "reserved" and scratch["placement"] == "clab"


def test_home_lan_attachment_is_outside_dhcp_pool(ipam: dict) -> None:
    """oob-gw's home-LAN leg must sit below the router's DHCP pool (.131-.250 of the /22)."""
    lan = ipam["home_lan"]
    gw = ipaddress.ip_address(lan["oob_gw_address"])
    assert gw in ipaddress.ip_network("192.168.68.0/22")
    pool_start = ipaddress.ip_address(lan["dhcp_pool_start"])
    assert gw < pool_start, f"{gw} is inside the home DHCP pool starting {pool_start}"
    assert gw not in {ipaddress.ip_address(x) for x in lan["known_statics"]}


def _plan_table_rows() -> dict[str, str]:
    """hostname -> ip from the static-assignment table in docs/ip-plan.md."""
    rows: dict[str, str] = {}
    for line in IP_PLAN_PATH.read_text().splitlines():
        m = re.match(r"\| (10\.100\.0\.\d+) \| ([a-z0-9-]+) \|", line)
        if m:
            rows[m.group(2)] = m.group(1)
    return rows


def test_yaml_matches_ip_plan_markdown(ipam: dict) -> None:
    """Two sources, one truth: every named static in the markdown table is in the YAML with the same IP, and vice versa."""
    md = _plan_table_rows()
    ya = {r["hostname"]: r["address"] for r in _addr_rows(ipam) if r["hostname"] != "verify-scratch"}
    assert md, "could not parse any rows from docs/ip-plan.md"
    assert set(md) == set(ya), f"only in markdown: {set(md) - set(ya)}; only in yaml: {set(ya) - set(md)}"
    for name, ip in md.items():
        assert ya[name] == ip, f"{name}: markdown {ip} vs yaml {ya[name]}"


def test_ip_plan_section_6_is_generated_not_maintained() -> None:
    """Section 6 is the in-band allocation, which lives in topology/enterprise.yaml. It was hand-written
    and drifted from the topology it describes - carried as an open item since phase 7. It is rendered by
    `topology/derive.py --section6` now, and this is what stops it drifting again."""
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "topology/derive.py", "--section6"],
        capture_output=True, text=True, cwd=ROOT, check=False,
    )
    assert out.returncode == 0, f"derive.py --section6 failed: {out.stderr[-800:]}"
    plan = (ROOT / "docs" / "ip-plan.md").read_text()
    assert "## 6. In-band allocation" in plan
    section = plan[plan.index("## 6. In-band allocation"):]
    assert section == out.stdout, (
        "docs/ip-plan.md section 6 differs from topology/enterprise.yaml.\n"
        "Regenerate it: python topology/derive.py --section6 and replace the section."
    )


def test_the_inference_host_is_declared_and_its_address_is_pinned(ipam: dict) -> None:
    """Every FlowAI local profile resolves ollama.lab.internal, which answers with the Mac Mini
    (ADR 0060). Its address is on the home LAN and inside the router's DHCP pool, so it is only stable
    while the router holds a reservation - without one the lease can move and every local agent fails
    at once, with nothing in the lab to blame. Declaring the reservation here is the closest this repo
    can get to enforcing something on the home router, so at least the requirement is not tacit."""
    host = ipam["home_lan"].get("inference_host")
    assert host, "home_lan.inference_host is missing: ollama.lab.internal would resolve nowhere"
    addr = ipaddress.ip_address(host["address"])
    assert addr in ipaddress.ip_network("192.168.68.0/22")
    assert "ollama" in host.get("aliases", []), "ollama.lab.internal must point at the inference host"

    pool = (
        ipaddress.ip_address(ipam["home_lan"]["dhcp_pool_start"]),
        ipaddress.ip_address(ipam["home_lan"]["dhcp_pool_end"]),
    )
    if pool[0] <= addr <= pool[1]:
        assert host.get("dhcp_reservation") is True, (
            f"{addr} is inside the home DHCP pool {pool[0]}-{pool[1]}: either move it below "
            f"{pool[0]} or record the router reservation with dhcp_reservation: true"
        )


def test_the_ollama_alias_left_tools_01(ipam: dict) -> None:
    """Inference moved off the lab entirely (ADR 0060). tools-01 keeps mcp; if `ollama` were still an
    alias here, unbound would render two A records for the same name and resolution would be a
    coin toss."""
    tools = next(r for r in _addr_rows(ipam) if r["hostname"] == "tools-01")
    assert "ollama" not in tools.get("aliases", []), (
        "tools-01 still claims the ollama alias; ollama.lab.internal must answer only the Mac Mini"
    )
