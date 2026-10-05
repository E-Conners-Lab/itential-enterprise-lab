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
    # open = New, In Progress or On Hold: a Resolved incident stays active=true in ServiceNow until it closes, and the
    # next outage must open its own (2026-10-05: the first drill's resolved incident had to be closed by hand)
    assert p["open_query"] == "correlation_id=aws-vpn-dc1-wan01^stateIN1,2,3"
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
    f = build.outage_fix({"agent": {"sessionId": "5e55", "sessionStatus": "COMPLETE", "lastMessage": answer}})
    assert f["fix"] == fix and f["card"] is True and f["session"] == "5e55"


@pytest.mark.parametrize("agent", [
    {"sessionStatus": "COMPLETE", "lastMessage": '{"fix": "reload router", "cause": "x"}'},  # off the menu
    {"sessionStatus": "COMPLETE", "lastMessage": "I think the tunnel is down."},              # no JSON line
    {"sessionStatus": "FAILED", "lastMessage": '{"fix": "reset-ike"}'},                        # did not finish
    {},
])
def test_anything_else_is_escalated_and_never_reaches_the_card(agent: dict) -> None:
    f = build.outage_fix({"agent": agent})
    assert f["fix"] == "escalate" and f["card"] is False and "Escalated" in f["escalation"]["work_notes"]


# ── the card: an HTML page in Work Center, built from the outage's own readings ──
# P6/P6b (production, 2026-10-05): InteractiveHTML renders the body in an iframe and keeps <style>, @keyframes, the
# form and its textarea; it strips <svg>, so every drawing travels as an <img> with an SVG data URI. Approve and
# Reject both return {"decision": {"note": <the textarea>}}.

CARD_IN = {
    "device": "dc1-wan01", "starts_at": "2026-10-05T02:00:00Z", "now": "2026-10-05T02:07:30Z", "number": "INC0010023",
    "judgement": {"stdout_json": {
        "verdict": "tunnel down: router down, data plane down, AWS monitor could not check",
        "signals": {"router": "down", "data_plane": "down", "aws_monitor": "could not check"},
        "readings": {"router": {"ike_sa_ready": False, "identities_match": False, "pfs_configured": True,
                                "packets_rising": False, "ping_success_percent": 0, "dropped_aws_to_self": 0,
                                "dropped_aws_to_inside": 0},
                     "aws_monitor": {"lambda_answered": True, "check_succeeded": False}}}},
    "alarm": _svc({"found": True, "state": "ALARM", "since": "2026-10-05T02:01:00Z"}),
    "fix": {"stdout_json": {"fix": "restart-strongswan", "cause": "strongSwan is not running in AWS",
                            "evidence": "The router side is healthy; the monitor gets no answer.", "session": "a1b2c3d4e5"}},
    "plan": {"stdout_json": {"edge_in": {"deployed": OUTPUTS}}},
}


def _card(**over) -> str:
    return build.outage_card({**CARD_IN, **over})["html"]


def _svgs(html: str) -> list[str]:
    import base64
    import re

    return [base64.b64decode(b).decode() for b in re.findall(r'src="data:image/svg\+xml;base64,([A-Za-z0-9+/=]+)"', html)]


def test_the_card_lays_out_the_ticket_the_fix_and_the_decision() -> None:
    html = _card()
    assert "The AWS VPN is down on dc1-wan01" in html and "INC0010023" in html
    assert "Restart strongSwan in AWS" in html and "strongSwan is not running in AWS" in html
    assert "7 min" in html and "02:00 UTC" in html  # down since the alert, counted to the card
    assert '<form name="decision">' in html and '<textarea id="oc-note" name="note"' in html
    assert "In alarm" in html and "0%" in html  # CloudWatch and the pings, from the readings


@pytest.mark.parametrize("fix, title", [
    ("reset-ike", "Reset the IKE session on dc1-wan01"),
    ("restart-strongswan", "Restart strongSwan in AWS"),
    ("repush-router-block", "Re-push the AWS VPN block to dc1-wan01"),
])
def test_each_fix_names_itself_and_its_scope(fix: str, title: str) -> None:
    html = _card(fix={"stdout_json": {"fix": fix, "cause": "c", "evidence": "e"}})
    assert title in html
    assert html.count('class="promise"') == 3


