"""k8s/platform/versions.yaml is the single oracle the Phase 3 play reads for chart and image
versions. This test proves it never drifts from docs/image-manifest.md section 4.1 (the
human-verified record), and that every version is fully pinned (no 'latest', no floating tags)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "k8s" / "platform" / "versions.yaml"
MANIFEST = ROOT / "docs" / "image-manifest.md"

# component key in versions.yaml -> (manifest row label, column that carries the version)
EXPECT = {
    "k3s": ("k3s", "version"),
    "cilium": ("Cilium", "chart"),
    "metallb": ("MetalLB", "chart"),
    "longhorn": ("Longhorn", "chart"),
    "cert_manager": ("cert-manager", "chart"),
    "cnpg": ("CloudNativePG", "chart"),
}


@pytest.fixture(scope="module")
def versions() -> dict:
    assert VERSIONS.exists(), f"{VERSIONS} is missing"
    return yaml.safe_load(VERSIONS.read_text())


def _manifest_platform_rows() -> dict[str, dict[str, str]]:
    """Parse the 4.1 platform table: component -> {version, chart, image} raw cell text."""
    text = MANIFEST.read_text()
    start = text.index("### 4.1 Platform")
    end = text.index("### 4.2")
    rows: dict[str, dict[str, str]] = {}
    for line in text[start:end].splitlines():
        if not line.startswith("| ") or line.startswith("| Component") or line.startswith("|---"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        rows[cells[0]] = {"version": cells[1], "chart": cells[2], "image": cells[3]}
    return rows


def test_every_pin_is_explicit(versions: dict) -> None:
    for name, spec in versions["components"].items():
        for key in ("chart_version", "app_version", "image_tag"):
            if key in spec:
                val = str(spec[key])
                assert val and val != "latest" and not val.endswith("-latest"), f"{name}.{key} is not pinned: {val!r}"
                assert re.search(r"\d", val), f"{name}.{key} has no digits: {val!r}"


def test_k3s_version_matches_manifest(versions: dict) -> None:
    rows = _manifest_platform_rows()
    assert versions["components"]["k3s"]["app_version"] in rows["k3s"]["version"]


@pytest.mark.parametrize("key", ["cilium", "metallb", "longhorn", "cert_manager", "cnpg"])
def test_chart_versions_match_manifest(versions: dict, key: str) -> None:
    rows = _manifest_platform_rows()
    label, col = EXPECT[key]
    spec = versions["components"][key]
    cell = rows[label][col]
    assert str(spec["chart_version"]).lstrip("v") in cell, f"{key}: chart_version {spec['chart_version']} not in manifest cell {cell!r}"
    assert spec["chart_repo"] in cell, f"{key}: chart_repo {spec['chart_repo']} not in manifest cell {cell!r}"


def test_metallb_pool_matches_ipam(versions: dict) -> None:
    ipam = yaml.safe_load((ROOT / "topology" / "ipam.yaml").read_text())
    pool = next(r for r in ipam["ranges"] if r["role"] == "metallb-pool")
    assert versions["metallb"]["pool"] == f"{pool['start']}-{pool['end']}"
    ingress = next(a for a in ipam["addresses"] if a["hostname"] == "ingress")
    assert versions["metallb"]["ingress_ip"] == ingress["address"]


def test_node_addresses_match_ipam(versions: dict) -> None:
    ipam = yaml.safe_load((ROOT / "topology" / "ipam.yaml").read_text())
    by_name = {a["hostname"]: a["address"] for a in ipam["addresses"]}
    for node in versions["cluster"]["nodes"]:
        assert by_name[node["name"]] == node["ip"], node
    assert versions["cluster"]["api_vip"] == by_name["k3s-api"]
