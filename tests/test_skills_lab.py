"""The skills eval suite's scratch lab (ADR 0084): topology/skills-lab.yaml held to topology/ipam.yaml, the
resource-budget loan, the scratch day-0 and the EVE builder's guards."""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import stat
import sys
import types
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / "topology" / "skills-lab.yaml"
ENTERPRISE = ROOT / "topology" / "enterprise.yaml"
IPAM = ROOT / "topology" / "ipam.yaml"
BUDGET = ROOT / "docs" / "resource-budget.md"
REFERENCE = ROOT / "topology" / "generated" / "skills-lab" / "configs"
PLAY = ROOT / "ansible" / "playbooks" / "skills-lab-devices.yml"
MAKEFILE = ROOT / "Makefile"
EVE_RAM_CEILING_MB = 115 * 1024
EVE_HOST_MB = 6 * 1024
PLACEHOLDER = "__AUTOMATION_PASSWORD__"

sys.path.insert(0, str(ROOT / "eve"))
sys.path.insert(0, str(ROOT))
import build


@pytest.fixture(scope="module")
def scratch() -> dict:
    return yaml.safe_load(SCRATCH.read_text())


@pytest.fixture(scope="module")
def enterprise() -> dict:
    return yaml.safe_load(ENTERPRISE.read_text())


def render(
    name: str, topo: dict, monkeypatch: pytest.MonkeyPatch, password: str = PLACEHOLDER
) -> str:
    monkeypatch.setenv(topo["lab"]["automation_password_env"], password)
    return build.render_config("c8000v", name, topo["nodes"][name], topo)


def test_shape_three_c8000v_in_a_triangle(scratch: dict) -> None:
    lab = scratch["lab"]
    assert lab["name"] == "skills-lab" and lab["path"] == "/skills-lab.unl"
    assert (
        lab["scratch"] is True
        and lab["firewalls"] is False
        and lab["mgmt_network"] == "pnet1"
    )
    assert lab["automation_password_env"] == "SKILLS_LAB_AUTOMATION_PASSWORD"
    assert sorted(scratch["nodes"]) == ["skl-r1", "skl-r2", "skl-r3"]
    for name, node in scratch["nodes"].items():
        assert (node["platform"], node["image"]) == ("c8000v", "c8000v-17.13.01a"), name
    pairs = {
        frozenset(e.split(":")[0] for e in (lk["a"], lk["b"]))
        for lk in scratch["links"]
    }
    triangle = (("skl-r1", "skl-r2"), ("skl-r2", "skl-r3"), ("skl-r1", "skl-r3"))
    assert pairs == {frozenset(p) for p in triangle}
    # the ADR's 2026-10-26 amendment: the standing OSPF baseline addresses every link
    assert all("prefix" in lk for lk in scratch["links"]), "every link carries its /30"


def test_ports_exist_and_are_used_once(scratch: dict) -> None:
    ends = [e for lk in scratch["links"] for e in (lk["a"], lk["b"])]
    assert len(ends) == len(set(ends)), "a port is cabled twice"
    for end in ends:
        node, iface = end.split(":")
        index = build.iface_index("c8000v", iface)
        assert 1 <= index < scratch["nodes"][node]["ethernet"], (
            f"{end}: no such data port"
        )


def test_every_scratch_router_has_its_ipam_address(scratch: dict) -> None:
    rows = {a["hostname"]: a for a in yaml.safe_load(IPAM.read_text())["addresses"]}
    ip_plan = (ROOT / "docs" / "ip-plan.md").read_text()
    for name, node in scratch["nodes"].items():
        assert name in rows, f"{name} not in topology/ipam.yaml"
        assert rows[name]["address"] == node["mgmt_ip"], name
        assert rows[name]["placement"] == "eve-router", (
            f"{name}: the C8000v block is .144-.159 (eve-router)"
        )
        assert f"| {node['mgmt_ip']} | {name} | EVE-NG |" in ip_plan, (
            f"{name} missing from docs/ip-plan.md"
        )


def test_node_sizes_match_the_budget_loan_table(scratch: dict) -> None:
    text = BUDGET.read_text()
    loan = text[text.index("### 3a.") : text.index("## 4.")]
    row = re.search(
        r"\| `skl-r1`, `skl-r2`, `skl-r3` \(scratch\) \| `c8000v` [^|]+\| (\d+) \| (\d+) \| (\d+) \|",
        loan,
    )
    assert row, "docs/resource-budget.md 3a has no scratch row"
    count, vcpu, ram_gb = (int(x) for x in row.groups())
    assert count == len(scratch["nodes"])
    for name, node in scratch["nodes"].items():
        assert node["cpu"] == vcpu and node["ram"] == ram_gb * 1024, name


