"""ADR 0083 unit tests: the account production's MCP server logs in as, what it may reach, and who may reach the server.

itential/mcp/roles.yaml is the role oracle. The exclusions below are written here independently of that file, so an edit
to the oracle cannot quietly hand MCP clients identity, session, server, adapter or application administration. The
tools VM's compose file, its DOCKER-USER allowlist and the plays that give the group its access are held here too.
No lab access."""

from __future__ import annotations

import ipaddress
import json
import re
from pathlib import Path

import jinja2
import yaml

ROOT = Path(__file__).resolve().parent.parent
ROLES = ROOT / "itential" / "mcp" / "roles.yaml"
VERSIONS = ROOT / "itential" / "versions.yaml"
HA2 = ROOT / "itential" / "ha2" / "versions.yaml"
COMPOSE = ROOT / "itential" / "ha2" / "tools.compose.yml.j2"
ALLOW_SCRIPT = ROOT / "itential" / "ha2" / "mcp-access.sh.j2"
PLAYS = ROOT / "ansible" / "playbooks"
TASKS = PLAYS / "tasks"

# identity, sessions and the server itself (ADR 0063's dev exclusions), plus adapter and application control, which can
# stop the LDAP adapter every login goes through (measured on production 6.6.0, 2026-10-18)
EXCLUDED = {
    "Authorization/admin",
    "UserManagement/admin",
    "AdminEssentials/admin",
    "OAuth/admin",
    "SSO/admin",
    "SSOGroupMappings/admin",
    "Customization/admin",
    "Profiles/admin",
    "Server/admin",
    "Indexes/admin",
    "LDAP/admin",
    "Adapters/admin",
    "Applications/admin",
}
# the tools that need the roles above, and the two that return node credentials (ADR 0039 amendment)
HIDDEN_TOOLS = {
    "start_adapter",
    "stop_adapter",
    "restart_adapter",
    "start_application",
    "stop_application",
    "restart_application",
    "describe_inventory",
    "get_devices",
}


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
    return [
        t
        for t in _tasks(path)
        if _module(t)[0] == "uri"
        and str(_module(t)[1].get("method", "GET")).upper() != "GET"
    ]


def _mcp_env() -> dict:
    text = re.sub(r"\{\{.*?\}\}", "X", COMPOSE.read_text())
    return yaml.safe_load(re.sub(r"\{%.*?%\}", "", text))["services"]["mcp"][
        "environment"
    ]


# --- the role oracle ---------------------------------------------------------------------------------------------


def test_the_oracle_is_everything_but_administration_and_names_the_mcp_group() -> None:
    prod = yaml.safe_load(ROLES.read_text())["prod"]
    assert prod["group"] == _versions()["mcp_access"]["group"] == "mcp-builders"
    assert set(prod) == {"group", "exclude"}, (
        "an exclude half: every role on the Platform but these"
    )
    assert {f"{e['provenance']}/{e['name']}" for e in prod["exclude"]} == EXCLUDED


def test_the_service_account_task_knows_the_mcp_kind_and_only_its_group_prefix() -> (
    None
):
    text = (TASKS / "ldap-service-account.yml").read_text()
    facts = yaml.safe_load(text)[0]
    kind = facts["vars"]["svc_kinds"]["mcp"]
    assert kind == {
        "oracle": "itential/mcp/roles.yaml",
        "group_prefix": "mcp-",
        "password_prefix": "SVC_MCP_",
        "description": "Production MCP server service account (ADR 0083)",
    }
    assert "svc_kind | default('copilot') in ['copilot', 'netclaw', 'mcp', 'eval', 'eval-approver']" in text
    assert "svc_prefix in ['copilot-', 'netclaw', 'mcp-', 'eval', 'eval-approvers']" in text
    access = _versions()["mcp_access"]
    assert access["group"].startswith(kind["group_prefix"]) and access[
        "password_env"
    ].startswith(kind["password_prefix"])


