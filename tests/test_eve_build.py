"""Unit tests for eve/build.py's pure functions: interface index mapping (EVE-NG index 0 is the
management port on every template) and link network naming. No EVE-NG access."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eve"))
import build  # noqa: E402


@pytest.mark.parametrize(
    "platform,iface,index",
    [
        ("c8000v", "Gi1", 0), ("c8000v", "Gi2", 1), ("c8000v", "Gi5", 4),
        ("veos", "Mgmt1", 0), ("veos", "Eth1", 1), ("veos", "Eth6", 6),
        ("pa-vm", "mgmt", 0), ("pa-vm", "eth1/1", 1), ("pa-vm", "eth1/4", 4),
        ("ubuntu", "eth0", 0), ("ubuntu", "eth1", 1), ("win11", "eth1", 1),
    ],
)
def test_iface_index(platform, iface, index):
    assert build.iface_index(platform, iface) == index


@pytest.mark.parametrize("platform,iface", [("c8000v", "Eth1"), ("veos", "Gi2"), ("pa-vm", "ethernet1/1"), ("ubuntu", "ens18")])
def test_iface_index_rejects_foreign_names(platform, iface):
    with pytest.raises(ValueError):
        build.iface_index(platform, iface)


def test_link_network_name_is_filesystem_safe():
    name = build.link_network_name({"a": "dc1-fw01:eth1/4", "b": "dc1-leaf01:Eth6"})
    assert name == "link-dc1-fw01_eth1-4--dc1-leaf01_Eth6"
    assert "/" not in name and ":" not in name


def test_every_topology_link_maps_to_a_valid_index():
    import yaml

    topo = yaml.safe_load((Path(__file__).resolve().parent.parent / "topology" / "enterprise.yaml").read_text())
    for lk in topo["links"]:
        for end in (lk["a"], lk["b"]):
            node, iface = end.split(":")
            idx = build.iface_index(topo["nodes"][node]["platform"], iface)
            assert 0 < idx < topo["nodes"][node]["ethernet"], end
