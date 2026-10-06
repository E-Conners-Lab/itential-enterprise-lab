"""Diagnose Fabric BGP Outage (R10 + A6, owner decisions 2026-10-05; ADR 0073): LabBgpSessionDown starts it; it reads
BOTH ends of the session with fabric-bgp, opens ONE incident per device pair, lets the fabric-diagnostics agent pick one
fix on one end from a fixed menu, shows it on an HTML card in Work Center (R6's pattern, owner 2026-10-05), and runs
that fix only after approval - proved Established - then reads the session again and resolves the incident."""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("wf_build_fabric", ROOT / "itential" / "workflows" / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)
VERSIONS = build.VERSIONS
NAME = "Diagnose Fabric BGP Outage"
SESSIONS, TARGETS = build.FABRIC_SESSIONS, build.FABRIC_TARGETS


def _svc(out, rc: int = 0) -> dict:
    return {"result": {"return_code": rc, "stdout_json": out}}


def _read(state: str, admin: bool = False, matches: bool = True, iface=None, local=65101, remote=65102) -> dict:
    out = {"session": {"state": state, "admin_shutdown": admin, "idle_reason": "admin-shutdown" if admin else None,
                       "local_as": local, "remote_as": remote, "matches_intent": matches}}
    if iface is not None:
        out["interface"] = iface
    return _svc(out)


ALERT = {"device": "dc1-spine01", "neighbor": "10.101.254.11", "vrf": "default", "peer": "dc1-leaf01"}
PLAN = build.fabric_plan(ALERT, SESSIONS, TARGETS, {"username": "automation"})
UP = {"name": "Ethernet1", "admin_up": True, "oper_up": True}


# ── the tables: every declared session, both ends, the interface toward the peer ──


def test_every_declared_session_end_is_in_the_table_with_its_far_end() -> None:
    assert len(SESSIONS) == 38  # 18 vEOS + 20 C8000v (probe P1)
    for key, end in SESSIONS.items():
        far = SESSIONS[end["far"]]
        assert SESSIONS[far["far"]] is end, key  # A's far end's far end is A
        assert far["device"] == end["peer"] and far["peer"] == end["device"] and far["pair"] == end["pair"]
        assert end["local_as"] == far["remote_as"] and end["remote_as"] == far["local_as"]


def test_an_evpn_session_knows_its_link_and_a_tunnel_session_has_no_interface_to_change() -> None:
    evpn = SESSIONS["dc1-spine01|10.101.254.11"]
    assert evpn["interface"] == "Ethernet1" and evpn["via"] == "10.101.255.1" and evpn["far"] == "dc1-leaf01|10.101.254.1"
    assert SESSIONS["dc1-wan01|10.101.3.2"]["interface"] == "GigabitEthernet6"
    assert SESSIONS["dc1-leaf01|10.101.3.1"]["vrf"] == "PROD"
    tunnel = SESSIONS["dc1-wan01|10.103.100.2"]
    assert "interface" not in tunnel and "via" not in tunnel  # fabric-bgp takes no Tunnel name: a shut tunnel escalates


def test_every_device_is_a_target_with_its_pinned_host_key() -> None:
    keys = VERSIONS["fabric_bgp"]["host_keys"]
    assert set(TARGETS) == {e["device"] for e in SESSIONS.values()} == set(keys)
    for name, t in TARGETS.items():
        assert t["host_keys"] == [keys[name]] and t["platform"] in ("eos", "ios-xe")
        kind, blob = keys[name].split()
        assert base64.b64decode(blob)[4:4 + len(kind)] == kind.encode()  # the type the blob itself names
    assert TARGETS["dc1-spine01"]["mgmt_host"] == "10.100.0.160" and TARGETS["dc1-wan01"]["mgmt_host"] == "10.100.0.144"


# ── the plan and the dedupe ──


