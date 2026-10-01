"""Step 10 part C (ADR 0068; F9 decided 2026-10-01): dc1-wan01's prerequisites for Hand Off AWS VPN as code. One source
(enterprise.yaml lab_edge) renders them into c8000v.j2 for dc1-wan01 alone; every value is held to where it is pinned
(versions.yaml's target for lab-edge's precheck, ipam.yaml and oob-gw for the management sources); Golden Config
requires only lines the render produces; NetBox gets the records Hand Off reads back; and the change set sends exactly
the difference between the router before and the new render, never leaving the internet port unfiltered."""

from __future__ import annotations

import importlib.util
import ipaddress
import re
import sys
from pathlib import Path

import jinja2
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from topology import derive  # noqa: E402

TOPO = derive.load_topology()
VERSIONS = yaml.safe_load((ROOT / "itential" / "versions.yaml").read_text())
TARGET = VERSIONS["aws_vpn"]["targets"]["dc1-wan01"]["target"]
EDGE = TOPO["lab_edge"]["dc1-wan01"]
CHANGES = yaml.safe_load((ROOT / "topology" / "changes" / "dc1-wan01-lab-edge.yaml").read_text())
BEFORE = (ROOT / "topology" / "changes" / "dc1-wan01-before.cfg").read_text()
GC_DEVICE = yaml.safe_load((ROOT / "itential" / "golden-config" / "cisco-ios" / "devices" / "dc1-wan01.yml").read_text())


def _render_all() -> dict[str, str]:
    import os

    sys.path.insert(0, str(ROOT / "eve"))
    import build

    os.environ["AUTOMATION_PASSWORD"] = "__AUTOMATION_PASSWORD__"
    return {n: build.render_config(node["platform"], n, node, TOPO) for n, node in TOPO["nodes"].items()
            if node["platform"] == "c8000v"}


RENDERED = _render_all()
AFTER = RENDERED["dc1-wan01"]


def _blocks(text: str) -> dict[str, list[str]]:
    """Top-level line -> its indented lines (every depth, as written)."""
    out, head = {}, None
    for line in text.splitlines():
        if not line.strip() or line.strip() == "!" or line.startswith("{#"):
            continue
        if not line.startswith(" "):
            head = line.rstrip()
            out.setdefault(head, [])
        elif head:
            out[head].append(line.rstrip())
    return out


A, B = _blocks(AFTER), _blocks(BEFORE)


def _global_neighbours(text: str) -> set[str]:
    bgp = _blocks(text)[f"router bgp {TARGET['bgp_asn']}"]
    head = [line for line in bgp if line.startswith(" neighbor ") and " remote-as " in line]
    return {line.split()[1] for line in head}


# ── one source, rendered for dc1-wan01 alone ──


def test_only_the_lab_edge_renders_the_prerequisites() -> None:
    assert set(TOPO["lab_edge"]) == {"dc1-wan01"}
    for name, text in RENDERED.items():
        if name == "dc1-wan01":
            continue
        for marker in ("MGMT-ONLY", "zone security", "zone-member", "archive", "password encryption", "NO-AWS-VPC"):
            assert marker not in text, f"{name} renders {marker}"


def test_the_management_sources_are_the_oob_network_and_the_home_lan_oob_gw_routes_in() -> None:
    ipam = yaml.safe_load((ROOT / "topology" / "ipam.yaml").read_text())
    oob = next(p["prefix"] for p in ipam["prefixes"] if p.get("role") == "oob-management")
    nft = (ROOT / "ansible" / "playbooks" / "templates" / "oob-gw-nftables.conf.j2").read_text()
    home = re.search(r'iifname "\{\{ lan_iface \}\}" oifname "\{\{ oob_iface \}\}" ip saddr (\S+) accept', nft).group(1)
    assert EDGE["mgmt_allow"] == [oob, home] == ["10.100.0.0/24", "192.168.68.0/22"]
    assert A["ip access-list standard MGMT-ONLY"] == [" 10 permit 10.100.0.0 0.0.0.255", " 20 permit 192.168.68.0 0.0.3.255"]
    for vty in ("line vty 0 4", "line vty 5 15"):
        assert A[vty] == [" access-class MGMT-ONLY in vrf-also"]


def test_the_values_are_the_ones_hand_off_is_pinned_to() -> None:
    assert EDGE["aws_vpc"] == TARGET["vpc_cidr"]
    assert EDGE["tunnel"]["address"] == TARGET["router_inner"] and EDGE["tunnel"]["id"] == 10