@pytest.mark.parametrize("cause, headline", [
    ("strongswan-down", "strongSwan is not answering in AWS"),
    ("tunnel-shut", "Tunnel10 is shut on dc1-wan01"),
    ("ike-blocked", "INET-IN no longer admits IKE from AWS"),
    ("stale-ike", "The IKE session to AWS is stale"),
])
def test_the_agents_cause_tag_reads_as_a_sentence(cause: str, headline: str) -> None:
    # the agent answers with a tag from its fixed list (itential/agents/tunnel-diagnostics.yaml): the first live card
    # (2026-10-05) showed `strongswan-down` as its headline
    html = _card(fix={"stdout_json": {"fix": "reset-ike", "cause": cause, "evidence": "e"}})
    assert f'<p class="cause">{headline}</p>' in html


def test_every_cause_the_agent_may_name_has_a_headline() -> None:
    import re

    agent = (ROOT / "itential" / "agents" / "tunnel-diagnostics.yaml").read_text()
    tags = set(re.findall(r"-> cause ([a-z-]+), fix", agent)) - {"unknown"}  # unknown always escalates: no card
    assert tags and tags <= set(build.OUTAGE_CAUSES)


def test_a_cause_off_the_list_is_shown_as_the_agent_wrote_it() -> None:
    html = _card(fix={"stdout_json": {"fix": "reset-ike", "cause": "something <odd>", "evidence": "e"}})
    assert '<p class="cause">something &lt;odd&gt;</p>' in html


def test_an_escalation_never_becomes_a_card() -> None:
    with pytest.raises(KeyError):
        _card(fix={"stdout_json": {"fix": "escalate"}})


def test_the_drawings_travel_as_images_because_work_center_strips_svg() -> None:
    import xml.etree.ElementTree as ET

    html = _card()
    assert "<svg" not in html
    drawings = _svgs(html)
    assert len(drawings) == 2  # the lab's mark and the tunnel
    for svg in drawings:
        ET.fromstring(svg)  # well-formed, or the browser shows a broken image
    assert "no IKE session" in drawings[1] and "10.0.64.0/20" in drawings[1]


def test_the_agents_words_and_the_engineers_cannot_inject_markup() -> None:
    evil = '<script>alert(1)</script><img src=x onerror=alert(2)>'
    html = _card(fix={"stdout_json": {"fix": "reset-ike", "cause": evil, "evidence": evil, "session": evil}},
                 number=evil)
    assert "<script>" not in html and "<img src=x" not in html
    assert "&lt;script&gt;" in html
    for svg in _svgs(_card(device='dc1-wan01"><x')):
        assert '"><x' not in svg


def test_a_reading_that_was_not_taken_says_so() -> None:
    bare = {"stdout_json": {"verdict": "could not check router", "signals": {"router": "could not check"}}}
    html = _card(judgement=bare, alarm={}, starts_at="garbage")
    assert "Not read" in html and "not read" in html


def test_the_card_runs_as_the_gateway_runs_it() -> None:
    run = subprocess.run([sys.executable, "-I", "-c", build.OUTAGE_CARD_CODE], input=json.dumps(CARD_IN),
                         capture_output=True, text=True, timeout=30, env={}, check=True)
    html = json.loads(run.stdout)["html"]
    assert "Restart strongSwan in AWS" in html and len(html) < 60_000


@pytest.mark.parametrize("decision, said", [
    ({"decision": {"note": "Checked the EC2 console first."}}, "Checked the EC2 console first."),
    ({"decision": {"note": "  "}}, "no note"),
    ({}, "no note"),
])
def test_a_rejection_notes_the_incident_with_the_engineers_words(decision: dict, said: str) -> None:
    n = build.outage_rejected(decision)["work_notes"]
    assert "rejected in Work Center" in n and "nothing was run" in n and said in n


