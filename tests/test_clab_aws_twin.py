"""The AWS end's twin in the clab dev topology (clab/versions.yaml aws_twin; itential-enterprise-lab step 6, build step 5).

Two containers behind clab-rtr1's own front door, as the AWS box sits behind its EIP: kept out of nodes/links (no
management address, no SSH, never an inventory device), addressed where nothing else in the lab lives, and clab-rtr1
carrying exactly the prerequisites lab-edge's precheck reads (cloud-devops-pipeline itential/lab_edge_device.py)."""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

import jinja2
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
ORACLE = yaml.safe_load((ROOT / "clab" / "versions.yaml").read_text())
TWIN = ORACLE["aws_twin"]
PLAYBOOKS = ROOT / "ansible" / "playbooks"
TWIN_DIR = ROOT / "clab" / "aws-twin"
NODES = {n["name"]: n for n in ORACLE["nodes"]}
FD = TWIN["front_door"]["ip"].split("/")[0]


def render(template: str, **extra) -> str:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(ROOT / "clab"), trim_blocks=True, lstrip_blocks=True,
                             undefined=jinja2.StrictUndefined)
    return env.get_template(template).render(**ORACLE, **extra)


def router_config(name: str) -> str:
    return render("configs/c8000v.cfg.j2", node=NODES[name], clab_password="x" * 32)


def blocks(config: str) -> dict[str, list[str]]:
    out, head = {}, None
    for line in config.splitlines():
        if not line.strip() or line.strip() == "!":
            continue
        if line.startswith(" "):
            out[head].append(line.strip())
        else:
            head = line.strip()
            out.setdefault(head, [])
    return out


# ── addressing ──


def test_the_twin_lives_where_nothing_else_in_the_lab_does() -> None:
    internet = ipaddress.ip_network("198.51.100.0/24")  # TEST-NET-2: never a real address, never routed off the VM
    fd, outside = ipaddress.ip_interface(TWIN["front_door"]["ip"]), ipaddress.ip_interface(TWIN["nat"]["outside"]["ip"])
    inside, twin = ipaddress.ip_interface(TWIN["nat"]["inside"]["ip"]), ipaddress.ip_interface(TWIN["twin"]["ip"])
    vpc = ipaddress.ip_network(TWIN["vpc_cidr"])
    assert fd.network == outside.network == internet and fd.ip != outside.ip
    assert inside.network == twin.network and twin.network.subnet_of(vpc)
    ipam = yaml.safe_load((ROOT / "topology" / "ipam.yaml").read_text())
    taken = [ipaddress.ip_network(v) for v in re.findall(r"\b\d+\.\d+\.\d+\.\d+/\d+\b", yaml.safe_dump(ipam))]
    lab = [ipaddress.ip_network(p) for p in (ORACLE["inband_prefix"], ORACLE["mgmt"]["prefix"])] + taken
    for net in (internet, vpc):
        assert not any(net.overlaps(other) for other in lab), net
    assert TWIN["lab_prefixes"] == [ORACLE["inband_prefix"]]


def test_the_twin_mirrors_the_aws_deployment() -> None:
    # cloud-devops-pipeline terraform/modules/vpn defaults: the ids, the inner /30, the XFRM if_id, the VPC
    assert TWIN["ids"] == {"aws": "aws-vpn.lab.internal", "lab": "lab-edge.lab.internal"}
    assert TWIN["tunnel"] == {"aws_inner": "169.254.10.2/30", "router_inner": "169.254.10.1/30", "if_id": 10}
    assert TWIN["vpc_cidr"] == "10.0.0.0/16"


def test_the_twin_image_is_pinned_by_digest() -> None:
    base = TWIN["image"]["base"]
    assert re.fullmatch(r"ubuntu:22\.04@sha256:[0-9a-f]{64}", base), "the AWS box runs Ubuntu 22.04"
    assert TWIN["image"]["tag"] == f"lab/aws-twin:jammy-{base.split(':')[-1][:12]}"
    docker = (TWIN_DIR / "Dockerfile").read_text()
    assert "ARG BASE\nFROM ${BASE}" in docker and "latest" not in docker
    # installed as the AWS box's user data does (recommends included: the gcm and openssl plugins behind
    # aes256gcm16 / ecp256), the plugins named, and the build fails without them
    assert "install -y strongswan strongswan-swanctl libstrongswan-standard-plugins iptables iproute2" in docker
    assert "--no-install-recommends" not in docker
    # nothing in the build may need a running charon (`swanctl --version` queries the daemon: exit 2 at build time)
    assert "swanctl --" not in docker.split("RUN", 1)[1].split("COPY", 1)[0]
    for plugin in ("gcm", "openssl"):
        assert f"test -f /usr/lib/ipsec/plugins/libstrongswan-{plugin}.so" in docker