def test_the_zones_hold_exactly_the_interfaces_lab_edge_expects() -> None:
    zoned = {h.split()[1]: [x for x in body if x.startswith(" zone-member ")] for h, body in A.items()
             if h.startswith("interface ")}
    assert {i for i, z in zoned.items() if z == [" zone-member security INSIDE"]} == set(TARGET["inside_interfaces"])
    assert not any(zoned.get(i) for i in TARGET["unzoned_interfaces"])
    assert "zone security INSIDE" in A and "zone security AWS" in A
    # Tunnel10 is Hand Off's: NetBox holds it, the startup configuration never does
    assert "interface Tunnel10" not in A


def test_the_archive_and_aes_encryption_are_there_and_the_master_key_never_is() -> None:
    assert A["archive"] == [" path bootflash:rb-", " maximum 5", " log config", "  logging enable", "  hidekeys"]
    assert "password encryption aes" in A
    assert "key config-key" not in AFTER


def test_the_vpc_reaches_only_its_bgp_peer() -> None:
    bgp = A[f"router bgp {TARGET['bgp_asn']}"]
    filtered = {line.split()[1] for line in bgp if line.endswith("prefix-list NO-AWS-VPC out")}
    peer = next(line.split()[1] for line in bgp
                if line.startswith(" neighbor ") and line.endswith(f"description {EDGE['aws_bgp_peer']} (firewall bypass)"))
    assert filtered == _global_neighbours(AFTER) - {peer} and peer not in filtered
    assert A["ip prefix-list NO-AWS-VPC seq 5 deny 10.0.0.0/16 le 32"] == []
    assert A["ip prefix-list NO-AWS-VPC seq 10 permit 0.0.0.0/0 le 32"] == []


# ── Golden Config: only what the render produces ──


def test_golden_config_requires_only_rendered_lines() -> None:
    assert GC_DEVICE["zone_members"] == {i: "INSIDE" for i in TARGET["inside_interfaces"]}
    for head, body in _blocks(GC_DEVICE["lines"]).items():
        head = head.removeprefix("<e/>")
        assert head in A, head
        assert set(body) <= set(A[head]), (head, set(body) - set(A[head]))
    # hidekeys is not required (IOS-XE shows it only with `all`); INET-IN's slots 20/30 are Hand Off's to change
    assert "hidekeys" not in GC_DEVICE["lines"]
    inet_in = _blocks(GC_DEVICE["lines"])["<e/>ip access-list extended INET-IN"]
    assert not any(line.split()[0] in ("20", "30") for line in inet_in)


def test_device_j2_merges_the_zone_members_and_appends_the_lines() -> None:
    # as Ansible's template lookup renders it (trim_blocks)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "itential" / "golden-config" / "cisco-ios")),
                             trim_blocks=True)

    def ipaddr(value: str, what: str) -> str:
        i = ipaddress.ip_interface(value)
        return str(i.ip) if what == "address" else str(i.netmask)

    env.filters["ansible.utils.ipaddr"] = ipaddr
    text = env.get_template("device.j2").render(
        device={"name": "dc1-wan01"}, mgmt_if="GigabitEthernet1", mgmt_ip="10.100.0.144",
        interfaces=[{"name": "GigabitEthernet5", "description": "to dc1-wan02:Gi5", "vrf": None,
                     "addresses": [{"address": "10.101.2.249/30"}]},
                    {"name": "GigabitEthernet6", "description": None, "vrf": None, "addresses": []},
                    {"name": "Tunnel10", "description": "IPsec VTI to AWS", "vrf": None,
                     "addresses": [{"address": "169.254.10.1/30"}]}],
        zone_members=GC_DEVICE["zone_members"], skip_interfaces=GC_DEVICE["skip_interfaces"],
        extra_lines=GC_DEVICE["lines"])
    blocks = _blocks(text)
    for iface in ("GigabitEthernet5", "GigabitEthernet6", "Tunnel1", "Tunnel2"):
        assert " zone-member security INSIDE" in blocks[f"<e/>interface {iface}"], iface
    assert "<e/>password encryption aes" in blocks
    # Tunnel10 is Hand Off's (NetBox holds it, a rebuilt router does not): never a requirement
    assert GC_DEVICE["skip_interfaces"] == [f"Tunnel{EDGE['tunnel']['id']}"]
    assert not any("Tunnel10" in h for h in blocks) and "169.254.10.1" not in text
    # a device without its own file renders as before
    plain = env.get_template("device.j2").render(device={"name": "br1-wan01"}, mgmt_if="GigabitEthernet1",
                                                 mgmt_ip="10.100.0.1", interfaces=[])
    assert "zone-member" not in plain


