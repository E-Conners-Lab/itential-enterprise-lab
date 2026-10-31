"""ADR 0083 unit tests: the account production's MCP server logs in as, what it may reach, and who may reach the server.

itential/mcp/roles.yaml is the role oracle. The exclusions below are written here independently of that file, so an edit
to the oracle cannot quietly hand MCP clients identity, session, server, adapter or application administration. The
tools VM's compose file, its DOCKER-USER allowlist and the plays that give the group its access are held here too.
No lab access."""

from __future__ import annotations

import collections
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
IPAM = ROOT / "topology" / "ipam.yaml"
CLIENT_SCRIPT = ROOT / "scripts" / "mcp-client.sh"

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


def test_the_allowlist_admits_exactly_the_recorded_client_machines() -> None:
    """tools.mcp_allow and topology/ipam.yaml home_lan.mcp_clients name the same machines: an address on the allowlist
    that no reservation holds would admit whatever device leases it next."""
    allow = set(yaml.safe_load(HA2.read_text())["tools"]["mcp_allow"])
    clients = yaml.safe_load(IPAM.read_text())["home_lan"]["mcp_clients"]
    assert allow == {f"{c['address']}/32" for c in clients}


def test_every_client_machine_holds_a_router_reservation() -> None:
    lan = yaml.safe_load(IPAM.read_text())["home_lan"]
    pool = (ipaddress.ip_address(lan["dhcp_pool_start"]), ipaddress.ip_address(lan["dhcp_pool_end"]))
    names = [c["hostname"] for c in lan["mcp_clients"]]
    assert len(names) == len(set(names)), "one record per machine"
    for c in lan["mcp_clients"]:
        addr = ipaddress.ip_address(c["address"])
        assert addr in ipaddress.ip_network("192.168.68.0/22"), c["hostname"]
        if pool[0] <= addr <= pool[1]:
            assert c.get("dhcp_reservation") is True, f"{c['hostname']}: inside the DHCP pool without a reservation"
        assert re.fullmatch(r"[a-z0-9-]+", c["hostname"]), "the AppRole name and the client script take it as is"
    inference = lan["inference_host"]
    for c in lan["mcp_clients"]:
        if c["hostname"] == inference["hostname"]:
            assert c["address"] == inference["address"], "one machine, one address"


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
    # every agent but the skills eval suite's pair (ADR 0084 decision 7: only the eval accounts reach the scratch routers)
    assert ("([] if dev_overlay | default(false) | bool or agent.name in skills_lab.agents else [mcp_access.group])"
            in text)
    assert "selectattr('name', 'in', agent_operator_names)" in text
    assert (
        'operators: "{{ agent_operators }}"' in text
        and "'operators': agent_operators" in text
    )


# --- ADR 0083 decision 5: the tokens the server will accept (itential/versions.yaml vault.mcp_tokens) -----------------


def _mcp_tokens() -> dict:
    return yaml.safe_load(VERSIONS.read_text())["vault"]["mcp_tokens"]


def test_the_client_policy_is_one_the_admin_login_may_write() -> None:
    """lab-admin may write only itential-* policies (vault-prod-config.yml), so the client roles need no root token."""
    t = _mcp_tokens()
    assert t["policy"].startswith("itential-")
    assert t["issuer"] == yaml.safe_load(VERSIONS.read_text())["vault"]["prod"]["url"], "the issuer is Vault's own name"
    assert t["key"] and t["role"] and t["client_id"]
    assert t["algorithm"] == "RS256"
    assert t["login_token_ttl"] <= t["login_token_max_ttl"] <= 900, "a login only has to last until it reads one token"


def test_the_client_policy_reads_one_token_and_nothing_else() -> None:
    tasks = (TASKS / "vault-mcp-tokens.yml").read_text()
    policy = re.search(r"policy: '(path .*)'", tasks).group(1)
    assert policy == 'path "identity/oidc/token/{{ mcp.role }}" { capabilities = ["read"] }'


def _lab_admin_policy() -> str:
    play = yaml.safe_load((PLAYS / "vault-prod-config.yml").read_text())
    return play[0]["vars"]["lab_admin_policy"]


