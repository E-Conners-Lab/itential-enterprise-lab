"""The skills eval suite's scratch lab (ADR 0084): topology/skills-lab.yaml held to topology/ipam.yaml, the
resource-budget loan, the scratch day-0 and the EVE builder's guards."""

from __future__ import annotations

import re
import sys
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
    assert all("prefix" not in lk for lk in scratch["links"]), (
        "skills address the data ports themselves"
    )


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


def test_scratch_day0_keeps_management_and_the_change_log_and_nothing_else(
    scratch: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = render("skl-r1", scratch, monkeypatch).splitlines()
    for line in (
        "hostname skl-r1",
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
        "interface Loopback0",
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