# ── NetBox: what Hand Off reads back ──


def test_netbox_gets_the_records_hand_off_reads_back() -> None:
    assert "cloud-aws" in TOPO["sites"]
    prefixes = {p["prefix"]: p for p in TOPO["inband_prefixes"]}
    assert prefixes[TARGET["vpc_cidr"]]["site"] == "cloud-aws"
    for p in TARGET["vpc_private_prefixes"]:
        assert prefixes[p]["site"] == "cloud-aws"
    inner = str(ipaddress.ip_interface(TARGET["router_inner"]).network)
    assert inner in prefixes
    rows = {r["name"]: r for r in derive.interfaces(TOPO)["dc1-wan01"]}
    assert rows["Tunnel10"]["address"] == TARGET["router_inner"]


# ── the change set: exactly the difference, the port never unfiltered ──


def _sent(step: dict) -> list[str]:
    return [line.rstrip() for line in (step.get("config") or step.get("by_hand") or "").splitlines() if line.strip()]


SCAFFOLD_HEADS = {"ip access-list extended INET-IN-NEXT", "no ip access-list extended INET-IN",
                  "no ip access-list extended INET-IN-NEXT", "key config-key password-encrypt", "end", "write memory"}


def _sent_blocks() -> dict[str, set[str]]:
    """Every step's lines (the pushes and the by-hand step), by head - so a line counts only under the head it is sent
    under."""
    out: dict[str, set[str]] = {}
    # the time-boxed account is the window's, not the router's configuration (never in the template)
    for step in [s for s in CHANGES["steps"] if s["name"] != "time-boxed account"]:
        for head, body in _blocks(step.get("config") or step.get("by_hand") or "").items():
            out.setdefault(head, set()).update(body)
    return out


def test_every_new_line_is_sent_under_its_head_and_nothing_else() -> None:
    sent = _sent_blocks()
    for head, body in A.items():
        new = set(body) - set(B.get(head, []))
        if head not in B:
            assert head in sent, f"new line not sent: {head}"
        assert new <= sent.get(head, set()), (head, sorted(new - sent.get(head, set())))
    for head, body in sent.items():
        if head not in A:
            assert head in SCAFFOLD_HEADS, f"sent but not rendered: {head}"
            continue
        swap = {" ip access-group INET-IN-NEXT in"} if head == "interface GigabitEthernet7" else set()
        extra = body - set(A[head]) - swap
        assert not extra, (head, sorted(extra))
    # the swap's temporary list is INET-IN's own layout
    assert sent["ip access-list extended INET-IN-NEXT"] == set(A["ip access-list extended INET-IN"])


def test_inet_in_is_swapped_never_left_open() -> None:
    lines = _sent(next(s for s in CHANGES["steps"] if "INET-IN" in (s.get("config") or "")))
    i = lines.index
    nxt = lines[i("ip access-list extended INET-IN-NEXT") + 1: i("interface GigabitEthernet7")]
    rebuilt = lines[i("ip access-list extended INET-IN") + 1: len(lines) - lines[::-1].index("interface GigabitEthernet7") - 1]
    assert nxt == rebuilt == A["ip access-list extended INET-IN"]
    # NEXT is applied before INET-IN is removed, INET-IN back on before NEXT is dropped: Gi7 always has a filter
    assert i(" ip access-group INET-IN-NEXT in") < i("no ip access-list extended INET-IN") < i(" ip access-group INET-IN in")
    dropped_at = i("no ip access-list extended INET-IN-NEXT")
    assert i(" ip access-group INET-IN in") < dropped_at
    assert not any("INET-IN-NEXT" in line for line in lines[dropped_at + 1:])
    # and every line the render dropped from INET-IN is gone with the old list
    dropped = set(B["ip access-list extended INET-IN"]) - set(A["ip access-list extended INET-IN"])
    assert dropped and "no ip access-list extended INET-IN" in lines


