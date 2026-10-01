"""The AWS VPN lab edge targets and the step-6 services on the dev Gateway (itential/versions.yaml aws_vpn,
terraform_run.dev_services, vault.dev_gateway_aliases). Every pinned value a target carries is held to the place it
comes from - the clab oracle for clab-rtr1, dc1-wan01's generated configuration and the deployed outputs for
dc1-wan01 - so Hand Off can never render against values that drifted from the router they describe."""

from __future__ import annotations

import importlib.util
import ipaddress
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
V = yaml.safe_load((ROOT / "itential" / "versions.yaml").read_text())
CLAB = yaml.safe_load((ROOT / "clab" / "versions.yaml").read_text())
TARGETS = V["aws_vpn"]["targets"]
DEV_SERVICES = {s["name"]: s for s in V["terraform_run"]["dev_services"]}
DC1_CFG = (ROOT / "topology" / "generated" / "configs" / "dc1-wan01.cfg").read_text()
TASKS = ROOT / "ansible" / "playbooks" / "tasks"

# cloud-devops-pipeline itential/lab_edge_render.py TARGET_KEYS (check_target refuses any other set)
TARGET_KEYS = {
    "name", "fvrf", "tunnel_source", "front_door_ip", "loopback_ip", "router_inner", "aws_inner", "vpc_cidr",
    "vpc_private_prefixes", "lab_prefixes", "local_id", "remote_id", "peer_rule", "peer_pinned", "bgp_asn",
    "inside_interfaces", "unzoned_interfaces", "mgmt_host",
}
OUTPUT_KEYS = {"strongswan_eip", "strongswan_private_ip", "tunnel_local_inner", "tunnel_remote_inner",
               "vpc_private_prefixes"}


def env(kind: str, target: str) -> str:
    """lab_edge_device.env_name: each target's secrets in their own variables."""
    return f"LAB_EDGE_{kind}_{target.upper().replace('-', '_')}"


def interfaces(config: str) -> dict[str, list[str]]:
    out, head = {}, None
    for line in config.splitlines():
        if line.startswith("interface "):
            head = line.split()[1]
            out[head] = []
        elif head and line.startswith(" "):
            out[head].append(line.strip())
        elif line.strip() and not line.startswith(" "):
            head = None
    return out


# ── every target is what the renderer takes ──


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_each_target_is_exactly_what_lab_edge_takes(name: str) -> None:
    entry = TARGETS[name]
    assert set(entry) >= {"window", "username", "monitor", "psk_path", "target"} and entry["window"] in ("open", "closed")
    t = entry["target"]
    assert set(t) == TARGET_KEYS and t["name"] == name
    assert t["router_inner"] == "169.254.10.1/30" and t["aws_inner"] == "169.254.10.2/30"
    assert t["vpc_cidr"] == "10.0.0.0/16" and t["fvrf"] == "INET"
    assert (t["local_id"], t["remote_id"]) == ("lab-edge.lab.internal", "aws-vpn.lab.internal")
    assert (t["peer_rule"] == "pinned") == (t["peer_pinned"] is not None)
    assert t["tunnel_source"] in t["unzoned_interfaces"] and not set(t["inside_interfaces"]) & set(t["unzoned_interfaces"])
    if "outputs" in entry:
        assert set(entry["outputs"]) == OUTPUT_KEYS


def test_both_targets_pin_the_deployed_vpc() -> None:
    # the vpc module's two private /20s, as Deploy AWS VPN's job 39fd9d23 reported them (2026-09-30)
    for entry in TARGETS.values():
        assert entry["target"]["vpc_private_prefixes"] == ["10.0.64.0/20", "10.0.80.0/20"]


# ── clab-rtr1: held to the clab oracle ──