def test_the_password_env_key_is_documented() -> None:
    key = _versions()["mcp_access"]["password_env"]
    assert key == "SVC_MCP_PROD_PASSWORD"
    assert re.search(rf"^{key}=\s", (ROOT / ".env.example").read_text(), re.MULTILINE)


# --- the MCP server on tools-01 ----------------------------------------------------------------------------------


def test_the_mcp_server_logs_in_as_svc_mcp_never_the_administrator() -> None:
    raw = COMPOSE.read_text().split("  mcp:", 1)[1].split("\n  openldap:", 1)[0]
    assert 'ITENTIAL_MCP_PLATFORM_USER: "{{ mcp_access.user }}"' in raw
    assert "ITENTIAL_MCP_PLATFORM_PASSWORD: ${SVC_MCP_PASSWORD:?}" in raw
    assert "ITENTIAL_ADMIN" not in raw, (
        "the administrator's credentials never reach the MCP container"
    )
    assert _versions()["mcp_access"]["user"] == "svc-mcp"


def test_the_mcp_server_hides_the_administration_and_credential_tools() -> None:
    tags = set(str(_mcp_env()["ITENTIAL_MCP_SERVER_EXCLUDE_TAGS"]).split(","))
    assert HIDDEN_TOOLS <= tags
    assert {"experimental", "beta"} <= tags, (
        "setting the variable replaces its default, which hides these two"
    )


def test_the_tools_play_persists_the_password_before_compose_reads_it() -> None:
    tasks = _tasks(PLAYS / "platform-ha2-tools.yml")
    names = [t["name"] for t in tasks]
    persist = names.index("The svc-mcp password persisted to .env before use")
    compose_env = names.index("Environment for compose")
    up = names.index("Containers up")
    assert persist < compose_env < up
    env = _module(tasks[compose_env])[1]
    assert "SVC_MCP_PASSWORD={{ mcp_pw }}" in env["content"] and env["mode"] == "0600"
    assert all(
        tasks[i].get("no_log") is True for i in (persist - 1, persist, compose_env)
    )


# --- the allowlist on the MCP port -------------------------------------------------------------------------------


def test_the_allowlist_admits_single_client_machines_only() -> None:
    allow = yaml.safe_load(HA2.read_text())["tools"]["mcp_allow"]
    assert allow, "an empty allowlist would drop every client"
    for src in allow:
        net = ipaddress.ip_network(src)
        assert net.prefixlen == 32, (
            f"{src}: one client machine per entry, never a range"
        )
    assert "192.168.68.142/32" in allow, "the Mac Mini"


def test_the_allowlist_script_guards_the_containers_own_port() -> None:
    script = ALLOW_SCRIPT.read_text()
    assert "CHAIN=TOOLS-MCP" in script and "PORT=8000" in script
    assert "{% for src in mcp_allow %}" in script
    lines = [ln for ln in script.splitlines() if ln.startswith("iptables")]
    assert lines.index('iptables -w -A "$CHAIN" -j DROP') > lines.index(
        'iptables -w -A "$CHAIN" -s {{ src }} -j ACCEPT'
    )
    assert (
        'iptables -w -I DOCKER-USER 1 -p tcp --dport "$PORT" -j "$CHAIN"' in script
    ), "the jump is first"
    # DOCKER-USER sees the DNATed destination: the container's own port, which the compose file publishes as 8000
    assert '"{{ ip }}:{{ tools.mcp_port }}:8000"' in COMPOSE.read_text()


def test_the_tools_play_installs_the_allowlist_bound_to_docker_and_checks_it_live() -> (
    None
):
    play = (PLAYS / "platform-ha2-tools.yml").read_text()
    assert 'mcp_allow: "{{ tools.mcp_allow }}"' in play
    unit = play.split("mcp-access.service", 1)[1].split("register: mcp_unit")[0]
    assert "PartOf=docker.service" in unit and "After=docker.service" in unit
    assert "'-A TOOLS-MCP -j DROP'" in play
    # both halves: the chain, and DOCKER-USER's jump to it (the chain alone looks right with DOCKER-USER flushed)
    check = next(
        t
        for t in _tasks(PLAYS / "platform-ha2-tools.yml")
        if t.get("register") == "mcp_rules"
    )
    assert check["loop"] == ["TOOLS-MCP", "DOCKER-USER"]
    assert "'-A DOCKER-USER -p tcp -m tcp --dport 8000 -j TOOLS-MCP'" in play