def test_the_steps_run_in_the_order_the_router_needs() -> None:
    names = [s["name"] for s in CHANGES["steps"]]
    assert names == ["archive", "time-boxed account", "management, INET-IN, prefix-lists, zones", "master key",
                     "zone membership"]
    first, account, *rest = CHANGES["steps"]
    # the archive on the production Platform (dc1-wan01 is in its inventory); the revert pushes on the dev one (the
    # service is dev-tier); the account typed after the archive, so hidekeys masks it, before any push needs it
    assert first["platform"] == "production" and all(s["platform"] == "dev" for s in rest if "workflow" in s)
    revert = VERSIONS["revert_push"]["targets"]["dc1-wan01"]
    # the type-9 line only (make edge-account-line): the plain password never reaches the router
    assert account["by_hand"].splitlines()[0] == (f"username {revert['username']} privilege 15 secret 9 "
                                                  "<the line from make edge-account-line>")
    assert account["by_hand"].splitlines()[-1] == "write memory" and "accepted risk" in (
        ROOT / "topology" / "changes" / "dc1-wan01-lab-edge.yaml").read_text()
    # after R1: unbind and converge FIRST, then the account, then the entry for good (the reverse of the binding hazard)
    unbind, account_off, entry_off = CHANGES["after_r1"]
    assert "unbind" in unbind and "dev converge" in unbind
    assert account_off.startswith(f"no username {revert['username']}") and "write memory" in account_off
    assert entry_off.startswith("vault kv metadata delete lab/devices/")
    # the archive before any revert timer, and never under one (the timer needs it)
    assert first["workflow"] == VERSIONS["workflows"]["config_push"] and _sent(first)[0] == "archive"
    assert all(s["workflow"] == VERSIONS["workflows"]["config_push_revert"] for s in rest if "workflow" in s)
    # the master key only by hand, before the zones (keyrings_type6 is lab-edge's precheck too)
    assert "workflow" not in rest[1] and "password encryption aes" in rest[1]["by_hand"]
    assert not any("config-key" in (s.get("config") or "") for s in CHANGES["steps"])
    assert rest[2]["revert_minutes"] == 10  # the spec: longer than a Hand Off push's 5, BGP must hold first


@pytest.mark.parametrize("step", [s for s in CHANGES["steps"] if s.get("checks")], ids=lambda s: s["name"])
def test_the_checks_cover_every_bgp_neighbour_and_ping_each(step: dict) -> None:
    neighbours = _global_neighbours(AFTER)
    checks = step["checks"]
    assert set(checks["bgp_established"]) == neighbours
    pinged = {p["target"] for p in checks["pings"]}
    # a broken path shows Established for its hold time (clab-rtr1, 2026-10-01): each neighbour is pinged too
    assert neighbours <= pinged
    gateways = {derive.render_context(TOPO, n)["lan"]["gw"] for n in ("br1-wan01", "br2-wan01")}
    assert {p["target"] for p in checks["pings"] if p.get("source") == "Loopback0"} == gateways