def test_clab_rtr1_is_the_twins_router_as_the_clab_oracle_has_it() -> None:
    twin, entry = CLAB["aws_twin"], TARGETS["clab-rtr1"]
    t, out = entry["target"], entry["outputs"]
    rtr1 = next(n for n in CLAB["nodes"] if n["name"] == "clab-rtr1")
    assert twin["router"] == "clab-rtr1" and entry["window"] == "open" and entry["monitor"] == "none"
    assert t["tunnel_source"] == twin["front_door"]["ifname"] and t["front_door_ip"] == twin["front_door"]["ip"].split("/")[0]
    assert t["fvrf"] == twin["front_door"]["vrf"] and t["vpc_cidr"] == twin["vpc_cidr"]
    assert t["peer_pinned"] == out["strongswan_eip"] == twin["nat"]["outside"]["ip"].split("/")[0]
    assert out["strongswan_private_ip"] == twin["twin"]["ip"].split("/")[0]
    assert (out["tunnel_local_inner"], out["tunnel_remote_inner"]) == (twin["tunnel"]["aws_inner"], twin["tunnel"]["router_inner"])
    assert (t["remote_id"], t["local_id"]) == (twin["ids"]["aws"], twin["ids"]["lab"])
    assert t["lab_prefixes"] == twin["lab_prefixes"]
    assert (t["loopback_ip"], t["mgmt_host"], t["bgp_asn"]) == (rtr1["loopback"], rtr1["mgmt_ipv4"], rtr1["asn"])
    transit = {side["ifname"] for link in CLAB["links"] for side in (link["a"], link["b"])
               if side["node"] == "clab-rtr1" and side.get("ip")}
    assert set(t["inside_interfaces"]) == transit
    assert set(t["unzoned_interfaces"]) == {"GigabitEthernet1", twin["front_door"]["ifname"]}
    assert entry["username"] == CLAB["credentials"]["user"]


# ── dc1-wan01: held to its generated configuration ──


def test_dc1_wan01_is_the_router_its_generated_configuration_describes() -> None:
    entry = TARGETS["dc1-wan01"]
    t, ifs = entry["target"], interfaces(DC1_CFG)
    assert entry["window"] == "closed" and entry["monitor"] == "aws" and entry["psk_path"] == V["vault"]["aws"]["psk_path"]
    assert f"vrf forwarding {t['fvrf']}" in ifs[t["tunnel_source"]]
    assert any(x.startswith(f"ip address {t['front_door_ip']} ") for x in ifs[t["tunnel_source"]])
    assert any(x.startswith(f"ip address {t['loopback_ip']} ") for x in ifs["Loopback0"])
    assert any(x.startswith(f"ip address {t['mgmt_host']} ") for x in ifs["GigabitEthernet1"])
    assert re.search(rf"^router bgp {t['bgp_asn']}$", DC1_CFG, re.MULTILINE)
    # INSIDE is every global-table transit interface (an address and no VRF), the loopback aside
    global_l3 = {i for i, body in ifs.items() if any(x.startswith("ip address ") for x in body)
                 and not any(x.startswith("vrf forwarding") for x in body) and not i.startswith("Loopback")}
    assert set(t["inside_interfaces"]) == global_l3
    in_vrf = {i for i, body in ifs.items() if any(x.startswith("vrf forwarding") for x in body)}
    assert set(t["unzoned_interfaces"]) == in_vrf
    assert t["peer_rule"] == "public" and "outputs" not in entry  # the deployment's own outputs (terraform-run)
    lab = [ipaddress.ip_network(p) for p in t["lab_prefixes"]]
    assert ipaddress.ip_network("10.101.0.0/16") in lab and ipaddress.ip_address(t["loopback_ip"]) in lab[1]


# ── the dev Gateway: open targets only, the key only in lab-edge-push ──


def test_only_open_targets_have_aliases_bound() -> None:
    # the AWS VPN services; config-push-revert binds the revert targets' passwords (revert_push, test_config_push_revert)
    aws_vpn = {n: s for n, s in DEV_SERVICES.items() if n != "config-push-revert"}
    for name, entry in TARGETS.items():
        bound = {s["target"] for svc in aws_vpn.values() for s in svc["secrets"]}
        names = {env("PASSWORD", name), env("PSK", name), env("PSK_VERSION", name)}
        if entry["window"] == "open":
            assert names <= bound, name
        else:
            assert not names & bound, f"{name} is closed: binding its aliases would fail every run until they exist"