# ── the topology: containers, not devices ──


def test_the_twin_is_two_containers_with_no_management_address() -> None:
    topo = yaml.safe_load(render("dev.clab.yml.j2"))
    nat, twin = topo["topology"]["nodes"][TWIN["nat"]["name"]], topo["topology"]["nodes"][TWIN["twin"]["name"]]
    for node in (nat, twin):
        assert node["kind"] == "linux" and node["network-mode"] == "none" and node["cap-add"] == ["NET_ADMIN"]
        assert "mgmt-ipv4" not in node and "startup-config" not in node and node["image"] == TWIN["image"]["tag"]
        assert all(b.endswith(":ro") and b.startswith("aws-twin/") for b in node["binds"])
    assert nat["sysctls"] == {"net.ipv4.ip_forward": 1} and "sysctls" not in twin
    assert nat["cmd"] == "/usr/local/sbin/nat.sh" and twin["cmd"] == "/usr/local/sbin/twin.sh"
    assert {TWIN["nat"]["name"], TWIN["twin"]["name"]}.isdisjoint(NODES)  # never in the device loops or inventories
    assert topo["topology"]["links"][-2:] == [
        {"endpoints": [f"{TWIN['router']}:{TWIN['front_door']['endpoint']}", f"{TWIN['nat']['name']}:eth1"]},
        {"endpoints": [f"{TWIN['nat']['name']}:eth2", f"{TWIN['twin']['name']}:{TWIN['twin']['endpoint']}"]}]


def test_the_front_door_uses_a_free_router_port() -> None:
    used = {(side["node"], side["endpoint"]) for link in ORACLE["links"] for side in (link["a"], link["b"])}
    assert (TWIN["router"], TWIN["front_door"]["endpoint"]) not in used
    n = int(TWIN["front_door"]["endpoint"].removeprefix("eth"))
    assert TWIN["front_door"]["ifname"] == f"GigabitEthernet{n + 1}"  # vrnetlab: ethN is GigabitEthernet(N+1)


# ── clab-rtr1: exactly what lab-edge's precheck reads ──


def test_clab_rtr1_carries_the_hand_off_prerequisites() -> None:
    b = blocks(router_config(TWIN["router"]))
    assert b["ip access-list extended INET-IN"] == [
        "10 deny ip any any fragments", f"20 deny udp any host {FD} eq isakmp log",
        f"30 deny udp any host {FD} eq non500-isakmp log", f"40 permit icmp any host {FD} echo-reply",
        f"50 permit icmp any host {FD} unreachable", f"60 permit icmp any host {FD} time-exceeded",
        "1000 deny ip any any log"]
    front = b[f"interface {TWIN['front_door']['ifname']}"]
    assert {"vrf forwarding INET", f"ip address {FD} 255.255.255.0", "ip access-group INET-IN in", "no shutdown"} <= set(front)
    assert not any("zone-member" in x or "ospf" in x for x in front)  # the front door stays unzoned, no routing protocol
    assert b["archive"] == ["path bootflash:rb-", "maximum 5", "log config", "logging enable", "hidekeys"]
    assert {"zone security INSIDE", "zone security AWS"} <= set(b)
    for vty in ("line vty 0 4", "line vty 5 15"):
        assert "access-class MGMT-ONLY in vrf-also" in b[vty]
    assert any(x.startswith("pre-shared-key ") for x in b["crypto ikev2 keyring CANARY"])
    assert "password encryption aes" not in router_config(TWIN["router"])  # the owner's step (spec Appendix A)


def test_exactly_the_transit_interfaces_are_inside() -> None:
    b = blocks(router_config(TWIN["router"]))
    inside = {h.split()[1] for h, body in b.items() if h.startswith("interface ") and "zone-member security INSIDE" in body}
    transit = {side["ifname"] for link in ORACLE["links"] for side in (link["a"], link["b"])
               if side["node"] == TWIN["router"] and side.get("ip")}
    assert inside == transit == {"GigabitEthernet2", "GigabitEthernet3"}