def test_the_steps_fit_the_workflows_input_gates() -> None:
    """The input gate's rules for the checks (jsonschema is not installed here, so each rule is applied by hand):
    known fields only, IPv4 targets and neighbours, a source that is an interface or an address, the limits."""
    spec = importlib.util.spec_from_file_location("build", ROOT / "itential" / "workflows" / "build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    gate = build.INPUT_GATES[VERSIONS["workflows"]["config_push_revert"]]
    props = gate["checks"]["properties"]
    ipv4 = re.compile(build.IPV4_TEXT["pattern"])
    source_ok = [re.compile(a["pattern"]) for a in props["pings"]["items"]["properties"]["source"]["anyOf"]]
    for step in CHANGES["steps"]:
        if step.get("workflow") != VERSIONS["workflows"]["config_push_revert"]:
            continue
        checks = step["checks"]
        assert set(checks) <= set(props)
        assert len(checks["bgp_established"]) <= props["bgp_established"]["maxItems"]
        assert all(ipv4.match(n) for n in checks["bgp_established"])
        assert len(checks["pings"]) <= props["pings"]["maxItems"]
        for p in checks["pings"]:
            assert set(p) <= set(props["pings"]["items"]["properties"]) and ipv4.match(p["target"])
            assert "source" not in p or any(r.match(p["source"]) for r in source_ok)
        assert props["settle_seconds"]["minimum"] <= checks["settle_seconds"] <= props["settle_seconds"]["maximum"]
        assert gate["revert_minutes"]["minimum"] <= step["revert_minutes"] <= gate["revert_minutes"]["maximum"]
        assert len(step["reason"]) <= gate["reason"]["maxLength"] and "<" not in step["reason"]


def test_the_zone_change_has_its_exact_inverse_and_a_transit_proof() -> None:
    """Step 4's checks all start or end on the router itself, which the zones never inspect (review of part C):
    forwarding through it is proved after the confirm - each probe counted only once its path is shown to cross
    dc1-wan01 - and the inverse is ready to go back the same way."""
    step = CHANGES["steps"][-1]
    assert step["inverse"].splitlines() == [line.replace(" zone-member", " no zone-member") for line in
                                            step["config"].splitlines()]
    gi6 = A["interface GigabitEthernet6"]
    wan01_gi6 = next(line.split()[2] for line in gi6 if line.startswith(" ip address "))
    leaf = wan01_gi6.rsplit(".", 1)[0] + "." + str(int(wan01_gi6.rsplit(".", 1)[1]) + 1)
    tunnel_of = {t["peer"]: t["ip"] for t in derive.render_context(TOPO, "dc1-wan01")["tunnels"]}
    gateways = {n: derive.render_context(TOPO, n)["lan"]["gw"] for n in ("br1-wan01", "br2-wan01")}
    devices = [p for p in step["transit_proof"] if p["device"] != "verify"]
    for p in devices:
        assert p["path"]["read"].split()[0] == "show" and p["probe"].split()[0] in ("ping", "traceroute")
        if p["device"] == "dc1-leaf01":
            # the leaf reaches each branch LAN through dc1-wan01's Gi6: INSIDE in, a tunnel (INSIDE) out
            assert p["path"]["via"] == wan01_gi6 and p["probe"].endswith(f"source {leaf}")
        else:
            # a branch's path to the leaf crosses dc1-wan01 only via its own tunnel to dc1-wan01
            assert p["path"]["via"] == tunnel_of[p["device"]] and p["probe"].endswith(f"source {gateways[p['device']]}")
    assert {p["probe"].split()[1] for p in devices if p["device"] == "dc1-leaf01"} == set(gateways.values())
    assert {p["device"] for p in devices} == {"dc1-leaf01", "br1-wan01", "br2-wan01"}
    verify = [p for p in step["transit_proof"] if p["device"] == "verify"][0]["probe"]
    assert "S3.5" in verify and 'check "S3.5 ' in (ROOT / "verify" / "test-04-topology.sh").read_text()


def test_the_pre_window_reads_change_nothing_and_the_master_key_step_saves() -> None:
    assert [c["command"].split()[0] for c in CHANGES["pre_window"]] == ["show"] * len(CHANGES["pre_window"])
    by_hand = [s for s in CHANGES["steps"] if "by_hand" in s][0]["by_hand"].splitlines()
    assert by_hand[-2:] == ["end", "write memory"]  # a later rollback must not take the master key step with it
    assert "remove the step-2 archive snapshots" in CHANGES["after_window"]


def test_netconf_and_restconf_admit_only_the_management_sources() -> None:
    """NETCONF and RESTCONF do not use the vty lines; their service-level ACLs (proved on clab-rtr1, IOS-XE 17.13,
    2026-10-01: outside the list NETCONF gets no session and RESTCONF answers 401; inside, a hello and 200)."""
    for line in ("netconf-yang ssh ipv4 access-list name MGMT-ONLY", "restconf ipv4 access-list name MGMT-ONLY"):
        assert line in A and line not in B
        assert f"<e/>{line}" in GC_DEVICE["lines"]
    twin = (ROOT / "clab" / "configs" / "c8000v.cfg.j2").read_text()
    assert twin.index("ip access-list standard MGMT-ONLY") < twin.index("netconf-yang ssh ipv4 access-list name MGMT-ONLY")
    assert "restconf ipv4 access-list name MGMT-ONLY" in twin


def test_the_converge_refuses_to_bind_a_dev_alias_before_its_entry_exists() -> None:
    """The guard in tasks/gateway-vault.yml (step 10 review): a missing entry, or one without the alias's key, stops
    the dev converge before the import binds it."""
    tasks = yaml.safe_load((ROOT / "ansible" / "playbooks" / "tasks" / "gateway-vault.yml").read_text())
    guard = next(t for t in tasks if t["name"] == "No dev alias is bound before its entry exists")
    names = [t["name"] for t in tasks]
    assert names.index("No dev alias is bound before its entry exists") < names.index(
        "Import the provider and aliases (replaces them by name)")
    missing = jinja2.Environment().from_string(guard["vars"]["missing"])

    def result(alias, key, status, data=None):
        return {"item": {"key": alias, "value": {"key": key}}, "status": status, "json": {"data": {"data": data or {}}}}

    out = missing.render(gw_dev_alias_entries={"results": [
        result("a-present", "password", 200, {"password": "x"}),
        result("b-absent", "password", 404),
        result("c-no-key", "psk", 200, {"version": "1"}),
        {"skipped": True, "item": {"key": "d-skipped"}},
    ]})
    assert out.strip() == "['b-absent', 'c-no-key']"