def test_the_key_is_bound_to_lab_edge_push_alone() -> None:
    psk_targets = {"LAB_EDGE_PSK_CLAB_RTR1", "LAB_EDGE_PSK_VERSION_CLAB_RTR1"}
    for name, svc in DEV_SERVICES.items():
        targets = {s["target"] for s in svc["secrets"]}
        assert bool(targets & psk_targets) == (name == "lab-edge-push"), name
    assert {s["target"] for s in DEV_SERVICES["aws-vpn-monitor"]["secrets"]} == {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}
    for svc in DEV_SERVICES.values():
        assert svc["filename"] == f"itential/{svc['name']}.py" and svc["working-directory"] == "."
        assert all(s["type"] == "env" for s in svc["secrets"])


def test_every_bound_alias_exists_and_the_key_reads_from_the_targets_path() -> None:
    aliases = {**V["vault"]["gateway_aliases"], **V["vault"]["dev_gateway_aliases"]}
    for svc in DEV_SERVICES.values():
        for s in svc["secrets"]:
            assert s["name"] in aliases, s["name"]
    dev = V["vault"]["dev_gateway_aliases"]
    psk_path = TARGETS["clab-rtr1"]["psk_path"]
    assert dev == {"aws-vpn-psk-clab-rtr1": {"path": psk_path, "key": "psk"},
                   "aws-vpn-psk-version-clab-rtr1": {"path": psk_path, "key": "version"},
                   # dc1-wan01's time-boxed account (make edge-account), bound by config-push-revert
                   "dc1-wan01-aws-vpn-password": {"path": "devices/dc1-wan01-aws-vpn", "key": "password"}}
    # under devices/*: the Gateway reads it, the Platform cannot
    roles = V["vault"]["approles"]
    assert psk_path.startswith("devices/") and "devices/*" in roles["itential-gateway"]["policy_paths"]
    assert roles["itential-platform"]["policy_paths"] == ["services/*"]
    # the login password is the device account's, the alias the inventory already uses
    assert aliases["lab-automation-password"] == {"path": "devices/automation", "key": "password"}


def _expr(path: Path, var: str) -> str:
    """The Jinja expression a task file sets `var` to, as written."""
    line = next(x for x in path.read_text().splitlines() if x.strip().startswith(f"{var}:"))
    return line.split(":", 1)[1].strip().strip('"')


@pytest.mark.skipif(not shutil.which("ansible"), reason="needs ansible on PATH")
@pytest.mark.parametrize("overlay", [None, "false", "False", "no", "true"])
def test_the_dev_only_items_are_imported_only_where_dev_overlay_is_set(overlay: str | None) -> None:
    # rendered by Ansible itself: `-e dev_overlay=false` arrives as the string 'false', which is truthy without | bool
    services = _expr(TASKS / "gateway-terraform-run.yml", "gw_tr_services")
    aliases = _expr(TASKS / "gateway-vault.yml", "gw_alias_map")
    msg = "{{ {'services': (%s) | map(attribute='name') | list, 'aliases': (%s) | list} | to_json }}" % (
        services[2:-2], aliases[2:-2])
    cmd = ["ansible", "localhost", "-i", "localhost,", "-c", "local", "-o", "-m", "ansible.builtin.debug", "-a",
           f"msg={msg}", "-e", f"@{ROOT / 'itential' / 'versions.yaml'}"]
    if overlay is not None:
        cmd += ["-e", f"dev_overlay={overlay}"]
    run = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=ROOT,
                         env={**os.environ, "ANSIBLE_NOCOLOR": "1"})
    assert run.returncode == 0, (run.stdout + run.stderr)[-500:]
    result = json.loads(run.stdout.split("localhost | SUCCESS => ", 1)[1])  # -o: one line per host
    got = result["msg"] if isinstance(result["msg"], dict) else json.loads(result["msg"])
    prod_services = [s["name"] for s in V["terraform_run"]["services"]]
    if overlay == "true":
        assert got["services"] == prod_services + list(DEV_SERVICES)
        assert set(got["aliases"]) == set(V["vault"]["gateway_aliases"]) | set(V["vault"]["dev_gateway_aliases"])
    else:
        assert got["services"] == prod_services and set(got["aliases"]) == set(V["vault"]["gateway_aliases"])
    dev_vars = yaml.safe_load((ROOT / "ansible" / "playbooks" / "vars" / "itential-dev.yml").read_text())
    assert dev_vars["dev_overlay"] is True