def test_the_plan_reads_both_ends_and_correlates_by_device_pair() -> None:
    assert PLAN["ok"] and PLAN["correlation_id"] == "bgp-dc1-leaf01--dc1-spine01"
    assert PLAN["open_query"] == "correlation_id=bgp-dc1-leaf01--dc1-spine01^stateIN1,2,3"
    near, far = PLAN["near_read"], PLAN["far_read"]
    assert near["action"] == far["action"] == "read" and near["username"] == "automation"
    assert json.loads(near["target_json"]) == TARGETS["dc1-spine01"] and json.loads(far["target_json"]) == TARGETS["dc1-leaf01"]
    assert json.loads(near["session_json"]) == {"neighbor": "10.101.254.11", "vrf": "default", "local_as": 65101,
                                                "remote_as": 65102, "interface": "Ethernet1", "via": "10.101.255.1"}
    assert json.loads(far["session_json"])["neighbor"] == "10.101.254.1"
    assert int(near["timeout"]) >= 90  # fabric-bgp's own floor for a read
    # the other end's alert lands on the same incident
    other = build.fabric_plan({"device": "dc1-leaf01", "neighbor": "10.101.254.1", "vrf": "default", "peer": "dc1-spine01"},
                              SESSIONS, TARGETS, {"username": "automation"})
    assert other["correlation_id"] == PLAN["correlation_id"]


@pytest.mark.parametrize("alert", [
    {**ALERT, "neighbor": "10.101.254.99"},  # not declared
    {**ALERT, "vrf": "PROD"},                # declared, but not in that VRF
    {**ALERT, "peer": "dc1-leaf02"},          # declared, but not toward that peer
    {},
])
def test_a_session_the_topology_does_not_declare_acts_on_nothing(alert: dict) -> None:
    p = build.fabric_plan(alert, SESSIONS, TARGETS, {"username": "automation"})
    assert p["ok"] is False and "nothing was changed" in p["message"]


def test_a_still_down_note_names_the_bgp_alert() -> None:
    found = build.open_incident({"open": {"body": {"result": [{"sys_id": "abc", "number": "INC0010030"}]}},
                                 "starts_at": "2026-10-05T19:26:53Z", "alert": "the BGP session-down alert"})
    assert found["open"] and "the BGP session-down alert fired again" in found["note"]["work_notes"]
    tunnel = build.open_incident({"open": {"body": {"result": [{"sys_id": "abc"}]}}})  # R6 keeps its words
    assert "the tunnel-down alert fired again" in tunnel["note"]["work_notes"]


# ── the incident and the agent ──


def test_the_incident_carries_both_ends_and_the_reads_to_confirm() -> None:
    s = build.fabric_summary({"plan": {"stdout_json": PLAN}, "starts_at": "2026-10-05T19:26:53Z",
                              "near": _read("Idle", admin=True, iface=UP),
                              "far": _read("Active", local=65102, remote=65101, iface=UP)})
    inc = s["incident"]
    assert inc["correlation_id"] == "bgp-dc1-leaf01--dc1-spine01"
    assert inc["short_description"] == "BGP session down: dc1-spine01 <-> dc1-leaf01 (10.101.254.11)"
    assert "neighbor is administratively shut down" in s["evidence"] and "reads Active" in s["evidence"]
    assert '"show ip bgp neighbors 10.101.254.11 | include BGP state"' in s["evidence"]
    assert '"show interfaces Ethernet1 | include line protocol"' in s["evidence"]
    assert "2026-10-05T19:26:53Z" in inc["description"]