def test_the_management_plane_admits_the_allowlist_and_the_clab_host_only() -> None:
    acl = blocks(router_config(TWIN["router"]))["ip access-list standard MGMT-ONLY"]
    # vrnetlab's QEMU user network: IOS sees every SSH login from 10.0.0.2 (without it, everyone is locked out)
    assert acl[0] == "permit 10.0.0.2 0.0.0.0"
    acl = acl[1:]
    wanted = [*ORACLE["access_allow"], ORACLE["mgmt"]["gateway"] + "/32"]
    assert [x.split()[1] for x in acl] == [w.split("/")[0] for w in wanted]
    for entry, cidr in zip(acl, wanted, strict=True):
        net = ipaddress.ip_network(cidr)
        assert entry == f"permit {net.network_address} {net.hostmask}"


def test_clab_rtr2_is_untouched() -> None:
    config = router_config("clab-rtr2")
    for marker in ("INET-IN", "zone security", "zone-member", "MGMT-ONLY", "archive", "CANARY", "vrf definition INET"):
        assert marker not in config, marker


# ── the containers mirror the AWS box ──


def test_the_twin_boots_like_the_aws_box() -> None:
    twin = (TWIN_DIR / "twin.sh").read_text()
    rules = [line.strip() for line in twin.splitlines() if re.match(r"\s*iptables -(A|P|F) (INPUT|FORWARD)", line)]
    # the policy is DROP before the chains are flushed, so a reload never leaves the box open, even for a moment
    assert rules[:4] == ["iptables -P INPUT DROP", "iptables -P FORWARD DROP", "iptables -F INPUT", "iptables -F FORWARD"]
    appended = [r for r in rules if r.startswith("iptables -A INPUT")]
    assert appended[:2] == ["iptables -A INPUT -i lo -j ACCEPT",
                            "iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT"]
    assert rules[-1] == "iptables -P INPUT DROP"
    assert not re.search(r"--dport 22|sshd|tcp", twin)
    assert twin.index("setup\nrender") < twin.index("/usr/lib/ipsec/charon")  # closed before charon starts
    # the key: its characters and length checked without grep (an embedded newline passed that), put in by shell
    # expansion (never a command's argument), never world-readable
    assert "*[!A-Za-z0-9._+/=-]*)" in twin and '"${#psk}" -lt 32' in twin and "umask 077" in twin
    assert not re.search(r"\bsed\b", twin) and "${template%%\\$PSK*}${psk}${template#*\\$PSK}" in twin
    assert "install_routes = no" in (TWIN_DIR / "charon-lab.conf").read_text()


def test_the_nat_is_one_to_one_like_an_eip() -> None:
    nat = (TWIN_DIR / "nat.sh").read_text()
    assert 'iptables -t nat -A PREROUTING -i eth1 -d "$EIP" -j DNAT --to-destination "$TWIN_IP"' in nat
    assert 'iptables -t nat -A POSTROUTING -o eth1 -s "$TWIN_IP" -j SNAT --to-source "$EIP"' in nat


# ── the plays ──


def test_clab_host_loads_the_modules_and_builds_the_pinned_image() -> None:
    tasks = {t["name"]: t for t in yaml.safe_load((PLAYBOOKS / "clab-host.yml").read_text())[0]["tasks"]}
    modules = tasks["Kernel modules for the twin's route-based IPsec (XFRM interface, ESP)"]
    assert modules["loop"] == ["xfrm_interface", "xfrm_user", "esp4"] and modules["community.general.modprobe"]["persistent"] == "present"
    build = tasks["Build the twin image {{ aws_twin.image.tag }}"]["ansible.builtin.command"]["cmd"]
    assert "--build-arg BASE={{ aws_twin.image.base }}" in build and "-t {{ aws_twin.image.tag }}" in build