# --- what the group may reach ------------------------------------------------------------------------------------


def test_mcp_access_adds_the_group_and_never_removes_anything() -> None:
    task_file = TASKS / "mcp-access.yml"
    writes = _writes(task_file)
    urls = sorted(
        _module(t)[1]["url"].split("}}/", 1)[1].split("/{{")[0] for t in writes
    )
    assert urls == [
        "agent-project-service/agents",
        "agent-project-service/projects",
        "gateway_manager/v1/gateways",
        "gateway_manager/v1/service-groups",
    ]
    for task in writes:
        body = json.dumps(_module(task)[1]["body"])
        assert (
            "+ [mcp_access.group]" in body
            or "+ [mca_ids[mcp_access.group]]" in body
            or "'role': 'editor'" in body
        ), task["name"]
        assert "admin_group" in json.dumps(
            task.get("when", "")
        ) or "mca_projects" in json.dumps(task.get("loop", "")), (
            f"{task['name']}: only objects admin_group reaches (the plays'), or the agents' own project"
        )
    text = task_file.read_text()
    assert "inventory_manager/v1/inventories/{{" not in text, (
        "6.6.0 cannot change an inventory's groups in place"
    )
    # roles and groups are only ever read here; the group's roles are tasks/ldap-service-account.yml's one write
    assert not any("authorization" in _module(w)[1]["url"] for w in writes)
    include = next(
        t
        for t in _tasks(task_file)
        if t.get("ansible.builtin.include_tasks") == "tasks/ldap-service-account.yml"
    )
    assert (
        include["vars"]["svc_kind"] == "mcp"
        and include["vars"]["svc_role_set"] == "prod"
    )
    assert include["vars"]["ldap_service_account_fallback"] is False


def test_the_gateway_put_sends_back_every_field_it_replaces() -> None:
    gw = next(
        t
        for t in _writes(TASKS / "mcp-access.yml")
        if "gateway_manager/v1/gateways" in _module(t)[1]["url"]
    )
    assert _module(gw)[1]["method"] == "PUT"
    assert set(_module(gw)[1]["body"]["payload"]) == {
        "cluster_id",
        "enabled",
        "readonly",
        "description",
        "certificates",
        "groups",
    }


def test_the_service_group_write_skips_the_default_group() -> None:
    # 6.6.0 answers 400 "Cannot modify a default service group" (production 2026-10-21); its record carries is_default
    patch = next(
        t
        for t in _writes(TASKS / "mcp-access.yml")
        if "service-groups" in _module(t)[1]["url"]
    )
    conditions = [jinja2.Environment().compile_expression(c) for c in patch["when"]]

    def sent(group: dict) -> bool:
        return all(
            c(item=group, mcp_access={"group": "mcp-builders"}) for c in conditions
        )

    admin = [{"name": "admin_group"}]
    assert not sent(
        {"name": "lab Default Service Group", "is_default": True, "groups": admin}
    )
    assert sent({"name": "lab ops", "is_default": False, "groups": admin})
    assert sent({"name": "lab ops", "groups": admin}), (
        "a record without the flag is not a default group"
    )
    assert not sent({"name": "lab ops", "groups": admin + [{"name": "mcp-builders"}]})
    assert not sent({"name": "department", "groups": [{"name": "finance"}]})


