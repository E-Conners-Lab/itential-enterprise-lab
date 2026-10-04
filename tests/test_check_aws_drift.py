"""Check AWS Drift (R3, owner decisions 2026-10-04): once a day, terraform-run drift (a refresh-only plan, cloud-devops-
pipeline #32) reports what changed in AWS outside Terraform. No drift and nothing deployed end quietly; drift opens a
Work Center task naming the resources and the attributes that changed - never a value - and the two ways back. The
check itself never changes anything."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("wf_build_drift", ROOT / "itential" / "workflows" / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)
VERSIONS = build.VERSIONS
NAME = "Check AWS Drift"


def _answer(drift: dict | None, rc: int = 0) -> dict:
    return {"drift": {"result": {"return_code": rc, "stdout_json": {"action": "drift", "drift": drift}}}}


DRIFTED = {"drifted": 1, "resources_checked": 50, "resources": [
    {"address": "module.vpn[0].aws_instance.strongswan", "actions": ["update"], "attributes": ["tags", "tags_all"]}]}


# ── what it says (pure; the same source runs on the Gateway) ──


def test_drift_is_found_named_and_the_ways_back_given() -> None:
    s = build.drift_summary(_answer(DRIFTED))
    assert s["ok"] and s["found"] and s["drifted"] == 1
    text = s["summary"]
    assert "module.vpn[0].aws_instance.strongswan (tags, tags_all)" in text and "1 of 50" in text
    assert "Deploy AWS VPN" in text and "Tear Down AWS VPN" in text and "nothing was changed" in text.lower()


def test_a_deleted_resource_says_so() -> None:
    gone = {"drifted": 1, "resources_checked": 50, "resources": [
        {"address": "module.vpn[0].aws_eip.strongswan", "actions": ["delete"], "attributes": []}]}
    assert "module.vpn[0].aws_eip.strongswan (deleted outside Terraform)" in build.drift_summary(_answer(gone))["summary"]


@pytest.mark.parametrize("drift, says", [
    ({"drifted": 0, "resources_checked": 50, "resources": []}, "no drift: 50 resources"),
    ({"drifted": 0, "resources_checked": 0, "resources": []}, "nothing is deployed"),
])
def test_no_drift_and_nothing_deployed_find_nothing(drift: dict, says: str) -> None:
    s = build.drift_summary(_answer(drift))
    assert s["ok"] and s["found"] is False and says in s["summary"]


@pytest.mark.parametrize("d", [_answer(None, rc=1), _answer(None), {"drift": {}}, {}])
def test_a_check_that_could_not_run_says_so(d: dict) -> None:
    s = build.drift_summary(d)
    assert s["ok"] is False and s["found"] is False and "could not" in s["summary"]


def test_the_summary_runs_as_the_gateway_runs_it() -> None:
    run = subprocess.run([sys.executable, "-I", "-c", build.DRIFT_CODE], input=json.dumps(_answer(DRIFTED)),
                         capture_output=True, text=True, timeout=30, env={}, check=True)
    assert json.loads(run.stdout)["found"] is True


# ── the workflow ──

WF = build.check_aws_drift()
TASKS, TR = WF["tasks"], WF["transitions"]


def _reach(start: str) -> set[str]:
    seen, todo = set(), [start]
    while todo:
        node = todo.pop()
        if node not in seen:
            seen.add(node)
            todo.extend(TR.get(node, {}))
    return seen


def test_it_is_registered_named_and_takes_no_input() -> None:
    assert WF["name"] == NAME == VERSIONS["workflows"]["check_aws_drift"]
    assert build.check_aws_drift in build.BUILDERS
    assert not (WF.get("inputSchema") or {}).get("properties")


def test_its_only_service_call_is_the_drift_check() -> None:
    services = [t for t in TASKS.values() if t.get("name") == "runService"]
    assert len(services) == 1 and services[0]["variables"]["incoming"]["serviceName"] == "terraform-run"
    parses = [t for t in TASKS.values() if t.get("name") == "parse"]
    assert [json.loads(t["variables"]["incoming"]["text"])["action"] for t in parses] == ["drift"]


def test_only_found_drift_opens_the_work_center_task() -> None:
    manual = [tid for tid, t in TASKS.items() if t.get("type") == "manual"]
    assert len(manual) == 1
    card = manual[0]
    into = [src for src, dst in TR.items() if card in dst]  # the drift, in words
    assert len(into) == 1
    gate = [src for src, dst in TR.items() if into[0] in dst]  # and before it, the evaluation of `found`
    assert len(gate) == 1 and TR[gate[0]][into[0]]["state"] == "success"
    assert TASKS[gate[0]]["name"] == "evaluation" and "stdout_json.found" in json.dumps(TASKS[gate[0]])


def test_every_task_reaches_the_end() -> None:
    for tid in TASKS:
        assert "workflow_end" in _reach(tid) or tid == "workflow_end", tid
    assert {"outcome", "drift", "error"} <= set(WF["outputSchema"]["properties"])


# ── the daily schedule (platform.yml; one task file for every AWS VPN schedule) ──


def test_the_schedule_runs_it_daily_at_seven_utc() -> None:
    om = VERSIONS["operations_manager"]["check_aws_drift"]
    assert om["automation"] == NAME
    assert om["schedule"]["repeat_unit"] == "day" and om["schedule"]["repeat_frequency"] == 1
    assert om["schedule"]["at_utc"] == "07:00"  # owner, 2026-10-04


def test_platform_yml_loops_the_schedule_task_over_the_drift_check_too() -> None:
    play = yaml.safe_load((ROOT / "ansible" / "playbooks" / "platform.yml").read_text())[0]
    task = next(t for t in play["tasks"] if t.get("ansible.builtin.include_tasks") == "tasks/aws-vpn-schedule.yml")
    assert "check_aws_drift" in task["loop"]
    assert task["loop_control"]["loop_var"] == "sk"


def test_the_retired_tier_also_loses_the_drift_check() -> None:
    text = (ROOT / "ansible" / "playbooks" / "tasks" / "aws-vpn-retire.yml").read_text()
    assert "'check_aws_drift'" in text
