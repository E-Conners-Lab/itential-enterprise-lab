"""Push Configuration with Revert Timer (step 10, F9 decision 2a; a sibling of Push Configuration with Approval by the
owner's choice of 2026-10-01). The approver sees exactly what is pushed and what must hold afterwards, nothing reaches
the router before the approval, the service gets the same lines the card showed, the outcome is read from what the
service proved (never the exit code alone), and the service is bound to exactly its routers' passwords."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = yaml.safe_load((ROOT / "itential" / "versions.yaml").read_text())
SPEC = importlib.util.spec_from_file_location("build", ROOT / "itential" / "workflows" / "build.py")
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)

NAME = VERSIONS["workflows"]["config_push_revert"]
WF = json.loads((ROOT / "itential" / "workflows" / "push-configuration-with-revert-timer.json").read_text())
TARGETS = VERSIONS["revert_push"]["targets"]
SERVICE = next(s for s in VERSIONS["terraform_run"]["edge_services"] if s["name"] == "config-push-revert")
INPUTS = {
    "device": "clab-rtr1",
    "config": "ip access-list standard REVERT-PROBE\n remark probe\n\n",
    "reason": "step 10 part B",
    "revert_minutes": 5,
    "checks": {"bgp_established": [{"neighbor": "10.1.0.1", "vrf": "WAN"}],
               "pings": [{"target": "10.1.0.1", "source": "Loopback0"}], "settle_seconds": 60},
}


def _task(name: str) -> str:
    return next(k for k, t in WF["tasks"].items() if t["name"] == name)


def _by_summary(start: str) -> str:
    return next(k for k, t in WF["tasks"].items() if t.get("summary", "").startswith(start))


PUSH = _by_summary("push under the revert timer")
CHECK = _by_summary("would config-push-revert take this?")


def _reach(start: str, skip: set[str] = frozenset(), states: set[str] | None = None) -> set[str]:
    seen, todo = set(), [start]
    while todo:
        n = todo.pop()
        if n in seen or n in skip:
            continue
        seen.add(n)
        todo.extend(dst for dst, e in WF["transitions"].get(n, {}).items() if states is None or e["state"] in states)
    return seen


def test_the_workflow_is_registered_under_its_name() -> None:
    assert WF["name"] == NAME == "Push Configuration with Revert Timer"
    assert build.config_push_revert in build.BUILDERS


def test_nothing_reaches_the_router_before_the_approval() -> None:
    approval = _task("ViewData")
    assert PUSH in _reach("workflow_start")
    assert PUSH not in _reach("workflow_start", skip={approval})
    # Reject (the view's failure) never reaches the push
    assert PUSH not in _reach(next(d for d, e in WF["transitions"][approval].items() if e["state"] == "failure"))
    # the service's no-device check runs before the card: what it refuses never reaches an approver
    assert CHECK in _reach("workflow_start", skip={approval}) and approval not in _reach("workflow_start", skip={CHECK})
    plan = build.revert_plan(INPUTS, TARGETS)
    assert plan["check"]["action"] == "check" and "username" not in plan["check"]


def test_the_card_shows_every_line_and_every_check_and_the_service_gets_the_same_lines() -> None:
    plan = build.revert_plan(INPUTS, TARGETS)
    assert plan["ok"]
    card, params = plan["card"], plan["params"]
    assert card["lines"] == ["ip access-list standard REVERT-PROBE", " remark probe"] == json.loads(params["lines_json"])
    assert card["kept only if"] == ["BGP neighbour 10.1.0.1 Established (VRF WAN)", "ping 10.1.0.1 from Loopback0 at least 80%",
                                    "a fresh login to the router still works"]
    assert card["revert timer"] == "5 minutes" and card["reason"] == "step 10 part B"
    assert json.loads(params["target_json"]) == {"name": "clab-rtr1", "mgmt_host": TARGETS["clab-rtr1"]["mgmt_host"]}
    assert params["username"] == TARGETS["clab-rtr1"]["username"] and params["revert_minutes"] == "5"
    assert json.loads(params["checks_json"]) == INPUTS["checks"]
    assert "write memory" in card["saving"]
    # the check is asked about exactly what the push will send
    assert {k: v for k, v in plan["check"].items() if k not in ("action", "timeout")} == {
        k: v for k, v in params.items() if k not in ("action", "username")}
    # the push's Gateway timeout is the one the check answers, set on the push params just before the push
    push_params = WF["tasks"][PUSH]["variables"]["incoming"]["params"].split(".")[1]
    timeout_key = WF["tasks"][push_params]["variables"]["incoming"]
    assert timeout_key["path"] == ["timeout"]
    answer = WF["tasks"][timeout_key["value"].split(".")[1]]["variables"]["incoming"]
    assert answer["query"] == "result.stdout_json.timeout" and answer["obj"] == f"$var.{CHECK}.result"
    # the card the approver sees is the plan's card, and the push takes the plan's params
    view = WF["tasks"][_task("ViewData")]["variables"]["incoming"]
    card = WF["tasks"][view["body"].split(".")[2] if view["body"].count(".") > 2 else view["body"].split(".")[1]]
    assert card["variables"]["incoming"]["query"] == "stdout_json.card"
    plan_task = card["variables"]["incoming"]["obj"].split(".")[1]
    with_timeout = WF["tasks"][WF["tasks"][PUSH]["variables"]["incoming"]["params"].split(".")[1]]
    params = WF["tasks"][with_timeout["variables"]["incoming"]["obj"].split(".")[1]]
    assert params["variables"]["incoming"] == {**params["variables"]["incoming"], "query": "stdout_json.params",
                                                "obj": f"$var.{plan_task}.result"}


@pytest.mark.parametrize("change, said", [({"device": "br1-wan01"}, "is not a router this workflow may change"),
                                          ({"config": "\n  \n"}, "no configuration lines")])
def test_a_plan_that_cannot_be_made_sends_nothing(change, said) -> None:
    plan = build.revert_plan({**INPUTS, **change}, TARGETS)
    assert not plan["ok"] and said in plan["message"]


def test_the_shipped_code_runs_as_the_runner_runs_it() -> None:
    def runs(code: str, data: dict) -> dict:
        return json.loads(subprocess.run([sys.executable, "-I", "-c", code], input=json.dumps(data),
                                         capture_output=True, text=True, check=True).stdout)

    assert runs(build.REVERT_PLAN_CODE, INPUTS) == build.revert_plan(INPUTS, TARGETS)
    answer = {"push": {"result": {"return_code": 0, "stdout_json": {"saved": True, "changed": True}}}}
    assert runs(build.REVERT_SUMMARY_CODE, answer) == build.revert_summary(answer)
    # each runCode task carries its own code
    code = {t["summary"]: t["variables"]["incoming"]["code"] for t in WF["tasks"].values() if t["name"] == "runCode"}
    assert code == {"the plan: the service's params and the approval card (Python on the runner)": build.REVERT_PLAN_CODE,
                    "what the push did (saved, rolled back, nothing sent or unknown)": build.REVERT_SUMMARY_CODE}


@pytest.mark.parametrize("answer, state, changed", [
    ({"return_code": 0, "stdout_json": {"saved": True, "changed": True, "router": "saved"}}, "saved", True),
    ({"return_code": 0, "stdout_json": {"saved": True, "changed": False, "router": "saved"}}, "saved", False),
    ({"return_code": 1, "stdout_json": {"rolled_back": True, "sent": True, "router": "rolled back",
                                        "error": "rolled back: a check failed after the change"}}, "rolled back", False),
    ({"return_code": 1, "stdout_json": {"sent": False, "error": "the checks fail before any change"}}, "unchanged", False),
    ({"return_code": 1, "stdout_json": {"action": None, "sent": False, "error": "refused"}}, "unchanged", False),
    ({"return_code": 1, "stdout_json": {"sent": True, "confirmed": True, "router": "changed and confirmed but NOT saved"}},
     "unknown", True),
    ({}, "unknown", True),  # no answer at all: a change may be pending
    ({"return_code": 0, "stdout_json": {"saved": False}}, "unknown", True),  # exit 0 alone proves nothing
])
def test_the_outcome_is_what_the_service_proved(answer, state, changed) -> None:
    out = build.revert_summary({"push": {"result": answer} if answer else {}})
    assert (out["state"], out["changed"]) == (state, changed)
    assert (out["message"] == "") == (state == "saved")


def test_only_a_saved_change_reads_as_saved() -> None:
    saved_eval = next(k for k, t in WF["tasks"].items() if t["summary"] == "confirmed and saved?")
    group = WF["tasks"][saved_eval]["variables"]["incoming"]["evaluation_groups"][0]["evaluations"][0]
    assert group["query"] == "stdout_json.state" and group["operand_2"] == {"task": "static", "variable": "saved"}


def test_the_service_binds_exactly_its_routers_passwords() -> None:
    want = {f"LAB_EDGE_PASSWORD_{n.upper().replace('-', '_')}": t["password_alias"] for n, t in TARGETS.items()}
    assert {s["target"]: s["name"] for s in SERVICE["secrets"]} == want
    # clab-rtr1 logs in as the device account; dc1-wan01 as the window's time-boxed account, its password only in the dev
    # Vault, and the same account Hand Off will use (aws_vpn.targets)
    assert TARGETS["clab-rtr1"]["password_alias"] == "lab-automation-password"
    assert TARGETS["dc1-wan01"]["username"] == VERSIONS["aws_vpn"]["targets"]["dc1-wan01"]["username"]
    assert TARGETS["dc1-wan01"]["mgmt_host"] == VERSIONS["aws_vpn"]["targets"]["dc1-wan01"]["target"]["mgmt_host"]
    path = VERSIONS["vault"]["edge_gateway_aliases"][TARGETS["dc1-wan01"]["password_alias"]]["path"]
    assert path.startswith("devices/")  # the Gateway reads it, the Platform cannot
    assert SERVICE["filename"] == "itential/config-push-revert.py"


def test_the_inputs_are_gated_to_what_the_service_takes() -> None:
    gate = build.INPUT_GATES[NAME]
    assert gate["device"]["enum"] == sorted(TARGETS) == WF["inputSchema"]["properties"]["device"]["enum"]
    assert (gate["revert_minutes"]["minimum"], gate["revert_minutes"]["maximum"]) == (2, 15)  # config-push-revert's range
    checks = gate["checks"]["properties"]
    assert checks["bgp_established"]["maxItems"] == checks["pings"]["maxItems"] == 20
    assert gate["checks"]["additionalProperties"] is False
    assert set(checks) == {"bgp_established", "pings", "settle_seconds"}


def test_push_configuration_with_approval_is_untouched() -> None:
    """The owner's choice: the agents' only write tool and Remove Branch VLAN's child keep exactly their workflow."""
    original = json.loads((ROOT / "itential" / "workflows" / "push-configuration-with-approval.json").read_text())
    assert set(original["inputSchema"]["properties"]) == {"device", "config", "reason"}
    assert not any(t["name"] == "runService" for t in original["tasks"].values())


def test_a_refusal_or_an_unavailable_service_before_the_card_sends_nothing() -> None:
    """On a Gateway without the service (production until step 10's window), or for inputs it refuses, the job ends
    before the card with changed = false - never after an approval."""
    first = _by_summary("changed = false")
    # changed = false is set before anything else can run, so every early end leaves it false
    assert CHECK not in _reach("workflow_start", skip={first})
    for src, state in (("13", "error"), ("14", "failure")):
        branch = next(d for d, e in WF["transitions"][src].items() if e["state"] == state)
        reached = _reach(branch)
        assert "workflow_end" in reached and PUSH not in reached and _task("ViewData") not in reached
        assert not any(WF["tasks"].get(k, {}).get("summary", "").startswith("changed = true") for k in reached)