def test_the_loan_needs_the_firewalls_off_and_fits_the_ceiling(
    scratch: dict, enterprise: dict
) -> None:
    """The scratch lab only exists on memory the four PA-VMs would use: with it present the firewalls stay off, and
    the enterprise nodes that run without them plus the scratch routers plus the host fit the EVE-NG ceiling."""
    assert enterprise["lab"]["firewalls"] is False, (
        "topology/enterprise.yaml has lab.firewalls true while topology/skills-lab.yaml exists: "
        "delete the scratch lab first (make skills-lab-down) and remove its topology"
    )
    running = sum(
        n["ram"] for n in enterprise["nodes"].values() if n["role"] != "firewall"
    )
    loan = sum(n["ram"] for n in scratch["nodes"].values())
    total = running + loan + EVE_HOST_MB
    assert total <= EVE_RAM_CEILING_MB, f"{total / 1024:.0f} GB > 115 GB"
    firewalls = sum(
        n["ram"] for n in enterprise["nodes"].values() if n["role"] == "firewall"
    )
    assert loan <= firewalls, (
        "the scratch lab borrows more than the Palo Alto reservation holds"
    )


def test_scratch_day0_keeps_management_the_change_log_and_the_ospf_baseline_and_nothing_else(
    scratch: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = render("skl-r1", scratch, monkeypatch).splitlines()
    for line in (
        "hostname skl-r1",
        "interface Loopback0",
        " ip address 10.255.0.1 255.255.255.255",
        " ip address 10.250.12.1 255.255.255.252",
        " ip address 10.250.13.1 255.255.255.252",
        " ip ospf 1 area 0",
        "router ospf 1",
        " router-id 10.255.0.1",
        "netconf-yang",
        "restconf",
        "ip http secure-server",
        " ip address 10.100.0.152 255.255.255.0",
        "archive",
        " log config",
        "  notify syslog contenttype plaintext",
        "  hidekeys",
        "login on-success log",
        "login on-failure log",
        "logging host 10.100.0.38 vrf MGMT",
    ):
        assert line in lines, f"missing: {line!r}"
    cfg = "\n".join(lines)
    for absent in (
        "router bgp",
        "vrf definition WAN",
        "crypto ikev2",
    ):
        assert absent not in cfg, f"scratch day-0 must not carry {absent!r}"


def test_scratch_routers_never_get_the_production_device_password(
    scratch: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOMATION_PASSWORD", "PRODUCTION-PASSWORD")
    cfg = render("skl-r2", scratch, monkeypatch, password="SCRATCH-PASSWORD")
    assert "SCRATCH-PASSWORD" in cfg and "PRODUCTION-PASSWORD" not in cfg


def test_the_change_log_block_is_scratch_only(
    enterprise: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOMATION_PASSWORD", PLACEHOLDER)
    cfg = build.render_config(
        "c8000v", "dc1-wan02", enterprise["nodes"]["dc1-wan02"], enterprise
    )
    assert (
        "notify syslog contenttype plaintext" not in cfg
        and "login on-success log" not in cfg
    )


def test_rendered_scratch_configs_match_reference(
    scratch: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The committed scratch day-0 (eve/build.py --topology topology/skills-lab.yaml render, placeholder password)."""
    assert {p.stem for p in REFERENCE.glob("*.cfg")} == set(scratch["nodes"])
    for name in scratch["nodes"]:
        assert (
            render(name, scratch, monkeypatch)
            == (REFERENCE / f"{name}.cfg").read_text()
        ), f"{name}: re-render the reference"


def test_generated_files_of_a_scratch_lab_never_overwrite_the_enterprise_lab(
    scratch: dict, enterprise: dict
) -> None:
    assert build.generated_dir(enterprise) == ROOT / "topology" / "generated"
    assert (
        build.generated_dir(scratch) == ROOT / "topology" / "generated" / "skills-lab"
    )


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["delete", "--yes"], "refusing to delete the enterprise lab"),
        (
            ["--topology", str(ENTERPRISE), "delete", "--yes"],
            "refusing to delete the enterprise lab",
        ),
        (["--topology", str(SCRATCH), "delete"], "re-run with --yes"),
    ],
)
def test_builder_delete_refuses_before_any_eve_login(
    argv: list[str], message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_login(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("delete must refuse before logging in to EVE-NG")

    monkeypatch.setattr(build, "Eve", no_login)
    monkeypatch.setattr(sys, "argv", ["build.py", *argv])
    with pytest.raises(SystemExit, match=re.escape(message)):
        build.main()


def test_devices_play_bootstraps_without_printing_a_password() -> None:
    play = PLAY.read_text()
    for var in (
        "SKILLS_LAB_AUTOMATION_PASSWORD",
        "SKILLS_LAB_READER_PASSWORD",
        "SKILLS_LAB_JUDGE_PASSWORD",
    ):
        assert f"lookup('ansible.builtin.env', '{var}')" in play, var
    for step in ("wait-ssh", "accounts", "pins", "check-readers", "baseline"):
        assert f", {step}," in play, f"the play never runs {step}"
    accounts_task = play[
        play.index("- name: Read accounts") : play.index("- name: HTTPS certificate")
    ]
    assert "no_log: true" in accounts_task
    helper = (ROOT / "scripts" / "skills-lab-device.py").read_text()
    assert "nacm populate-read-rules privilege 1" in helper
    assert "copy running-config bootflash:baseline.cfg" in helper
    assert "username {user} privilege 1 secret 0" in helper


def test_makefile_wires_the_scratch_lab() -> None:
    mk = MAKEFILE.read_text()
    for target in (
        "skills-lab-up",
        "skills-lab-devices",
        "skills-lab-down",
        "skills-lab-creds",
    ):
        assert re.search(rf"^{target}:.*##", mk, re.MULTILINE), target
    assert "eve/build.py --topology topology/skills-lab.yaml delete --yes" in mk


def test_env_example_declares_the_scratch_passwords() -> None:
    env = (ROOT / ".env.example").read_text()
    for var in (
        "SKILLS_LAB_AUTOMATION_PASSWORD",
        "SKILLS_LAB_READER_PASSWORD",
        "SKILLS_LAB_JUDGE_PASSWORD",
    ):
        assert re.search(rf"^{var}=\s", env, re.MULTILINE), var


# --- review fixes (2026-10-22): behaviour tests ---------------------------------------------------------------------

_spec = importlib.util.spec_from_file_location("skills_lab_device", ROOT / "scripts" / "skills-lab-device.py")
device = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(device)


class FakeEve:
    """Just enough of eve/build.py's Eve for the guards: nodes by name, a create that may drop firstmac."""

    def __init__(self, nodes: dict | None = None, keep_firstmac: bool = True) -> None:
        self._nodes = dict(nodes or {})
        self.keep_firstmac = keep_firstmac
        self.started: list[int] = []

    def nodes(self) -> dict:
        return self._nodes

    def add_node(self, payload: dict) -> int:
        nid = len(self._nodes) + 1
        mac = payload.get("firstmac") if self.keep_firstmac else f"50:00:00:{nid:02x}:00:00"
        self._nodes[payload["name"]] = {"id": nid, "firstmac": mac, "status": 0}
        return nid

    def start(self, node_id: int) -> None:
        self.started.append(node_id)


def test_scratch_management_macs_never_collide_with_the_enterprise_lab(scratch: dict) -> None:
    enterprise_macs = build.enterprise_macs()
    assert enterprise_macs, "topology/generated/eve-nodes.yaml must list the enterprise lab's MACs"
    scratch_macs = [n["eve"]["firstmac"].lower() for n in scratch["nodes"].values()]
    assert len(set(scratch_macs)) == len(scratch_macs)
    for mac in scratch_macs:
        assert mac not in enterprise_macs
        assert not build.EVE_DERIVED_MAC.match(mac), "EVE-NG could derive this MAC for some node id"
        first = int(mac[:2], 16)
        assert first & 2 and not first & 1, "locally administered unicast"
    build.mac_guard(scratch)  # the shipped topology passes the builder's own guard


def test_builder_refuses_a_scratch_node_without_its_own_mac(scratch: dict) -> None:
    topo = copy.deepcopy(scratch)
    del topo["nodes"]["skl-r2"]["eve"]["firstmac"]
    with pytest.raises(SystemExit, match="skl-r2: needs eve.firstmac"):
        build.mac_guard(topo)
    topo["nodes"]["skl-r2"]["eve"]["firstmac"] = "50:00:00:02:00:00"  # dc1-wan01's
    with pytest.raises(SystemExit, match="skl-r2"):
        build.mac_guard(topo)
    topo["nodes"]["skl-r2"]["eve"]["firstmac"] = topo["nodes"]["skl-r1"]["eve"]["firstmac"]
    with pytest.raises(SystemExit, match="collides"):
        build.mac_guard(topo)


def test_node_payload_carries_firstmac_and_a_dropped_mac_stops_the_build(scratch: dict) -> None:
    node = scratch["nodes"]["skl-r1"]
    assert build.node_payload("skl-r1", node)["firstmac"] == "52:5c:00:01:00:00"
    kept = FakeEve()
    kept.add_node(build.node_payload("skl-r1", node))
    build.check_firstmac(kept, "skl-r1", node)
    dropped = FakeEve(keep_firstmac=False)
    dropped.add_node(build.node_payload("skl-r1", node))
    with pytest.raises(SystemExit, match="stopping before any node starts"):
        build.check_firstmac(dropped, "skl-r1", node)


def test_start_refuses_a_scratch_node_running_on_an_enterprise_mac(
    scratch: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    eve = FakeEve({"skl-r1": {"id": 1, "firstmac": "50:00:00:01:00:00", "status": 0}})
    with pytest.raises(SystemExit, match="skl-r1: EVE-NG kept firstmac"):
        build.start(eve, scratch, waves=False)
    assert eve.started == []


def test_memory_guard_both_directions(scratch: dict, enterprise: dict, monkeypatch: pytest.MonkeyPatch,
                                      tmp_path: Path) -> None:
    on = copy.deepcopy(enterprise)
    on["lab"]["firewalls"] = True
    build.memory_guard(enterprise)  # firewalls off: fine
    with pytest.raises(SystemExit, match="skills-lab.yaml exists"):
        build.memory_guard(on)  # the scratch topology exists in this repository
    monkeypatch.setattr(build, "SCRATCH_TOPO", tmp_path / "absent.yaml")
    build.memory_guard(on)  # no scratch lab: the firewalls may come back
    build.memory_guard(scratch)
    monkeypatch.setattr(build, "load_topology", lambda _path=None: on)
    with pytest.raises(SystemExit, match="lab.firewalls true"):
        build.memory_guard(scratch)


def test_push_configs_logs_in_with_the_scratch_password_only(
    scratch: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOMATION_PASSWORD", "production-secret")
    monkeypatch.setenv("SKILLS_LAB_AUTOMATION_PASSWORD", "scratch-secret")
    assert build.device_password(scratch) == "scratch-secret"
    seen: list[str | None] = []

    def fake_run(ip: str, command: str, user: str = "automation", timeout: int = 0, password=None) -> str:
        seen.append(password)
        return "License Level: network-advantage"

    monkeypatch.setitem(sys.modules, "devcmd", types.SimpleNamespace(run=fake_run, reload=None))
    monkeypatch.setattr(build.time, "sleep", lambda _s: None)

    class PushEve(FakeEve):
        lab = "/skills-lab.unl"

        def config_set(self) -> int:
            return 1

        def upload_config(self, *_a) -> None: ...
        def enable_config(self, *_a) -> None: ...
        def _req(self, *_a, **_k) -> dict:
            return {}

        def stop(self, *_a) -> None: ...

    eve = PushEve({n: {"id": i, "firstmac": scratch["nodes"][n]["eve"]["firstmac"], "status": 0}
                   for i, n in enumerate(scratch["nodes"], 1)})
    build.push_configs(eve, scratch, only=None)
    assert seen and set(seen) == {"scratch-secret"}


def test_ensure_secrets_fills_empty_lines_and_writes_mode_600(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AUTOMATION_PASSWORD=keep-me\n"
        "SKILLS_LAB_AUTOMATION_PASSWORD=  # generated by make skills-lab-up\n"
        "SKILLS_LAB_READER_PASSWORD=existing-value\n"
    )
    env_file.chmod(0o644)
    added = device.ensure_secrets(env_file)
    assert added == ["SKILLS_LAB_AUTOMATION_PASSWORD", "SKILLS_LAB_JUDGE_PASSWORD"]
    values = dict(line.split("=", 1) for line in env_file.read_text().splitlines() if "=" in line)
    assert values["AUTOMATION_PASSWORD"] == "keep-me"
    assert values["SKILLS_LAB_READER_PASSWORD"] == "existing-value"
    assert len(values["SKILLS_LAB_AUTOMATION_PASSWORD"]) >= 16
    assert len(values["SKILLS_LAB_JUDGE_PASSWORD"]) >= 16
    assert env_file.read_text().count("SKILLS_LAB_AUTOMATION_PASSWORD=") == 1
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert device.ensure_secrets(env_file) == []


def test_creds_file_is_private_from_the_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKILLS_LAB_JUDGE_PASSWORD", "judge-secret")
    for name in ("SVC_EVAL_PROD_PASSWORD", "SVC_EVAL_APPROVER_PROD_PASSWORD"):
        monkeypatch.delenv(name, raising=False)  # a shell with .env loaded must not change the test
    dest = tmp_path / "workbench.env"
    dest.write_text("OTHER=1\n")
    dest.chmod(0o644)
    device.creds(dest)
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert "SKILLEVAL_JUDGE_PASSWORD=judge-secret" in dest.read_text()
    assert "SKILLEVAL_RUNNER_PASSWORD" not in dest.read_text(), "no runner key before the account has a password"
    # once the replay has built the accounts: the names the workbench reads, never the lab's own key names
    monkeypatch.setenv("SVC_EVAL_PROD_PASSWORD", "runner-secret")
    monkeypatch.setenv("SVC_EVAL_APPROVER_PROD_PASSWORD", "approver-secret")
    device.creds(dest)
    values = dict(line.split("=", 1) for line in dest.read_text().splitlines())
    assert values == {"OTHER": "1", "SKILLEVAL_JUDGE_USER": "judge", "SKILLEVAL_JUDGE_PASSWORD": "judge-secret",
                      "SKILLEVAL_RUNNER_USER": "svc-eval", "SKILLEVAL_RUNNER_PASSWORD": "runner-secret",
                      "SKILLEVAL_APPROVER_USER": "svc-eval-approver", "SKILLEVAL_APPROVER_PASSWORD": "approver-secret"}
    with pytest.raises(SystemExit, match="public repository"):
        device.creds(ROOT / "tmp-creds.env")


@pytest.mark.parametrize("status", [400, 401, 405, 500])
def test_only_nacm_access_denied_proves_read_only(status: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKILLS_LAB_READER_PASSWORD", "r")
    monkeypatch.setenv("SKILLS_LAB_JUDGE_PASSWORD", "j")

    def fake_restconf(ip, pin, user, password, method, body=None):
        if method == "GET":
            return 200, b'{"Cisco-IOS-XE-native:hostname": "skl-r1"}'
        return status, b""

    monkeypatch.setattr(device, "_restconf", fake_restconf)
    with pytest.raises(SystemExit, match=f"HTTP {status}, not 403"):
        device.check_readers("10.100.0.152", "a" * 64)


def test_nacm_403_passes_and_a_write_that_succeeds_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKILLS_LAB_READER_PASSWORD", "r")
    monkeypatch.setenv("SKILLS_LAB_JUDGE_PASSWORD", "j")
    monkeypatch.setenv("SKILLS_LAB_AUTOMATION_PASSWORD", "a")
    answers = {"PATCH": 403}

    def fake_restconf(ip, pin, user, password, method, body=None):
        if method == "GET":
            return 200, b'{"Cisco-IOS-XE-native:hostname": "skl-r1"}'
        return answers["PATCH"], b""

    monkeypatch.setattr(device, "_restconf", fake_restconf)
    device.check_readers("10.100.0.152", "a" * 64)
    answers["PATCH"] = 204
    with pytest.raises(SystemExit, match="allowed to write"):
        device.check_readers("10.100.0.152", "a" * 64)


def test_the_write_probe_asks_for_a_real_change(monkeypatch: pytest.MonkeyPatch) -> None:
    # IOS XE skips the NACM write check when a write changes nothing, so a same-value PATCH proves nothing
    monkeypatch.setenv("SKILLS_LAB_READER_PASSWORD", "r")
    monkeypatch.setenv("SKILLS_LAB_JUDGE_PASSWORD", "j")
    bodies = []

    def fake_restconf(ip, pin, user, password, method, body=None):
        if method == "GET":
            return 200, b'{"Cisco-IOS-XE-native:hostname": "skl-r1"}'
        bodies.append(json.loads(body)["Cisco-IOS-XE-native:hostname"])
        return 403, b""

    monkeypatch.setattr(device, "_restconf", fake_restconf)
    device.check_readers("10.100.0.152", "a" * 64)
    assert len(bodies) == 2 and all(name != "skl-r1" for name in bodies)


@pytest.mark.parametrize(("revert_status", "message"), [(204, "hostname put back to skl-r1"), (500, "got HTTP 500: fix it by hand")])
def test_a_write_that_lands_is_put_back_by_the_automation_account(
    revert_status: int, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SKILLS_LAB_READER_PASSWORD", "r")
    monkeypatch.setenv("SKILLS_LAB_JUDGE_PASSWORD", "j")
    monkeypatch.setenv("SKILLS_LAB_AUTOMATION_PASSWORD", "a")
    patches = []

    def fake_restconf(ip, pin, user, password, method, body=None):
        if method == "GET":
            return 200, b'{"Cisco-IOS-XE-native:hostname": "skl-r1"}'
        patches.append((user, json.loads(body)["Cisco-IOS-XE-native:hostname"]))
        return (revert_status if user == "automation" else 204), b""

    monkeypatch.setattr(device, "_restconf", fake_restconf)
    with pytest.raises(SystemExit, match=message):
        device.check_readers("10.100.0.152", "a" * 64)
    assert patches[-1] == ("automation", "skl-r1")


class _Client:
    def close(self) -> None:
        pass


REFUSED = "skl-r1#request platform software yang-management nacm populate-read-rules privilege 1\n% NACM not ready\nskl-r1#"
ACCEPTED = "skl-r1#request platform software yang-management nacm populate-read-rules privilege 1\nskl-r1#"


def test_nacm_read_rules_are_retried_until_the_router_accepts_them(monkeypatch: pytest.MonkeyPatch) -> None:
    replies = [REFUSED, REFUSED, ACCEPTED]
    sleeps = []
    monkeypatch.setattr(device, "_ssh", lambda ip: _Client())
    monkeypatch.setattr(device, "_shell", lambda client, commands: replies.pop(0))
    monkeypatch.setattr(device.time, "sleep", sleeps.append)
    device.populate_read_rules("10.100.0.153", attempts=5, wait=15)
    assert replies == [] and sleeps == [15, 15]


def test_nacm_read_rules_give_up_after_the_last_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(device, "_ssh", lambda ip: _Client())
    monkeypatch.setattr(device, "_shell", lambda client, commands: REFUSED)
    monkeypatch.setattr(device.time, "sleep", lambda seconds: None)
    with pytest.raises(SystemExit, match="refused the NACM read rules 3 times .*NACM not ready"):
        device.populate_read_rules("10.100.0.153", attempts=3, wait=0)


@pytest.mark.parametrize(
    "line",
    [
        "% Invalid input detected at '^' marker.",
        "  % Incomplete command.",
        "Error: NACM rules could not be populated",
        "Populate read rules? [confirm]",
    ],
)
def test_command_refusals_are_detected(line: str) -> None:
    transcript = f"skl-r1#request platform software yang-management nacm populate-read-rules privilege 1\n{line}\nskl-r1#"
    assert device.command_refusal(transcript) == line.strip()


def test_a_clean_transcript_has_no_refusal() -> None:
    transcript = (
        "skl-r1#configure terminal\nEnter configuration commands, one per line.  End with CNTL/Z.\n"
        "skl-r1(config)#username judge privilege 1 secret 0 <redacted>\nskl-r1(config)#end\n"
        "skl-r1#request platform software yang-management nacm populate-read-rules privilege 1\nskl-r1#"
    )
    assert device.command_refusal(transcript) is None


WRITE_OK = "skl-r1#write memory\nBuilding configuration...\n[OK]\n"
COPY_OK = (
    "skl-r1#copy running-config bootflash:baseline.cfg\nDestination filename [baseline.cfg]? \n"
    "5321 bytes copied in 0.123 secs (43260 bytes/sec)\n"
)
DIR_OK = (
    "skl-r1#dir bootflash:baseline.cfg\nDirectory of bootflash:/baseline.cfg\n\n"
    "   21  -rw-             5321  Oct 22 2026 06:01:02 +00:00  baseline.cfg\n\n"
    "6286540800 bytes total (5000000000 bytes free)\nskl-r1#"
)


@pytest.mark.parametrize("warning", ["%Warning:There", "%Warning: There"])
def test_baseline_capture_is_proved_by_each_command_output(warning: str) -> None:
    """An overwrite of an earlier baseline prints one warning, then asks to confirm; 17.13.01a spells it with no space
    after the colon (the helper refused its own successful copy on 2026-10-24 when it expected the space)."""
    assert device.baseline_problem(WRITE_OK + COPY_OK + DIR_OK) is None
    overwrite = COPY_OK.replace(
        "Destination filename [baseline.cfg]? \n",
        f"Destination filename [baseline.cfg]? \n{warning} is a file already existing with this name\n"
        "Do you want to over write? [confirm]\n",
    )
    assert device.baseline_problem(WRITE_OK + overwrite + DIR_OK) is None


@pytest.mark.parametrize(
    ("transcript", "reason"),
    [
        (WRITE_OK + "skl-r1#copy running-config bootflash:baseline.cfg\n"
         "%Error copying nvram:startup-config (Not enough space on device)\n" + DIR_OK, "refused"),
        (WRITE_OK + "skl-r1#copy running-config bootflash:baseline.cfg\nDestination filename [baseline.cfg]? \n"
         + DIR_OK, "bytes copied"),
        (WRITE_OK + COPY_OK + "skl-r1#dir bootflash:baseline.cfg\n"
         "%Error opening bootflash:baseline.cfg (No such file or directory)\nskl-r1#", "listing failed"),
        (WRITE_OK + COPY_OK + DIR_OK.replace("5321  Oct", "0  Oct"), "with a size"),
        ("skl-r1#write memory\n%Error: NVRAM full\n" + COPY_OK + DIR_OK, "[OK]"),
        (COPY_OK + DIR_OK, "missing one of the three commands"),
    ],
)
def test_a_failed_baseline_capture_is_never_reported_as_captured(transcript: str, reason: str) -> None:
    problem = device.baseline_problem(transcript)
    assert problem is not None and reason in problem


def test_push_configs_runs_the_guards_before_touching_any_node(
    scratch: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    touched: list[str] = []

    class PushEve(FakeEve):
        lab = "/skills-lab.unl"

        def config_set(self) -> int:
            return 1

        def upload_config(self, *_a) -> None:
            touched.append("upload")

        def enable_config(self, *_a) -> None:
            touched.append("enable")

        def _req(self, *_a, **_k) -> dict:
            touched.append("wipe")
            return {}

        def stop(self, *_a) -> None:
            touched.append("stop")

    monkeypatch.setattr(build.time, "sleep", lambda _s: None)
    # EVE-NG dropped the first MAC: the node runs on the derived 50:00:00:01:00:00, an enterprise MAC
    dropped = PushEve({n: {"id": i, "firstmac": f"50:00:00:{i:02x}:00:00", "status": 0}
                       for i, n in enumerate(scratch["nodes"], 1)})
    with pytest.raises(SystemExit):
        build.push_configs(dropped, scratch, only=None)
    assert touched == [] and dropped.started == []
    # a topology node without its own MAC is refused before EVE-NG is asked anything
    no_mac = copy.deepcopy(scratch)
    no_mac["nodes"]["skl-r1"]["eve"].pop("firstmac")
    with pytest.raises(SystemExit):
        build.push_configs(PushEve({}), no_mac, only=None)
    assert touched == []


# --- ADR 0084's second change: the certificate pins, restconf-get and the scratch passwords in Vault ----------------

VERSIONS = yaml.safe_load((ROOT / "itential" / "versions.yaml").read_text())
VAULT = VERSIONS["vault"]
EXPORT = ROOT / "topology" / "generated" / "skills-lab" / "eve-nodes.yaml"
JUDGE_PATH = "eval/skills-lab-judge"


def test_pinned_nodes_are_the_scratch_routers_as_restconf_get_takes_them(scratch: dict) -> None:
    nodes = VERSIONS["skills_lab"]["nodes"]
    assert set(nodes) == set(scratch["nodes"])
    for name, node in nodes.items():
        # with its name added, exactly restconf-get's --target_json fields (cloud-devops-pipeline itential/restconf-get.py)
        assert set(node) == {"mgmt_host", "cert_sha256"}, name
        assert node["mgmt_host"] == scratch["nodes"][name]["mgmt_ip"], name
        assert re.fullmatch(r"[0-9a-f]{64}", node["cert_sha256"]), name
    assert len({node["cert_sha256"] for node in nodes.values()}) == len(nodes)  # one certificate per router
    # restconf-get logs in only to these names at these addresses (cloud-devops-pipeline SCRATCH_ROUTERS): a router
    # added or moved here needs the same change there, then a pin bump
    assert {name: node["mgmt_host"] for name, node in nodes.items()} == {
        "skl-r1": "10.100.0.152",
        "skl-r2": "10.100.0.153",
        "skl-r3": "10.100.0.154",
    }


def test_restconf_get_logs_in_as_the_reader_and_the_inventory_alias_is_the_scratch_account() -> None:
    (svc,) = [s for s in VERSIONS["terraform_run"]["services"] if s["name"] == "restconf-get"]
    assert svc["filename"] == "itential/restconf-get.py" and svc["working-directory"] == "."
    assert svc["secrets"] == [{"name": "skills-lab-reader-password", "type": "env", "target": "RESTCONF_GET_PASSWORD"}]
    aliases = VAULT["gateway_aliases"]
    assert aliases["skills-lab-reader-password"] == {"path": "devices/skills-lab-reader", "key": "password"}
    assert aliases["skills-lab-automation-password"] == {"path": "devices/skills-lab", "key": "password"}
    # never the production device account's entry
    assert VAULT["secrets"]["devices/skills-lab-reader"]["password"] == {"env": "SKILLS_LAB_READER_PASSWORD"}
    assert VAULT["secrets"]["devices/skills-lab"]["password"] == {"env": "SKILLS_LAB_AUTOMATION_PASSWORD"}


def test_the_scratch_passwords_are_seeded_on_production_only() -> None:
    # the scratch lab is production's (ADR 0084): a dev Vault never gets them, and dev has no dev_env to swap in
    scratch_paths = sorted(
        path for path, keys in VAULT["secrets"].items()
        if any(isinstance(src, dict) and str(src.get("env", "")).startswith("SKILLS_LAB_") for src in keys.values())
    )
    assert scratch_paths == ["devices/skills-lab", "devices/skills-lab-reader", JUDGE_PATH]
    for path in scratch_paths:
        keys = VAULT["secrets"][path]
        assert keys.get("prod_only") is True, path
        assert not any(isinstance(src, dict) and "dev_env" in src for src in keys.values()), path


def test_the_judge_password_is_where_no_gateway_or_platform_reader_reaches() -> None:
    # the judge stays independent of the governed path it judges: no alias names its entry, no AppRole policy covers it.
    # Found by its .env source, so moving the entry cannot slip past this test
    (judge,) = [path for path, keys in VAULT["secrets"].items()
                if any(isinstance(src, dict) and src.get("env") == "SKILLS_LAB_JUDGE_PASSWORD" for src in keys.values())]
    every_alias = {**VAULT["gateway_aliases"], **VAULT["edge_gateway_aliases"]}
    assert all(ref["path"] != judge for ref in every_alias.values())
    for role, cfg in VAULT["approles"].items():
        for granted in cfg["policy_paths"]:
            covers = judge == granted or (granted.endswith("*") and judge.startswith(granted[:-1]))
            assert not covers, (role, granted)
        assert not any(judge in endpoint for endpoint in cfg.get("kv_endpoints", {})), role


def test_the_exported_node_file_is_the_lab_the_topology_asks_for(scratch: dict) -> None:
    # eve/build.py export after make skills-lab-up (2026-10-22): names, addresses and each node's own first MAC
    export = yaml.safe_load(EXPORT.read_text())
    assert export["lab"] == f"/{scratch['lab']['name']}.unl"
    assert set(export["nodes"]) == set(scratch["nodes"])
    for name, node in export["nodes"].items():
        want = scratch["nodes"][name]
        assert (node["mgmt_ip"], node["platform"]) == (want["mgmt_ip"], want["platform"]), name
        assert node["mac0"] == want["eve"]["firstmac"], name


# --- the standing OSPF baseline (ADR 0084 amendment, 2026-10-26) ----------------------------------------------------

_CIDR = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3}/\d{1,2})\b")


def _sections(config: str) -> dict[str, list[str]]:
    """Each top-level line of a configuration with the indented lines under it."""
    found: dict[str, list[str]] = {}
    head = ""
    for line in config.splitlines():
        if line and not line.startswith(" ") and line != "!":
            head = line
            found.setdefault(head, [])
        elif line.startswith(" ") and head:
            found[head].append(line)
    return found


def _helper_sections(lines: list[str]) -> dict[str, list[str]]:
    return _sections("\n".join(lines))


def test_ospf_addresses_are_unique_and_clear_of_the_enterprise_lab(scratch: dict) -> None:
    import ipaddress

    links = [ipaddress.ip_network(link["prefix"]) for link in scratch["links"]]
    assert all(net.prefixlen == 30 for net in links) and len(set(links)) == len(links)
    loopbacks = [ipaddress.ip_address(node["loopback"]) for node in scratch["nodes"].values()]
    assert len(set(loopbacks)) == len(loopbacks)
    ours = links + [ipaddress.ip_network(f"{lo}/32") for lo in loopbacks]
    # every prefix the enterprise lab names, less the plan's supernets and default routes
    theirs = {
        ipaddress.ip_network(cidr, strict=False)
        for path in (ENTERPRISE, IPAM)
        for cidr in _CIDR.findall(path.read_text())
        if int(cidr.split("/")[1]) > 8
    }
    clashes = [(str(a), str(b)) for a in ours for b in theirs if a.overlaps(b)]
    assert clashes == []


def test_the_helper_applies_exactly_what_the_day0_renders(
    scratch: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in scratch["nodes"]:
        rendered = _sections(render(name, scratch, monkeypatch))
        for head, body in _helper_sections(device.ospf_lines(scratch, name)).items():
            assert head in rendered, f"{name}: {head!r} not in the day-0"
            assert set(body) <= set(rendered[head]), f"{name}: {head!r}"


def test_state_absent_renders_no_ospf_and_the_helper_takes_it_away(
    scratch: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    topo = copy.deepcopy(scratch)
    topo["lab"]["ospf"]["state"] = "absent"
    cfg = render("skl-r2", topo, monkeypatch)
    assert "router ospf" not in cfg and "interface Loopback0" not in cfg and "ip ospf" not in cfg
    assert "10.250." not in cfg, "with the baseline absent the links carry no address (review finding)"
    lines = device.ospf_lines(topo, "skl-r2")
    assert lines[-2:] == ["no router ospf 1", "no interface Loopback0"]
    assert lines.count(" no ip ospf 1 area 0") == 2 and lines.count(" no ip address") == 2
    topo["lab"]["ospf"]["state"] = "maybe"
    with pytest.raises(SystemExit, match="present or absent"):
        device.ospf_lines(topo, "skl-r2")


def test_the_play_applies_ospf_before_it_captures_the_baseline() -> None:
    tasks = [task["name"] for task in yaml.safe_load(PLAY.read_text())[0]["tasks"]]
    ospf = tasks.index("The standing OSPF baseline from topology/skills-lab.yaml")
    assert ospf < tasks.index("Running configuration saved and captured as bootflash:baseline.cfg")


def test_a_second_rollback_skips_the_loopback_that_is_already_gone(scratch: dict) -> None:
    # review finding, measured on skl-r1: `no interface Loopback0` is refused once the loopback is gone
    topo = copy.deepcopy(scratch)
    topo["lab"]["ospf"]["state"] = "absent"
    lines = device.ospf_lines(topo, "skl-r1")
    echo_only = f"skl-r1#{device.LOOPBACK_SHOW}\nskl-r1#"
    assert "no interface Loopback0" not in device.removals_needed(lines, echo_only)
    still_there = f"skl-r1#{device.LOOPBACK_SHOW}\ninterface Loopback0\nskl-r1#"
    assert device.removals_needed(lines, still_there) == lines
