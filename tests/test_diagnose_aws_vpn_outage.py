"""Diagnose AWS VPN Outage (R6 + A3, owner decisions 2026-10-05; ADR 0072): the tunnel-down alert starts it; it opens
ONE incident per outage, lets the tunnel-diagnostics agent pick one fix from a fixed menu, and runs that fix only after
a Work Center approval - then reads the tunnel again and resolves the incident or leaves it open with a task."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("wf_build_outage", ROOT / "itential" / "workflows" / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)
VERSIONS = build.VERSIONS
NAME = "Diagnose AWS VPN Outage"
OUTPUTS = {"strongswan_eip": "198.51.100.20", "strongswan_instance_id": "i-0abc", "strongswan_private_ip": "10.0.1.10",
           "vpc_private_prefixes": ["10.0.64.0/20"]}


def _svc(out, rc: int = 0) -> dict:
    return {"result": {"return_code": rc, "stdout_json": out}}


# ── the plan and the dedupe ──


def test_the_plan_names_the_target_its_correlation_and_the_restart() -> None:
    p = build.outage_plan({"outputs": _svc({"outputs": OUTPUTS}), "device": "dc1-wan01"}, build.VERIFY_TARGETS)
    assert p["ok"] and p["deployed"] and p["correlation_id"] == "aws-vpn-dc1-wan01"
    assert p["open_query"] == "correlation_id=aws-vpn-dc1-wan01^active=true"
    assert p["restart"] == {"action": "restart", "instance_id": "i-0abc", "timeout": build.RELOAD_TIMEOUT}
    assert p["edge_in"]["deployed"] == OUTPUTS


@pytest.mark.parametrize("d, deployed", [
    ({"outputs": _svc({"outputs": {}}), "device": "dc1-wan01"}, False),
    ({"outputs": _svc({"outputs": OUTPUTS}), "device": "clab-rtr1"}, None),  # closed, not AWS-monitored
    ({"outputs": _svc(None, 1), "device": "dc1-wan01"}, None),
])
def test_no_deployment_or_another_device_acts_on_nothing(d: dict, deployed) -> None:
    p = build.outage_plan(d, build.VERIFY_TARGETS)
    assert p.get("deployed") is deployed if deployed is False else p["ok"] is False


def test_an_open_incident_is_found_and_only_noted() -> None:
    found = build.open_incident({"open": {"body": {"result": [{"sys_id": "abc", "number": "INC0010001"}]}},
                                 "starts_at": "2026-10-05T02:00:00Z"})
    assert found["open"] and found["sys_id"] == "abc" and "Still down" in found["note"]["work_notes"]
    assert build.open_incident({"open": {"body": {"result": []}}})["open"] is False


# ── the incident and the agent ──


def test_the_incident_carries_the_evidence_and_the_correlation() -> None:
    judgement = {"stdout_json": {"verdict": "tunnel down: router down, data plane down, AWS monitor down",
                                 "signals": {"router": "down", "data_plane": "down", "aws_monitor": "down"}}}
    alarm = _svc({"found": True, "state": "ALARM"})
    s = build.outage_summary({"judgement": judgement, "alarm": alarm, "device": "dc1-wan01",
                              "starts_at": "2026-10-05T02:00:00Z"})
    inc = s["incident"]
    assert inc["correlation_id"] == "aws-vpn-dc1-wan01" and inc["short_description"] == "AWS VPN down: dc1-wan01 Tunnel10"
    assert "ALARM" in s["evidence"] and "tunnel down" in s["evidence"] and "2026-10-05T02:00:00Z" in inc["description"]


def test_the_agents_request_names_the_new_incident() -> None:
    r = build.outage_request({"created": {"body": {"result": {"sys_id": "s1", "number": "INC0010002"}}},
                              "summary": {"stdout_json": {"evidence": "Verify says tunnel down"}}})
    assert r["ok"] and "INC0010002 (sys_id s1)" in r["request"] and "Verify says tunnel down" in r["request"]
    assert build.outage_request({"created": {"body": {}}})["ok"] is False


@pytest.mark.parametrize("fix", ["repush-router-block", "restart-strongswan", "reset-ike"])
def test_a_menu_fix_goes_to_the_card(fix: str) -> None:
    answer = f'Read three things.\n{{"fix": "{fix}", "cause": "x", "evidence": "y"}}'
    f = build.outage_fix({"agent": {"sessionStatus": "COMPLETE", "lastMessage": answer}, "number": "INC1"})
    assert f["fix"] == fix and f["card"] is True and f["card_body"]["incident"] == "INC1"


@pytest.mark.parametrize("agent", [
    {"sessionStatus": "COMPLETE", "lastMessage": '{"fix": "reload router", "cause": "x"}'},  # off the menu
    {"sessionStatus": "COMPLETE", "lastMessage": "I think the tunnel is down."},              # no JSON line
    {"sessionStatus": "FAILED", "lastMessage": '{"fix": "reset-ike"}'},                        # did not finish
    {},
])
def test_anything_else_is_escalated_and_never_reaches_the_card(agent: dict) -> None:
    f = build.outage_fix({"agent": agent})
    assert f["fix"] == "escalate" and f["card"] is False and "Escalated" in f["escalation"]["work_notes"]


# ── after the fix ──


@pytest.mark.parametrize("fix, answer", [
    ("reset-ike", {"cleared": True}), ("restart-strongswan", {"restarted": True}), ("repush-router-block", {"saved": True}),
])
def test_a_fix_that_brings_the_tunnel_back_resolves_the_incident(fix: str, answer: dict) -> None:
    r = build.outage_result({"fix": fix, "answer": _svc(answer), "check": _svc({"router": "up", "data_plane": "up"})})
    assert r["fixed"] and r["resolve"]["state"] == "6" and r["resolve"]["close_code"] == "Solution provided"
    assert "it ran" in r["message"]


def test_a_tunnel_still_down_leaves_the_incident_open_with_a_note() -> None:
    r = build.outage_result({"fix": "reset-ike", "answer": _svc({"cleared": True}),
                             "check": _svc({"router": "down", "data_plane": "down"})})
    assert r["fixed"] is False and "stays open" in r["note"]["work_notes"] and "resolve" not in r


@pytest.mark.parametrize("code, data", [
    ("OUTAGE_PLAN_CODE", {"outputs": _svc({"outputs": OUTPUTS}), "device": "dc1-wan01"}),
    ("OUTAGE_FIX_CODE", {"agent": {"sessionStatus": "COMPLETE", "lastMessage": '{"fix": "reset-ike"}'}}),
    ("OUTAGE_RESULT_CODE", {"fix": "reset-ike", "answer": _svc({"cleared": True}),
                            "check": _svc({"router": "up", "data_plane": "up"})}),
])
def test_the_pure_functions_run_as_the_gateway_runs_them(code: str, data: dict) -> None:
    run = subprocess.run([sys.executable, "-I", "-c", getattr(build, code)], input=json.dumps(data),
                         capture_output=True, text=True, timeout=30, env={}, check=True)
    assert json.loads(run.stdout)


# ── the workflow ──

WF = build.diagnose_aws_vpn_outage()
TASKS, TR = WF["tasks"], WF["transitions"]


def _reach(start: str) -> set[str]:
    seen, todo = set(), [start]
    while todo:
        node = todo.pop()
        if node not in seen:
            seen.add(node)
            todo.extend(TR.get(node, {}))
    return seen


def test_it_is_registered_named_and_gated_on_the_relays_fields() -> None:
    assert WF["name"] == NAME == VERSIONS["workflows"]["diagnose_aws_vpn_outage"]
    assert build.diagnose_aws_vpn_outage in build.BUILDERS
    gate = build.INPUT_GATES[NAME]
    assert set(gate) == {"alertname", "device", "interface", "starts_at", "fingerprint"}
    assert gate["device"]["enum"] == ["dc1-wan01"] and gate["alertname"]["enum"] == ["LabAwsTunnelDown"]


def test_no_fix_runs_before_the_card_is_approved() -> None:
    card = "5c"
    assert TASKS[card]["type"] == "manual" and TASKS[card]["name"] == "ViewData"
    fixes = {"63", "66", "6e"}  # reset-sa, restart, push
    for tid in fixes:
        assert tid in _reach("60") and tid not in (_reach("4f") - _reach(card)), tid
    assert TR[card]["60"]["state"] == "success" and TR[card]["50"]["state"] == "failure"
    assert not fixes & _reach("50")  # a rejection runs nothing


def test_each_menu_item_runs_exactly_its_service() -> None:
    svc = lambda tid: TASKS[tid]["variables"]["incoming"]["serviceName"]  # noqa: E731
    assert svc("63") == "lab-edge-push" and svc("66") == "aws-vpn-monitor" and svc("6e") == "lab-edge-push"
    assert TASKS["62"]["variables"]["incoming"]["value"] == "reset-sa"  # reset-sa, never a push
    for tid, fix in (("60", "reset-ike"), ("64", "restart-strongswan"), ("67", "repush-router-block")):
        assert fix in json.dumps(TASKS[tid]), tid


def test_an_escalation_notes_the_incident_and_shows_no_card() -> None:
    assert TR["40"]["5a"]["state"] == "success" and TR["40"]["41"]["state"] == "failure"
    assert "5c" not in _reach("41") and TASKS["43"]["name"] == "updateIncident"


def test_an_open_incident_or_a_recovered_tunnel_opens_no_incident() -> None:
    assert "37" not in _reach("20") and "37" not in _reach("30")  # createIncident
    assert TASKS["37"]["name"] == "createIncident" and TASKS["22"]["name"] == "updateIncident"


def test_the_agent_is_named_by_a_marker_the_import_fills_in() -> None:
    agent = TASKS["4b"]
    assert agent["name"] == "runAgent" and agent["app"] == "AgentSessionManager"
    assert agent["variables"]["incoming"]["agent"] == build.AGENT_MARKER == "__AGENT_ID:tunnel-diagnostics__"
    assert "terminationCallbackSignature" not in agent["variables"]["incoming"]  # the engine adds it (P5)


def test_every_task_reaches_the_end() -> None:
    for tid in TASKS:
        assert "workflow_end" in _reach(tid) or tid == "workflow_end", tid


def test_the_alert_relays_route_is_this_workflows_trigger() -> None:
    import yaml

    obs = yaml.safe_load((ROOT / "observability" / "observability.yaml").read_text())
    om = VERSIONS["operations_manager"]["diagnose_aws_vpn_outage"]
    assert om["automation"] == NAME and om["endpoint"]["route"] == obs["alert_relay"]["routes"]["LabAwsTunnelDown"]


# ── the wiring: the trigger, the agent's UUID at import, the retired tier ──


def test_the_trigger_task_takes_the_workflows_own_gate_and_refuses_a_marker() -> None:
    text = (ROOT / "ansible" / "playbooks" / "tasks" / "outage-trigger.yml").read_text()
    assert "diagnose-aws-vpn-outage.json" in text and "tasks['9a0a'].variables.incoming.schema" in text
    assert "type: endpoint" in text and "type: manual" not in text  # only the relay starts it
    assert "regex_search('__AGENT_ID:')" in text
    play = (ROOT / "ansible" / "playbooks" / "platform.yml").read_text()
    assert "include_tasks: tasks/outage-trigger.yml" in play
    assert "'diagnose_aws_vpn_outage'" in (ROOT / "ansible" / "playbooks" / "tasks" / "aws-vpn-retire.yml").read_text()


def test_the_import_fills_in_the_agents_uuid() -> None:
    assets = (ROOT / "ansible" / "playbooks" / "tasks" / "platform-assets.yml").read_text()
    assert "include_tasks: workflow-agent-ids.yml" in assets
    assert assets.index("workflow-agent-ids.yml") < assets.index("- name: Workflow documents parsed")
    assert "regex_findall('__AGENT_ID:([a-z0-9-]+)__')" in assets
    ids = (ROOT / "ansible" / "playbooks" / "tasks" / "workflow-agent-ids.yml").read_text()
    assert "/agent-project-service/operable-agents" in ids and "attribute='_id'" in ids