def test_the_admin_login_may_keep_the_key_and_role_and_generate_root_but_not_move_the_issuer() -> None:
    """After the setup lab-admin maintains the signing key and the token role, and may generate root with the owner's
    unseal key (Vault 2.0 authenticates generate-root); moving the issuer and minting MCP tokens stay outside its policy
    text. That is the policy as written, not a boundary Vault enforces: lab-admin writes itential-* policies and AppRole
    roles, so it can grant itself more (ADR 0083 amendment 2026-10-31). One text, never two copies."""
    policy = _lab_admin_policy()
    assert 'path "identity/oidc/key/{{ vault.mcp_tokens.key }}" { capabilities = ["create", "read", "update"] }' in policy
    assert 'path "identity/oidc/role/{{ vault.mcp_tokens.role }}" { capabilities = ["create", "read", "update"] }' in policy
    assert 'path "sys/generate-root/attempt"' in policy and 'path "sys/generate-root/update"' in policy
    assert "identity/oidc/config" not in policy and "identity/oidc/token" not in policy
    play = (PLAYS / "vault-prod-config.yml").read_text()
    assert play.count('policy: "{{ lab_admin_policy }}"') == 1
    assert "tasks/vault-mcp-tokens.yml" in play
    tasks = (TASKS / "vault-mcp-tokens.yml").read_text()
    assert 'body: {policy: "{{ lab_admin_policy }}"}' in tasks


def test_the_setup_role_works_once_from_here_and_is_always_removed() -> None:
    """The owner's choice over a root ceremony that Vault 2.0 no longer allows (ADR 0083 amendment 2026-10-31)."""
    tasks = yaml.safe_load((TASKS / "vault-mcp-tokens.yml").read_text())
    block = next(t for t in tasks if t.get("name") == "The identity provider")
    role = next(t for t in block["block"] if t["name"].startswith("Temporary setup role"))["ansible.builtin.uri"]["body"]
    assert role["secret_id_num_uses"] == 1 and role["secret_id_ttl"] == role["token_max_ttl"] == 900
    assert role["secret_id_bound_cidrs"] == role["token_bound_cidrs"] == ["{{ mcp_ws_address }}/32"]
    cleanup = [t["name"] for t in block["always"]]
    assert cleanup == [
        "The temporary setup role removed (by its own token, which alone may)",
        "Its token revoked",
        "The temporary setup policy removed",
    ]
    setup_policy = next(t for t in block["block"] if t["name"].startswith("Temporary setup policy"))
    text = setup_policy["ansible.builtin.uri"]["body"]["policy"]
    # what this run writes, nothing else: lab-admin can still grant itself more (ADR 0083 amendment, correction to 0065)
    assert "sys/policies/acl/itential-" not in text and "sys/generate-root" not in text, "only what this setup writes"
    assert "mcp_setup_role=true" in (ROOT / "Makefile").read_text()


def test_the_identity_provider_is_written_only_by_a_token_that_may() -> None:
    tasks = (TASKS / "vault-mcp-tokens.yml").read_text()
    assert "sys/capabilities-self" in tasks
    for task in ("Signing key", "Token role {{ mcp.role }}"):
        block = tasks.split(f"- name: {task}", 1)[1].split("- name:", 1)[0]
        assert "when: mcp_may_provider" in block, task
    issuer = tasks.split("- name: Issuer set", 1)[1].split("- name:", 1)[0]
    assert "when: mcp_may_issuer and" in issuer
    assert 'allowed_client_ids: ["{{ mcp.client_id }}"]' in tasks, "no other role may sign with the key"


def test_each_client_role_is_bound_to_its_own_address_and_proven_from_here() -> None:
    tasks = (TASKS / "vault-mcp-tokens.yml").read_text()
    assert 'secret_id_bound_cidrs: ["{{ cidr }}"]' in tasks and 'token_bound_cidrs: ["{{ cidr }}"]' in tasks
    assert 'cidr: "{{ item.item.address }}/32"' in tasks
    assert "that: (item.1 == 200) == (item.0 == mcp_ws)" in tasks, "only this workstation's own role logs in from here"
    assert "mcp_claims.aud == mcp.client_id" in tasks and "mcp_claims.iss == mcp.issuer ~ '/v1/identity/oidc'" in tasks