def test_ios_xe_and_vrf_reads_use_their_own_tables() -> None:
    assert build.fabric_commands(SESSIONS["dc1-wan01|10.103.0.1"], "ios-xe") == [
        "show ip bgp vpnv4 vrf WAN summary | include 10.103.0.1", "show interfaces GigabitEthernet2 | include line protocol"]
    assert build.fabric_commands(SESSIONS["dc1-leaf01|10.101.3.1"], "eos")[0] == \
        "show ip bgp neighbors 10.101.3.1 vrf PROD | include BGP state"
    gate = re.compile(build.SHOW_COMMAND["pattern"])
    for end in SESSIONS.values():
        for cmd in build.fabric_commands(end, TARGETS[end["device"]]["platform"]):
            assert gate.fullmatch(cmd), cmd  # the agent's read tool would refuse anything else


def test_the_evidence_names_a_device_that_differs_from_netbox_or_was_not_read() -> None:
    s = build.fabric_summary({"plan": {"stdout_json": PLAN}, "near": _read("Idle", matches=False, remote=65199),
                              "far": _svc(None, 1)})
    assert "NetBox intends 65101 -> 65102" in s["evidence"] and "dc1-leaf01 could not be read" in s["evidence"]


def _agent(line: str, status: str = "COMPLETE") -> dict:
    return {"agent": {"sessionId": "f00d", "sessionStatus": status, "lastMessage": f"Read both ends.\n{line}"},
            "plan": {"stdout_json": PLAN}}


@pytest.mark.parametrize("fix, device", [
    ("no-shut-neighbor", "dc1-spine01"), ("no-shut-interface", "dc1-leaf01"), ("clear-session", "dc1-spine01"),
])
def test_a_menu_fix_on_one_end_goes_to_the_card_with_that_ends_params(fix: str, device: str) -> None:
    f = build.fabric_fix(_agent(json.dumps({"fix": fix, "device": device, "cause": "x", "evidence": "y"})))
    assert f["fix"] == fix and f["card"] is True and f["device"] == device and f["session"] == "f00d"
    p = f["params"]
    assert p["action"] == fix and json.loads(p["target_json"])["name"] == device
    assert p["wait_seconds"] == str(VERSIONS["fabric_bgp"]["wait_seconds"]) and int(p["timeout"]) >= 90 + 90 + 60
    assert json.loads(p["session_json"]) == build.fabric_service_session(f["end"])


@pytest.mark.parametrize("line, why", [
    ('{"fix": "no-shut-neighbor", "device": "dc1-spine02", "cause": "x"}', "not one end"),  # another device
    ('{"fix": "reload", "device": "dc1-spine01"}', "unknown"),                                 # off the menu
    ("I think BGP is down.", "not the JSON line"),
])
def test_anything_else_is_escalated_and_never_reaches_the_card(line: str, why: str) -> None:
    f = build.fabric_fix(_agent(line))
    assert f["fix"] == "escalate" and f["card"] is False and "params" not in f
    assert "Escalated" in f["escalation"]["work_notes"]


def test_no_shut_interface_on_a_tunnel_session_is_escalated() -> None:
    plan = build.fabric_plan({"device": "dc1-wan01", "neighbor": "10.103.100.2", "vrf": "default", "peer": "br1-wan01"},
                             SESSIONS, TARGETS, {"username": "automation"})
    f = build.fabric_fix({"agent": {"sessionStatus": "COMPLETE", "lastMessage": json.dumps(
        {"fix": "no-shut-interface", "device": "dc1-wan01", "cause": "interface-shut"})}, "plan": {"stdout_json": plan}})
    assert f["fix"] == "escalate" and "not an interface fabric-bgp may change" in f["cause"]


def test_an_agent_that_did_not_finish_is_escalated() -> None:
    f = build.fabric_fix(_agent('{"fix": "clear-session", "device": "dc1-spine01"}', status="FAILED"))
    assert f["fix"] == "escalate" and f["cause"] == "the agent did not finish"


# ── after the fix ──