def test_inventories_are_created_with_the_group_and_recreated_only_on_request() -> None:
    assets = (TASKS / "platform-assets.yml").read_text()
    default = (
        "groups: \"{{ inventory_groups | default(['admin_group'] + ([] if dev_overlay | default(false) | bool"
        ' else [mcp_access.group])) }}"'
    )
    assert assets.count(default) == 2, (
        "both lab inventories, on production's defaults (the dev overlay sets its own)"
    )
    deletes = [
        t
        for t in _writes(PLAYS / "mcp-access.yml")
        if _module(t)[1]["method"] == "DELETE"
    ]
    assert len(deletes) == 1
    args = _module(deletes[0])[1]
    assert (
        args["method"] == "DELETE"
        and "inventory_manager/v1/inventories/{{ item }}" in args["url"]
    )
    assert deletes[0]["loop"] == "{{ mca_inventories_without_group }}"
    assert deletes[0]["when"] == "mcp_recreate_inventories | default(false) | bool"
    assert "RECREATE_INVENTORIES" in (ROOT / "Makefile").read_text()


def test_the_group_reads_every_automation_on_production_and_writes_none() -> None:
    access = _versions()["automation_access"]
    group = _versions()["mcp_access"]["group"]
    assert group in access["read"]["prod"] and group not in access["read"]["dev"]
    assert access["write"] == ["admin_group"]


def test_the_replay_makes_the_group_before_the_inventories_and_after_the_agents() -> (
    None
):
    first = yaml.safe_load((PLAYS / "platform-ha2-replay.yml").read_text())[0]["tasks"]
    order = [
        t.get("ansible.builtin.include_tasks") or t.get("ansible.builtin.import_tasks")
        for t in first
    ]
    assert order.index("tasks/mcp-access.yml") < order.index(
        "tasks/platform-assets.yml"
    )
    # flowai-assets.yml: after the agents' project exists, before tasks/flowai-agent.yml names the group as an operator
    flowai = _tasks(TASKS / "flowai-assets.yml")
    names = [t.get("name") for t in flowai]
    inc = next(
        i
        for i, t in enumerate(flowai)
        if t.get("ansible.builtin.include_tasks") == "tasks/mcp-access.yml"
    )
    agents = next(
        i
        for i, t in enumerate(flowai)
        if "flowai-agent.yml" in str(t.get("ansible.builtin.include_tasks", ""))
    )
    assert names.index("Project id") < inc < agents
    assert flowai[inc]["when"] == "not (dev_overlay | default(false) | bool)"
    assert flowai[-1]["ansible.builtin.include_tasks"] == "tasks/automation-access.yml"
    # platform.yml: before automation_access names the group, production only
    platform = _tasks(PLAYS / "platform.yml")
    includes = [t.get("ansible.builtin.include_tasks") for t in platform]
    # ADR 0084's eval accounts sit between them, production only as well
    assert includes[-3:] == ["tasks/mcp-access.yml", "tasks/eval-access.yml", "tasks/automation-access.yml"]
    assert platform[-3]["when"] == "not (dev_overlay | default(false) | bool)"


def test_every_excluded_role_must_exist_before_any_role_is_assigned() -> None:
    """An exclude half widens silently when an upgrade renames an excluded role; the play stops instead."""
    tasks = _tasks(TASKS / "mcp-access.yml")
    names = [t["name"] for t in tasks]
    guard = names.index(
        "MCP access - every role the oracle excludes exists here (an upgrade's rename stops the play)"
    )
    include = next(
        i
        for i, t in enumerate(tasks)
        if t.get("ansible.builtin.include_tasks") == "tasks/ldap-service-account.yml"
    )
    assert guard < include
    gvars = tasks[guard]["vars"]
    assert (
        "itential/mcp/roles.yaml" in gvars["oracle"]
        and ".prod.exclude" in gvars["oracle"]
    )
    assert "reject('in', have)" in gvars["absent"]
    assert "absent | length == 0" in tasks[guard]["ansible.builtin.assert"]["that"]


def test_every_agent_update_keeps_the_mcp_group_on_production() -> None:
    text = (TASKS / "flowai-agent.yml").read_text()
    assert "([] if dev_overlay | default(false) | bool else [mcp_access.group])" in text
    assert "selectattr('name', 'in', agent_operator_names)" in text
    assert (
        'operators: "{{ agent_operators }}"' in text
        and "'operators': agent_operators" in text
    )
