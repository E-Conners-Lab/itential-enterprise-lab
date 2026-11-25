"""ADR 0086: itential/agents/cloud_bundle.py builds Agent Project import bundles for another Platform from the agent
documents. These tests hold the converter to the documents, the lab's own tool resolution (the play) and the bundle
shape the Platform's importer accepts. They run in CI with no Platform access."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("cloud_bundle", ROOT / "itential" / "agents" / "cloud_bundle.py")
cb = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cb)

MODELS = yaml.safe_load((ROOT / "itential" / "versions.yaml").read_text())["integrations"]["models"]
DOCS = cb.load_documents()
UUID = "11111111-2222-3333-4444-555555555555"


def _all_workflows() -> dict[str, str]:
    """Every workflow name any document uses, mapped to a stand-in uuid."""
    return {t["reference"]: UUID for d in DOCS for t in d.get("tools") or [] if t["kind"] == "workflow"}


def _build(**kw):
    return cb.build_bundles(DOCS, MODELS, kw.pop("workflows", _all_workflows()), **kw)


def test_local_twins_are_skipped_unless_asked():
    bundles, _ = _build()
    names = {a["name"] for b in bundles.values() for a in b["agents"]}
    assert names and not any(n.endswith("-local") for n in names)
    with_local, _ = _build(include_local=True)
    assert any(a["name"].endswith("-local") for b in with_local.values() for a in b["agents"])


def test_every_document_converts():
    """A new document with a tool kind the converter does not know fails here, not on the day of an import."""
    bundles, report = _build(include_local=True)
    assert sum(len(b["agents"]) for b in bundles.values()) == len(DOCS)
    assert len(report) == len(DOCS)


def test_integration_reference_matches_the_plays_format():
    netbox = MODELS["netbox"]
    ref, name = cb.tool_reference(
        {"kind": "integration", "model": "netbox", "reference": "dcim_devices_list"}, MODELS, {}, None
    )
    assert ref == f"integration:{netbox['title']}%3A{netbox['version']}:{netbox['instance']}:dcim_devices_list"
    assert name == "dcim_devices_list"


def test_gateway_and_application_tools():
    assert cb.tool_reference({"kind": "gateway-service", "reference": "send-command"}, MODELS, {}, None)[0] == (
        "application:GatewayManager:sendCommand"
    )
    assert cb.tool_reference(
        {"kind": "application", "app": "ConfigurationManager", "reference": "getComplianceReportDetail"}, MODELS, {}, None
    )[0] == "application:ConfigurationManager:getComplianceReportDetail"


def test_workflows_resolve_by_name_and_a_missing_one_is_dropped_and_reported():
    wf = {"Get AWS VPN Status": UUID}
    ref, name = cb.tool_reference({"kind": "workflow", "reference": "Get AWS VPN Status"}, MODELS, wf, None)
    assert ref == f"workflow:{UUID}" and name == "Get AWS VPN Status"
    assert cb.tool_reference({"kind": "workflow", "reference": "Nope"}, MODELS, wf, None)[0] is None
    _, report = _build(workflows={})
    assert any(dropped for _, _, dropped in report), "with no workflow map every workflow tool must be reported dropped"


def test_mcp_tools_are_dropped_without_a_cluster_and_kept_with_one():
    tool = {"kind": "mcp-tool", "server": "netops-knowledge", "reference": "search_scenarios"}
    assert cb.tool_reference(tool, MODELS, {}, None)[0] is None
    assert cb.tool_reference(tool, MODELS, {}, "homelab")[0] == (
        "gatewayService:homelab:python-script:netops-knowledge_search_scenarios"
    )


def test_bundle_shape_is_what_the_importer_accepts():
    """Measured on Platform 6.7.0: a bundle without these fields is 'Not a valid agent project file'."""
    bundles, _ = _build(rename={"lab-netops": "lab-netops-cloud"})
    assert "lab-netops-cloud" in bundles and "lab-netops" not in bundles
    for project, b in bundles.items():
        assert b["name"] == project and b["agentProjectBundleVersion"] == 1
        assert {"_id", "description", "created", "createdBy", "agents"} <= set(b)
        for a in b["agents"]:
            assert {"_id", "name", "description", "instructions", "inputSchema", "created", "createdBy", "provider", "tools"} <= set(a)
            assert a["provider"] == {"profileName": cb.DEFAULT_PROFILE, "modelName": cb.DEFAULT_MODEL}
            assert all(t["referenceId"] and t["lastKnownName"] for t in a["tools"])


def test_build_is_deterministic():
    a, _ = _build()
    b, _ = _build()
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_no_credential_shaped_values_in_a_bundle():
    """A bundle carries names and prompts only. Prose may say 'password'; a key block or a token value must never appear."""
    bundles, _ = _build(include_local=True)
    text = json.dumps(bundles).lower()
    for needle in ("begin private key", "begin rsa private key", "authorization: token", "bearer ey", "token="):
        assert needle not in text, needle


def test_rename_argument_is_validated():
    assert cb.parse_rename(["a=b"]) == {"a": "b"}
    with pytest.raises(SystemExit):
        cb.parse_rename(["nope"])


def test_the_workflows_map_file_is_not_committable():
    """uuids belong to one instance: the default output folder and any cloud-workflows map stay out of git."""
    ignore = (ROOT / ".gitignore").read_text().splitlines()
    assert "build/" in ignore


def test_agent_filter_builds_only_the_named_agents_and_refuses_an_unknown_one():
    bundles, report = _build(only={"netbox-sot"})
    assert [a["name"] for b in bundles.values() for a in b["agents"]] == ["netbox-sot"]
    assert [(p, n) for p, n, _ in report] == [("lab-netops", "netbox-sot")]
    # a name that is not a document (or is a local twin without --include-local) builds nothing, and the CLI says so
    empty, _ = _build(only={"no-such-agent"})
    assert not empty
    with pytest.raises(SystemExit):
        cb.main(["--agent", "no-such-agent", "--out", str(ROOT / "build" / "test-unknown-agent")])