# ── S13.2d (verify/vaultcheck.py lab-edge): the probes never reach a device or AWS, and each failure is caught ──

CDP_RENDER = Path.home() / "PycharmProjects" / "cloud-devops-pipeline" / "itential" / "lab_edge_render.py"
KEY = "K" * 40


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def lab_edge(monkeypatch):
    if not CDP_RENDER.exists():
        pytest.skip("needs the cloud-devops-pipeline clone for the render module")
    vc, render = _load("vaultcheck", ROOT / "verify" / "vaultcheck.py"), _load("lab_edge_render_local", CDP_RENDER)
    monkeypatch.delenv("VAULT_TIER", raising=False)
    entry = TARGETS["clab-rtr1"]
    checked = render.check_target(entry["target"])
    sha = render.sha256(render.render(checked, render.check_outputs(checked, entry["outputs"])))
    world = {"caps": {"itential-gateway": ["read"], "itential-platform": ["deny"]}, "data": {"psk": KEY, "version": "a-1"},
             "sha": sha, "push": {"sent": False, "key_sent": False, "router": "unchanged: nothing was sent",
                                  "error": "the block rendered now is not the approved one (SHA-256 differs): nothing sent"},
             "masked": "crypto ikev2 keyring ... pre-shared-key <from Vault>", "runs": []}

    def vault(method, path, token=None, body=None):
        if path == "sys/capabilities-self":
            return 200, {"capabilities": world["caps"][token]}
        if method == "GET":
            return 200, {"data": {"data": world["data"]}}
        return 204, None

    def service(name, params):
        world["runs"].append((name, params))
        if name == "lab-edge":
            return 0, {"target": "clab-rtr1", "sha256": world["sha"], "block_masked": world["masked"]}
        if name == "lab-edge-push":
            return 1, world["push"]
        return 1, {"error": "--instance_id is not an EC2 instance ID"}

    monkeypatch.setattr(vc, "vault", vault)
    monkeypatch.setattr(vc, "admin", lambda: "admin-token")
    monkeypatch.setattr(vc, "_policy_token", lambda role: role)
    monkeypatch.setattr(vc, "_service", service)
    monkeypatch.setattr(vc, "_pinned_source", CDP_RENDER.read_text)  # the real subprocess render runs on it
    return vc, world


def test_s13_2d_passes_when_everything_holds_and_never_hands_push_the_real_sha(lab_edge) -> None:
    vc, world = lab_edge
    assert vc.c_lab_edge() is True
    push = [p for n, p in world["runs"] if n == "lab-edge-push"]
    assert push and all(p["sha256"] != world["sha"] for p in push)  # a matching SHA would log in and push
    monitor = [p for n, p in world["runs"] if n == "aws-vpn-monitor"]
    assert monitor and not re.fullmatch(r"i-[0-9a-f]{8,17}", monitor[0]["instance_id"])  # refused before AWS


@pytest.mark.parametrize("break_it", [
    lambda w: w["caps"].update({"itential-platform": ["read"]}),
    lambda w: w["caps"].update({"itential-gateway": ["deny"]}),
    lambda w: w.update(data={"psk": "short", "version": "a-1"}),
    lambda w: w.update(data={"psk": KEY, "version": "not valid!"}),
    lambda w: w.update(sha="f" * 64),
    lambda w: w.update(masked=f"pre-shared-key {KEY}"),
    lambda w: w["push"].update(sent=True),
    lambda w: w["push"].update(error="KeyError"),
], ids=["platform-reads", "gateway-denied", "short-key", "bad-version", "sha-differs", "key-in-output", "push-sent",
        "push-other-error"])
def test_s13_2d_fails_on_each_broken_part(lab_edge, break_it) -> None:
    vc, world = lab_edge
    break_it(world)
    assert vc.c_lab_edge() is False


