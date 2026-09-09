"""PID S4d element 5 (ADR 0046): the agent fleet (netbox-sot, device-ops, compliance, diagnostics, remediation) with a
local twin each under itential/agents/, tiered by tool kind. These tests hold the documents, the play, the verify script
and the PID to each other. They run in CI with no lab access."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
AGENTS = ROOT / "itential" / "agents"
VERSIONS = ROOT / "itential" / "versions.yaml"
AGENT_TASK = ROOT / "ansible" / "playbooks" / "tasks" / "flowai-agent.yml"
VERIFY = ROOT / "verify" / "test-06-flowai.sh"
PID = ROOT / "docs" / "PID.md"
ADR = ROOT / "docs" / "adr" / "0046-agent-fleet-tiered-autonomy.md"

FLEET = ("netbox-sot", "device-ops", "compliance", "diagnostics", "remediation")
DEVICE_WRITE_TOOLS = {
    "send-config",
    "wf-config-push-v1",
    "wf-branch-vlan-v1",
    "wf-branch-vlan-delete-v1",
}


@pytest.fixture(scope="module")
def docs() -> dict:
    return {p.stem: yaml.safe_load(p.read_text()) for p in AGENTS.glob("*.yaml")}


@pytest.fixture(scope="module")
def versions() -> dict:
    return yaml.safe_load(VERSIONS.read_text())


def tool_names(doc: dict) -> set[str]:
    return {t["reference"] for t in doc["tools"]}


def test_every_fleet_agent_exists_on_claude_with_a_local_twin(
    docs: dict, versions: dict
) -> None:
    profiles = {p["name"] for p in versions["llm"]["profiles"]}
    for name in FLEET:
        assert name in docs and f"{name}-local" in docs, (
            f"{name} needs a Claude document and a local twin"
        )
        assert (
            docs[name]["profile"] == "anthropic"
            and docs[f"{name}-local"]["profile"] == "ollama-lab"
        )
        assert docs[name]["project"] == docs[f"{name}-local"]["project"] == "lab-netops"
        assert {docs[name]["profile"], docs[f"{name}-local"]["profile"]} <= profiles
        assert 1 <= len(docs[f"{name}-local"]["tools"]) <= 3, (
            f"{name}-local: a 7B model gets one to three tools"
        )
        assert len(docs[f"{name}-local"]["tools"]) <= len(docs[name]["tools"])


def test_tiered_autonomy_by_tool_kind(docs: dict) -> None:
    # read-only tiers hold no device-writing tool at all; remediation's only write is the governed push
    for name in ("netbox-sot", "device-ops", "compliance", "diagnostics"):
        for variant in (name, f"{name}-local"):
            assert not (tool_names(docs[variant]) & DEVICE_WRITE_TOOLS), (
                f"{variant} must not write to devices"
            )
    assert tool_names(docs["netbox-sot"]) == {
        "dcim_devices_list",
        "dcim_devices_retrieve",
        "dcim_interfaces_list",
        "ipam_ip_addresses_list",
        "ipam_vlans_list",
        "dcim_sites_list",
        # S4e (ADR 0048): the enriched objects
        "dcim_racks_list",
        "circuits_circuits_list",
        "ipam_asns_list",
        "ipam_vrfs_list",
        "ipam_prefixes_list",
    }
    assert all(
        t["kind"] == "integration" and t["model"] == "netbox"
        for t in docs["netbox-sot"]["tools"]
    )
    assert {
        "wf-show-command-v1",
        "wf-show-all-v1",
        "send-command",
        "dcim_devices_list",
    } == tool_names(docs["device-ops"])
    for name in ("device-ops-local", "lab-netops-local"):
        assert "wf-show-all-v1" in tool_names(
            docs[name]
        ) and "send-command" not in tool_names(docs[name]), (
            f"{name}: the fleet-wide read is a workflow; a raw gateway tool makes a small model invent node names (measured)"
        )
    assert "wf-compliance-report-v1" in tool_names(
        docs["compliance"]
    ) and "wf-compliance-report-v1" in tool_names(docs["remediation"])
    assert not (
        {"searchCompliancePlanInstances", "getJSONComplianceReportsByBatch"}
        & tool_names(docs["compliance"])
    ), (
        "the raw report tools cost an agent 638k tokens (measured): the summarising workflow replaces them"
    )
    assert all(
        t.get("app") == "ConfigurationManager"
        for t in docs["compliance"]["tools"]
        if t["kind"] == "application"
    )
    assert "updateIncident" in tool_names(
        docs["diagnostics"]
    ) and "listIncidents" in tool_names(docs["diagnostics"])
    assert "wf-show-command-v1" in tool_names(docs["diagnostics"]), (
        "diagnostics reads the device before it proposes"
    )
    for variant in ("remediation", "remediation-local"):
        writes = tool_names(docs[variant]) & DEVICE_WRITE_TOOLS
        assert writes == {"wf-config-push-v1"}, (
            f"{variant}: the governed push is the only write tool"
        )
        assert "updateIncident" not in tool_names(docs[variant])


def test_instructions_state_the_guardrails(docs: dict) -> None:
    assert (
        "never remediate" in docs["compliance"]["instructions"].lower()
        or "never push" in docs["compliance"]["instructions"].lower()
    )
    d = docs["diagnostics"]["instructions"]
    assert (
        "Proposed fix:" in d
        and "one work note" in d.lower()
        and "Never push configuration" in d
    )
    for cause in (
        "hostname",
        "ntp server vrf MGMT 10.100.0.1",
        "show vlan",
        "BGP",
        "no shutdown",
    ):
        assert cause in d, f"diagnostics lacks the known root cause {cause!r}"
    r = docs["remediation"]["instructions"]
    assert (
        "Work Center" in r
        and "wf-config-push-v1" in r
        and "single configuration line" in r
    )
    assert "never bypass" in r.lower()
    for name in FLEET:
        assert "{{request}}" in docs[name]["instructions"] and docs[name][
            "input_schema"
        ]["required"] == ["request"]


def test_show_all_workflow_fans_out_deterministically(versions: dict) -> None:
    import json

    assert versions["workflows"]["show_all"] == "wf-show-all-v1"
    wf = json.loads(
        (ROOT / "itential" / "workflows" / "wf-show-all-v1.json").read_text()
    )
    tasks = wf["tasks"]
    names = [t.get("name") for t in tasks.values()]
    assert (
        "getDevicesFiltered" in names and "join" in names and "setObjectKey" in names
    ), "device list -> selector -> merged parser input"
    send = [t for t in tasks.values() if t.get("name") == "sendCommand"]
    assert (
        len(send) == 1
        and send[0]["variables"]["incoming"]["inventory"].startswith("$var.")
        and send[0]["variables"]["incoming"]["commands"].startswith("$var.")
    )
    code = [t for t in tasks.values() if t.get("name") == "runCode"]
    assert len(code) == 1 and set(code[0]["variables"]["incoming"]["packages"]) >= {
        "genie==26.8",
        "ntc-templates==9.2.0",
    }
    assert "LIMIT = 1500" in code[0]["variables"]["incoming"]["code"], (
        "per-device output capped for a small model's context"
    )
    assert set(wf["outputSchema"]["properties"]) >= {
        "devices",
        "devices_checked",
        "results",
        "parser_errors",
    }
    assert wf["inputSchema"]["required"] == ["command"]
    override = (ROOT / "itential" / "compose.override.yml").read_text()
    assert 'OLLAMA_CONTEXT_LENGTH: "16384"' in override, (
        "the local model must hold a twelve-device result"
    )


def test_play_resolves_application_tools() -> None:
    text = AGENT_TASK.read_text()
    assert "item.kind == 'application'" in text and "'application:' ~ item.app" in text


def test_verify_pid_and_adr_cover_s4d5() -> None:
    text = VERIFY.read_text()
    for name in FLEET:
        assert re.search(rf'check "S4d\.5[a-z]? {re.escape(name)}', text), (
            f"verify 06 lacks the {name} check"
        )
    assert "netbox-sot-local" in text, "one local twin runs in the verify"
    assert re.search(r'check "S4d\.5g ', text) and "wf-show-all-v1" in text, (
        "the fleet-wide read on the local generalist"
    )
    for s in (
        "wf-config-push-v1",
        "devcmd.py",
        "service-now.com",
        "comments_and_work_notes",
        "close_code",
    ):
        assert s in text, f"verify 06 S4d.5 lacks {s}"
    assert "| 1.11 |" in PID.read_text() and ADR.exists()
