"""Containerlab dev topology (PID S10.6-S10.12, ADR 0063): clab/versions.yaml is the single oracle the clab
plays, templates, images/fetch.sh and verify/test-12a-clab-dev.sh read. These tests hold it to the tofu module,
topology/ipam.yaml, docs/resource-budget.md and docs/image-manifest.md, prove the templates render from it
without a literal secret, and keep the clab devices out of NetBox and off the EVE-NG lab's names.
They run in CI with no lab access."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
from pathlib import Path

import jinja2
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
ORACLE = ROOT / "clab" / "versions.yaml"
TOPOLOGY_TEMPLATE = "dev.clab.yml.j2"
C8000V_TEMPLATE = "configs/c8000v.cfg.j2"
VEOS_TEMPLATE = "configs/veos.cfg.j2"
# one startup-config template per containerlab kind (clab-dev.yml config_template holds the same map)
CONFIG_TEMPLATES = {"cisco_c8000v": C8000V_TEMPLATE, "arista_veos": VEOS_TEMPLATE}
ACCESS_TEMPLATE = "docker-user.sh.j2"
TOFU = ROOT / "tofu" / "clab"
IPAM = ROOT / "topology" / "ipam.yaml"
ENTERPRISE = ROOT / "topology" / "enterprise.yaml"
BUDGET = ROOT / "docs" / "resource-budget.md"
MANIFEST = ROOT / "docs" / "image-manifest.md"
PLAYBOOKS = ROOT / "ansible" / "playbooks"
FETCH = ROOT / "images" / "fetch.sh"
VERIFY = ROOT / "verify" / "test-12a-clab-dev.sh"
ITENTIAL_VERSIONS = ROOT / "itential" / "versions.yaml"
MAKEFILE = ROOT / "Makefile"

SENTINEL = "Sentinel-not-a-real-password-42"


@pytest.fixture(scope="module")
def oracle() -> dict:
    assert ORACLE.exists(), f"{ORACLE} is missing"
    return yaml.safe_load(ORACLE.read_text())


# What ansible.builtin.template renders with (its defaults: trim_blocks true, lstrip_blocks false). The first live
# clab-host.yml run (2026-09-16) rendered the allowlist script with four iptables commands on one line, and the
# topology and both startup configs had lines run together too: `{%- ...` strips the newline BEFORE a tag and
# trim_blocks the one AFTER it. These tests used plain Jinja, which keeps that second newline, so they passed.
ANSIBLE_TEMPLATE_OPTIONS = {"trim_blocks": True, "lstrip_blocks": False}


@pytest.fixture(scope="module")
def env() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(ROOT / "clab"), undefined=jinja2.StrictUndefined, **ANSIBLE_TEMPLATE_OPTIONS
    )


def render_config(env: jinja2.Environment, oracle: dict, node: dict) -> str:
    template = CONFIG_TEMPLATES[node["kind"]]
    return env.get_template(template).render(node=node, clab_password=SENTINEL, **oracle)


def nodes_by_name(oracle: dict) -> dict:
    return {n["name"]: n for n in oracle["nodes"]}


# --- the oracle's shape (other plays and agents read these exact keys) -------------------------------------


def test_oracle_has_the_agreed_keys(oracle: dict) -> None:
    for key in ("vm", "containerlab", "mgmt", "inband_prefix", "images", "credentials", "nodes", "links", "vlans", "routing", "access_allow"):
        assert key in oracle, f"clab/versions.yaml has no {key}"
    assert set(oracle["vm"]) == {"name", "vm_id", "ip", "cores", "memory_mb", "disk_gb"}
    assert set(oracle["mgmt"]) == {"network", "prefix", "gateway", "bridge"}
    assert set(oracle["images"]) == {"veos", "c8000v"}, "the dev topology runs vEOS and C8000v, nothing else"
    assert set(oracle["images"]["veos"]) == {"version", "source", "staged", "vmdk", "tag", "ram_mb", "cpu"}
    assert set(oracle["images"]["c8000v"]) == {"version", "source", "staged", "vrnetlab_repo", "vrnetlab_commit", "tag", "ram_mb"}
    assert oracle["credentials"] == {"user": "automation", "password_env": "CLAB_AUTOMATION_PASSWORD"}
    for node in oracle["nodes"]:
        assert set(node) == {"name", "kind", "mgmt_ipv4", "netmiko", "role", "asn", "loopback"}, node


def test_the_owner_decisions_hold(oracle: dict) -> None:
    assert oracle["vm"] == {"name": "clab", "vm_id": 230, "ip": "10.100.0.224", "cores": 8, "memory_mb": 16384, "disk_gb": 60}
    assert oracle["mgmt"] == {"network": "clab-dev-mgmt", "prefix": "10.100.2.0/24", "gateway": "10.100.2.1", "bridge": "br-clab-dev"}
    assert oracle["inband_prefix"] == "10.100.3.0/24"
    assert {n["name"]: (n["kind"], n["mgmt_ipv4"]) for n in oracle["nodes"]} == {
        "clab-rtr1": ("cisco_c8000v", "10.100.2.11"),
        "clab-rtr2": ("cisco_c8000v", "10.100.2.12"),
        "clab-sw1": ("arista_veos", "10.100.2.21"),
        "clab-sw2": ("arista_veos", "10.100.2.22"),
    }
    # owner decision 2026-09-16 (ADR 0063 amendment): the EVE-NG lab's own vEOS-lab image, built with vrnetlab
    assert oracle["images"]["veos"] == {
        "version": "4.33.1.1F",
        "source": "eve:/opt/unetlab/addons/qemu/veos-4.33.1.1F/hda.qcow2",
        "staged": "/srv/images/veos/vEOS-lab-4.33.1.1F.qcow2",
        "vmdk": "vEOS-lab-4.33.1.1F.vmdk",
        "tag": "vrnetlab/arista_veos:4.33.1.1F",
        "ram_mb": 2048,
        # nested KVM: the default -cpu host,level=9 aborts on MSR 0x345; the virtual PMU must be off (2026-09-16)
        "cpu": "host,level=9,pmu=off",
    }
    assert oracle["images"]["c8000v"]["tag"] == f"vrnetlab/cisco_c8000v:{oracle['images']['c8000v']['version']}"


def test_images_are_pinned(oracle: dict) -> None:
    c8k = oracle["images"]["c8000v"]
    assert re.fullmatch(r"[0-9a-f]{40}", c8k["vrnetlab_commit"]), "vrnetlab_commit must be a full commit SHA"
    assert c8k["vrnetlab_repo"] == "https://github.com/srl-labs/vrnetlab"
    assert re.fullmatch(r"\d+\.\d+\.\d+", str(oracle["containerlab"]))
    for image in oracle["images"].values():
        assert "latest" not in image["tag"] and image["tag"].endswith(":" + image["version"])


def test_the_staged_c8000v_filename_carries_the_version_vrnetlab_parses(oracle: dict) -> None:
    c8k = oracle["images"]["c8000v"]
    name = Path(c8k["staged"]).name
    # cisco/c8000v/Makefile at the pinned commit: sed -n 's/.*[^0-9]\([0-9]\+\.[0-9]\+\.[0-9]\+[a-z]\?\).*/\1/p'
    parsed = re.match(r".*[^0-9]([0-9]+\.[0-9]+\.[0-9]+[a-z]?).*", name)
    assert parsed and parsed.group(1) == c8k["version"], f"vrnetlab would read {parsed and parsed.group(1)!r} from {name}"
    assert name.endswith(".qcow2") and c8k["staged"].startswith("/srv/images/c8000v/")
    assert c8k["source"] == f"eve:/opt/unetlab/addons/qemu/c8000v-{c8k['version']}/virtioa.qcow2"


def veos_makefile_version(image: str) -> str:
    """arista/veos/Makefile at the pinned commit, as Python:
    sed -e 's/.*-\\([0-9]\\.\\([0-9]\\+\\.\\)\\{1,2\\}[0-9]\\{1,2\\}\\([A-Z]\\|\\-EFT[0-9]\\)\\)\\.vmdk$$/\\1/'
    (no match leaves the name unchanged, which vrnetlab's docker-build-common rejects)."""
    m = re.fullmatch(r".*-([0-9]\.([0-9]+\.){1,2}[0-9]{1,2}([A-Z]|-EFT[0-9]))\.vmdk", image)
    return m.group(1) if m else image


def test_the_veos_vmdk_name_carries_the_version_vrnetlab_parses(oracle: dict) -> None:
    veos = oracle["images"]["veos"]
    # the Makefile's own examples, then ours
    assert veos_makefile_version("vEOS-lab-4.17.1.1F.vmdk") == "4.17.1.1F"
    assert veos_makefile_version("vEOS-lab-4.16.14M.vmdk") == "4.16.14M"
    assert veos_makefile_version(veos["vmdk"]) == veos["version"], f"vrnetlab would read {veos_makefile_version(veos['vmdk'])!r}"
    staged = Path(veos["staged"])
    assert staged.suffix == ".qcow2" and veos["staged"].startswith("/srv/images/veos/")
    assert veos["vmdk"] == staged.stem + ".vmdk", "clab-host.yml converts the staged qcow2 to the vmdk of the same name"
    # the vrnetlab tag: $(REGISTRY)$(vendor)_$(name):$(VERSION), REGISTRY=vrnetlab/, VENDOR=Arista NAME=vEOS lower-cased
    assert veos["tag"] == f"vrnetlab/arista_veos:{veos['version']}"
    assert veos["source"] == f"eve:/opt/unetlab/addons/qemu/veos-{veos['version']}/hda.qcow2"


# --- the oracle against the other records ------------------------------------------------------------------


def test_tofu_module_matches_the_oracle(oracle: dict) -> None:
    variables = (TOFU / "variables.tf").read_text()
    default = re.search(r'variable "vm" \{.*?default = \{(.*?)\n  \}', variables, re.S)
    assert default, "tofu/clab/variables.tf has no vm default"
    tf = dict(re.findall(r"(\w+)\s*=\s*\"?([^\"\n]+)\"?", default.group(1)))
    vm = oracle["vm"]
    assert tf == {k: str(v) for k, v in vm.items()}, f"tofu {tf} != oracle {vm}"
    main = (TOFU / "clab.tf").read_text()
    assert re.search(r'cpu \{\s*type\s*=\s*"host"', main), "nested KVM needs cpu type host"
    assert "name        = var.vm.name" in main
    assert 'tags        = ["phase-12", "clab"]' in main
    assert '"${var.vm.ip}/24"' in main


def test_vm_matches_ipam_and_the_budget(oracle: dict) -> None:
    vm = oracle["vm"]
    ipam = yaml.safe_load(IPAM.read_text())
    rows = [a for a in ipam["addresses"] if a["address"] == vm["ip"]]
    assert len(rows) == 1 and rows[0]["hostname"] == vm["name"], rows
    budget = re.search(r"^\| `clab` \| \d+ \| [^|]+ \| (\d+) \| (\d+) \| (\d+) \|", BUDGET.read_text(), re.M)
    assert budget, "docs/resource-budget.md has no clab row"
    assert tuple(int(x) for x in budget.groups()) == (vm["cores"], vm["memory_mb"] // 1024, vm["disk_gb"])


def test_prefixes_match_ipam(oracle: dict) -> None:
    prefixes = {p["prefix"]: p for p in yaml.safe_load(IPAM.read_text())["prefixes"]}
    by_role = {p["role"]: p["prefix"] for p in prefixes.values()}
    if "clab-management" not in by_role:
        pytest.skip("topology/ipam.yaml has no clab-management prefix yet (edited by the records change)")
    assert by_role["clab-management"] == oracle["mgmt"]["prefix"]
    assert oracle["inband_prefix"] in prefixes, f"{oracle['inband_prefix']} is not in topology/ipam.yaml"


def test_containerlab_version_equals_the_manifest(oracle: dict) -> None:
    row = re.search(r"^\| Containerlab \| ([0-9.]+) ", MANIFEST.read_text(), re.M)
    assert row and row.group(1) == str(oracle["containerlab"])


def test_c8000v_version_equals_the_manifest(oracle: dict) -> None:
    assert f"Running: {oracle['images']['c8000v']['version']}" in MANIFEST.read_text()


def test_veos_version_equals_the_eve_ng_lab_and_the_manifest(oracle: dict) -> None:
    """Exact parity (ADR 0063 amendment): the dev switches run the image the EVE-NG lab's vEOS nodes run."""
    version = oracle["images"]["veos"]["version"]
    lab = yaml.safe_load(ENTERPRISE.read_text())["nodes"]
    lab_images = {n["image"] for n in lab.values() if n.get("platform") == "veos"}
    assert lab_images == {f"veos-{version}"}, f"topology/enterprise.yaml vEOS images {sorted(lab_images)} != veos-{version}"
    assert f"/veos-{version}/" in oracle["images"]["veos"]["source"], "the copy is the lab's own image folder"
    text = MANIFEST.read_text()
    section = re.search(r"^### 2\.4 `veos`.*?^\| Version \|([^|]*)\|", text, re.M | re.S)
    assert section and f"Running: {version}" in section.group(1), f"manifest 2.4 says {section and section.group(1).strip()!r}"
    summary = re.search(r"^\| `veos-vrnetlab` \|[^|]*\|([^|]*)\|", text, re.M)
    assert summary and version in summary.group(1), "manifest summary has no veos-vrnetlab row naming the version"
    built = re.search(r"^\| `vrnetlab/arista_veos` \| ([^|]+) \|([^\n]*)$", text, re.M)
    assert built and built.group(1) == version, "manifest 4.5 has no vrnetlab/arista_veos row at the version"
    # the checksum is recorded when fetch.sh copies the image (sha256 compared on EVE-NG and on the host)
    assert oracle["images"]["veos"]["tag"] in built.group(2) and "`MANIFEST.sha256` at fetch" in built.group(2)
    ceos = re.search(r"^\| `ceos` \|[^|]*\|([^|]*)\|", text, re.M)
    assert ceos and "not used by the dev topology" in ceos.group(1)


# --- addressing ------------------------------------------------------------------------------------------


def test_mgmt_addresses_are_unique_and_usable(oracle: dict) -> None:
    net = ipaddress.ip_network(oracle["mgmt"]["prefix"])
    gateway = ipaddress.ip_address(oracle["mgmt"]["gateway"])
    assert gateway in net
    ips = [ipaddress.ip_address(n["mgmt_ipv4"]) for n in oracle["nodes"]]
    assert len(set(ips)) == len(ips), "duplicate mgmt address"
    for ip in ips:
        assert ip in net, f"{ip} outside {net}"
        assert ip not in (net.network_address, net.broadcast_address, gateway, net.network_address + 1), f"{ip} is reserved"


def test_inband_addressing_is_inside_the_prefix_and_does_not_overlap(oracle: dict) -> None:
    inband = ipaddress.ip_network(oracle["inband_prefix"])
    blocks: list[tuple[str, ipaddress.IPv4Network]] = []
    for n in oracle["nodes"]:
        blocks.append((f"{n['name']} loopback", ipaddress.ip_network(f"{n['loopback']}/32")))
    for link in oracle["links"]:
        pair = [link["a"].get("ip"), link["b"].get("ip")] if "svi" not in link else [link["svi"]["a_ip"], link["svi"]["b_ip"]]
        a, b = (ipaddress.ip_interface(x) for x in pair)
        assert a.network == b.network and a.network.prefixlen == 31 and a.ip != b.ip, f"{pair} is not one /31"
        blocks.append((f"link {link['a']['node']}-{link['b']['node']}", a.network))
    for v in oracle["vlans"]:
        prefix = ipaddress.ip_network(v["prefix"])
        blocks.append((f"VLAN {v['id']}", prefix))
        for node, svi in v["svi"].items():
            assert ipaddress.ip_interface(svi).network == prefix, f"VLAN {v['id']} {node} {svi}"
    for name, block in blocks:
        assert block.subnet_of(inband), f"{name} {block} outside {inband}"
    for i, (n1, b1) in enumerate(blocks):
        for n2, b2 in blocks[i + 1 :]:
            assert not b1.overlaps(b2), f"{n1} {b1} overlaps {n2} {b2}"
    assert not ipaddress.ip_network(oracle["mgmt"]["prefix"]).overlaps(inband)


def test_node_names_never_collide_with_the_lab(oracle: dict) -> None:
    lab = set(yaml.safe_load(ENTERPRISE.read_text())["nodes"])
    names = [n["name"] for n in oracle["nodes"]]
    assert len(set(names)) == len(names)
    for name in names:
        assert name.startswith("clab-"), name
        assert name not in lab, f"{name} is an EVE-NG lab device"


def test_links_name_real_interfaces(oracle: dict) -> None:
    nodes = nodes_by_name(oracle)
    seen: set[tuple[str, str]] = set()
    for link in oracle["links"]:
        for side in (link["a"], link["b"]):
            node = nodes[side["node"]]
            index = int(re.fullmatch(r"eth(\d+)", side["endpoint"]).group(1))
            assert index >= 1, "eth0 is management"
            # cisco_c8000v: eth0 is GigabitEthernet1, so ethN is GigabitEthernet(N+1); arista_veos: eth0 is
            # Management1, so ethN is EthernetN
            want = {"cisco_c8000v": f"GigabitEthernet{index + 1}", "arista_veos": f"Ethernet{index}"}[node["kind"]]
            assert side["ifname"] == want, f"{side}: expected {want}"
            assert (side["node"], side["endpoint"]) not in seen, f"{side} used twice"
            seen.add((side["node"], side["endpoint"]))
        if "trunk" in link:
            assert {v["id"] for v in oracle["vlans"]} | {link["svi"]["vlan"]} == set(link["trunk"])


def test_bgp_sessions_agree_with_loopbacks_links_and_asns(oracle: dict) -> None:
    nodes = nodes_by_name(oracle)
    link_pairs = {
        frozenset({(l["a"]["node"], l["a"]["ip"].split("/")[0]), (l["b"]["node"], l["b"]["ip"].split("/")[0])})
        for l in oracle["links"]
        if "ip" in l["a"]
    }
    for s in oracle["routing"]["bgp"]:
        a, b = nodes[s["a"]], nodes[s["b"]]
        if s["type"] == "ibgp":
            assert a["asn"] == b["asn"] and (s["a_ip"], s["b_ip"]) == (a["loopback"], b["loopback"]), s
        else:
            assert s["type"] == "ebgp" and a["asn"] != b["asn"], s
            assert frozenset({(s["a"], s["a_ip"]), (s["b"], s["b_ip"])}) in link_pairs, f"{s} is not on a link"
    for node, prefixes in oracle["routing"]["announce"].items():
        assert set(prefixes) <= {v["prefix"] for v in oracle["vlans"]} and node in nodes


# --- templates -----------------------------------------------------------------------------------------------


def test_topology_renders_from_the_oracle(env: jinja2.Environment, oracle: dict) -> None:
    topo = yaml.safe_load(env.get_template(TOPOLOGY_TEMPLATE).render(**oracle))
    assert topo["name"] == "dev"
    assert topo["mgmt"]["network"] == oracle["mgmt"]["network"]
    assert topo["mgmt"]["ipv4-subnet"] == oracle["mgmt"]["prefix"]
    assert topo["mgmt"]["ipv4-gw"] == oracle["mgmt"]["gateway"]
    assert topo["mgmt"]["bridge"] == oracle["mgmt"]["bridge"]
    assert topo["mgmt"]["external-access"] is False, "clab-host.yml's allowlist is the only gate"
    rendered = topo["topology"]["nodes"]
    assert set(rendered) == {n["name"] for n in oracle["nodes"]}
    for n in oracle["nodes"]:
        assert rendered[n["name"]]["mgmt-ipv4"] == n["mgmt_ipv4"]
        assert rendered[n["name"]]["kind"] == n["kind"]
        assert rendered[n["name"]]["startup-config"] == f"configs/{n['name']}.cfg"
    assert topo["topology"]["kinds"]["cisco_c8000v"]["image"] == oracle["images"]["c8000v"]["tag"]
    assert set(topo["topology"]["kinds"]) == {"cisco_c8000v", "arista_veos"}
    assert topo["topology"]["kinds"]["arista_veos"]["image"] == oracle["images"]["veos"]["tag"]
    # vrnetlab reads QEMU_MEMORY (common/vrnetlab.py VM.ram), so the oracle's number is the one QEMU gets
    # and QEMU_CPU (VM.cpu): without pmu=off the vEOS VM aborts under nested KVM two seconds after launch
    assert topo["topology"]["kinds"]["arista_veos"]["env"] == {
        "QEMU_MEMORY": str(oracle["images"]["veos"]["ram_mb"]),
        "QEMU_CPU": oracle["images"]["veos"]["cpu"],
    }
    assert "pmu=off" in oracle["images"]["veos"]["cpu"].split(",")
    assert topo["topology"]["kinds"]["cisco_c8000v"]["env"] == {"QEMU_MEMORY": str(oracle["images"]["c8000v"]["ram_mb"])}
    assert [l["endpoints"] for l in topo["topology"]["links"]] == [
        [f"{l['a']['node']}:{l['a']['endpoint']}", f"{l['b']['node']}:{l['b']['endpoint']}"] for l in oracle["links"]
    ]


def test_c8000v_config_sets_the_licence_level_and_leaves_management_alone(env: jinja2.Environment, oracle: dict) -> None:
    for n in [n for n in oracle["nodes"] if n["kind"] == "cisco_c8000v"]:
        cfg = render_config(env, oracle, n)
        assert "license boot level network-advantage" in cfg
        assert f"hostname {n['name']}" in cfg
        assert "interface GigabitEthernet1\n" not in cfg, "vrnetlab owns GigabitEthernet1 (mgmt VRF clab-mgmt)"
        assert f"router bgp {n['asn']}" in cfg and "router ospf" in cfg


def test_veos_config_leaves_management_alone(env: jinja2.Environment, oracle: dict) -> None:
    switches = [n for n in oracle["nodes"] if n["kind"] == "arista_veos"]
    assert len(switches) == 2
    for n in switches:
        cfg = render_config(env, oracle, n)
        assert "Management0" not in cfg and "Management1" not in cfg, "vrnetlab's bootstrap owns Management1"
        assert not re.search(r"^ip route 0\.0\.0\.0/0", cfg, re.M), "vrnetlab's bootstrap owns the default route"
        # launch.py types each line in `configure terminal`, then sends `end` itself
        assert not re.search(r"^end$", cfg, re.M), "vrnetlab ends the config session; an `end` here would run twice"
        assert f"hostname {n['name']}" in cfg and f"router bgp {n['asn']}" in cfg and "router ospf" in cfg
        for v in oracle["vlans"]:
            assert f"vlan {v['id']}\n   name {v['name']}" in cfg
            assert f"ip address {v['svi'][n['name']]}" in cfg


def test_every_config_carries_the_automation_login_from_the_variable_only(env: jinja2.Environment, oracle: dict) -> None:
    for n in oracle["nodes"]:
        cfg = render_config(env, oracle, n)
        assert re.search(rf"^username {oracle['credentials']['user']} privilege 15 .*secret 0 {SENTINEL}$", cfg, re.M), n["name"]
        assert "ip ssh version 2" in cfg or "management ssh" in cfg
    # the raw templates: every secret/password/key word is followed by a Jinja variable, never a literal
    for path in [ROOT / "clab" / C8000V_TEMPLATE, ROOT / "clab" / VEOS_TEMPLATE, ROOT / "clab" / TOPOLOGY_TEMPLATE]:
        for m in re.finditer(r"\b(secret|password|pre-shared-key|key)\s+(?:0\s+|7\s+)?(\S+)", path.read_text()):
            assert m.group(2).startswith("{{"), f"{path.name}: literal after {m.group(1)!r}: {m.group(2)!r}"
    assert "clab_password" not in (ROOT / "clab" / TOPOLOGY_TEMPLATE).read_text(), "the topology file holds no secret"


def test_no_node_keeps_the_vendor_default_admin_password(env: jinja2.Environment, oracle: dict) -> None:
    """PID success criterion 5: containerlab starts vrnetlab's C8000v and vEOS bootstraps with admin/admin, and the
    startup config is applied after them, so the config must give `admin` the generated password too."""
    for n in oracle["nodes"]:
        cfg = render_config(env, oracle, n)
        admin = [ln for ln in cfg.splitlines() if re.match(r"username admin\b", ln)]
        assert len(admin) == 1, f"{n['name']}: exactly one line sets the admin user ({admin})"
        assert re.fullmatch(rf"username admin privilege 15 (role network-admin )?secret 0 {SENTINEL}", admin[0]), admin[0]
        assert not re.search(r"\b(password|secret)( [0-9])? admin\b", cfg), f"{n['name']}: a literal admin password"
    # nothing the lab runs logs in to a node as admin: the plays and the verify use the automation account
    for path in (PLAYBOOKS / "clab-dev.yml", PLAYBOOKS / "clab-host.yml", VERIFY, ROOT / "verify" / "devcmd.py"):
        text = path.read_text()
        assert not re.search(r"username[=:]\s*['\"]?admin\b|-u admin\b|admin@clab|\badmin/admin\b", text), path.name
    assert 'user: str = "automation"' in (ROOT / "verify" / "devcmd.py").read_text()
    assert "admin`/`admin` login on every node beside" not in (ROOT / "clab" / "README.md").read_text()


# --- plays, access and scripts --------------------------------------------------------------------------------


def test_docker_user_allowlist_equals_access_allow(env: jinja2.Environment, oracle: dict) -> None:
    itential_dev = yaml.safe_load(ITENTIAL_VERSIONS.read_text())["vm"]["ip"]
    assert f"{itential_dev}/32" in oracle["access_allow"] and f"{oracle['vm']['ip']}/32" in oracle["access_allow"]
    assert "192.168.68.0/22" in oracle["access_allow"]
    script = env.get_template(ACCESS_TEMPLATE).render(**oracle)
    accepted = re.findall(r'^iptables -w -A "\$CHAIN" -s (\S+) -j ACCEPT$', script, re.M)
    assert accepted == oracle["access_allow"]
    rules = [l for l in script.splitlines() if l.startswith('iptables -w -A "$CHAIN"')]
    assert rules[-1] == 'iptables -w -A "$CHAIN" -j DROP', "the chain must end in DROP"
    assert f"BRIDGE={oracle['mgmt']['bridge']}" in script
    assert 'iptables -w -I DOCKER-USER 1 -o "$BRIDGE" -j "$CHAIN"' in script
    host = (PLAYBOOKS / "clab-host.yml").read_text()
    assert "src: ../../clab/docker-user.sh.j2" in host and "WantedBy=docker.service" in host


def test_the_test_renderer_matches_ansible(env: jinja2.Environment) -> None:
    """Every template assertion in this file is only as good as the renderer; it must be Ansible's."""
    assert env.trim_blocks is True and env.lstrip_blocks is False


def test_rendered_files_have_one_command_per_line(env: jinja2.Environment, oracle: dict) -> None:
    """The failure shape of a whitespace-stripping tag: two commands on one line. Checked on the rendered output."""
    script = env.get_template(ACCESS_TEMPLATE).render(**oracle)
    assert subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True).returncode == 0
    assert not [l for l in script.splitlines() if l.count("iptables ") > 1], "two iptables commands on one line"
    # N + F + ESTABLISHED + RETURN + DROP, one ACCEPT per source, then N DOCKER-USER and the jump (the -D loop is a
    # `while` line)
    assert len(re.findall(r"^iptables ", script, re.M)) == 5 + len(oracle["access_allow"]) + 2
    topo = env.get_template(TOPOLOGY_TEMPLATE).render(**oracle)
    assert not re.search(r":[ \t]+\S+:[ \t]*$", topo, re.M), "a YAML key followed by another key on the same line"
    starts = re.compile(r"(?:^|\s)(neighbor|username|interface|network|router|vlan|ip address|hostname) ")
    for n in oracle["nodes"]:
        for line in render_config(env, oracle, n).splitlines():
            assert len(starts.findall(line)) <= 1, f"{n['name']}: two commands on one line: {line!r}"


def test_plays_read_the_oracle(oracle: dict) -> None:
    host = (PLAYBOOKS / "clab-host.yml").read_text()
    dev = (PLAYBOOKS / "clab-dev.yml").read_text()
    for play in (host, dev):
        assert "../../clab/versions.yaml" in play
        assert str(oracle["containerlab"]) not in play.replace(f"v{oracle['containerlab']}", ""), "no literal containerlab version"
    assert "hosts: clab-host" in host and "hosts: clab-host" in dev
    assert "kvm-ok" in host and "{{ images.c8000v.vrnetlab_commit }}" in host
    assert "docker network create" in host and "{{ mgmt.bridge }}" in host
    # the device password: persisted to .env before use, never logged
    secrets = yaml.safe_load(dev)[0]
    assert secrets["hosts"] == "localhost"
    persist = [t for t in secrets["tasks"] if "lineinfile" in str(t)]
    assert persist and all(t.get("no_log") for t in secrets["tasks"])
    deploy = yaml.safe_load(dev)[1]
    for task in deploy["tasks"]:
        if "clab_password" in str(task):
            assert task.get("no_log") is True, f"{task['name']} handles the password without no_log"


def test_oob_gw_routes_task_never_applies_netplan() -> None:
    play = yaml.safe_load((PLAYBOOKS / "oob-gw.yml").read_text())[0]
    routes = [t for t in play["tasks"] if "routes" in (t.get("tags") or [])]
    assert len(routes) == 1, "oob-gw.yml needs exactly one routes-tagged task"
    text = yaml.safe_dump(routes[0])
    assert "clab/versions.yaml" in text
    assert "ip route replace" in text
    assert "61-lab-routes.yaml" in text
    assert "netplan apply" not in text
    assert "register: return_path" not in text and "return_path" not in text
    # the only apply in the play still fires on the return-path file alone
    applies = [t for t in play["tasks"] if "netplan apply" in yaml.safe_dump(t)]
    assert len(applies) == 1 and applies[0]["when"] == "return_path is changed"


def test_no_netbox_play_registers_clab_devices(oracle: dict) -> None:
    for play in PLAYBOOKS.glob("netbox-*.yml"):
        text = play.read_text()
        assert "clab/versions.yaml" not in text, f"{play.name} reads the clab oracle"
        for n in oracle["nodes"]:
            assert n["name"] not in text, f"{play.name} names {n['name']}"


def test_fetch_reads_versions_from_the_oracle() -> None:
    text = FETCH.read_text()
    assert "4.33.10M" not in text, "ARISTA_CEOS_VERSION defaults to clab/versions.yaml, not a literal"
    assert "ARISTA_CEOS_VERSION:-$(clab_value images.veos.version)" in text
    assert "images.ceos" not in text, "clab/versions.yaml has no cEOS image any more"
    assert re.search(r"^\s+c8000v\) c8000v ;;$", text, re.M)
    assert re.search(r"^\s+veos\) veos ;;$", text, re.M)
    assert re.search(r"^\s+clab-load\) clab_load ;;$", text, re.M)
    for kept in ("microsoft) microsoft ;;", "arista) arista ;;", "itential) itential ;;", "itential-load) itential_load ;;", "itential-load-ha2) itential_load_ha2 ;;"):
        assert kept in text, f"existing target {kept} changed"


def test_fetch_never_hides_a_failed_lookup_behind_local() -> None:
    """`local x=$(cmd)` returns local's status, so set -e never sees cmd fail; declare first, assign, fail loud if empty."""
    text = FETCH.read_text()
    assert not re.search(r"^\s*local\s+\w+=[^\n]*\$\(", text, re.M), "a `local x=$(...)` masks the substitution's failure"
    body = re.search(r"^arista\(\) \{.*?^\}", text, re.M | re.S).group(0)
    assert re.search(r"^\s+local ver$", body, re.M)
    assert re.search(r"^\s+ver=\$\{ARISTA_CEOS_VERSION:-\$\(clab_value images\.veos\.version\)\}$", body, re.M)
    assert re.search(r'^\s+\[ -n "\$ver" \] \|\| \{ echo "[^"]+"; exit 1; \}$', body, re.M)
    assert body.index("ver=") < body.index('[ -n "$ver" ]') < body.index("$SSH")
    assert subprocess.run(["bash", "-n", str(FETCH)], capture_output=True).returncode == 0
    # the pattern itself, in bash: the fixed form stops, the old form carries on
    fixed = 'set -e; f() { local v; v=${X:-$(false)}; echo reached; }; f'
    old = 'set -e; f() { local v=${X:-$(false)}; echo reached; }; f'
    assert "reached" not in subprocess.run(["bash", "-c", fixed], capture_output=True, text=True, env={"PATH": os.environ["PATH"]}).stdout
    assert "reached" in subprocess.run(["bash", "-c", old], capture_output=True, text=True, env={"PATH": os.environ["PATH"]}).stdout


def _function_body(text: str, name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{.*?^\}}", text, re.M | re.S)
    assert m, f"images/fetch.sh has no {name}()"
    return m.group(0)


def test_fetch_veos_copies_the_eve_ng_image_like_c8000v_with_the_checksum_compared_on_both_ends() -> None:
    text = FETCH.read_text()
    veos, c8k = _function_body(text, "veos"), _function_body(text, "c8000v")
    # a mirror of the reviewed c8000v copy, not a second implementation
    assert veos.replace("veos", "X") == c8k.replace("c8000v", "X")
    assert "src=$(clab_value images.veos.source); src=${src#eve:}" in veos
    assert "staged=$(clab_value images.veos.staged)" in veos
    eve_sum = veos.index('want=$($EVE "sha256sum')
    host_sum = veos.index('got=$($SSH "sha256sum')
    compare = veos.index('[ "$got" = "$want" ]')
    manifest = veos.index("sha256sum ${key}/${name} >> MANIFEST.sha256")
    assert eve_sum < host_sum < compare < manifest, "the MANIFEST line is written only after both sums agree"
    assert "${name}.part" in veos and 'mv ${STAGING}/${key}/${name}.part' in veos


def test_clab_load_relays_the_veos_qcow2_not_a_ceos_tarball() -> None:
    body = _function_body(FETCH.read_text(), "clab_load")
    assert 'for rel in "veos/${veos_name}" "c8000v/${c8000v_name}"; do' in body
    assert 'veos_name=$(basename "$(clab_value images.veos.staged)")' in body
    assert "ceos" not in body.lower() and "tar.xz" not in body


def test_make_clab_dev_stages_veos_not_the_arista_download() -> None:
    recipe = re.search(r"^clab-dev:.*?\n((?:\t[^\n]*\n)+)", MAKEFILE.read_text(), re.M)
    assert recipe, "Makefile has no clab-dev target"
    lines = [ln.strip() for ln in recipe.group(1).splitlines()]
    assert "images/fetch.sh veos" in lines and "images/fetch.sh arista" not in lines
    assert lines.index("images/fetch.sh veos") < lines.index("images/fetch.sh clab-load")


def test_clab_host_builds_veos_with_vrnetlab_and_asserts_the_tags(oracle: dict) -> None:
    text = (PLAYBOOKS / "clab-host.yml").read_text()
    tasks = {t["name"]: t for t in yaml.safe_load(text)[0]["tasks"]}
    assert "docker import" not in text
    apt = next(t for t in tasks.values() if "ansible.builtin.apt" in t and isinstance(t["ansible.builtin.apt"].get("name"), list))
    assert {"qemu-utils", "bsdutils"} <= set(apt["ansible.builtin.apt"]["name"])
    # both loops list vEOS first, the when conditions index [0] for vEOS and [1] for the C8000v
    for name in ("Images already in Docker", "Images present now"):
        assert tasks[name]["loop"] == ["{{ images.veos.tag }}", "{{ images.c8000v.tag }}"], name
    assert tasks["Staged files from images/fetch.sh clab-load"]["loop"] == ["{{ veos_file }}", "{{ c8000v_file }}"]
    convert = tasks["Staged vEOS qcow2 converted to the vmdk in the vrnetlab build directory"]
    cmd = convert["ansible.builtin.shell"]["cmd"]
    assert cmd.startswith("qemu-img convert -O vmdk {{ stage_dir }}/{{ veos_file }} {{ images.veos.vmdk }}.part")
    assert "mv {{ images.veos.vmdk }}.part {{ images.veos.vmdk }}" in cmd
    assert convert["ansible.builtin.shell"]["chdir"] == "{{ vrnetlab_dir }}/arista/veos"
    build = tasks["Build the vEOS image with vrnetlab {{ images.veos.tag }}"]
    assert build["ansible.builtin.command"]["cmd"] == 'script -qec "make IMAGE={{ images.veos.vmdk }} docker-build" /dev/null'
    assert build["ansible.builtin.command"]["chdir"] == "{{ vrnetlab_dir }}/arista/veos"
    idempotent = "have_images.results[0].rc != 0 and staged.results[0].stat.exists"
    assert convert["when"] == idempotent and build["when"] == idempotent
    assert build["async"] >= 1800 and build["poll"] > 0
    # the staged-but-missing assertion runs after every build
    names = list(tasks)
    check = tasks["Every staged image is in Docker under the oracle tag"]
    assert check["ansible.builtin.assert"]["that"] == "item.1.rc == 0" and check["when"] == "item.0.stat.exists"
    assert check["loop"] == "{{ staged.results | zip(have_images_after.results) | list }}"
    assert names.index("Images present now") < names.index("Every staged image is in Docker under the oracle tag")


def test_every_oracle_kind_has_a_template_and_a_mgmt_interface(oracle: dict) -> None:
    kinds = {n["kind"] for n in oracle["nodes"]}
    assert kinds == set(CONFIG_TEMPLATES)
    deploy = yaml.safe_load((PLAYBOOKS / "clab-dev.yml").read_text())[1]
    assert {k: f"configs/{v}.cfg.j2" for k, v in deploy["vars"]["config_template"].items()} == CONFIG_TEMPLATES
    for template in CONFIG_TEMPLATES.values():
        assert (ROOT / "clab" / template).exists(), template

    def find(node: object) -> dict | None:
        if isinstance(node, dict):
            if "clab_mgmt_if" in node:
                return node["clab_mgmt_if"]
            node = list(node.values())
        if isinstance(node, list):
            for item in node:
                found = find(item)
                if found is not None:
                    return found
        return None

    mgmt_if = find(yaml.safe_load((PLAYBOOKS / "platform.yml").read_text()))
    assert mgmt_if == {"cisco_c8000v": "GigabitEthernet1", "arista_veos": "Management1"}


def test_bgp_convergence_check_reads_eos_json_for_veos() -> None:
    deploy = yaml.safe_load((PLAYBOOKS / "clab-dev.yml").read_text())[1]
    bgp = next(t for t in deploy["tasks"] if t["name"] == "BGP sessions Established on both ends")
    assert "'show ip bgp summary | json' if node.kind == 'arista_veos'" in bgp["ansible.builtin.command"]
    until = bgp["until"]
    assert "node.kind == 'arista_veos'" in until and "node.kind == 'cisco_c8000v'" in until
    # vrnetlab types the vEOS config in after the login works: a missing default VRF is "not yet", never an error
    assert "(bgp.stdout | from_json).vrfs | default({})).get('default', {}).get('peers', {})" in until
    assert "bgp.stdout | trim | first | default('') == '{'" in until


def test_no_ceos_is_left_in_the_dev_topology() -> None:
    paths = [p for p in (ROOT / "clab").rglob("*") if p.is_file()]
    paths += [PLAYBOOKS / "clab-host.yml", PLAYBOOKS / "clab-dev.yml", VERIFY, ROOT / "verify" / "test-05b-dev-copilot.sh"]
    for path in paths:
        assert "ceos" not in path.read_text().lower(), f"{path.relative_to(ROOT)} still names cEOS"


def test_verify_checks_the_veos_model_and_version() -> None:
    text = VERIFY.read_text()
    assert 'veos, c8k = o["images"]["veos"]["version"], o["images"]["c8000v"]["version"]' in text
    assert 'if model != "vEOS-lab":' in text
    assert 'if not (v == veos or v.startswith(veos + "-")):' in text
    assert 'arista_veos) dev "$ip" "show version | json"' in text
    # the lab side of the parity, read at run time as well
    assert 'if n.get("platform") == "veos"' in text and 'f"veos-{veos}"' in text


def _running_before() -> str:
    deploy = yaml.safe_load((PLAYBOOKS / "clab-dev.yml").read_text())[1]
    task = next(t for t in deploy["tasks"] if t["name"] == "Running node count")
    return task["ansible.builtin.set_fact"]["running_before"]


@pytest.mark.parametrize(
    ("rc", "stdout", "running"),
    [
        (0, "", 0),
        (0, "   \n", 0),
        (0, "no containers found", 0),  # a plain-text notice with rc 0 must not reach from_json
        (1, '{"dev": []}', 0),
        (0, '{"dev": [{"state": "running"}, {"state": "exited"}, {"state": "running"}]}', 2),
        (0, '\n  [{"state": "running"}]\n', 1),
    ],
)
def test_running_before_parses_only_json(rc: int, stdout: str, running: int) -> None:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["from_json"] = json.loads
    env.filters["flatten"] = lambda rows: [x for row in rows for x in (row if isinstance(row, list) else [row])]  # Ansible's
    rendered = env.from_string(_running_before()).render(inspect_before={"rc": rc, "stdout": stdout})
    assert int(rendered.strip()) == running


def test_verify_script_covers_s10_6_to_s10_12() -> None:
    assert VERIFY.exists(), f"{VERIFY} is missing"
    assert os.access(VERIFY, os.X_OK), f"{VERIFY} is not executable"
    text = VERIFY.read_text()
    for n in range(6, 13):
        assert re.search(rf'^check "S10\.{n} ', text, re.M), f"S10.{n} not checked"
    assert "CLAB_AUTOMATION_PASSWORD" in text and "verify/devcmd.py" in text
    assert "verify/results/" in text


def _jinja_search_env() -> jinja2.Environment:
    env = jinja2.Environment()
    env.tests["search"] = lambda value, pattern: re.search(pattern, value) is not None
    env.filters["regex_escape"] = re.escape
    return env


def test_the_containerlab_version_check_matches_real_output() -> None:
    """The first live run installed the pinned 0.79.0 and still failed: the assert ended its regex in '\\b', which a
    Jinja string literal turns into a backspace. Runs the play's own expression against containerlab's real output."""
    play = yaml.safe_load((ROOT / "ansible" / "playbooks" / "clab-host.yml").read_text())
    task = next(t for p in play for t in p.get("tasks", []) if t.get("name") == "Containerlab version equals clab/versions.yaml")
    check = _jinja_search_env().compile_expression(task["ansible.builtin.assert"]["that"])
    pinned = yaml.safe_load((ROOT / "clab" / "versions.yaml").read_text())["containerlab"]
    real = f"  ____ ___  _   _ _____  _    ___ _   _ _____ ____  _       _\n    version: {pinned}\n     commit: 1a2b3c4\n       date: 2026-08-21"
    assert check(clab_version={"stdout": real}, containerlab=pinned) is True
    assert check(clab_version={"stdout": real.replace(pinned, pinned + "1")}, containerlab=pinned) is False, "0.79.01 is not 0.79.0"
    assert check(clab_version={"stdout": "    version: 0.78.0"}, containerlab=pinned) is False


def test_no_clab_play_uses_an_escape_ansible_and_jinja_read_differently() -> None:
    """Measured on ansible-core 2.20.3 (2026-09-16): Ansible does not process escapes in Jinja string literals, plain
    Jinja does. So '\\\\d' stays a double backslash under Ansible and never matches (it stripped nothing from
    containerlab's `10.100.2.11/24`, and the deploy check failed), while '\\b' is a word boundary under Ansible and a
    backspace under plain Jinja. A test rendering with jinja2 cannot vouch for either, so the clab plays use neither
    inside a regex argument; '\\s' and '\\d' read the same in both."""
    offenders = []
    for name in ("clab-dev.yml", "clab-host.yml"):
        for n, line in enumerate((PLAYBOOKS / name).read_text().splitlines(), 1):
            code = line.split(" # ")[0]
            # `x is search('...')`, `regex_replace('...')` and `map('regex_replace', '...')` / `select('match', '...')`
            patterns = re.findall(r"(?:search|match|regex_\w+)\(\s*'([^']*)'", code)
            patterns += re.findall(r"'(?:search|match|regex_\w+)'\s*,\s*'([^']*)'", code)
            for arg in patterns:
                if "\\\\" in arg or re.search(r"\\b", arg):
                    offenders.append(f"{name}:{n}: {arg}")
    assert not offenders, offenders


def test_the_deploy_check_compares_bare_addresses() -> None:
    """containerlab 0.79 `inspect --format json` reports ipv4_address with its prefix length (measured 2026-09-16:
    `10.100.2.11/24`); the oracle holds bare addresses. Runs the play's own expression over the real row shape."""
    play = yaml.safe_load((PLAYBOOKS / "clab-dev.yml").read_text())
    task = next(t for p in play for t in p.get("tasks", []) if t.get("name") == "Every node running with its oracle mgmt address")
    env = jinja2.Environment()
    env.filters["regex_replace"] = lambda value, pattern, repl="": re.sub(pattern, repl, value)
    rows = [
        {"name": "clab-dev-clab-rtr1", "state": "running", "ipv4_address": "10.100.2.11/24"},
        {"name": "clab-dev-clab-sw1", "state": "exited", "ipv4_address": "10.100.2.21/24"},
    ]
    running = yaml.safe_load(env.from_string(task["vars"]["running_ips"]).render(rows=rows))
    # plain Jinja can't tell '\\d' from '\\\\d' the way Ansible does; the escape test above covers that half
    assert running == ["10.100.2.11"], "prefix stripped, exited nodes left out"
