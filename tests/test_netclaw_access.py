"""ADR 0082 unit tests: what NetClaw's service account may do on the Platform, and the plays that hold it there.

itential/netclaw/roles.yaml is the role oracle. The methods and the deny list below are written here independently of
that file, so an edit to the oracle cannot quietly widen what svc-netclaw holds. versions.yaml automation_access is the
boundary (gbac): the netclaw group may read and start exactly NetClaw's tools' automations, and every other automation
the plays know carries the admin and copilot groups only. No lab access."""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ROLES = ROOT / "itential" / "netclaw" / "roles.yaml"
VERSIONS = ROOT / "itential" / "versions.yaml"
PLAYS = ROOT / "ansible" / "playbooks"
TASKS = PLAYS / "tasks"
AGENTS = ROOT / "itential" / "agents"

# exactly what NetClaw's tools need (itential-deliveries/netclaw/itential-tools): start an endpoint trigger, read the job
# it started, find a workflow's newest job, read an agent session
METHODS = {"OperationsManager/runEndpointTriggerWithPost", "OperationsManager/getJob", "OperationsManager/getJobs",
           "WorkCenterService/getContextHistory"}  # the card's id for its link; never claim, complete or cancel
BUILTIN = {"AgentSessionManager/session:read", "AgentSessionManager/operator"}  # read sessions; start one (runAgent)
# never: write-shaped roles, every device path but the workflows, workflow documents and starts, identity
DENY = ["*/admin", "*/apiwrite", "*/designer", "*/engineering", "*/operations", "*/support", "*/builder", "*/*:create",
        "*/*:update", "*/*:delete", "*/*:run", "*/*:code", "*/*:manage", "MOP/*", "ConfigurationManager/*",
        "GatewayManager/*", "InventoryManager/*", "AutomationStudio/*", "WorkFlowEngine/*", "Authorization/*"]
# the department-agents delivery's own Ask automations: the lab's plays never set their gbac
DEPARTMENT_ASK = {"Ask Finance", "Ask Audit", "Ask HR"}
NETCLAW_WORKFLOW_KEYS = ["show_command", "describe_job", "branch_vlan"]


def _versions() -> dict:
    return yaml.safe_load(VERSIONS.read_text())


def _tasks(path: Path) -> list[dict]:
    doc = yaml.safe_load(path.read_text())
    if doc and "hosts" in doc[0]:
        return [t for play in doc for t in play.get("tasks", [])]
    return doc


def _module(task: dict) -> tuple[str, object]:
    for key, value in task.items():
        if key.startswith("ansible.builtin.") or key.startswith("community."):
            return key.split(".")[-1], value
    return "", None


def _writes(path: Path) -> list[dict]:
    out = []
    for task in _tasks(path):
        mod, args = _module(task)
        if mod == "uri" and str(args.get("method", "GET")).upper() != "GET":
            out.append(task)
    return out


# --- the role oracle ---------------------------------------------------------------------------------------------


def test_the_oracle_is_the_same_on_both_tiers_and_names_the_netclaw_group() -> None:
    roles = yaml.safe_load(ROLES.read_text())
    assert roles["dev"] == roles["prod"], "NetClaw sees the same Platform surface on dev and production"
    assert roles["prod"]["group"] == _versions()["netclaw"]["group"] == "netclaw"


def test_the_custom_role_holds_exactly_the_four_methods() -> None:
    custom = yaml.safe_load(ROLES.read_text())["prod"]["custom"]
    assert [c["name"] for c in custom] == ["netclaw-run"]
    assert {f"{m['provenance']}/{m['name']}" for m in custom[0]["methods"]} == METHODS
    assert not any("*" in m["name"] for m in custom[0]["methods"]), "exact names, never a glob"


def test_the_builtins_are_the_session_reads_and_the_agent_start_and_nothing_denied() -> None:
    prod = yaml.safe_load(ROLES.read_text())["prod"]
    keys = {f"{b['provenance']}/{b['name']}" for b in prod["builtin"]}
    assert keys == BUILTIN
    for key in keys:
        assert not any(fnmatch.fnmatchcase(key, pattern) for pattern in DENY), key
    # the oracle's own deny list covers at least this test's (the play asserts the resolved roles against it)
    oracle_deny = {f"{d['provenance']}/{d['name']}" for d in prod["deny"]}
    assert set(DENY) <= oracle_deny


# --- versions.yaml: the endpoints and the boundary -----------------------------------------------------------------


def test_each_netclaw_workflow_has_its_own_endpoint_on_its_workflows_key() -> None:
    v = _versions()
    routes = {}
    for key in NETCLAW_WORKFLOW_KEYS:
        om = v["operations_manager"][key]
        assert om["automation"] == v["workflows"][key], "the describer reads the endpoint by the workflows key"
        assert om["endpoint"]["name"].endswith("from NetClaw") and "svc-netclaw" in om["endpoint"]["description"]
        assert "manual" not in om and "schedule" not in om, "no form, no schedule: only NetClaw starts these"
        routes[key] = om["endpoint"]["route"]
    assert routes == {"show_command": "run-show-command", "describe_job": "describe-job", "branch_vlan": "add-branch-vlan"}
    others = [om.get("endpoint", {}).get("route") for k, om in v["operations_manager"].items() if k not in routes]
    assert not set(routes.values()) & set(others)