def test_a_fix_proved_established_resolves_the_incident_with_the_engineers_note() -> None:
    f = build.fabric_fix(_agent('{"fix": "no-shut-neighbor", "device": "dc1-spine01", "cause": "neighbor-shut"}'))
    r = build.fabric_result({"fix": {"stdout_json": f}, "answer": _svc({"established": True, "saved": True}),
                             "check": _read("Established"), "decision": {"decision": {"note": "drill"}}})
    assert r["fixed"] and r["resolve"]["state"] == "6" and r["resolve"]["close_code"] == "Solution provided"
    assert r["message"].startswith("Approved in Work Center (note: drill).")
    assert "no-shut-neighbor on dc1-spine01 (it proved Established and saved the configuration)" in r["message"]


def test_a_session_still_down_leaves_the_incident_open_with_a_note() -> None:
    f = build.fabric_fix(_agent('{"fix": "clear-session", "device": "dc1-spine01", "cause": "stuck-session"}'))
    r = build.fabric_result({"fix": {"stdout_json": f}, "answer": _svc({"error": "sent, but not Established"}, 1),
                             "check": _read("Active")})
    assert r["fixed"] is False and "stays open" in r["note"]["work_notes"] and "resolve" not in r
    assert "still reads Active" in r["message"]


# ── the card: an HTML page in Work Center, R6's design (owner 2026-10-05) ──

CARD_FIX = build.fabric_fix(_agent(json.dumps({"fix": "no-shut-neighbor", "device": "dc1-spine01", "cause": "neighbor-shut",
                                              "evidence": "dc1-spine01 has neighbor 10.101.254.11 shut down."})))
CARD_IN = {"plan": {"stdout_json": PLAN}, "fix": {"stdout_json": CARD_FIX}, "number": "INC0010031",
           "starts_at": "2026-10-05T19:26:53Z", "now": "2026-10-05T19:31:00Z",
           "near": _read("Idle", admin=True, iface=UP),
           "far": _read("Active", local=65102, remote=65101, iface=UP)}


def _card(**over) -> str:
    return build.fabric_card({**CARD_IN, **over})["html"]


def _svgs(page: str) -> list[str]:
    return [base64.b64decode(b).decode() for b in re.findall(r'src="data:image/svg\+xml;base64,([A-Za-z0-9+/=]+)"', page)]


def test_the_card_lays_out_both_ends_the_fix_and_the_decision() -> None:
    page = _card()
    assert "A BGP session is down: dc1-spine01 and dc1-leaf01" in page and "INC0010031" in page
    assert "Bring the BGP neighbor back on dc1-spine01" in page
    assert "The BGP neighbor 10.101.254.11 is shut down on dc1-spine01" in page
    assert "4 min" in page and "19:26 UTC" in page
    assert '<form name="decision">' in page and '<textarea id="oc-note" name="note"' in page
    assert "Idle: neighbor shut" in page and "Active" in page and "Matches" in page


@pytest.mark.parametrize("fix, device, title", [
    ("no-shut-neighbor", "dc1-spine01", "Bring the BGP neighbor back on dc1-spine01"),
    ("no-shut-interface", "dc1-leaf01", "Bring Ethernet1 back up on dc1-leaf01"),
    ("clear-session", "dc1-spine01", "Reset the BGP session on dc1-spine01"),
])
def test_each_fix_names_itself_its_end_and_its_scope(fix: str, device: str, title: str) -> None:
    f = build.fabric_fix(_agent(json.dumps({"fix": fix, "device": device, "cause": "c", "evidence": "e"})))
    page = _card(fix={"stdout_json": f})
    assert title in page and page.count('class="promise"') == 3


def test_every_cause_the_agent_may_name_for_a_fix_has_a_headline() -> None:
    agent = (ROOT / "itential" / "agents" / "fabric-diagnostics.yaml").read_text()
    tags = {t for t, fix in re.findall(r"-> cause ([a-z-]+), fix ([a-z-]+)", agent) if fix != "escalate"}
    assert tags == set(build.FABRIC_CAUSES)
    fixes = {fix for _, fix in re.findall(r"-> cause ([a-z-]+), fix ([a-z-]+)", agent)}
    assert fixes == set(build.FABRIC_FIXES)


