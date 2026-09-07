"""PID S4d element 1 (ADR 0040/0041): Golden Config trees, compliance plan and device groups are built by
ansible/playbooks/platform.yml from documents in itential/golden-config/ and the golden_config section of
itential/versions.yaml; remediation only ever goes through wf-config-push-v1 with an approval. These tests
hold the documents, the oracle, the generator, the play, the verify script and the PID to each other.
They run in CI with no lab access."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "itential" / "versions.yaml"
GC = ROOT / "itential" / "golden-config"
WORKFLOWS = ROOT / "itential" / "workflows"
PLAY = ROOT / "ansible" / "playbooks" / "platform.yml"
TASK_FILES = sorted((ROOT / "ansible" / "playbooks" / "tasks").glob("golden-config*.yml"))
ITENTIAL_PLAY = ROOT / "ansible" / "playbooks" / "itential.yml"
VERIFY = ROOT / "verify" / "test-06b-platform.sh"
PID = ROOT / "docs" / "PID.md"
ADRS = ROOT / "docs" / "adr"

# Golden Config must never push anything to a device (ADR 0040; Platform 7 removes auto-remediation)
REMEDIATION_TASKS = ("runAutoRemediation", "advancedAutoRemediation", "convertChangesToConfig", "patchDeviceConfiguration",
                     "advancedPatchDeviceConfiguration", "patchCMDeviceConfiguration", "ManualRemediation", "ManualRemediationResults")


@pytest.fixture(scope="module")
def versions() -> dict:
    return yaml.safe_load(VERSIONS.read_text())


@pytest.fixture(scope="module")
def gc(versions: dict) -> dict:
    assert "golden_config" in versions, "itential/versions.yaml needs a golden_config section (ADR 0040)"
    return versions["golden_config"]


def test_one_tree_per_os_with_platform_device_types(gc: dict) -> None:
    trees = gc["trees"]
    assert set(trees) == {"cisco-ios", "arista-eos"}, "one tree per OS, keyed by the Golden Config deviceType"
    # the inventory carries netmiko names (ADR 0039); the tree keyed by deviceType picks the parser (measured, ADR 0040)
    assert trees["cisco-ios"]["netmiko"] == "cisco_ios" and trees["arista-eos"]["netmiko"] == "arista_eos"
    play = ITENTIAL_PLAY.read_text()
    for t in trees.values():
        assert t["netmiko"] in play, f"{t['netmiko']} is not a platform_map value in itential.yml"
        assert t["name"].startswith("lab-"), t
    assert gc["plan"] and gc["schedule"]["repeat_unit"] == "days" and gc["schedule"]["repeat_interval"] == 1
    assert re.fullmatch(r"\d\d:\d\d", gc["schedule"]["at_utc"]), "nightly run time as HH:MM UTC"
    assert set(gc["device_groups"]["site"]) == {"dc1", "br1", "br2", "wan"}
    assert set(gc["device_groups"]["role"]) >= {"wan-edge", "isp-core", "spine", "leaf", "access", "branch-switch"}


def test_documents_per_tree(gc: dict) -> None:
    for dtype in gc["trees"]:
        base, device = GC / dtype / "base.gc", GC / dtype / "device.j2"
        assert base.exists() and device.exists(), f"itential/golden-config/{dtype}/ needs base.gc and device.j2"
        b = base.read_text()
        assert "{{" not in b, "the OS baseline is literal Golden Config template text; per-device values live in device.j2"
        assert not re.search(r"^\s*(<e/>)?hostname ", b, re.M), "hostname is per device (NetBox intent), not baseline"
        for want in ("username automation", "ntp server", "aaa", "{d/}"):
            assert want in b, f"{base.name} lacks {want!r}"
        d = device.read_text()
        assert "hostname {{ device.name }}" in d, "the device leaf pins the NetBox name (S4d.1 drift check)"
        assert "{{ device.primary_ip4" in d or "{{ mgmt_ip" in d, "the device leaf pins the NetBox primary address"
        assert "interface " in d, "the device leaf pins the management interface"


def test_workflows_generated_for_push_and_plan_run(versions: dict) -> None:
    names = versions["workflows"]
    assert names["config_push"] == "wf-config-push-v1" and names["compliance_run"] == "wf-compliance-run-v1"
    push = json.loads((WORKFLOWS / "wf-config-push-v1.json").read_text())
    tasks = push["tasks"]
    assert set(push["inputSchema"]["required"]) == {"device", "config", "reason"}
    manual = [k for k, t in tasks.items() if t.get("type") == "manual"]
    assert len(manual) == 1, "exactly one Work Center approval"
    cfg = [k for k, t in tasks.items() if t.get("name") == "sendConfig"]
    save = [k for k, t in tasks.items() if t.get("name") == "sendCommand" and t["variables"]["incoming"].get("commands") == ["write memory"]]
    assert len(cfg) == 1 and len(save) == 1
    # approval before the push; a rejection never reaches sendConfig or workflow_end
    assert cfg[0] in push["transitions"][manual[0]] and push["transitions"][manual[0]][cfg[0]]["state"] == "success"
    rejected = [n for n, e in push["transitions"][manual[0]].items() if e["state"] == "failure"]
    assert rejected and push["transitions"][rejected[0]] == {}
    run = json.loads((WORKFLOWS / "wf-compliance-run-v1.json").read_text())
    rc = [t for t in run["tasks"].values() if t.get("name") == "runCompliancePlan"]
    assert len(rc) == 1 and rc[0]["app"] == "ConfigurationManager" and rc[0]["variables"]["incoming"]["planId"].startswith("$var.")
    search = [t for t in run["tasks"].values() if t.get("name") == "searchCompliancePlans"]
    assert search and search[0]["variables"]["incoming"]["name"] == "^" + versions["golden_config"]["plan"] + "$", "anchored regex: the search misses an unescaped hyphen"
    assert run["inputSchema"]["properties"] == {}, "schedule triggers do not persist formData on 6.5.2: the workflow takes no input"
    for path in WORKFLOWS.glob("wf-*.json"):
        blob = path.read_text()
        for bad in REMEDIATION_TASKS:
            assert f'"name": "{bad}"' not in blob, f"{path.name} wires {bad} (ADR 0040 forbids remediation tasks)"


def test_play_builds_everything_through_the_api_and_never_remediates(gc: dict) -> None:
    assert PLAY.exists() and TASK_FILES, "platform.yml and tasks/golden-config*.yml"
    play, tasks = PLAY.read_text(), "\n".join(f.read_text() for f in TASK_FILES)
    assert "tasks/golden-config.yml" in play and "dcim/devices" in play
    for s in ("configuration_manager/devicegroup", "configuration_manager/configs", "configuration_manager/node/config",
              "configuration_manager/compliance_plans", "operations-manager/triggers", "type: schedule", "processMissedRuns"):
        assert s in tasks, f"tasks/golden-config*.yml lack {s}"
    for bad in REMEDIATION_TASKS + ("remediat",):
        assert bad.lower() not in (play + tasks).lower().replace("never remediat", "").replace("no remediat", ""), bad
    assert "updateVariables" in tasks, "node config update needs the updateVariables boolean"


def test_verify_and_pid_cover_s4d1() -> None:
    assert VERIFY.exists() and VERIFY.stat().st_mode & 0o111
    text = VERIFY.read_text()
    assert re.search(r'check "S4d\.1 ', text) and "devcmd.py" in text and "wf-config-push-v1" in text
    pid = PID.read_text()
    assert "### S4d" in pid and "| E13 |" in pid and "| 1.8 |" in pid
    assert (ADRS / "0040-golden-config-compliance-device-groups.md").exists()
    assert (ADRS / "0041-iosxe-hostname-defect-and-governed-fix.md").exists()