def test_the_netclaw_group_reads_exactly_its_tools_automations() -> None:
    v = _versions()
    access = v["automation_access"]
    workflows = [v["operations_manager"][k]["automation"] for k in NETCLAW_WORKFLOW_KEYS]
    agents = sorted(v["netclaw"]["platform_guide_routes"].values())
    assert sorted(access["netclaw"]) == sorted(workflows + agents)
    assert set(access["unmanaged"]) == DEPARTMENT_ASK
    assert access["write"] == ["admin_group"]
    for tier in ("prod", "dev"):
        assert "admin_group" in access["read"][tier], "the plays' admin login holds admin_group (measured)"
        assert v["netclaw"]["group"] not in access["read"][tier], "netclaw only where automation_access.netclaw says"
    assert access["read"]["prod"] == ["admin_group", "copilot-readonly"], "the values the department session set by hand"
    assert access["read"]["dev"] == ["admin_group", v["dev"]["copilot"]["group"]]


def test_platform_guide_is_an_agent_automation_on_each_route() -> None:
    routes = _versions()["netclaw"]["platform_guide_routes"]
    for agent in routes.values():
        doc = yaml.safe_load((AGENTS / f"{agent}.yaml").read_text())
        assert doc["expose_route"] == agent, "flowai-agent.yml names the automation after the agent"
        assert doc["tools"] == [{"reference": "Describe for an Agent", "kind": "workflow"}], "still one read-only tool"
        assert doc["operator_groups"] == ["netclaw"], "svc-netclaw may start it (runAgent is scoped by operators)"
    assert routes == {"twin": "platform-guide-local", "claude": "platform-guide"}
    # no other agent names an operator group: NetClaw reaches no agent but platform-guide
    others = [p.name for p in AGENTS.glob("*.yaml") if p.stem not in routes.values()
              and "operator_groups" in yaml.safe_load(p.read_text())]
    assert others == []
    task = (TASKS / "flowai-agent.yml").read_text()
    assert "operators: \"{{ agent_operators }}\"" in task and "'operators': agent_operators" in task


def test_the_password_env_keys_are_netclaw_keys_and_documented() -> None:
    env = _versions()["netclaw"]["password_env"]
    assert env == {"prod": "SVC_NETCLAW_PROD_PASSWORD", "dev": "SVC_NETCLAW_DEV_PASSWORD"}
    example = (ROOT / ".env.example").read_text()
    for key in env.values():
        assert re.search(rf"^{key}=\s", example, re.M), key


# --- the plays -----------------------------------------------------------------------------------------------------


def test_platform_yml_builds_the_endpoints_then_the_account_then_the_gbac() -> None:
    tasks = _tasks(PLAYS / "platform.yml")
    endpoints = next(t for t in tasks if t.get("loop") == NETCLAW_WORKFLOW_KEYS)
    assert endpoints["ansible.builtin.include_tasks"] == "tasks/outage-trigger.yml" and "when" not in endpoints
    includes = [t.get("ansible.builtin.include_tasks") for t in tasks]
    assert includes[-2:] == ["tasks/netclaw-access.yml", "tasks/automation-access.yml"], "last: every automation exists"
    assert all("when" not in t for t in tasks[-2:]), "both tiers"
    flowai = _tasks(TASKS / "flowai-assets.yml")
    assert flowai[-1].get("ansible.builtin.include_tasks") == "tasks/automation-access.yml", "the agents' automations"


def test_the_trigger_task_finds_each_workflow_by_its_name() -> None:
    v = _versions()
    text = (TASKS / "outage-trigger.yml").read_text()
    assert "workflows[otr_key] | lower | replace(' ', '-')" in text
    for key in NETCLAW_WORKFLOW_KEYS + ["diagnose_aws_vpn_outage", "diagnose_fabric_bgp_outage", "ask_platform_guide"]:
        doc = ROOT / "itential" / "workflows" / (v["workflows"][key].lower().replace(" ", "-") + ".json")
        gate = json.loads(doc.read_text())["tasks"]["9a0a"]["variables"]["incoming"]["schema"]
        assert gate["additionalProperties"] is False and gate["required"], key


def test_netclaw_access_writes_only_its_own_role_and_its_account() -> None:
    task_file = TASKS / "netclaw-access.yml"
    for task in _writes(task_file):
        url = _module(task)[1]["url"]
        assert "/authorization/roles" in url, f"{task['name']}: unexpected write {url}"
        assert "ncl_oracle[ncl_tier].custom" in json.dumps(task.get("loop", "")), "only the oracle's custom roles"
        assert "'provenance', 'equalto', 'Custom'" in json.dumps(task), "only a Custom role is ever reset"
    assert "admin_group" not in task_file.read_text()
    include = next(t for t in _tasks(task_file) if t.get("ansible.builtin.include_tasks") == "tasks/ldap-service-account.yml")
    assert include["vars"]["svc_kind"] == "netclaw"
    assert include["vars"]["ldap_service_account_fallback"] == "{{ ncl_tier == 'dev' }}", "production stops, dev falls back"


def test_automation_access_writes_only_gbac_and_never_the_department_ask() -> None:
    task_file = TASKS / "automation-access.yml"
    writes = _writes(task_file)
    assert len(writes) == 1
    args = _module(writes[0])[1]
    assert args["method"] == "PATCH" and "/operations-manager/automations/" in args["url"]
    assert list(args["body"]) == ["gbac"], "only the gbac field"
    assert writes[0]["loop"] == "{{ aac_autos }}"
    facts = next(t for t in _tasks(task_file) if "aac_autos" in json.dumps(_module(t)[1]))
    assert "rejectattr('name', 'in', automation_access.unmanaged)" in _module(facts)[1]["aac_autos"]


def test_the_guide_pages_name_netclaws_endpoints() -> None:
    v = _versions()
    for key in NETCLAW_WORKFLOW_KEYS:
        ep = v["operations_manager"][key]["endpoint"]
        page = (ROOT / "docs" / "workflows" / (v["workflows"][key].lower().replace(" ", "-") + ".md")).read_text()
        assert f'From the endpoint trigger "{ep["name"]}" (route `{ep["route"]}`).' in page
