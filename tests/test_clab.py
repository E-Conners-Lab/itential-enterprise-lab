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
CEOS_TEMPLATE = "configs/ceos.cfg.j2"
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

SENTINEL = "Sentinel-not-a-real-password-42"


@pytest.fixture(scope="module")
def oracle() -> dict:
    assert ORACLE.exists(), f"{ORACLE} is missing"
    return yaml.safe_load(ORACLE.read_text())


@pytest.fixture(scope="module")
def env() -> jinja2.Environment:
    return jinja2.Environment(loader=jinja2.FileSystemLoader(ROOT / "clab"), undefined=jinja2.StrictUndefined)


def render_config(env: jinja2.Environment, oracle: dict, node: dict) -> str:
    template = C8000V_TEMPLATE if node["kind"] == "cisco_c8000v" else CEOS_TEMPLATE
    return env.get_template(template).render(node=node, clab_password=SENTINEL, **oracle)


def nodes_by_name(oracle: dict) -> dict:
    return {n["name"]: n for n in oracle["nodes"]}


# --- the oracle's shape (other plays and agents read these exact keys) -------------------------------------


def test_oracle_has_the_agreed_keys(oracle: dict) -> None:
    for key in ("vm", "containerlab", "mgmt", "inband_prefix", "images", "credentials", "nodes", "links", "vlans", "routing", "access_allow"):
        assert key in oracle, f"clab/versions.yaml has no {key}"
    assert set(oracle["vm"]) == {"name", "vm_id", "ip", "cores", "memory_mb", "disk_gb"}
    assert set(oracle["mgmt"]) == {"network", "prefix", "gateway", "bridge"}
    assert set(oracle["images"]["ceos"]) == {"version", "tag"}
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
        "clab-sw1": ("ceos", "10.100.2.21"),
        "clab-sw2": ("ceos", "10.100.2.22"),
    }
    assert oracle["images"]["ceos"] == {"version": "4.33.1.1F", "tag": "ceos:4.33.1.1F"}
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


def test_ceos_version_equals_the_manifest(oracle: dict) -> None:
    text = MANIFEST.read_text()
    summary = re.search(r"^\| `ceos` \|[^|]*\|([^|]*)\|", text, re.M)
    section = re.search(r"^### 2\.5 `ceos`.*?^\| Version \|([^|]*)\|", text, re.M | re.S)
    version = oracle["images"]["ceos"]["version"]
    if not (summary and version in summary.group(1)):
        pytest.skip(f"docs/image-manifest.md does not name cEOS {version} yet (edited by the records change)")
    assert section and version in section.group(1), f"manifest section 2.5 says {section and section.group(1).strip()!r}"


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
            # cisco_c8000v: eth0 is GigabitEthernet1, so ethN is GigabitEthernet(N+1); ceos: ethN is EthernetN
            want = f"GigabitEthernet{index + 1}" if node["kind"] == "cisco_c8000v" else f"Ethernet{index}"
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
    assert topo["topology"]["kinds"]["ceos"]["image"] == oracle["images"]["ceos"]["tag"]
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


def test_ceos_config_leaves_management_alone(env: jinja2.Environment, oracle: dict) -> None:
    for n in [n for n in oracle["nodes"] if n["kind"] == "ceos"]:
        cfg = render_config(env, oracle, n)
        assert "Management0" not in cfg and "Management1" not in cfg, "containerlab configures the mgmt interface"
        for v in oracle["vlans"]:
            assert f"vlan {v['id']}\n   name {v['name']}" in cfg
            assert f"ip address {v['svi'][n['name']]}" in cfg


def test_every_config_carries_the_automation_login_from_the_variable_only(env: jinja2.Environment, oracle: dict) -> None:
    for n in oracle["nodes"]:
        cfg = render_config(env, oracle, n)
        assert re.search(rf"^username {oracle['credentials']['user']} privilege 15 .*secret 0 {SENTINEL}$", cfg, re.M), n["name"]
        assert "ip ssh version 2" in cfg or "management ssh" in cfg
    # the raw templates: every secret/password/key word is followed by a Jinja variable, never a literal
    for path in [ROOT / "clab" / C8000V_TEMPLATE, ROOT / "clab" / CEOS_TEMPLATE, ROOT / "clab" / TOPOLOGY_TEMPLATE]:
        for m in re.finditer(r"\b(secret|password|pre-shared-key|key)\s+(?:0\s+|7\s+)?(\S+)", path.read_text()):
            assert m.group(2).startswith("{{"), f"{path.name}: literal after {m.group(1)!r}: {m.group(2)!r}"
    assert "clab_password" not in (ROOT / "clab" / TOPOLOGY_TEMPLATE).read_text(), "the topology file holds no secret"


def test_no_node_keeps_the_vendor_default_admin_password(env: jinja2.Environment, oracle: dict) -> None:
    """PID success criterion 5: vrnetlab's bootstrap and containerlab's cEOS kind create admin/admin, and the startup
    config is applied after it, so the config must give `admin` the generated password too."""
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
    assert "ARISTA_CEOS_VERSION:-$(clab_value images.ceos.version)" in text
    assert re.search(r"^\s+c8000v\) c8000v ;;$", text, re.M)
    assert re.search(r"^\s+clab-load\) clab_load ;;$", text, re.M)
    for kept in ("microsoft) microsoft ;;", "arista) arista ;;", "itential) itential ;;", "itential-load) itential_load ;;", "itential-load-ha2) itential_load_ha2 ;;"):
        assert kept in text, f"existing target {kept} changed"


def test_fetch_never_hides_a_failed_lookup_behind_local() -> None:
    """`local x=$(cmd)` returns local's status, so set -e never sees cmd fail; declare first, assign, fail loud if empty."""
    text = FETCH.read_text()
    assert not re.search(r"^\s*local\s+\w+=[^\n]*\$\(", text, re.M), "a `local x=$(...)` masks the substitution's failure"
    body = re.search(r"^arista\(\) \{.*?^\}", text, re.M | re.S).group(0)
    assert re.search(r"^\s+local ver$", body, re.M)
    assert re.search(r"^\s+ver=\$\{ARISTA_CEOS_VERSION:-\$\(clab_value images\.ceos\.version\)\}$", body, re.M)
    assert re.search(r'^\s+\[ -n "\$ver" \] \|\| \{ echo "[^"]+"; exit 1; \}$', body, re.M)
    assert body.index("ver=") < body.index('[ -n "$ver" ]') < body.index("$SSH")
    assert subprocess.run(["bash", "-n", str(FETCH)], capture_output=True).returncode == 0
    # the pattern itself, in bash: the fixed form stops, the old form carries on
    fixed = 'set -e; f() { local v; v=${X:-$(false)}; echo reached; }; f'
    old = 'set -e; f() { local v=${X:-$(false)}; echo reached; }; f'
    assert "reached" not in subprocess.run(["bash", "-c", fixed], capture_output=True, text=True, env={"PATH": os.environ["PATH"]}).stdout
    assert "reached" in subprocess.run(["bash", "-c", old], capture_output=True, text=True, env={"PATH": os.environ["PATH"]}).stdout


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
