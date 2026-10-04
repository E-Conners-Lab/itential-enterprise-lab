"""Get AWS VPN Status (A1, Cloud Status; owner decisions 2026-10-04): a read-only workflow, the Cloud Status agent's
first tool. From terraform-run outputs alone it says whether anything is deployed, where, until when, whether the NAT
gateway is on, when AWS last changed (the state's own metadata) and an ESTIMATED cost from unit prices pinned in
itential/versions.yaml - never real spend, and always said to be an estimate."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("wf_build_status", ROOT / "itential" / "workflows" / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)
VERSIONS = build.VERSIONS
PRICES = VERSIONS["aws_vpn"]["prices"]
NOW = datetime(2026, 10, 4, 14, 26, 1, tzinfo=timezone.utc)
NAME = "Get AWS VPN Status"

DEPLOYED = {"strongswan_eip": "198.51.100.20", "strongswan_instance_id": "i-0abc", "strongswan_private_ip": "10.0.1.10",
            "expires_at": "none", "nat_gateway_enabled": False, "strongswan_instance_type": "t3.micro",
            "vpc_private_prefixes": ["10.0.64.0/20", "10.0.80.0/20"]}
STATE = {"read": True, "last_modified": "2026-10-04T06:26:01Z", "version_id": "v1"}


def _answer(outputs, state=STATE, rc: int = 0) -> dict:
    return {"outputs": {"result": {"return_code": rc, "stdout_json": {"action": "outputs", "outputs": outputs,
                                                                     "state": state}}}}


def _status(d: dict) -> dict:
    return build.aws_vpn_status(d, NOW, PRICES)


# ── what it reports (pure; the same source runs on the Gateway) ──


def test_a_deployment_is_reported_with_its_end_time_nat_and_last_change() -> None:
    s = _status(_answer(DEPLOYED))
    assert s["ok"] and s["deployed"] and s["strongswan_eip"] == "198.51.100.20" and s["instance_id"] == "i-0abc"
    assert s["ends"] == "stays up until torn down" and s["nat_gateway"] == "off"
    assert s["last_changed"] == "2026-10-04T06:26:01Z" and s["hours_since_change"] == 8.0
    assert "198.51.100.20" in s["summary"] and "estimate" in s["summary"].lower()


def test_the_estimate_is_the_pinned_prices_times_the_hours() -> None:
    s = _status(_answer(DEPLOYED))
    hourly = (PRICES["instance_hour"]["t3.micro"] + PRICES["public_ipv4_hour"]
              + PRICES["secret_month"] / 730 + PRICES["root_volume_month"] / 730)
    assert s["estimate"]["per_day_usd"] == round(hourly * 24, 2)
    assert s["estimate"]["since_change_usd"] == round(hourly * 8, 2)
    assert s["estimate"]["basis"] == PRICES["basis"]


def test_the_nat_gateway_is_in_the_estimate_only_when_it_is_on() -> None:
    off = _status(_answer(DEPLOYED))["estimate"]["per_day_usd"]
    on = _status(_answer({**DEPLOYED, "nat_gateway_enabled": True}))
    assert on["nat_gateway"] == "on" and on["estimate"]["per_day_usd"] == round(off + PRICES["nat_gateway_hour"] * 24, 2)


def test_an_end_time_is_reported_as_one() -> None:
    s = _status(_answer({**DEPLOYED, "expires_at": "2026-10-04T20:00:00Z"}))
    assert s["ends"] == "ends at 2026-10-04T20:00:00Z (Tear Down Expired AWS VPN removes it then)"


def test_a_deployment_from_before_a1_says_what_it_does_not_know() -> None:
    """The NAT gateway and the instance type become outputs on the next apply (cloud-devops-pipeline #31)."""
    old = {k: v for k, v in DEPLOYED.items() if k not in ("nat_gateway_enabled", "strongswan_instance_type")}
    s = _status(_answer(old))
    assert s["nat_gateway"] == "unknown (recorded by the next Deploy)"
    assert s["instance_type"] == f"{PRICES['assumed_instance_type']} (assumed: recorded by the next Deploy)"
    assert "assumed" in s["summary"]


def test_an_unread_state_time_leaves_out_only_the_cost_since_the_change() -> None:
    s = _status(_answer(DEPLOYED, state={"read": False, "error": "AccessDenied"}))
    assert s["ok"] and s["last_changed"] is None and s["estimate"]["since_change_usd"] is None
    assert s["estimate"]["per_day_usd"] > 0 and "when Terraform last applied it is not known" in s["summary"]


def test_the_state_time_is_called_the_last_terraform_apply_not_a_change() -> None:
    """A no-change apply moves it too (2026-10-04: the agent read it as "up since"): the words say what it is."""
    s = _status(_answer(DEPLOYED))["summary"]
    assert "last Terraform apply 2026-10-04T06:26:01Z" in s and "last changed" not in s


def test_nothing_deployed_costs_nothing_but_says_when_it_was_torn_down() -> None:
    s = _status(_answer({}))
    assert s["ok"] and s["deployed"] is False and s["estimate"]["per_day_usd"] == 0
    assert "Nothing is deployed" in s["summary"] and "2026-10-04T06:26:01Z" in s["summary"]


@pytest.mark.parametrize("d", [_answer({}, rc=1), {"outputs": {}}, {}, _answer(None)])
def test_an_answer_that_cannot_be_read_is_said_so(d: dict) -> None:
    s = _status(d)
    assert s["ok"] is False and "could not" in s["summary"]


def test_an_unknown_instance_type_is_not_priced_as_something_else() -> None:
    s = _status(_answer({**DEPLOYED, "strongswan_instance_type": "m7i.48xlarge"}))
    assert s["estimate"]["per_day_usd"] is None and "no pinned price" in s["summary"]


def test_the_status_runs_as_the_gateway_runs_it() -> None:
    run = subprocess.run([sys.executable, "-I", "-c", build.STATUS_CODE], input=json.dumps(_answer(DEPLOYED)),
                         capture_output=True, text=True, timeout=30, env={}, check=True)
    out = json.loads(run.stdout)
    assert out["ok"] and out["deployed"] and out["estimate"]["per_day_usd"] > 0


def test_the_prices_say_what_they_are() -> None:
    assert "estimate" in PRICES["basis"].lower() and "us-east-1" in PRICES["basis"]
    assert PRICES["assumed_instance_type"] in PRICES["instance_hour"]


# ── the workflow: read-only, one service call, every path ends ──

WF = build.get_aws_vpn_status()
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
    assert WF["name"] == NAME == VERSIONS["workflows"]["get_aws_vpn_status"]
    assert build.get_aws_vpn_status in build.BUILDERS
    assert not (WF.get("inputSchema") or {}).get("properties")


def test_its_only_service_call_is_a_state_read() -> None:
    services = [t for t in TASKS.values() if t.get("name") == "runService"]
    assert len(services) == 1 and services[0]["variables"]["incoming"]["serviceName"] == "terraform-run"
    parses = [t for t in TASKS.values() if t.get("name") == "parse"]
    assert [json.loads(t["variables"]["incoming"]["text"])["action"] for t in parses] == ["outputs"]
    assert not [t for t in TASKS.values() if t.get("type") == "manual"]  # no card: nothing to approve


def test_every_task_reaches_the_end_and_publishes_a_summary() -> None:
    for tid in TASKS:
        assert "workflow_end" in _reach(tid) or tid == "workflow_end", tid
    assert {"summary", "status", "error"} <= set(WF["outputSchema"]["properties"])