def _script_constants() -> dict[str, str]:
    return dict(re.findall(r"^([A-Z_]+)=([^\s$]+)$", CLIENT_SCRIPT.read_text(), re.M))


def test_the_client_script_matches_versions_yaml() -> None:
    v = yaml.safe_load(VERSIONS.read_text())["vault"]
    c = _script_constants()
    assert f"https://{c['VAULT_HOST']}:{c['VAULT_PORT']}" == v["prod"]["url"]
    assert c["VAULT_VIP"] == v["prod"]["vip"]
    assert c["APPROLE_MOUNT"] == v["approle_mount"]
    assert c["APPROLE_PREFIX"] == v["mcp_tokens"]["approle_prefix"]
    assert c["TOKEN_ROLE"] == v["mcp_tokens"]["role"]
    assert c["WRAP_TTL"] == v["mcp_tokens"]["wrap_ttl"]


def test_the_client_script_keeps_secrets_out_of_argv_and_files() -> None:
    """A value in argv shows in ps; bash 3.2 (stock macOS) writes a here-string or heredoc to a temporary file."""
    script = CLIENT_SCRIPT.read_text()
    code = "\n".join(ln for ln in script.splitlines() if not ln.lstrip().startswith("#"))
    assert "<<<" not in code and "<<" not in code.replace("<<<", ""), "no here-strings or heredocs"
    assert "| security -i" in code, "the Keychain write reads its command on stdin"
    assert not re.search(r"security add-generic-password", code), "never with the value in argv"
    assert "X-Vault-Token: %s" in code and "-H @<(printf" in code, "the token reaches curl through a descriptor"
    assert script.startswith("#!/bin/bash") and "\nset -eu\n" in script
    for name in ("sid", "vt", "at", "wt", "jwt"):
        assert f"unset {name}" in code or f"unset rid {name}" in code or re.search(rf"unset [a-z ]*\b{name}\b", code), name


def test_the_wrapping_token_is_the_only_value_the_script_shows() -> None:
    script = CLIENT_SCRIPT.read_text()
    shown = [ln for ln in script.splitlines() if re.search(r"jget (data\.secret_id|data\.role_id|auth\.client_token|data\.token|wrap_info\.token)", ln)]
    for ln in shown:
        assert "=$(" in ln or "wrap_info.token" in ln, ln
    assert sum("wrap_info.token" in ln for ln in shown) == 1


# --- ADR 0083 decisions 5-7, server side: TLS and a Vault-signed token on tools-01 ----------------------------------


def _mcp_env_rendered() -> dict:
    """The MCP service's environment with the real versions.yaml values the auth settings are made from."""
    rendered = jinja2.Environment(undefined=jinja2.ChainableUndefined).from_string(COMPOSE.read_text()).render(
        vault=yaml.safe_load(VERSIONS.read_text())["vault"],
        images=collections.defaultdict(lambda: {"repository": "r", "tag": "t"}),
        ip="10.100.0.81", tools={"mcp_port": 8000}, service_name="itential", domain="lab.internal",
        loadbalancer={"listen_port": 443}, mcp_access={"user": "svc-mcp"},
    )
    return yaml.safe_load(rendered)["services"]["mcp"]["environment"]


def test_the_server_takes_only_vault_signed_tokens_over_tls() -> None:
    env = _mcp_env_rendered()
    t = _mcp_tokens()
    assert env["ITENTIAL_MCP_SERVER_AUTH_TYPE"] == "jwt"
    assert env["ITENTIAL_MCP_SERVER_AUTH_JWKS_URI"] == f"{t['issuer']}/v1/identity/oidc/.well-known/keys"
    assert env["ITENTIAL_MCP_SERVER_AUTH_ISSUER"] == f"{t['issuer']}/v1/identity/oidc", "Vault's iss claim"
    assert env["ITENTIAL_MCP_SERVER_AUTH_AUDIENCE"] == t["client_id"]
    assert env["ITENTIAL_MCP_SERVER_AUTH_ALGORITHM"] == t["algorithm"]
    assert "ITENTIAL_MCP_SERVER_AUTH_PUBLIC_KEY" not in env, "exactly one of a JWKS URI and a static key"
    assert env["ITENTIAL_MCP_SERVER_CERTIFICATE_FILE"] == "/etc/ssl/lab/mcp.crt"
    assert env["ITENTIAL_MCP_SERVER_PRIVATE_KEY_FILE"] == "/etc/ssl/lab/mcp.key"
    assert env["SSL_CERT_FILE"] == "/etc/ssl/lab/ca.crt", "the JWKS fetch trusts the lab CA"


