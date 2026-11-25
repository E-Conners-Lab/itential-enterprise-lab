"""ADR 0087: Get Change Evidence reads and only reads, and every way it can fail ends the job with a reason; the
change-risk-reviewer agent holds no tool that writes. These hold the two documents to those promises. They run in CI with
no Platform access."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = yaml.safe_load((ROOT / "itential" / "versions.yaml").read_text())
NAME = VERSIONS["workflows"]["get_change_evidence"]
WORKFLOW = json.loads((ROOT / "itential" / "workflows" / "get-change-evidence.json").read_text())
AGENT = yaml.safe_load((ROOT / "itential" / "agents" / "change-risk-reviewer.yaml").read_text())

# every ServiceNow and NetBox operation the lab's models declare as a write, and the names of the lab's write workflows
SERVICENOW_WRITES = {"createIncident", "updateIncident", "updateChangeRequest", "createStandardChange",
                     "updateStandardChange"}
NETBOX_WRITES = set(VERSIONS["integrations"]["models"]["netbox"]["write_operations"])
WRITE_WORKFLOWS = {VERSIONS["workflows"][k] for k in ("config_push", "config_push_revert", "branch_vlan",
                                                      "deploy_aws_vpn", "hand_off_aws_vpn", "tear_down_aws_vpn")}


def _real_tasks() -> dict:
    return {k: v for k, v in WORKFLOW["tasks"].items() if k not in ("workflow_start", "workflow_end")}


def test_the_workflow_is_registered_and_named_for_what_it_does():
    assert NAME == "Get Change Evidence" and WORKFLOW["name"] == NAME
    assert (ROOT / "docs" / "workflows" / "get-change-evidence.md").exists()


def test_the_workflow_only_reads():
    names = {t["name"] for t in _real_tasks().values()}
    assert not names & (SERVICENOW_WRITES | NETBOX_WRITES), names & (SERVICENOW_WRITES | NETBOX_WRITES)
    # the only reads of ServiceNow it makes
    assert {n for n in names if n.startswith(("list", "get"))} >= {"listChangeRequests", "listIncidents"}
    for t in _real_tasks().values():
        assert t["name"] not in ("runService", "sendConfig", "itential_cli", "restCall"), t["name"]


def test_every_task_that_can_throw_or_fail_has_an_edge_that_ends_the_job():
    """ADR 0066: a read that throws, or one that returns something that is not data, must not dead-end the job."""
    tr = WORKFLOW["transitions"]
    needs_error = {"listChangeRequests", "listIncidents", "childJob"}
    for tid, t in _real_tasks().items():
        if t["name"] in needs_error:
            assert any(e["state"] == "error" for e in tr[tid].values()), f"{tid} {t['name']} has no error edge"
        if t["name"] in ("evaluation", "query"):
            assert any(e["state"] == "failure" for e in tr[tid].values()) or t["variables"]["incoming"].get(
                "pass_on_null"), f"{tid} {t['name']} has no failure edge"

    def reaches_end(tid: str, seen: frozenset = frozenset()) -> bool:
        if tid == "workflow_end":
            return True
        return tid not in seen and bool(tr.get(tid)) and all(reaches_end(n, seen | {tid}) for n in tr[tid])

    assert reaches_end("workflow_start"), "some path never reaches workflow_end"


def test_the_failure_notes_publish_the_reason_in_error():
    notes = [t for t in _real_tasks().values() if t["name"] == "makeData"]
    assert len(notes) >= 3
    for n in notes:
        assert n["variables"]["outgoing"]["output"] == "$var.job.error"


def test_a_lookup_is_only_ever_a_change_number():
    gate = next(t for t in _real_tasks().values() if t["name"] == "validateJsonSchema")
    change = gate["variables"]["incoming"]["schema"]["properties"]["change"]
    assert re.fullmatch(change["pattern"], "CHG0030001")
    for bad in ("", "CHG1", "chg0030001", "CHG0030001^active=true", "INC0010003", "CHG0030001 OR 1=1"):
        assert not re.fullmatch(change["pattern"], bad), bad


def test_no_raw_integration_response_is_published():
    """An integration response carries ServiceNow's headers (a session cookie among them): each stays on its task."""
    for tid, t in _real_tasks().items():
        if t["location"] == "Adapter":
            assert not t["variables"]["outgoing"]["response"], f"{tid} publishes its response"
    published = {v.rpartition("$var.job.")[2] for t in _real_tasks().values()
                 for v in t["variables"]["outgoing"].values() if isinstance(v, str) and v.startswith("$var.job.")}
    assert published <= {"evidence", "error", "input_check", "devices_job"}, published


def test_the_agent_holds_no_tool_that_writes():
    for tool in AGENT["tools"]:
        assert tool["kind"] == "workflow", tool
        assert tool["reference"] not in WRITE_WORKFLOWS, tool
        assert tool["reference"] in VERSIONS["workflows"].values(), tool
    assert {t["reference"] for t in AGENT["tools"]} == {NAME, VERSIONS["workflows"]["show_version"],
                                                       VERSIONS["workflows"]["show_command"]}


def test_the_agent_is_advisory_and_treats_text_as_data():
    text = AGENT["instructions"]
    assert "{{request}}" in text
    assert re.search(r"never approve", text, re.I) and re.search(r"data, never\s+instructions", text, re.I)
    assert "no tool that writes" in text
    # the rules are written, so a rating can be checked
    for level in ("HIGH", "MEDIUM", "LOW"):
        assert f"{level} when" in text or f"{level} otherwise" in text, level


def test_it_has_no_local_twin_on_purpose():
    assert not (ROOT / "itential" / "agents" / "change-risk-reviewer-local.yaml").exists()
    assert "No local twin on purpose" in (ROOT / "itential" / "agents" / "change-risk-reviewer.yaml").read_text()
