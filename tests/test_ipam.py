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