def test_an_escalation_never_becomes_a_card() -> None:
    with pytest.raises(KeyError):
        _card(fix={"stdout_json": {"fix": "escalate"}})


def test_the_drawings_travel_as_images_because_work_center_strips_svg() -> None:
    page = _card()
    assert "<svg" not in page
    drawings = _svgs(page)
    assert len(drawings) == 2  # the lab's mark and the session
    for svg in drawings:
        ET.fromstring(svg)
    assert "dc1-spine01" in drawings[1] and "AS 65101" in drawings[1] and "AS 65102" in drawings[1]
    assert "Ethernet1 up" in drawings[1] and "down" in drawings[1]


def test_values_from_devices_the_agent_and_the_engineer_cannot_inject_markup() -> None:
    evil = '<script>alert(1)</script><img src=x onerror=alert(2)>'
    bad_fix = {**CARD_FIX, "cause": evil, "evidence": evil, "session": evil}
    page = _card(fix={"stdout_json": bad_fix}, number=evil,
                 near=_svc({"session": {"state": evil, "admin_shutdown": False, "matches_intent": True,
                                        "local_as": evil, "remote_as": 65102}}))
    assert "<script>" not in page and "<img src=x" not in page and "&lt;script&gt;" in page
    for svg in _svgs(page):
        ET.fromstring(svg)


def test_a_reading_that_was_not_taken_says_so() -> None:
    page = _card(near=_svc(None, 1), far={}, starts_at="garbage")
    assert "not read" in page and "Not read" in page


def test_the_card_runs_as_the_gateway_runs_it() -> None:
    run = subprocess.run([sys.executable, "-I", "-c", build.FABRIC_CARD_CODE], input=json.dumps(CARD_IN),
                         capture_output=True, text=True, timeout=30, env={}, check=True)
    page = json.loads(run.stdout)["html"]
    assert "Bring the BGP neighbor back on dc1-spine01" in page and len(page) < 60_000


@pytest.mark.parametrize("code, data", [
    ("FABRIC_PLAN_CODE", ALERT),
    ("FABRIC_OPEN_CODE", {"open": {"body": {"result": []}}, "alert": "the BGP session-down alert"}),
    ("FABRIC_SUMMARY_CODE", {"plan": {"stdout_json": PLAN}, "near": _read("Idle", admin=True), "far": _read("Active")}),
    ("FABRIC_FIX_CODE", _agent('{"fix": "no-shut-neighbor", "device": "dc1-spine01"}')),
    ("FABRIC_RESULT_CODE", {"fix": {"stdout_json": CARD_FIX}, "answer": _svc({"established": True}),
                            "check": _read("Established")}),
])
def test_the_pure_functions_run_as_the_gateway_runs_them(code: str, data: dict) -> None:
    run = subprocess.run([sys.executable, "-I", "-c", getattr(build, code)], input=json.dumps(data),
                         capture_output=True, text=True, timeout=30, env={}, check=True)
    assert json.loads(run.stdout)


# ── the workflow ──

WF = build.diagnose_fabric_bgp_outage()
TASKS, TR = WF["tasks"], WF["transitions"]
RECHECKS = [("b0", "b1", "b2"), ("b3", "b4", "b5"), ("b6", "b7", "b8"), ("b9", "ba", "bb")]


def _reach(start: str) -> set[str]:
    seen, todo = set(), [start]
    while todo:
        node = todo.pop()
        if node not in seen:
            seen.add(node)
            todo.extend(TR.get(node, {}))
    return seen


def test_the_committed_document_is_what_build_py_makes() -> None:
    committed = json.loads((ROOT / "itential" / "workflows" / "diagnose-fabric-bgp-outage.json").read_text())
    assert committed == json.loads(json.dumps(WF))