def test_clab_dev_keeps_the_twin_out_of_the_device_loops_and_reads_the_pinned_template() -> None:
    plays = yaml.safe_load((PLAYBOOKS / "clab-dev.yml").read_text())
    tasks = {t["name"]: t for t in plays[1]["tasks"]}
    fetch = tasks["The twin's swanctl.conf, filled in from clab/versions.yaml (only $PSK left, put in at the twin's start)"]
    assert fetch["ansible.builtin.command"] == "git -C {{ cdp_repo }} show {{ cdp_pin }}:{{ twin_template }}"
    assert plays[1]["vars"]["twin_template"] == "terraform/modules/vpn/swanctl.conf.tftpl"
    assert "terraform_run.repository.reference" in plays[1]["vars"]["cdp_pin"]
    assert fetch["delegate_to"] == "localhost" and fetch["become"] is False  # read on the Mac, from the local clone
    for name in ("Every node answers SSH as the automation user (nested boot up to 30 min)",
                 "OSPF adjacencies FULL equal the node's links", "BGP sessions Established on both ends"):
        assert tasks[name]["loop"] == "{{ nodes }}", name  # devices only: the twin is not in nodes
    deploy_when = tasks["Deploy (only when a rendered file changed or a node is not running)"]["when"]
    # a redeploy boots every device and wipes clab-rtr1's master key: only a device change or a device down triggers it
    assert "(devices_running | int) < (nodes | length)" in deploy_when and "running_before" not in deploy_when
    counted = tasks["Running device count (the twin's containers not counted)"]["ansible.builtin.set_fact"]["devices_running"]
    assert "device_names" in plays[1]["vars"] and "selectattr('name', 'in', device_names)" in counted
    # the twin is reloaded in place, never restarted (a new network namespace would lose containerlab's links)
    commands = [yaml.safe_dump({k: v for k, v in t.items() if k.startswith("ansible.builtin.")}) for t in tasks.values()]
    assert not any("docker restart" in c or "docker start" in c for c in commands)
    reload = tasks["Twin reloaded in place on a changed file"]
    assert reload["ansible.builtin.command"] == "docker exec {{ item.container }} /usr/local/sbin/{{ item.script }} reload"
    key = tasks["Twin key, generated once on the clab VM (the dev twin's own; build step 6 moves it to the dev Vault)"]
    assert key["no_log"] is True and key["ansible.builtin.shell"]["creates"] == "{{ twin_dir }}/psk"
    assert "umask 077" in key["ansible.builtin.shell"]["cmd"]


def test_every_task_that_touches_the_twin_key_is_not_logged() -> None:
    touching = [t for t in yaml.safe_load((PLAYBOOKS / "clab-dev.yml").read_text())[1]["tasks"]
                if re.search(r"\bpsk\b", yaml.safe_dump(t)) and "swanctl" not in t["name"]]
    assert touching, "the key-generation task"
    for task in touching:
        assert task.get("no_log") is True, task["name"]


def test_the_scripts_reload_in_place() -> None:
    for script in ("twin.sh", "nat.sh"):
        text = (TWIN_DIR / script).read_text()
        assert "reload" in text and "ip addr flush" in text, script
    assert "ip link show \"xfrm$IF_ID\" >/dev/null 2>&1 || ip link add" in (TWIN_DIR / "twin.sh").read_text()
    nat = (TWIN_DIR / "nat.sh").read_text()
    assert nat.index("iptables -t nat -F PREROUTING") < nat.index("iptables -t nat -A PREROUTING")


def test_the_hidekeys_check_reads_what_ios_xe_shows_and_matches_the_whole_line() -> None:
    tasks = {t["name"]: t for t in yaml.safe_load((PLAYBOOKS / "clab-dev.yml").read_text())[1]["tasks"]}
    reads = tasks["Router clab-rtr1 holds the prerequisites lab-edge's precheck reads (the master key aside, the owner's)"]
    archive = next(r for r in reads["loop"] if "archive" in r["cmd"])
    # 17.13 shows hidekeys only in `show running-config all` (measured on clab-rtr1, 2026-10-01)
    assert archive["cmd"] == "show running-config all | section ^archive"
    shown = "archive\n log config\n  logging enable\n  hidekeys\n path bootflash:rb-\n"
    disabled = shown.replace("  hidekeys", "  no hidekeys")
    assert all(w in shown for w in archive["want"]) and not all(w in disabled for w in archive["want"])


def test_the_verify_counts_devices_and_the_twins_containers_apart() -> None:
    # S10.7 compares containerlab's devices with the oracle's nodes, and wants both twin containers running (2026-10-01:
    # it failed when the twin's containers were counted as nodes)
    verify = (ROOT / "verify" / "test-12a-clab-dev.sh").read_text()
    assert 'twin = {o["aws_twin"]["nat"]["name"], o["aws_twin"]["twin"]["name"]}' in verify
    assert "have = {k: v for k, v in have.items() if k not in twin}" in verify
    assert 'twins != {name: "running" for name in twin}' in verify
