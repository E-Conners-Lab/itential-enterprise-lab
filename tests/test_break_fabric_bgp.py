"""Break Fabric BGP (R10 PR C, owner decisions 2026-10-05 / 2026-10-06; ADR 0073 amendment): the outage loop's drill.
One fault - a neighbor shutdown or the interface toward the peer shut - on a healthy, declared vEOS session, behind an
HTML approval card that shows the exact lines; the fault goes in under an EOS commit timer, the workflow waits for the
outage loop to bring the session back and only then cancels the timer, and otherwise lets EOS roll it back."""

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

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("wf_build_drill", ROOT / "itential" / "workflows" / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)
NAME = "Break Fabric BGP"
SESSIONS, TARGETS = build.FABRIC_SESSIONS, build.FABRIC_TARGETS


def _plan(device: str = "dc1-spine01", neighbor: str = "10.101.254.11", fault: str = "neighbor-shutdown") -> dict:
    return build.drill_plan({"device": device, "neighbor": neighbor, "fault": fault}, SESSIONS, TARGETS,
                            {"username": "automation"})


def _svc(out, rc: int = 0) -> dict:
    return {"result": {"return_code": rc, "stdout_json": out}}


# ── the plan ──


def test_a_neighbor_drill_on_the_fabric_plans_its_inject_plan_and_confirm() -> None:
    p = _plan()
    assert p["ok"] and p["action"] == "inject-neighbor-shutdown" and p["revert_minutes"] == build.DRILL_REVERT_MINUTES
    assert p["inject"]["action"] == "inject-neighbor-shutdown" and p["inject"]["revert_minutes"] == "20"
    assert p["plan_params"]["plan_for"] == p["inject"]["action"] and "username" not in p["plan_params"]
    assert p["confirm"]["action"] == "confirm-drill" and "drill_session" not in p["confirm"]  # added after the inject
    assert json.loads(p["inject"]["target_json"]) == TARGETS["dc1-spine01"]
    assert p["far"]["device"] == "dc1-leaf01"


def test_an_interface_drill_needs_the_interface_toward_the_peer() -> None:
    p = _plan(neighbor="10.101.255.1", fault="interface-shutdown")
    assert p["ok"] and json.loads(p["inject"]["session_json"])["interface"] == "Ethernet1"


@pytest.mark.parametrize(("device", "neighbor", "fault", "why"), [
    ("dc1-wan01", "10.101.3.2", "neighbor-shutdown", "not vEOS"),        # the IOS-XE revert timer needs archive
    ("dc1-spine01", "10.101.254.99", "neighbor-shutdown", "not a session"),
    ("dc1-spine01", "10.101.254.11", "reload", "not a session"),
])
def test_anything_else_breaks_nothing(device, neighbor, fault, why) -> None:
    p = _plan(device, neighbor, fault)
    assert p["ok"] is False and why in p["message"] and "nothing was changed" in p["message"]


# ── the card ──

LINES = _svc({"plan": {"for": "inject-neighbor-shutdown", "device": "dc1-spine01", "mode": "configure session",
                       "lines": ["router bgp 65101", "neighbor 10.101.254.11 shutdown", "commit timer 00:20:00"]}})


def _up(local: int, remote: int) -> dict:
    return _svc({"session": {"state": "Established", "admin_shutdown": False, "local_as": local, "remote_as": remote,
                             "matches_intent": True}})


CARD_IN = {"plan": {"stdout_json": _plan()}, "near": _up(65101, 65102), "far": _up(65102, 65101), "lines": LINES,
           "now": "2026-10-06T18:00:00Z"}


def test_the_card_shows_the_session_healthy_the_exact_lines_and_when_it_rolls_back() -> None:
    page = build.drill_card(CARD_IN)["html"]
    assert "Break a BGP session on purpose: dc1-spine01 and dc1-leaf01" in page
    assert "Shut the BGP neighbor 10.101.254.11 on dc1-spine01" in page
    assert "<pre>router bgp 65101\nneighbor 10.101.254.11 shutdown\ncommit timer 00:20:00</pre>" in page
    assert "a configuration session under a commit timer; never saved" in page
    assert "rolls it back at 18:20 UTC" in page and "Approve breaks this one session on purpose" in page
    assert "Note for this drill" in page and "incident" not in page.split("Your decision")[1].lower()
    svgs = [base64.b64decode(b).decode() for b in re.findall(r'src="data:image/svg\+xml;base64,([A-Za-z0-9+/=]+)"', page)]
    assert "<svg" not in page and len(svgs) == 2
    for svg in svgs:
        ET.fromstring(svg)
    assert "up now" in svgs[1] and "Established" in svgs[1]


def test_the_card_runs_as_the_gateway_runs_it() -> None:
    run = subprocess.run([sys.executable, "-I", "-c", build.DRILL_CARD_CODE], input=json.dumps(CARD_IN),
                         capture_output=True, text=True, timeout=30, env={}, check=True)
    assert "commit timer 00:20:00" in json.loads(run.stdout)["html"]


# ── the outcome ──


def test_a_drill_the_loop_fixed_is_confirmed() -> None:
    r = build.drill_result({"inject": _svc({"drill_session": "r10-drill-1791306000"}), "check": _up(65101, 65102),
                            "confirm": _svc({"confirmed": True})})
    assert r["ok"] and "confirmed" in r["message"] and "r10-drill-1791306000" in r["message"]