def test_it_is_registered_named_and_gated_on_the_relays_fields() -> None:
    assert WF["name"] == NAME == VERSIONS["workflows"]["diagnose_fabric_bgp_outage"]
    assert build.diagnose_fabric_bgp_outage in build.BUILDERS
    gate = build.INPUT_GATES[NAME]
    assert set(gate) == {"alertname", "device", "neighbor", "vrf", "peer", "starts_at", "fingerprint"}
    assert gate["alertname"]["enum"] == ["LabBgpSessionDown"] and gate["device"]["enum"] == sorted(TARGETS)
    assert re.fullmatch(gate["neighbor"]["pattern"], "10.101.254.11")
    assert not re.fullmatch(gate["neighbor"]["pattern"], "10.101.254.11; reload")


def test_no_fix_runs_before_the_card_is_approved() -> None:
    card = "5c"
    assert TASKS[card]["type"] == "manual" and TASKS[card]["name"] == "InteractiveHTML"
    assert "61" in _reach("60") and "61" not in (_reach("4f") - _reach(card))
    assert TR[card]["60"]["state"] == "success" and TR[card]["50"]["state"] == "failure"
    assert "61" not in _reach("50")  # a rejection runs nothing


def test_the_approved_fix_is_one_fabric_bgp_call_with_the_params_the_menu_made() -> None:
    fix = TASKS["61"]["variables"]["incoming"]
    assert fix["serviceName"] == "fabric-bgp" and fix["params"] == "$var.60.return_data"
    assert TASKS["60"]["variables"]["incoming"]["query"] == "stdout_json.params"
    services = [t["variables"]["incoming"].get("serviceName") for t in TASKS.values() if t.get("name") == "runService"]
    assert set(services) == {"fabric-bgp"}


def test_both_ends_are_read_and_a_far_end_that_cannot_be_read_is_evidence_too() -> None:
    assert TASKS["3b"]["variables"]["incoming"]["params"] == "$var.3a.return_data"
    assert TASKS["3a"]["variables"]["incoming"]["query"] == "stdout_json.near_read"
    assert TASKS["3d"]["variables"]["incoming"]["query"] == "stdout_json.far_read"
    assert TR["3e"]["3f"]["state"] == "error" and TR["3f"]["c0"]["state"] == "success"
    assert TR["c0"]["30"]["state"] == "success" and "37" not in _reach("30")  # Established again: no incident


def test_the_card_is_the_html_page_built_from_this_outage() -> None:
    card = TASKS["5c"]
    assert card["app"] == "WorkCenter" and card["view"] == "/work-center/task/InteractiveHTML"
    incoming = card["variables"]["incoming"]
    assert incoming["body"] == "$var.5f.return_data" and incoming["btn_success"] == "Approve and run the fix"
    assert card["variables"]["outgoing"] == {"export": "$var.job.card_decision"}
    assert TASKS["5e"]["variables"]["incoming"]["code"] == build.FABRIC_CARD_CODE
    assert TASKS["5f"]["variables"]["incoming"]["query"] == "stdout_json.html"
    assert TR["5e"]["8e"]["state"] == "error"


def test_the_engineers_note_reaches_the_incident_either_way() -> None:
    assert TASKS["58"]["variables"]["incoming"]["code"] == build.OUTAGE_REJECTED_CODE
    assert TASKS["58"]["variables"]["incoming"]["data"] == "$var.job.card_decision"
    assert TASKS["81"]["variables"]["incoming"]["value"] == "$var.job.card_decision"
    assert TASKS["76"]["variables"]["incoming"]["data"] == "$var.81.object"
    assert not {"58", "59"} & _reach("60") and "81" not in _reach("50")


