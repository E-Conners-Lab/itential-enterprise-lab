"""Tear Down AWS VPN (PID S13 criterion 5, R2): the router's block removed under a revert timer after the first
approval and proved gone, and only then - for a target deployed in AWS - the destroy planned, approved and applied as
approved. Nothing that fails or is rejected on the router side may reach AWS."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import ipaddress

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("wf_build_teardown", ROOT / "itential" / "workflows" / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)
TD = json.loads((ROOT / "itential" / "workflows" / "tear-down-aws-vpn.json").read_text())
TASKS = TD["tasks"]
TR = TD["transitions"]
VERSIONS = build.VERSIONS


def _reach(start: str) -> set[str]:
    seen, todo = set(), [start]
    while todo:
        node = todo.pop()
        if node not in seen:
            seen.add(node)
            todo.extend(TR.get(node, {}))
    return seen


def _run(code: str, data: dict) -> dict:
    run = subprocess.run([sys.executable, "-I", "-c", code], input=json.dumps(data), capture_output=True, text=True,
                         timeout=30, env={}, check=True)
    return json.loads(run.stdout)


def _plan(d: dict) -> dict:
    return build.teardown_plan(d, build.TEARDOWN_TARGETS, build.REVERT_TARGETS, build.TEARDOWN_REVERT_MINUTES,
                               build.LAB_EDGE_TIMEOUT)


def _removal(target: str, rc: int = 0, lines: str = "no interface Tunnel10\n") -> dict:
    return {"result": {"return_code": rc, "stdout_json": {"action": "removal", "target": target, "lines": lines,
                                                          "sha256": "ab" * 32}}}


# ── the plan (pure; the same source runs on the Gateway) ──


@pytest.mark.parametrize("target, aws", [("dc1-wan01", True), ("clab-rtr1", False)])
def test_the_first_call_gives_the_lab_edge_params_and_whether_aws_follows(target: str, aws: bool) -> None:
    p = _plan({"target": target})
    entry = VERSIONS["aws_vpn"]["targets"][target]
    assert p["ok"] and p["aws"] is aws
    assert p["removal"] == {"action": "removal", "target_json": json.dumps(entry["target"]), "timeout": "120"}
    assert p["absent"] == {"action": "absent", "target_json": json.dumps(entry["target"]), "username": entry["username"],
                           "timeout": build.LAB_EDGE_TIMEOUT}
    assert "check" not in p and "card" not in p  # nothing to push before the lines exist


def test_the_second_call_pushes_the_removal_lines_with_the_routers_own_checks() -> None:
    p = _plan({"target": "dc1-wan01", "removal": _removal("dc1-wan01", lines="no interface Tunnel10\nno crypto ikev2 keyring AWS\n")})
    assert p["ok"] and p["params"]["action"] == "push" and p["check"]["action"] == "check"
    assert json.loads(p["params"]["lines_json"]) == ["no interface Tunnel10", "no crypto ikev2 keyring AWS"]
    assert json.loads(p["params"]["checks_json"]) == VERSIONS["revert_push"]["targets"]["dc1-wan01"]["teardown_checks"]
    assert p["params"]["revert_minutes"] == str(build.TEARDOWN_REVERT_MINUTES)
    assert p["params"]["username"] == "itential-aws-vpn"  # the revert target's login, never the key's service
    assert "second card" in p["card"]["after this"] and p["card"]["router"] == "dc1-wan01"
    twin = _plan({"target": "clab-rtr1", "removal": _removal("clab-rtr1")})
    assert "nothing of this target is deployed in AWS" in twin["card"]["after this"]


@pytest.mark.parametrize("d, says", [
    ({"target": "br1-wan01"}, "is not a router Tear Down may change"),
    ({"target": "dc1-wan01", "removal": _removal("dc1-wan01", rc=1)}, "gave no lines"),
    ({"target": "dc1-wan01", "removal": _removal("clab-rtr1")}, "gave no lines"),  # an answer for another router
    ({"target": "dc1-wan01", "removal": _removal("dc1-wan01", lines="")}, "gave no lines"),
], ids=["unknown-router", "removal-failed", "other-router", "no-lines"])
def test_the_plan_refuses_what_it_cannot_tear_down(d: dict, says: str) -> None:
    p = _plan(d)
    assert p["ok"] is False and says in p["message"]


def test_the_plan_runs_as_the_gateway_runs_it() -> None:
    for d in ({"target": "dc1-wan01"}, {"target": "dc1-wan01", "removal": _removal("dc1-wan01")}, {"target": "nope"}):
        assert _run(build.TEARDOWN_PLAN_CODE, d) == _plan(d)
    assert TASKS["1b"]["variables"]["incoming"]["code"] == TASKS["18"]["variables"]["incoming"]["code"] == build.TEARDOWN_PLAN_CODE


def test_each_routers_teardown_checks_are_the_ones_its_changes_passed() -> None:
    changes = yaml.safe_load((ROOT / "topology" / "changes" / "dc1-wan01-lab-edge.yaml").read_text())
    window = next(s for s in changes["steps"] if s["name"].startswith("management"))["checks"]
    targets = VERSIONS["revert_push"]["targets"]
    assert targets["dc1-wan01"]["teardown_checks"] == window
    allowed = build.REVERT_CHECKS["properties"]
    for name, entry in targets.items():
        checks = entry["teardown_checks"]
        # the shape config-push-revert takes (build.REVERT_CHECKS), checked without a schema library
        assert set(checks) <= set(allowed) and checks["bgp_established"] and checks["pings"], name
        assert all(ipaddress.ip_address(n) for n in checks["bgp_established"])
        for ping in checks["pings"]:
            assert set(ping) <= set(allowed["pings"]["items"]["properties"]) and ipaddress.ip_address(ping["target"])
            assert 1 <= ping.get("min_percent", 80) <= 100
        assert 0 <= checks["settle_seconds"] <= allowed["settle_seconds"]["maximum"]


# ── the order, and nothing on the router side reaching AWS ──


def test_the_router_is_removed_and_proved_before_aws_is_touched() -> None:
    path = ["1e", "17", "19", "13", "14", "2c", "3b", "33", "4b", "4d", "4e", "5b", "5f", "6e"]
    for a, b in zip(path, path[1:]):
        assert b in _reach(a), (a, b)
    assert TR["2c"]["a0"]["state"] == "failure" and TR["5f"]["a2"]["state"] == "failure"  # Reject on each card


@pytest.mark.parametrize("start", ["a0", "b0", "b1", "b2", "b3", "b4", "c0", "c2", "c3", "c4", "4f"])
def test_no_router_failure_or_rejection_reaches_the_aws_destroy(start: str) -> None:
    assert not _reach(start) & {"5a", "5b", "5f", "6e"}, start


def test_only_a_proved_removal_reaches_aws() -> None:
    # the only way into the AWS side is 4e's success, and 4e is reached only from 4d's success (nothing left)
    into_aws = {src for src, dsts in TR.items() if "5a" in dsts}
    assert into_aws == {"4e"} and TR["4e"]["5a"]["state"] == "success"
    assert {src for src, dsts in TR.items() if "4e" in dsts} == {"4d"} and TR["4d"]["4e"]["state"] == "success"
    assert TASKS["4d"]["variables"]["incoming"]["evaluation_groups"][0]["evaluations"][0]["query"] == "result.stdout_json.absent"


def test_the_destroy_plan_has_no_variables_and_the_apply_is_the_approved_plan() -> None:
    assert json.loads(build.DESTROY_PLAN_PARAMS) == {"action": "plan-destroy", "job": "new", "timeout": "900"}
    assert TASKS["5a"]["variables"]["incoming"]["text"] == build.DESTROY_PLAN_PARAMS
    assert TASKS["6a"]["variables"]["incoming"]["obj"] == "$var.5b.result"  # the SHA-256 of the plan the card showed
    assert TASKS["6b"]["variables"]["incoming"]["str"] == build.APPLY_TPL or build.APPLY_TPL in json.dumps(TASKS["6b"])
    assert TASKS["5f"]["variables"]["incoming"]["body"] == "$var.job.destroy_plan"
    assert TASKS["a5"]["variables"]["incoming"]["serviceName"] == "terraform-run"  # a rejected plan is discarded


@pytest.mark.parametrize("answer, empty, read", [
    ({"result": {"return_code": 0, "stdout_json": {"outputs": {}}}}, True, True),
    ({"result": {"return_code": 0, "stdout_json": {"outputs": {"strongswan_eip": "x"}}}}, False, True),
    ({"result": {"return_code": 1, "stdout_json": {"error": "no state"}}}, False, False),
    ({}, False, False),
], ids=["nothing-left", "outputs-left", "read-failed", "no-answer"])
def test_destroy_left_is_empty_only_when_the_state_has_no_outputs(answer: dict, empty: bool, read: bool) -> None:
    got = build.destroy_left({"outputs": answer})
    assert got == {"empty": empty, "read": read} and _run(build.DESTROY_LEFT_CODE, {"outputs": answer}) == got


def test_tear_down_is_gated_to_the_routers_hand_off_may_push() -> None:
    gate = build.INPUT_GATES[build.WF["tear_down_aws_vpn"]]["target"]["enum"]
    assert gate == sorted(build.TEARDOWN_TARGETS) == build.INPUT_GATES[build.WF["hand_off_aws_vpn"]]["target"]["enum"]
    assert TD["inputSchema"]["properties"]["target"]["enum"] == gate if "inputSchema" in TD else True