def test_s13_2d_renders_the_fetched_module_away_from_the_tokens(lab_edge, monkeypatch) -> None:
    vc, world = lab_edge
    monkeypatch.setenv("VAULT_ADMIN_TOKEN", "must-not-reach-the-render")
    leak = "import os; assert 'VAULT_ADMIN_TOKEN' not in os.environ, 'token in the render env'\n"
    monkeypatch.setattr(vc, "_pinned_source", lambda: CDP_RENDER.read_text() + "\n" + leak)
    entry = TARGETS["clab-rtr1"]
    assert vc._pinned_sha(entry["target"], entry["outputs"]) == world["sha"]
    monkeypatch.setattr(vc, "_pinned_source", lambda: "raise SystemExit(3)")
    with pytest.raises(RuntimeError):
        vc._pinned_sha(entry["target"], entry["outputs"])


def test_s13_2d_is_dev_only_and_wired_into_the_dev_verify(lab_edge, monkeypatch) -> None:
    vc, _ = lab_edge
    monkeypatch.setenv("VAULT_TIER", "prod")
    assert vc.c_lab_edge() is False
    assert 'check "S13.2d ' in (ROOT / "verify" / "test-09a-vault-dev.sh").read_text()
    assert '" vc lab-edge' in (ROOT / "verify" / "test-09a-vault-dev.sh").read_text()


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_each_target_passes_the_renderers_own_check(name: str) -> None:
    if not CDP_RENDER.exists():
        pytest.skip("needs the cloud-devops-pipeline clone for the render module")
    render = _load("lab_edge_render_check", CDP_RENDER)
    checked = render.check_target(TARGETS[name]["target"])
    assert set(render.TARGET_KEYS) == TARGET_KEYS  # the copy above is the renderer's
    if "outputs" in TARGETS[name]:
        assert render.check_outputs(checked, TARGETS[name]["outputs"])


# ── S8.3b (verify/vaultcheck.py gateway): the Gateway's exported aliases against the oracle, per tier ──


def _export(aliases: dict, provider: str) -> list[dict]:
    return [{"name": k, "provider": provider, "secret": v["path"], "key": v["key"]} for k, v in aliases.items()]


@pytest.fixture
def gateway(monkeypatch):
    vc = _load("vaultcheck_gw", ROOT / "verify" / "vaultcheck.py")
    world = {"secrets": []}
    prov = {"name": vc.VAULT["gateway_provider"], "type": "vault", "auth-method": "approle", "url": "https://vault.test",
            "secrets-endpoint": f"{vc.VAULT['kv_mount']}/data", "secret-id-file": vc.VAULT["gateway_secret_id_file"]}

    class FakePlatform:
        def call(self, method, path, body=None):
            return {"secret-providers": [prov], "secrets": world["secrets"]}

    monkeypatch.setattr(vc, "Platform", FakePlatform)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("VAULT_ADMIN_TOKEN", raising=False)
    return vc, world


@pytest.mark.parametrize("tier", ["dev", "prod"])
def test_s8_3b_passes_only_on_the_tiers_own_alias_set(gateway, monkeypatch, tier) -> None:
    vc, world = gateway
    monkeypatch.setenv("VAULT_TIER", tier) if tier == "prod" else monkeypatch.delenv("VAULT_TIER", raising=False)
    base, dev, name = vc.VAULT["gateway_aliases"], vc.VAULT["dev_gateway_aliases"], vc.VAULT["gateway_provider"]
    own = {**base, **dev} if tier == "dev" else base
    world["secrets"] = _export(own, name)
    assert vc.c_gateway() is True
    world["secrets"] = _export(own, name)[1:]  # one alias missing
    assert vc.c_gateway() is False
    world["secrets"] = _export({**own, "stray": {"path": "devices/x", "key": "y"}}, name)
    assert vc.c_gateway() is False
    first = next(iter(own))
    world["secrets"] = _export({**own, first: {"path": own[first]["path"], "key": "other"}}, name)  # same name, wrong key
    assert vc.c_gateway() is False
    other = {**base, **dev} if tier == "prod" else base  # the other tier's set: prod must never hold the dev aliases
    world["secrets"] = _export(other, name)
    assert vc.c_gateway() is False