def test_the_tools_play_installs_the_certificate_and_proves_both_answers_from_here() -> None:
    plays = yaml.safe_load((PLAYS / "platform-ha2-tools.yml").read_text())
    cert = plays[0]
    assert cert["hosts"] == "localhost"
    spec = next(t for t in cert["tasks"] if "kubernetes.core.k8s" in t and t["kubernetes.core.k8s"]["definition"]["kind"] == "Certificate")
    spec = spec["kubernetes.core.k8s"]["definition"]["spec"]
    assert spec["issuerRef"] == {"name": "lab-ca", "kind": "ClusterIssuer"} and spec["ipAddresses"] == ["{{ tools_vm.ip }}"]
    tools = next(p for p in plays if p.get("hosts") == "ha2-tools")
    by_name = {t["name"]: t for t in tools["tasks"]}
    files = by_name["The MCP server's certificate and key (ADR 0083 decision 6)"]
    key = next(i for i in files["loop"] if i["name"] == "mcp.key")
    assert key["mode"] == "0600" and files["ansible.builtin.copy"]["owner"] == "root" and files["no_log"] is True
    assert by_name["MCP restarted for a new certificate"]["when"] == "mcp_tls_files is changed"
    refused = by_name["MCP refuses an initialize without a token"]["ansible.builtin.uri"]
    assert refused["status_code"] == [401]
    accepted = by_name["MCP answers an initialize with that token"]
    assert accepted["ansible.builtin.uri"]["status_code"] == [200] and accepted["no_log"] is True
    assert accepted["delegate_to"] == "localhost", "from this Mac, through the allowlist, as Claude Code calls it"
    minted = by_name["A token from this Mac's own Vault login (scripts/mcp-client.sh token, Claude Code's headersHelper)"]
    assert minted["no_log"] is True and "mcp-client.sh token" in minted["ansible.builtin.command"]["cmd"]
    assert tools["vars"]["mcp_url"] == "https://mcp.{{ domain }}:{{ tools.mcp_port }}/mcp"


def test_break_glass_serves_loopback_only_without_auth_and_is_never_the_default() -> None:
    """Decision 7: when Vault cannot sign, the way in is an SSH tunnel to tools-01, whose root already owns the box."""
    text = (ROOT / "itential" / "ha2" / "tools.compose.breakglass.yml").read_text()
    assert "ports: !override" in text, "replace the published address, do not add to it"
    body = yaml.safe_load(text.replace("!override", ""))
    mcp = body["services"]["mcp"]
    assert mcp["ports"] == ["127.0.0.1:8000:8000"]
    assert mcp["environment"]["ITENTIAL_MCP_SERVER_AUTH_TYPE"] == "none"
    play = (PLAYS / "platform-ha2-tools.yml").read_text()
    assert "dest: \"{{ dir }}/docker-compose.breakglass.yml\"" in play
    assert "breakglass" not in COMPOSE.read_text(), "the normal compose file never pulls it in"
    assert "cmd: docker compose up -d\n" in play, "the play's own up uses the normal file only, so a run leaves break-glass"


def test_no_verify_calls_the_production_mcp_server_without_tls_and_a_token() -> None:
    """Every caller of production's MCP server moved with the server (verify 06 S4c.6 was missed once)."""
    for script in sorted((ROOT / "verify").glob("*.sh")):
        text = script.read_text()
        assert "http://mcp.lab.internal" not in text, f"{script.name} calls the production MCP server over plain HTTP"
        if "https://mcp.lab.internal" in text and "mcpcall.py" in text:
            assert 'MCP_HEADERS_CMD="scripts/mcp-client.sh token"' in text, f"{script.name} sends no token"
