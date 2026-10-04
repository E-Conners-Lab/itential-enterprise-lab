"""Tear Down Expired AWS VPN (R2b, owner decisions 2026-10-04): an hourly Operations Manager schedule ends a deployment
whose end time - approved on the Deploy card, carried by the Terraform state as `expires_at` - has passed. Most runs
find nothing due and end there. A due run is Tear Down without its cards: the same tasks (no child jobs), router first,
the AWS destroy only after the block is proved gone, and any failure opens a Work Center task."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("wf_build_expired", ROOT / "itential" / "workflows" / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)
VERSIONS = build.VERSIONS
NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)
NAME = "Tear Down Expired AWS VPN"


def _outputs(outputs, rc: int = 0) -> dict:
    return {"outputs": {"result": {"return_code": rc, "stdout_json": {"action": "outputs", "outputs": outputs}}}}


def _due(d: dict, targets: dict | None = None) -> dict:
    return build.expiry_due(d, NOW, build.EXPIRY_TARGETS if targets is None else targets)


# ── is it due? (pure; the same source runs on the Gateway) ──

DEPLOYED = {"strongswan_eip": "198.51.100.20", "vpc_id": "vpc-0"}


def test_a_deployment_whose_time_is_up_is_due_and_names_the_router() -> None:
    d = _due(_outputs({**DEPLOYED, "expires_at": "2026-10-04T11:00:00Z"}))
    assert d["ok"] and d["due"] and d["target"] == "dc1-wan01" and d["expires_at"] == "2026-10-04T11:00:00Z"


def test_the_end_time_itself_is_due() -> None:
    assert _due(_outputs({**DEPLOYED, "expires_at": "2026-10-04T12:00:00Z"}))["due"]


@pytest.mark.parametrize("outputs, says", [
    ({**DEPLOYED, "expires_at": "2026-10-04T13:00:00Z"}, "ends at 2026-10-04T13:00:00Z"),
    ({**DEPLOYED, "expires_at": "none"}, "no end time"),
    ({**DEPLOYED, "expires_at": ""}, "no end time"),
    (DEPLOYED, "no end time"),  # deployed before R2b: today's tunnel, which the owner keeps up
    ({}, "nothing is deployed"),
])
def test_nothing_is_due_otherwise(outputs: dict, says: str) -> None:
    d = _due(_outputs(outputs))
    assert d["ok"] and d["due"] is False and says in d["message"], d


@pytest.mark.parametrize("d, says", [
    (_outputs({}, rc=1), "did not answer"),
    ({"outputs": {}}, "did not answer"),
    ({}, "did not answer"),
    (_outputs(None), "no outputs"),
    (_outputs({**DEPLOYED, "expires_at": "tomorrow"}), "not a time"),
])
def test_what_cannot_be_read_is_not_due_and_says_why(d: dict, says: str) -> None:
    out = _due(d)
    assert out["ok"] is False and says in out["message"], out


def test_a_due_deployment_with_no_single_aws_router_is_refused() -> None:
    two = {"a": {"monitor": "aws"}, "b": {"monitor": "aws"}}
    out = _due(_outputs({**DEPLOYED, "expires_at": "2026-10-04T11:00:00Z"}), two)
    assert out["ok"] is False and "exactly one" in out["message"]


def test_the_router_is_the_one_teardown_target_deployed_in_aws() -> None:
    aws = [n for n, t in build.TEARDOWN_TARGETS.items() if t["monitor"] == "aws"]
    assert sorted(build.EXPIRY_TARGETS) == sorted(build.TEARDOWN_TARGETS) and aws == ["dc1-wan01"]


def test_the_check_runs_as_the_gateway_runs_it() -> None:
    run = subprocess.run([sys.executable, "-I", "-c", build.EXPIRY_CODE],
                         input=json.dumps(_outputs({**DEPLOYED, "expires_at": "2000-01-01T00:00:00Z"})),
                         capture_output=True, text=True, timeout=30, env={}, check=True)
    out = json.loads(run.stdout)
    assert out["ok"] and out["due"] and out["target"] == "dc1-wan01"
    run = subprocess.run([sys.executable, "-I", "-c", build.EXPIRY_CODE], input="", capture_output=True, text=True,
                         timeout=30, env={}, check=True)
    assert json.loads(run.stdout)["ok"] is False


# ── the workflow ──

WF = build.tear_down_expired_aws_vpn()
TASKS, TR = WF["tasks"], WF["transitions"]
TD = build.tear_down_aws_vpn()


def _reach(start: str, tr: dict = TR) -> set[str]:
    seen, todo = set(), [start]
    while todo:
        node = todo.pop()
        if node not in seen:
            seen.add(node)
            todo.extend(tr.get(node, {}))
    return seen


def _without_location(t: dict) -> dict:
    return {k: v for k, v in t.items() if k != "nodeLocation"}


def test_it_is_registered_named_verb_object_and_takes_no_input() -> None:
    assert WF["name"] == NAME == VERSIONS["workflows"]["tear_down_expired_aws_vpn"]
    assert build.tear_down_expired_aws_vpn in build.BUILDERS
    assert not (WF.get("inputSchema") or {}).get("properties")  # a schedule passes nothing (measured, golden-config.yml)


def test_the_teardown_tasks_are_tear_downs_own_copies() -> None:
    section, _ = build.teardown_section(timed=True)
    manual, _ = build.teardown_section(timed=False)
    assert set(section) == set(manual) - set(build.TEARDOWN_CARDS) and set(manual) <= set(TD["tasks"])
    for tid in section:
        assert _without_location(TASKS[tid]) == _without_location(TD["tasks"][tid]), tid


def test_there_is_no_card_and_the_only_manual_task_is_the_failure_task() -> None:
    manual = sorted(tid for tid, t in TASKS.items() if t.get("type") == "manual")
    assert manual == ["f0"] and TASKS["f0"]["name"] == "ViewData"


def test_only_a_due_check_reaches_the_router() -> None:
    into_10 = [src for src, dst in TR.items() if "10" in dst]
    assert into_10 == ["d7"] and TR["d6"]["d7"]["state"] == "success"
    assert TR["db"]["d3"]["state"] == "success" and TR["db"]["da"]["state"] == "failure"  # the state read is checked
    assert "10" not in _reach("d8") and "10" not in _reach("d9")


def test_every_teardown_failure_opens_the_work_center_task_and_nothing_else_does() -> None:
    for tid in build.TEARDOWN_FAILED:
        assert list(TR[tid]) == ["f0"], tid
    assert set(TR["f0"]) == {"workflow_end"}
    for done in ("4f", "66", "d8", "d9", "da"):
        assert "f0" not in _reach(done), done


def test_the_router_is_proved_clean_before_aws_is_planned() -> None:
    before_aws = _reach("10") - _reach("5a")
    assert {"3b", "33", "4b", "4d"} <= before_aws
    assert TR["4d"]["4e"]["state"] == "success" and "5a" in TR["4e"]


def test_every_task_reaches_the_end() -> None:
    for tid in TASKS:
        assert "workflow_end" in _reach(tid) or tid == "workflow_end", tid


def test_the_check_reads_the_state_through_the_outputs_action_only() -> None:
    assert TASKS["d2"]["variables"]["incoming"]["serviceName"] == "terraform-run"
    assert json.loads(TASKS["d1"]["variables"]["incoming"]["text"]) == json.loads(build.OUTPUTS_PARAMS)
    assert json.loads(build.OUTPUTS_PARAMS)["action"] == "outputs"


# ── the hourly schedule (platform.yml) ──


def test_the_schedule_runs_the_workflow_every_hour() -> None:
    om = VERSIONS["operations_manager"]["tear_down_expired_aws_vpn"]
    assert om["automation"] == NAME
    assert om["schedule"]["repeat_unit"] == "hour" and om["schedule"]["repeat_frequency"] == 1  # probe P1, 2026-10-04


def test_platform_yml_creates_the_automation_and_the_schedule() -> None:
    play = yaml.safe_load((ROOT / "ansible" / "playbooks" / "platform.yml").read_text())
    text = json.dumps(play)
    assert "tasks/aws-vpn-schedule.yml" in text
    tasks = yaml.safe_load((ROOT / "ansible" / "playbooks" / "tasks" / "aws-vpn-schedule.yml").read_text())
    bodies = [t["ansible.builtin.uri"].get("body") or {} for t in tasks if "ansible.builtin.uri" in t]
    trigger = next(b for b in bodies if b.get("type") == "schedule")
    assert trigger["actionType"] == "automations" and trigger["processMissedRuns"] == "none"
    assert trigger["repeatUnit"].startswith("{{") and trigger["enabled"] is True