def test_after_the_fix_the_session_is_read_again_with_patience() -> None:
    waits = [TASKS[d]["variables"]["incoming"]["time"] for d, _, _ in RECHECKS]
    assert all(TASKS[d]["name"] == "delay" for d, _, _ in RECHECKS) and 120 <= sum(waits) <= 200
    assert TR["70"]["b0"]["state"] == "success"
    for i, (_delay, read, up) in enumerate(RECHECKS):
        assert TASKS[read]["variables"]["incoming"]["serviceName"] == "fabric-bgp"
        assert TASKS[read]["variables"]["incoming"]["params"] == "$var.70.return_data"
        assert TASKS[read]["variables"]["outgoing"]["result"] == "$var.job.check_result"
        evals = [e for g in TASKS[up]["variables"]["incoming"]["evaluation_groups"] for e in g["evaluations"]]
        assert [(e["query"], e["operand_2"]["variable"]) for e in evals] == [("result.stdout_json.session.state",
                                                                               "Established")]
        last = i + 1 == len(RECHECKS)
        assert TR[up]["72"]["state"] == "success"
        assert TR[up]["bc" if last else RECHECKS[i + 1][0]]["state"] == "failure"
        assert TR[read]["80" if last else RECHECKS[i + 1][0]]["state"] == "error"
    assert all(node not in set().union(*(_reach(n) for n in TR.get(node, {}))) for node in TASKS)  # no cycle


def test_an_escalation_notes_the_incident_and_shows_no_card() -> None:
    assert TR["40"]["4e"]["state"] == "success" and TR["40"]["41"]["state"] == "failure"
    assert "5c" not in _reach("41") and TASKS["43"]["name"] == "updateIncident"


def test_an_open_incident_for_the_pair_is_only_noted() -> None:
    assert "37" not in _reach("20") and TASKS["37"]["name"] == "createIncident" and TASKS["22"]["name"] == "updateIncident"
    assert TASKS["24"]["variables"]["incoming"]["value"] == "the BGP session-down alert"


def test_the_agent_is_named_by_a_marker_the_import_fills_in() -> None:
    agent = TASKS["4b"]
    assert agent["name"] == "runAgent" and agent["app"] == "AgentSessionManager"
    assert agent["variables"]["incoming"]["agent"] == build.FABRIC_AGENT_MARKER == "__AGENT_ID:fabric-diagnostics__"


def test_every_task_reaches_the_end() -> None:
    for tid in TASKS:
        assert "workflow_end" in _reach(tid) or tid == "workflow_end", tid


# ── the wiring: the relay route, the trigger, the service ──


def test_the_alert_relays_route_is_this_workflows_trigger() -> None:
    obs = yaml.safe_load((ROOT / "observability" / "observability.yaml").read_text())
    om = VERSIONS["operations_manager"]["diagnose_fabric_bgp_outage"]
    route = obs["alert_relay"]["routes"]["LabBgpSessionDown"]
    assert om["automation"] == NAME and om["endpoint"]["route"] == route["route"]
    # the relay forwards exactly the gate's alert fields (alertname, starts_at, fingerprint are always added)
    assert set(route["labels"]) | {"alertname", "starts_at", "fingerprint"} == set(build.INPUT_GATES[NAME])


def test_the_trigger_is_made_on_production_only() -> None:
    play = (ROOT / "ansible" / "playbooks" / "platform.yml").read_text()
    block = play[play.index("otr_key: diagnose_fabric_bgp_outage"):][:200]
    assert "when: not (dev_overlay | default(false) | bool)" in block


def test_fabric_bgp_is_a_gateway_service_on_the_shared_device_account() -> None:
    (svc,) = [s for s in VERSIONS["terraform_run"]["services"] if s["name"] == "fabric-bgp"]
    assert svc["filename"] == "itential/fabric-bgp.py"
    assert svc["secrets"] == [{"name": "lab-automation-password", "type": "env", "target": "FABRIC_BGP_PASSWORD"}]
    assert "lab-automation-password" in VERSIONS["vault"]["gateway_aliases"]  # a base alias, bound on every tier
    assert VERSIONS["fabric_bgp"]["username"] == "automation"