def test_a_drill_left_to_the_timer_is_proved_rolled_back() -> None:
    r = build.drill_result({"inject": _svc({"drill_session": "r10-drill-1791306000"}),
                            "check": _svc({"session": {"state": "Idle"}}), "after_timer": _up(65101, 65102)})
    assert r["ok"] and "rolled r10-drill-1791306000 back" in r["message"] and "Established" in r["message"]
    bad = build.drill_result({"inject": _svc({}), "after_timer": _svc({"session": {"state": "Idle"}})})
    assert bad["ok"] is False


@pytest.mark.parametrize(("code", "data"), [
    ("DRILL_PLAN_CODE", {"device": "dc1-spine01", "neighbor": "10.101.254.11", "fault": "neighbor-shutdown"}),
    ("DRILL_RESULT_CODE", {"confirm": _svc({"confirmed": True})}),
])
def test_the_pure_functions_run_as_the_gateway_runs_them(code: str, data: dict) -> None:
    run = subprocess.run([sys.executable, "-I", "-c", getattr(build, code)], input=json.dumps(data),
                         capture_output=True, text=True, timeout=30, env={}, check=True)
    assert json.loads(run.stdout)


# ── the workflow ──

WF = build.break_fabric_bgp()
TASKS, TR = WF["tasks"], WF["transitions"]


def _reach(start: str) -> set[str]:
    seen, todo = set(), [start]
    while todo:
        node = todo.pop()
        if node not in seen:
            seen.add(node)
            todo.extend(TR.get(node, {}))
    return seen


def test_the_committed_document_is_what_build_py_makes() -> None:
    committed = json.loads((ROOT / "itential" / "workflows" / "break-fabric-bgp.json").read_text())
    assert committed == json.loads(json.dumps(WF))


def test_it_is_registered_named_and_gated() -> None:
    assert WF["name"] == NAME == build.VERSIONS["workflows"]["break_fabric_bgp"]
    assert build.break_fabric_bgp in build.BUILDERS
    gate = build.INPUT_GATES[NAME]
    assert set(gate) == {"device", "neighbor", "fault"}
    assert gate["fault"]["enum"] == sorted(build.DRILL_FAULTS)


def test_nothing_breaks_before_the_card_is_approved_or_on_an_unhealthy_session() -> None:
    card = TASKS["47"]
    assert card["name"] == "InteractiveHTML" and card["variables"]["incoming"]["btn_success"] == "Approve and break it"
    assert TASKS["4e"]["variables"]["incoming"]["code"] == build.DRILL_CARD_CODE
    assert "5b" in _reach("47") and "5b" not in _reach("48") and TR["47"]["48"]["state"] == "failure"
    assert TR["2c"]["2d"]["state"] == TR["29"]["2d"]["state"] == "failure" and "5b" not in _reach("2d")
    far = TASKS["29"]["variables"]["incoming"]["evaluation_groups"][0]["evaluations"][0]  # the far end must read
    assert far["operand_1"]["task"] == "2f" and far["operand_2"]["variable"] == "Established"  # Established too
    assert TR["3c"]["e0"]["state"] == "failure"  # no exact lines: no blind card
    assert TASKS["5b"]["variables"]["incoming"]["serviceName"] == "fabric-bgp"


def test_the_drill_never_fixes_and_confirms_only_after_a_read_says_established() -> None:
    services = {t["variables"]["incoming"]["params"] for t in TASKS.values() if t.get("name") == "runService"}
    assert services == {"$var.2a.return_data", "$var.2e.return_data", "$var.3a.return_data", "$var.5a.return_data",
                        "$var.50.object"}  # reads, plan, inject, confirm: no fix call
    assert TASKS["50"]["variables"]["incoming"]["value"] == "$var.5e.return_data"
    for _delay, read, up in [r[:3] for r in build.DRILL_READS]:
        assert TR[up]["7a"]["state"] == "success"
        evals = [e for g in TASKS[up]["variables"]["incoming"]["evaluation_groups"] for e in g["evaluations"]]
        assert [(e["query"], e["operand_2"]["variable"]) for e in evals] == [("result.stdout_json.session.state",
                                                                               "Established")]
    assert sum(r[3] for r in build.DRILL_READS) < build.DRILL_REVERT_MINUTES * 60  # the last read beats the timer


def test_a_session_not_back_is_left_to_the_timer_and_read_after_it() -> None:
    last_up = build.DRILL_READS[-1][2]
    assert TR[last_up]["8a"]["state"] == "failure" and TR["8a"] == {"8b": {"state": "success", "type": "standard"}}
    wait = TASKS["8b"]["variables"]["incoming"]["time"]
    assert sum(r[3] for r in build.DRILL_READS) + wait > build.DRILL_REVERT_MINUTES * 60
    assert TASKS["8c"]["variables"]["outgoing"]["result"] == "$var.job.after_timer_result"
    assert "7a" not in _reach("8a")


def test_every_task_reaches_the_end_without_a_cycle() -> None:
    for tid in TASKS:
        assert "workflow_end" in _reach(tid) or tid == "workflow_end", tid
    assert all(node not in set().union(*(_reach(n) for n in TR.get(node, {}))) for node in TASKS)