def test_an_approval_carries_the_engineers_note_into_the_result() -> None:
    r = build.outage_result({"fix": "reset-ike", "answer": _svc({"cleared": True}),
                             "check": _svc({"router": "up", "data_plane": "up"}),
                             "decision": {"decision": {"note": "go"}}})
    assert r["message"].startswith("Approved in Work Center (note: go).")
    assert r["resolve"]["close_notes"] == r["message"]


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
    assert TASKS[card]["type"] == "manual" and TASKS[card]["name"] == "InteractiveHTML"
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


def test_the_card_is_the_html_page_built_from_this_outage() -> None:
    card = TASKS["5c"]
    assert card["app"] == "WorkCenter" and card["view"] == "/work-center/task/InteractiveHTML"
    incoming = card["variables"]["incoming"]
    assert incoming["body"] == "$var.5f.return_data" and incoming["btn_success"] == "Approve and run the fix"
    assert card["variables"]["outgoing"] == {"export": "$var.job.card_decision"}
    assert TASKS["5e"]["variables"]["incoming"]["code"] == build.OUTAGE_CARD_CODE
    assert TASKS["5f"]["variables"]["incoming"]["query"] == "stdout_json.html"
    assert TR["5e"]["8e"]["state"] == "error"  # a card that cannot be drawn runs nothing: a person looks


def test_the_engineers_note_reaches_the_incident_either_way() -> None:
    assert TASKS["58"]["variables"]["incoming"]["code"] == build.OUTAGE_REJECTED_CODE
    assert TASKS["58"]["variables"]["incoming"]["data"] == "$var.job.card_decision"
    assert TASKS["51"]["variables"]["incoming"]["requestBodyPayload"] == "$var.59.return_data"
    assert TASKS["81"]["variables"]["incoming"]["value"] == "$var.job.card_decision"
    assert TASKS["76"]["variables"]["incoming"]["data"] == "$var.81.object"
    assert not {"58", "59"} & _reach("60") and "81" not in _reach("50")


RECHECKS = [("a0", "a1", "a2"), ("a3", "a4", "a5"), ("a6", "a7", "a8"), ("a9", "aa", "ab")]


def test_after_the_fix_the_tunnel_is_read_again_with_patience() -> None:
    # 2026-10-05 run 2: the restart worked, but the router took ~2 minutes to rebuild IKE and the one immediate read
    # said down. The loop now reads up to four times over about three minutes and stops at the first read that is up.
    waits = [TASKS[d]["variables"]["incoming"]["time"] for d, _, _ in RECHECKS]
    assert all(TASKS[d]["name"] == "delay" for d, _, _ in RECHECKS) and 150 <= sum(waits) <= 240
    assert TR["70"]["a0"]["state"] == "success" and "71" not in TASKS and "7f" not in TASKS
    for i, (delay, read, up) in enumerate(RECHECKS):
        assert TASKS[read]["variables"]["incoming"]["serviceName"] == "lab-edge"
        assert TASKS[read]["variables"]["incoming"]["params"] == "$var.70.return_data"
        assert TASKS[read]["variables"]["outgoing"]["result"] == "$var.job.check_result"  # the last read is the result's
        checks = {e["query"]: e["operand_2"]["variable"] for g in TASKS[up]["variables"]["incoming"]["evaluation_groups"]
                  for e in g["evaluations"]}
        assert checks == {"result.return_code": 0, "result.stdout_json.router": "up", "result.stdout_json.data_plane": "up"}
        assert TR[delay] == {read: {"state": "success", "type": "standard"}}
        assert TR[up]["72"]["state"] == "success"  # up: straight to the result
        last = i + 1 == len(RECHECKS)
        assert TR[up]["ac" if last else RECHECKS[i + 1][0]]["state"] == "failure"  # not up: the next read, or the note
        assert TR[read]["80" if last else RECHECKS[i + 1][0]]["state"] == "error"  # unreadable: the next read retries
    assert TR["ac"] == {"72": {"state": "success", "type": "standard"}} and "still not up" in json.dumps(TASKS["ac"])
    assert all(node not in set().union(*(_reach(n) for n in TR.get(node, {}))) for node in TASKS)  # no cycle


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
