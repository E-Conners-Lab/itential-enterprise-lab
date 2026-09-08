"""PID S4d element 2 (ADR 0042): MOP command and analytic templates from itential/command-templates/ and the
nightly backups (wf-backup-all-v1 + schedule trigger), built by ansible/playbooks/platform.yml. These tests
hold the documents, the oracle, the generator, the play, the verify script and the PID to each other. They
run in CI with no lab access."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "itential" / "versions.yaml"
DOCS = ROOT / "itential" / "command-templates"
WORKFLOWS = ROOT / "itential" / "workflows"
PLAY = ROOT / "ansible" / "playbooks" / "platform.yml"
TASKS = ROOT / "ansible" / "playbooks" / "tasks" / "mop.yml"
VERIFY = ROOT / "verify" / "test-06b-platform.sh"
PID = ROOT / "docs" / "PID.md"
ADR = ROOT / "docs" / "adr" / "0042-command-templates-nightly-backups.md"

EVALS = {"contains", "!contains", "contains1", "RegEx", "!RegEx", "#comparison"}  # case-sensitive in MOP


@pytest.fixture(scope="module")
def versions() -> dict:
    return yaml.safe_load(VERSIONS.read_text())


@pytest.fixture(scope="module")
def mop(versions: dict) -> dict:
    assert "mop" in versions, "itential/versions.yaml needs a mop section (ADR 0042)"
    return versions["mop"]


def test_one_document_per_os_named_by_the_oracle(versions: dict, mop: dict) -> None:
    assert set(mop["templates"]) == set(versions["golden_config"]["trees"]), "same OS types as the Golden Config trees"
    for dtype, names in mop["templates"].items():
        doc = yaml.safe_load((DOCS / f"{dtype}.yaml").read_text())
        ct, at = doc["command_template"], doc["analytic_template"]
        assert ct["name"] == names["command"] and at["name"] == names["analytic"]
        assert ct["os"] == versions["golden_config"]["trees"][dtype]["netmiko"], "MOP os filter = the device ostype"
        assert ct["passRule"] is True and "ignoreWarnings" not in ct, "6.5.2 does not store ignoreWarnings; every rule is an error"
        assert at["passRule"] is True and at["prepostCommands"]


def test_rules_are_read_only_show_commands_with_valid_evals(mop: dict) -> None:
    for dtype in mop["templates"]:
        doc = yaml.safe_load((DOCS / f"{dtype}.yaml").read_text())
        cmds = doc["command_template"]["commands"]
        names = [c["command"] for c in cmds]
        assert all(n.startswith("show ") for n in names), names
        assert any("version" in n for n in names) and any("interface" in n for n in names) and any("bgp" in n for n in names)
        for c in cmds:
            assert c["rules"], f"{c['command']}: a command without rules always passes"
            for r in c["rules"]:
                assert r["eval"] in EVALS, r
                assert r["severity"] in ("error", "warning", "info"), r
                if "RegEx" in r["eval"]:
                    assert not r["rule"].startswith("/"), f"bare pattern: a /.../ wrapper never matches on 6.5.2 (measured): {r}"
                assert "<!" not in r["rule"], "a missing template variable silently passes: no variables in these checks"
        # a stuck BGP peer is an error on every device; every rule is an error (ignoreWarnings is not stored)
        bgp = next(c for c in cmds if "bgp" in c["command"])
        assert any(r["eval"] == "!RegEx" and "Idle" in r["rule"] and r["severity"] == "error" for r in bgp["rules"])
        assert all(r["severity"] == "error" for c in cmds for r in c["rules"])
        for pp in doc["analytic_template"]["prepostCommands"]:
            assert pp["preRawCommand"].startswith("show ") and pp["postRawCommand"] == pp["preRawCommand"]
            for r in pp["rules"]:
                assert r["type"] == "regex" and r["evaluator"] == "=" and not r["preRegex"].startswith("/"), \
                    f"regex extracts and compares a value (preElm/postElm); matches does not (measured): {r}"


def test_backup_workflow_loops_over_every_configuration_manager_device(versions: dict) -> None:
    assert versions["workflows"]["backup_all"] == "wf-backup-all-v1"
    wf = json.loads((WORKFLOWS / "wf-backup-all-v1.json").read_text())
    tasks = wf["tasks"]
    assert wf["inputSchema"]["properties"] == {}, "no inputs: schedule triggers do not persist formData"
    src = [k for k, t in tasks.items() if t.get("name") == "getDevicesFiltered"]
    each = [k for k, t in tasks.items() if t.get("name") == "forEach"]
    bak = [k for k, t in tasks.items() if t.get("name") == "backUpDevice"]
    assert len(src) == 1 and len(each) == 1 and len(bak) == 1
    assert tasks[bak[0]]["variables"]["incoming"]["name"] == f"$var.{each[0]}.current_item"
    assert wf["transitions"][each[0]][bak[0]]["state"] == "loop", "forEach starts an iteration with the loop transition"
    assert wf["transitions"][each[0]]["workflow_end"]["state"] == "success"
    assert wf["transitions"][bak[0]] == {}, "the body returns to the forEach implicitly"


def test_play_creates_templates_through_mop_and_schedules_backups(mop: dict) -> None:
    assert TASKS.exists() and "tasks/mop.yml" in PLAY.read_text()
    text = TASKS.read_text()
    for s in ("mop/createTemplate", "mop/updateTemplate", "mop/createAnalyticTemplate", "mop/updateAnalyticTemplate",
              "operations-manager/triggers", "type: schedule", "processMissedRuns", "workflows.backup_all"):
        assert s in text, s
    for bad in ("RunCommandTemplate", "apply", "sendConfig", "patchDevice"):
        assert bad not in text, f"the play only creates read-only templates; {bad} does not belong here"
    assert re.fullmatch(r"\d\d:\d\d", mop["backup_schedule"]["at_utc"]) and mop["backup_schedule"]["repeat_unit"] == "days"


def test_verify_pid_and_adr_cover_s4d2() -> None:
    text = VERIFY.read_text()
    assert re.search(r'check "S4d\.2 ', text) and "RunCommandTemplate" in text and "runAnalyticsTemplate" in text
    assert "wf-backup-all-v1" in text and "show running-config" in text, "backups compared with direct SSH"
    assert "runs no OSPF" in PID.read_text()
    assert ADR.exists()
