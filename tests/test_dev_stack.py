"""ADR 0063: the dev stack `itential-dev` and its isolation contract with production.

The dev stack and the production replay share the phase 5-7 plays and task files (ADR 0055). Two things keep a dev
run from changing production, and these tests hold both:

1. The plays refuse to run without exactly one overlay, and the production replay refuses the dev overlay.
2. Every variable the dev overlay sets is read with `| default(<what production has always done>)`, and the
   production overlay sets none of them, so production's behaviour cannot drift because the dev stack exists.

They run in CI with no lab access.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import jinja2
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
PLAYS = ROOT / "ansible" / "playbooks"
TASKS = PLAYS / "tasks"
DEV_VARS = PLAYS / "vars" / "itential-dev.yml"
PROD_VARS = PLAYS / "vars" / "itential-prod.yml"
ITENTIAL = yaml.safe_load((ROOT / "itential" / "versions.yaml").read_text())

GUARD_THAT = "(platform_target is defined) != (dev_overlay | default(false) | bool)"
GUARD_MSG = "pass exactly one overlay: vars/itential-dev.yml (dev) or vars/itential-prod.yml (production)"
GUARDED_PLAYS = ("itential.yml", "platform.yml", "flowai.yml")

# The keys only the dev overlay sets. The production overlay must set none of them.
DEV_KEYS = (
    "dev_overlay",
    "enc_key_env",
    "device_source",
    "llm_profile_names",
    "integration_models",
    "platform_elements",
    "lcm_import_instances",
    "netbox_vlan_groups_managed",
    "inventory_groups",
    "gateway_groups",
    "agent_exclude",
    "mcp_platform_user",
    "mcp_platform_password",
)

# Every play and task file this change touched: a YAML slip in any of them must fail CI, not a lab run.
CHANGED_YAML = (
    PLAYS / "itential.yml",
    PLAYS / "platform.yml",
    PLAYS / "flowai.yml",
    PLAYS / "platform-ha2-replay.yml",
    PLAYS / "itential-host.yml",
    PLAYS / "netbox-vms.yml",
    DEV_VARS,
    TASKS / "platform-assets.yml",
    TASKS / "lcm.yml",
    TASKS / "integrations.yml",
    TASKS / "flowai-assets.yml",
)


def dev() -> dict:
    return yaml.safe_load(DEV_VARS.read_text())


def tasks_of(play_file: str, play_index: int = 0) -> list[dict]:
    return yaml.safe_load((PLAYS / play_file).read_text())[play_index]["tasks"]


def code_of(path: Path) -> str:
    """The file without its comment lines: the comments explain the production names they avoid."""
    return "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("#"))


def ansible_bool(value: object) -> bool:
    """Ansible's `bool` filter: True only for a boolean true or one of its true spellings."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("yes", "on", "1", "true")


def ansible_env(**variables: str) -> jinja2.Environment:
    """A Jinja environment with the Ansible filter and the env lookup these tests render against."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["bool"] = ansible_bool
    env.globals["lookup"] = lambda plugin, key: variables.get(key, "") if plugin == "ansible.builtin.env" else None
    return env


def render(template: str, **context: object) -> str:
    return ansible_env().from_string(template).render(**context)


def evaluate(expression: str, env_vars: dict[str, str] | None = None, **context: object) -> object:
    """One Ansible `that`/`when` expression, evaluated the way the assert module does."""
    return ansible_env(**(env_vars or {})).compile_expression(expression)(**context)


@pytest.mark.parametrize("path", CHANGED_YAML, ids=lambda p: p.name)
def test_changed_yaml_parses(path: Path) -> None:
    assert yaml.safe_load(path.read_text()) is not None, f"{path.name} is empty or not YAML"


# --- the overlay ----------------------------------------------------------------------------------------


def test_the_dev_overlay_selects_the_dev_behaviour() -> None:
    d = dev()
    assert d["dev_overlay"] is True
    for key in ("platform_target", "platform_api", "platform_admin_user"):
        # platform_target is the guards' production marker; the other two already default to the dev stack
        assert key not in d, f"the dev overlay must not set {key}"
    assert d["enc_key_env"] == "ITENTIAL_DEV_ENCRYPTION_KEY" == ITENTIAL["dev"]["encryption_key_env"]
    assert d["enc_key_env"] != "ITENTIAL_ENCRYPTION_KEY", "production's key protects every stored adapter secret"
    assert d["device_source"] == "clab", "the dev inventory is the Containerlab oracle, never NetBox"
    assert d["llm_profile_names"] == ["ollama-mac"], "no Anthropic spend from the dev stack"
    assert d["integration_models"] == ["netbox"], "ServiceNow is off on dev (the PDI is shared with production)"
    assert "golden_config" not in d["platform_elements"], "Golden Config needs NetBox intent clab devices lack"
    assert set(d["platform_elements"]) == {"mop", "lcm", "integrations"}
    assert d["lcm_import_instances"] is False, "the LCM instances describe production NetBox VLANs"
    assert d["netbox_vlan_groups_managed"] is False, "dev's NetBox token is view-only"
    for key in ("inventory_groups", "gateway_groups"):
        expr = d[key].strip().removeprefix("{{").removesuffix("}}")
        # before the service-account task has run, and after it: the grant follows the group svc-copilot holds
        assert evaluate(expr) == ["admin_group", ITENTIAL["dev"]["copilot"]["group"]], key
        assert evaluate(expr, svc_effective_group="copilot-builders") == ["admin_group", "copilot-builders"], key
        assert evaluate(expr, svc_effective_group="copilot-builders-local") == ["admin_group", "copilot-builders-local"], key
    assert d["mcp_platform_user"] == ITENTIAL["dev"]["copilot"]["user"] == "svc-copilot"
    assert ITENTIAL["dev"]["copilot"]["password_env"] in d["mcp_platform_password"]


def test_agent_exclude_drops_exactly_the_local_twins_bound_to_servicenow() -> None:
    """With ServiceNow off, a twin binding lab-servicenow tools would reference tools that do not exist on dev."""
    agents = [yaml.safe_load(p.read_text()) for p in sorted((ROOT / "itential" / "agents").glob("*.yaml"))]
    local = [a for a in agents if a.get("profile") in dev()["llm_profile_names"]]
    assert local, "the dev overlay keeps at least one -local twin"
    bound = {a["name"] for a in local if any(t.get("model") == "servicenow" for t in a.get("tools", []))}
    assert set(dev()["agent_exclude"]) == bound, f"agent_exclude must be exactly the ServiceNow-bound twins {bound}"


def test_the_production_overlay_sets_no_dev_key() -> None:
    prod = yaml.safe_load(PROD_VARS.read_text())
    leaked = sorted(set(DEV_KEYS) & set(prod))
    assert not leaked, f"vars/itential-prod.yml sets dev-only keys {leaked}; production must run on the defaults"
    assert "platform_target" in prod, "the production overlay is what passes the guard on production"


def test_every_dev_key_is_read_with_a_default() -> None:
    """A dev key read without a default would make production depend on a value only the dev overlay provides."""
    sources = "\n".join(p.read_text() for p in (*PLAYS.glob("*.yml"), *TASKS.glob("*.yml")))
    for key in DEV_KEYS:
        if key == "dev_overlay":
            assert "dev_overlay | default(false)" in sources
            continue
        assert re.search(rf"\b{key} \| default\(", sources), f"{key} is never read with a default"


# --- the guards -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("play", GUARDED_PLAYS)
def test_the_asset_plays_refuse_anything_but_exactly_one_overlay(play: str) -> None:
    first = tasks_of(play)[0]
    assert "ansible.builtin.assert" in first, f"the first task of {play} must be the overlay guard"
    assert first["ansible.builtin.assert"]["that"] == GUARD_THAT, play
    assert first["ansible.builtin.assert"]["fail_msg"] == GUARD_MSG, play


def test_the_guard_logic_accepts_one_overlay_and_rejects_none_or_both() -> None:
    """The guard's truth table, rendered with Jinja: production defines platform_target, dev sets dev_overlay."""
    expr = "{{ " + GUARD_THAT + " }}"
    assert render(expr, platform_target="ha2-platform[0]") == "True", "production overlay"
    assert render(expr, dev_overlay=True) == "True", "dev overlay"
    assert render(expr) == "False", "no overlay: the default group is the dev VM with production settings"
    assert render(expr, platform_target="ha2-platform[0]", dev_overlay=True) == "False", "both overlays"
    # `-e dev_overlay=false|true` arrives as a string, and 'false' is truthy to Jinja without `| bool`
    assert render(expr, dev_overlay="false") == "False", "-e dev_overlay=false is no overlay"
    assert render(expr, platform_target="ha2-platform[0]", dev_overlay="false") == "True", "production, -e dev_overlay=false"
    assert render(expr, dev_overlay="true") == "True", "-e dev_overlay=true is the dev overlay"
    assert render(expr, platform_target="ha2-platform[0]", dev_overlay="true") == "False", "production, -e dev_overlay=true"


RO_TOKEN_THAT = [
    "lookup('ansible.builtin.env', 'NETBOX_DEV_RO_TOKEN') | length > 0",
    "lookup('ansible.builtin.env', 'NETBOX_TOKEN') == lookup('ansible.builtin.env', 'NETBOX_DEV_RO_TOKEN')",
]


@pytest.mark.parametrize("play", GUARDED_PLAYS)
def test_the_dev_plays_refuse_the_full_netbox_token(play: str) -> None:
    """Only the Makefile swapped the tokens before; each play now checks it, right after the overlay guard."""
    task = tasks_of(play)[1]
    assert "ansible.builtin.assert" in task, f"the second task of {play} must be the read-only token guard"
    assertion = task["ansible.builtin.assert"]
    assert assertion["that"] == RO_TOKEN_THAT, play
    assert task["when"] == "dev_overlay | default(false) | bool", "dev only: production runs with the full token"
    assert task["no_log"] is True, "the lookups hold both tokens"
    assert "make netbox-token-dev" in assertion["fail_msg"] and "load_env_dev" in assertion["fail_msg"]
    assert "{{" not in assertion["fail_msg"], "the message must not template a token"

    def passes(**env_vars: str) -> bool:
        return all(evaluate(expr, env_vars) for expr in assertion["that"])

    assert passes(NETBOX_TOKEN="nbt_ro.secret", NETBOX_DEV_RO_TOKEN="nbt_ro.secret"), "load_env_dev swapped the token"
    assert not passes(NETBOX_TOKEN="nbt_full.secret", NETBOX_DEV_RO_TOKEN="nbt_ro.secret"), "the full token"
    assert not passes(NETBOX_TOKEN="nbt_full.secret"), "no read-only token minted"
    assert not passes(), "neither token: equal, but empty"
    for overlay, runs in ((True, True), ("true", True), ("false", False), (False, False)):
        assert evaluate(task["when"], dev_overlay=overlay) is runs, f"dev_overlay={overlay!r}"
    assert evaluate(task["when"]) is False, "production (no dev_overlay) skips it"


def test_the_production_replay_refuses_the_dev_overlay() -> None:
    first = tasks_of("platform-ha2-replay.yml")[0]
    that = first["ansible.builtin.assert"]["that"]
    assert that == "not (dev_overlay | default(false) | bool)"
    assert evaluate(that) is True and evaluate(that, dev_overlay="false") is True, "production runs"
    assert evaluate(that, dev_overlay=True) is False and evaluate(that, dev_overlay="true") is False, "-e dev_overlay=true"
    text = (PLAYS / "platform-ha2-replay.yml").read_text()
    assert "import_playbook: platform.yml" in text, "the replay still runs platform.yml, whose guard needs platform_target"


# --- itential.yml: secrets, MCP identity, the service account --------------------------------------------


def test_the_secrets_play_writes_the_overlays_key_never_a_literal() -> None:
    text = (PLAYS / "itential.yml").read_text()
    assert 'regexp: "^ITENTIAL_ENCRYPTION_KEY="' not in text, "a literal regexp would overwrite production's key"
    secrets = yaml.safe_load(text)[0]
    assert secrets["vars"]["enc_key_var"] == "{{ enc_key_env | default('ITENTIAL_ENCRYPTION_KEY') }}"
    tasks = secrets["tasks"]
    lookup = next(t for t in tasks if t["name"] == "Current values")["ansible.builtin.set_fact"]["cur_key"]
    assert lookup == "{{ lookup('ansible.builtin.env', enc_key_var) }}"
    persist = next(t for t in tasks if t["name"] == "Persist to .env before use")["ansible.builtin.lineinfile"]
    assert persist["regexp"] == "^{{ enc_key_var }}="
    assert persist["line"] == "{{ enc_key_var }}={{ enc_key }}"
    # rendered both ways: production keeps its line, dev gets its own
    default = "{{ enc_key_env | default('ITENTIAL_ENCRYPTION_KEY') }}"
    assert render(default) == "ITENTIAL_ENCRYPTION_KEY"
    assert render(default, enc_key_env=dev()["enc_key_env"]) == "ITENTIAL_DEV_ENCRYPTION_KEY"


def test_itential_yml_defaults_are_production() -> None:
    text = (PLAYS / "itential.yml").read_text()
    assert "MCP_PLATFORM_USER={{ mcp_platform_user | default(admin_user) }}" in text
    assert "MCP_PLATFORM_PASSWORD={{ mcp_platform_password | default(admin_pw) }}" in text
    assert "groups: \"{{ gateway_groups | default(['admin_group']) }}\"" in text
    assert "groups: [admin_group]" not in text


def test_the_service_account_exists_before_the_gateway_and_inventories_use_its_group() -> None:
    tasks = tasks_of("itential.yml", 1)
    names = [t["name"] for t in tasks]
    svc = next(t for t in tasks if t.get("ansible.builtin.include_tasks") == "tasks/ldap-service-account.yml")
    assert svc["when"] == "dev_overlay | default(false)", "production never runs the dev service account"
    assert svc["vars"]["svc_user"] == "{{ dev.copilot.user }}" and svc["vars"]["svc_group"] == "{{ dev.copilot.group }}"
    assert svc["vars"]["password_env"] == "{{ dev.copilot.password_env }}"
    at = names.index(svc["name"])
    assert names.index("The Platform's LDAP administrator (shared with production, ADR 0055)") < at
    gateway = next(i for i, t in enumerate(tasks) if t["name"].startswith("Gateway cluster"))
    assets = next(i for i, t in enumerate(tasks) if t.get("ansible.builtin.import_tasks") == "tasks/platform-assets.yml")
    assert at < gateway < assets, "copilot-builders must exist before a gateway or inventory is created with it"


def test_flowai_re_syncs_the_copilot_roles_last_and_only_on_dev() -> None:
    """itential.yml resolves copilot-builders' roles before the adapters and Integration Models add theirs."""
    tasks = tasks_of("flowai.yml")
    last = tasks[-1]
    assert last.get("ansible.builtin.include_tasks") == "tasks/ldap-service-account.yml", "the re-sync is the last task"
    assert last["when"] == "dev_overlay | default(false) | bool"
    assert evaluate(last["when"]) is False, "a run without dev_overlay skips it"
    assert evaluate(last["when"], dev_overlay="false") is False and evaluate(last["when"], dev_overlay=True) is True
    first_run = next(t for t in tasks_of("itential.yml", 1) if t.get("ansible.builtin.include_tasks") == "tasks/ldap-service-account.yml")
    same = {k: v for k, v in first_run["vars"].items() if k != "svc_password"}  # itential.yml's generated value
    assert last["vars"] == same, "the same account, group, role set and directory host as itential.yml"
    # the task needs an administrator session named `login`: flowai-assets.yml registers it
    assets = yaml.safe_load((TASKS / "flowai-assets.yml").read_text())
    assert any(t.get("register") == "login" and "/login" in str(t.get("ansible.builtin.uri", {})) for t in assets)
    assert "platform_local" in yaml.safe_load((PLAYS / "flowai.yml").read_text())[0]["vars"]


def test_the_service_account_task_reports_the_group_it_really_used() -> None:
    tasks = yaml.safe_load((TASKS / "ldap-service-account.yml").read_text())
    fact = tasks[-1]["ansible.builtin.set_fact"]["svc_effective_group"]
    expr = fact.strip().removeprefix("{{").removesuffix("}}")
    assert evaluate(expr, svc_group="copilot-builders", svc_group_persisted=True) == "copilot-builders"
    assert evaluate(expr, svc_group="copilot-builders", svc_group_persisted="False",
                    svc_fallback_group="copilot-builders-local") == "copilot-builders-local"
    prod = yaml.safe_load((PROD_VARS).read_text())
    assert "svc_effective_group" not in prod and "inventory_groups" not in prod, "production keeps ['admin_group']"


# --- the shared task files keep production's behaviour ---------------------------------------------------


def test_platform_assets_defaults_are_production() -> None:
    text = (TASKS / "platform-assets.yml").read_text()
    assert "device_source | default('netbox') == 'netbox'" in text
    assert text.count("groups: \"{{ inventory_groups | default(['admin_group']) }}\"") == 2, "lab and lab-hosts"
    assert "groups: [admin_group]" not in text
    assert "netbox_vlan_groups_managed | default(true)" in text
    tasks = yaml.safe_load(text)
    by_name = {t["name"]: t for t in tasks}
    for name in (
        "Network devices from NetBox (active, with a management address, by platform)",
        "Inventory nodes",
        "Ubuntu hosts from NetBox (active, with a management address, by platform)",
        "Host nodes (netmiko platform linux, the automation account with its password)",
    ):
        assert by_name[name]["when"] == "device_source | default('netbox') == 'netbox'", name
    clab = by_name["Inventory nodes from the Containerlab oracle (dev only)"]
    assert clab["when"] == "device_source | default('netbox') == 'clab'"
    assert clab["no_log"] is True, "the node attributes carry the device password"
    assert "loop" not in clab, "a loop would be templated (and the oracle read) on production before `when`"
    body = clab["ansible.builtin.set_fact"]["inv_nodes"]
    for key in ("clab.nodes", "n.mgmt_ipv4", "n.netmiko", "clab.credentials.user", "clab.credentials.password_env"):
        assert key in body, f"the clab inventory reads {key} from clab/versions.yaml"


def test_the_vlan_group_post_is_the_only_netbox_write_and_it_is_gated() -> None:
    tasks = yaml.safe_load((TASKS / "platform-assets.yml").read_text())
    writes = [t for t in tasks if isinstance(t.get("ansible.builtin.uri"), dict)
              and t["ansible.builtin.uri"].get("method") == "POST" and "netbox_oob_host" in t["ansible.builtin.uri"]["url"]]
    assert [t["name"] for t in writes] == ["VLAN group per branch"]
    assert writes[0]["when"][0] == "netbox_vlan_groups_managed | default(true)"


def test_platform_yml_defaults_are_production() -> None:
    text = (PLAYS / "platform.yml").read_text()
    all_four = "platform_elements | default(['golden_config', 'mop', 'lcm', 'integrations'])"
    tasks = tasks_of("platform.yml")
    includes = {t["ansible.builtin.include_tasks"]: t["when"] for t in tasks if "ansible.builtin.include_tasks" in t}
    for element, path in (("golden_config", "golden-config"), ("mop", "mop"), ("lcm", "lcm"), ("integrations", "integrations")):
        assert includes[f"tasks/{path}.yml"] == f"'{element}' in {all_four}", path
        # rendered: the default runs every element, the dev overlay skips only golden_config
        cond = "{{ " + includes[f"tasks/{path}.yml"] + " }}"
        assert render(cond) == "True", f"production runs {element}"
        assert render(cond, platform_elements=dev()["platform_elements"]) == str(element != "golden_config")
    assert text.count("device_source | default('netbox') == 'netbox'") >= 5, "every NetBox intent task is gated"
    clab = next(t for t in tasks if t["name"] == "Device intent from the Containerlab oracle (dev)")
    assert clab["when"] == "device_source | default('netbox') == 'clab'" and "loop" not in clab


def test_lcm_defaults_are_production() -> None:
    tasks = yaml.safe_load((TASKS / "lcm.yml").read_text())
    imp = next(t for t in tasks if t["name"].startswith("Instances imported for the NetBox VLANs"))
    assert imp["when"][0] == "lcm_import_instances | default(true)"


def test_integrations_defaults_are_production() -> None:
    text = (TASKS / "integrations.yml").read_text()
    assert "integration_models | default(integrations.models.keys() | list)" in text
    # every loop goes through the filtered list; the raw dict2items survives only in its definition
    assert text.count("integrations.models | dict2items") == 1
    first = yaml.safe_load(text)[0]
    assert first["name"] == "Integration models this environment builds"
    expr = first["ansible.builtin.set_fact"]["int_models"].strip().removeprefix("{{").removesuffix("}}")
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["dict2items"] = lambda d: [{"key": k, "value": v} for k, v in d.items()]  # Ansible's filter
    selected = env.compile_expression(expr)

    def keys(**overlay: object) -> list[str]:
        return [i["key"] for i in selected(integrations=ITENTIAL["integrations"], **overlay)]

    assert keys() == list(ITENTIAL["integrations"]["models"]), "production builds every model in the oracle"
    assert keys(integration_models=dev()["integration_models"]) == ["netbox"]
    assert "'servicenow' not in (int_models | map(attribute='key') | list) or" in text, "the PDI assert follows the list"


def test_flowai_defaults_are_production() -> None:
    play = (PLAYS / "flowai.yml").read_text()
    assert "llm_profile_names | default(llm.profiles | map(attribute='name') | list)" in play
    assets = (TASKS / "flowai-assets.yml").read_text()
    assert "rejectattr('name', 'in', agent_exclude | default([]))" in assets


# --- the dev VM's names: never production's --------------------------------------------------------------


def test_the_certificate_carries_only_the_dev_names() -> None:
    plays = yaml.safe_load((PLAYS / "itential-host.yml").read_text())
    issue = next(p for p in plays if p["hosts"] == "localhost")
    ctx = {"vm": ITENTIAL["vm"], "dev": ITENTIAL["dev"]}
    ns = next(t for t in issue["tasks"] if "Namespace" in t["name"])["kubernetes.core.k8s"]["definition"]
    assert render(ns["metadata"]["name"], **ctx) == "itential-dev"
    cert = next(t for t in issue["tasks"] if t["name"].startswith("Certificate"))["kubernetes.core.k8s"]["definition"]
    assert render(cert["metadata"]["name"], **ctx) == "itential-dev-platform"
    assert render(cert["metadata"]["namespace"], **ctx) == "itential-dev"
    assert render(cert["spec"]["secretName"], **ctx) == "itential-dev-platform-tls"
    dns = [render(n, **ctx) for n in cert["spec"]["dnsNames"]]
    assert dns == ["itential-dev.lab.internal", "mcp-dev.lab.internal"]
    assert render(cert["spec"]["commonName"], **ctx) == "itential-dev.lab.internal"
    secret = next(t for t in issue["tasks"] if t["name"] == "Read the issued secret")["kubernetes.core.k8s_info"]
    assert render(secret["namespace"], **ctx) == "itential-dev"
    assert render(secret["name"], **ctx) == "itential-dev-platform-tls"
    text = code_of(PLAYS / "itential-host.yml")
    for prod_name in ("itential.lab.internal", "mcp.lab.internal", "itential-platform", "namespace: itential}"):
        assert prod_name not in text, f"itential-host.yml still names production's {prod_name}"


def test_the_dev_vm_routes_the_clab_prefix_without_netplan_apply() -> None:
    plays = yaml.safe_load((PLAYS / "itential-host.yml").read_text())
    host = plays[0]
    assert "clab/versions.yaml" in host["vars"]["clab"], "the route comes from the clab oracle"
    assert "../../clab/versions.yaml" not in host.get("vars_files", []), "its vm block would replace the dev VM's"
    tasks = host["tasks"]
    persist = next(t for t in tasks if "61-clab-route.yaml" in str(t.get("ansible.builtin.copy", {})))
    assert "{{ clab.mgmt.prefix }}" in persist["ansible.builtin.copy"]["content"]
    assert "{{ clab.vm.ip }}" in persist["ansible.builtin.copy"]["content"]
    live = next(t for t in tasks if "ip route replace" in str(t.get("ansible.builtin.command", "")))
    assert "when" in live and live["changed_when"] is True, "idempotent: only when the route is missing"
    assert "netplan apply" not in code_of(PLAYS / "itential-host.yml"), "applying would bounce the VM's only interface"


def test_netbox_registers_the_dev_vm_and_clab_only() -> None:
    play = yaml.safe_load((PLAYS / "netbox-vms.yml").read_text())[0]
    vms = {v["name"]: v for v in play["vars"]["vms"]}
    assert vms["itential-dev"] == {"name": "itential-dev", "vcpus": 8, "memory": 24576, "disk": 160,
                                   "role": "itential-host", "ips": ["10.100.0.65"], "tags": ["phase-5"]}
    assert vms["clab"] == {"name": "clab", "vcpus": 8, "memory": 20480, "disk": 60,
                           "role": "clab-host", "ips": ["10.100.0.224"], "tags": ["phase-12"]}
    vm = ITENTIAL["vm"]
    assert (vm["cores"], vm["memory_mb"], vm["disk_gb"], vm["ip"]) == (8, 24576, 160, "10.100.0.65")
    assert "itential" not in vms and "mcp" not in vms, "production's service names are not VMs"
    names = [t["name"] for t in play["tasks"]]
    assert "Retired virtual machines deleted" in names, "the pruning of unlisted VMs stays"
    text = (PLAYS / "netbox-vms.yml").read_text()
    assert "was retired at S11.8" not in text, "the retirement comment is superseded by ADR 0063"


def test_no_netbox_group_is_named_like_a_host() -> None:
    """nb_inventory turns tags and roles into groups (group_names_raw), so a tag equal to a VM name makes a group and
    a host with one name; the clab plays address the role group clab-host, and nothing may address `clab`."""
    play = yaml.safe_load((PLAYS / "netbox-vms.yml").read_text())[0]
    vms = play["vars"]["vms"]
    names = {v["name"] for v in vms}
    groups = {g for v in vms for g in (*v["tags"], v["role"])}
    assert not names & groups, f"a NetBox tag or role equals a VM name: {sorted(names & groups)}"
    for path in PLAYS.glob("*.yml"):
        for doc in yaml.safe_load(path.read_text()) or []:
            if isinstance(doc, dict) and "hosts" in doc:
                assert doc["hosts"] != "clab", f"{path.name} targets the ambiguous group clab"
    assert yaml.safe_load((PLAYS / "clab-host.yml").read_text())[0]["hosts"] == "clab-host"
    assert yaml.safe_load((PLAYS / "clab-dev.yml").read_text())[1]["hosts"] == "clab-host"


def test_the_dev_vms_stay_active_because_the_inventory_only_returns_active_vms() -> None:
    """Review finding M6 asked for `staged`: the NetBox inventory filters on status=active, so a staged itential-dev or
    clab would vanish from the itential-host and clab-host groups and every dev play would run against no host.
    The owner chose to keep them active and exclude them from monitoring by NetBox role instead (ADR 0063)."""
    inventory = yaml.safe_load((ROOT / "ansible" / "inventory" / "netbox.yml").read_text())
    assert {"status": "active"} in inventory["vm_query_filters"], "if this filter goes, M6 can revisit status"
    play = yaml.safe_load((PLAYS / "netbox-vms.yml").read_text())[0]
    register = next(t for t in play["tasks"] if t["name"] == "Virtual machines")
    assert register["netbox.netbox.netbox_virtual_machine"]["data"]["status"] == "active"


OBS_ZABBIX = yaml.safe_load((ROOT / "observability" / "observability.yaml").read_text())["zabbix"]
SAMPLE_VMS = [
    {"name": "tools-01", "role": {"slug": "ha2-tools"}, "platform": {"slug": "ubuntu-24-04"},
     "primary_ip4": {"address": "10.100.0.81/24"}},
    {"name": "itential-dev", "role": {"slug": "itential-host"}, "platform": {"slug": "ubuntu-24-04"},
     "primary_ip4": {"address": "10.100.0.65/24"}},
    {"name": "clab", "role": {"slug": "clab-host"}, "platform": {"slug": "ubuntu-24-04"},
     "primary_ip4": {"address": "10.100.0.224/24"}},
    {"name": "no-role", "role": None, "platform": {"slug": "ubuntu-24-04"}, "primary_ip4": {"address": "10.100.0.9/24"}},
]


def test_the_sandbox_is_excluded_from_monitoring_by_exactly_its_netbox_roles() -> None:
    """Owner decision (review M6): the sandbox VMs stay active for the inventory, and are kept out of monitoring by
    NetBox role. The roles must be exactly the ones netbox-vms.yml gives itential-dev and clab - a renamed role would
    otherwise put a stopped sandbox back into Zabbix and turn production's S7.1 red."""
    play = yaml.safe_load((PLAYS / "netbox-vms.yml").read_text())[0]
    roles = {v["name"]: v["role"] for v in play["vars"]["vms"]}
    assert set(OBS_ZABBIX["excluded_vm_roles"]) == {roles["itential-dev"], roles["clab"]}
    ha2_roles = {"ha2-" + v["role"] for v in yaml.safe_load((ROOT / "itential" / "ha2" / "versions.yaml").read_text())["vms"]}
    assert not ha2_roles & set(OBS_ZABBIX["excluded_vm_roles"]), "no production VM role may ever be excluded"


def test_zabbix_hosts_from_netbox_leave_out_the_sandbox() -> None:
    """Runs observability.yml's own linux_hosts loop over sample VMs: production VMs stay, the sandbox goes."""
    play = yaml.safe_load((PLAYS / "observability.yml").read_text())
    task = next(t for p in play for t in p.get("tasks", []) if t.get("name", "").startswith("Monitored hosts derived"))
    template = task["ansible.builtin.set_fact"]["linux_hosts"]
    env = jinja2.Environment(extensions=["jinja2.ext.do"])
    env.filters["ansible.utils.ipaddr"] = lambda value, _query: value.split("/")[0]
    out = env.from_string(template).render(
        nb_devices={"json": {"results": []}}, nb_vms={"json": {"results": SAMPLE_VMS}},
        zabbix={**OBS_ZABBIX, "extra_hosts": []},
    )
    names = {h["name"] for h in yaml.safe_load(out)}
    assert names == {"tools-01", "no-role"}, names


def test_s7_1_expects_no_sandbox_host() -> None:
    """Runs test-07's own expected_hosts VM filter over sample VMs, with NetBox replaced by the sample list."""
    text = (ROOT / "verify" / "test-07-observability.sh").read_text()
    body = text.split("expected_hosts() {", 1)[1].split("PY\n}", 1)[0].split("<<'PY'\n", 1)[1]
    vm_loop = body[body.index('for v in get("virtualization'):body.index("for e in obs")]
    rows: list = []
    exec(  # noqa: S102 - the script's own loop, run against fixed sample data
        vm_loop,
        {"get": lambda _path: SAMPLE_VMS, "obs": {**OBS_ZABBIX}, "rows": rows},
    )
    assert {r[0] for r in rows} == {"tools-01", "no-role"}, rows


def test_the_agent_play_skips_the_sandbox_groups() -> None:
    hosts = yaml.safe_load((PLAYS / "observability-hosts.yml").read_text())[0]["hosts"]
    for role in OBS_ZABBIX["excluded_vm_roles"]:
        assert f":!{role}" in hosts, f"observability-hosts.yml would install an agent on the unmonitored {role}"


def test_tofu_names_the_vm_from_the_variable() -> None:
    tf = (ROOT / "tofu" / "itential" / "itential.tf").read_text()
    assert 'resource "proxmox_virtual_environment_vm" "itential"' in tf, "the state address must not change"
    assert "name        = var.vm.name" in tf
    assert 'tags        = ["phase-5", "itential-dev"]' in tf
    assert "ADR 0063" in tf
    variables = (ROOT / "tofu" / "itential" / "variables.tf").read_text()
    assert f'name      = "{ITENTIAL["vm"]["name"]}"' in variables and ITENTIAL["vm"]["name"] == "itential-dev"


def test_verify_05_can_target_the_dev_stack_by_name() -> None:
    text = (ROOT / "verify" / "test-05-itential.sh").read_text()
    assert "IT_HOST=${IT_HOST:-itential.lab.internal}" in text, "production stays the default"
    assert "MCP_HOST=${MCP_HOST:-mcp.lab.internal}" in text
    assert "RESOLVE=${IT_IP:+--resolve ${IT_HOST}:443:${IT_IP}}" in text
    assert 'ONLY="S4.1 S4.7"' in text.split("set -uo pipefail")[0], "the header says what is meaningful against dev"


def test_env_example_declares_the_dev_secrets() -> None:
    env = (ROOT / ".env.example").read_text()
    for key in ("ITENTIAL_DEV_ENCRYPTION_KEY", "NETBOX_DEV_RO_TOKEN", "CLAB_AUTOMATION_PASSWORD",
                "SVC_COPILOT_DEV_PASSWORD", "SVC_COPILOT_PROD_PASSWORD"):
        m = re.search(rf"^{key}=(\S*)", env, re.MULTILINE)
        assert m, f"{key} missing from .env.example"
        assert m.group(1) == "", f"{key} must be declared empty; the plays generate or mint it"


def _make_prerequisites(target: str) -> list[str]:
    """The prerequisites of one Makefile rule: the words between `target:` and the `##` help text."""
    line = next(ln for ln in (ROOT / "Makefile").read_text().splitlines() if ln.startswith(f"{target}:"))
    return line.split(":", 1)[1].split("##", 1)[0].split()


def _make_recipe(target: str) -> list[str]:
    """The recipe lines of one Makefile target (the tab-indented lines that follow it)."""
    lines = (ROOT / "Makefile").read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{target}:"))
    recipe = []
    for ln in lines[start + 1 :]:
        if not ln.startswith("\t"):
            break
        recipe.append(ln)
    return recipe


def test_make_dev_targets_run_every_platform_play_with_the_overlay_and_the_read_only_token() -> None:
    """The overlay and the token are the Makefile's half of the contract: a dev Platform play run without the
    overlay trips the guard assert, but one run with the full NETBOX_TOKEN would hand a dev prototype write
    access to production NetBox - nothing in the plays can tell the two tokens apart."""
    make = (ROOT / "Makefile").read_text()
    assert "DEV := -e @playbooks/vars/itential-dev.yml" in make
    assert "export NETBOX_TOKEN=$$NETBOX_DEV_RO_TOKEN" in make
    assert "NETBOX_DEV_RO_TOKEN missing" in make, "no silent fallback to the full token"
    for target in ("phase-itential", "phase-flowai"):
        for ln in _make_recipe(target):
            if any(p in ln for p in ("itential-host.yml", "itential.yml", "platform.yml", "flowai.yml")):
                assert "$(load_env_dev)" in ln and "$(DEV)" in ln, f"{target}: {ln.strip()}"
            assert "itential-prod.yml" not in ln and "platform_target" not in ln, f"{target}: {ln.strip()}"


def test_make_dev_targets_never_run_the_production_verify_suite() -> None:
    """verify/run.sh targets production and its S4.4 writes NetBox and EVE-NG devices; the dev tier verifies with
    verify-dev, which make verify never calls, so a torn-down dev stack cannot turn the lab's verify red."""
    for target in ("phase-itential", "phase-flowai", "clab-dev", "verify-dev", "netbox-token-dev"):
        for ln in _make_recipe(target):
            assert "verify/run.sh" not in ln, f"{target} runs the production verify suite"
    assert _make_recipe("verify") == ["\tverify/run.sh"], "make verify is run.sh's selection, nothing else"
    assert _make_recipe("verify-dev") == ["\tverify/test-12a-clab-dev.sh", "\tverify/test-05b-dev-copilot.sh"]


def _run_sh_selection(tmp_path: Path, names: list[str]) -> list[str]:
    """verify/run.sh --list, run on a copy beside empty stand-ins named `names`: the real selection code, no test run."""
    shutil.copy(ROOT / "verify" / "run.sh", tmp_path / "run.sh")
    for name in names:
        (tmp_path / name).write_text("exit 99\n")
    done = subprocess.run(["bash", str(tmp_path / "run.sh"), "--list"], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not (tmp_path / "results").exists(), "--list must run nothing and write nothing"
    return done.stdout.split()


def test_run_sh_never_selects_the_dev_tier(tmp_path: Path) -> None:
    """ADR 0063: make verify must not go red because the sandbox is torn down, so run.sh skips the dev scripts."""
    real = sorted(p.name for p in (ROOT / "verify").glob("test-*.sh"))
    extra = ["test-99-device.sh", "test-98x-devices-dev.sh", "test-97-dev-probe.sh"]
    selected = _run_sh_selection(tmp_path, real + extra)
    dev_tier = [ln.strip().removeprefix("verify/") for ln in _make_recipe("verify-dev")]
    assert dev_tier and not set(dev_tier) & set(selected), f"run.sh selects dev scripts: {set(dev_tier) & set(selected)}"
    # every other real script is still selected: the rule excludes the dev tier and nothing else
    assert set(real) - set(dev_tier) <= set(selected)
    assert set(real) - set(selected) == set(dev_tier), "run.sh's exclusion is exactly make verify-dev's scripts"
    # `dev` is a whole word after the test number
    assert "test-99-device.sh" in selected
    assert "test-98x-devices-dev.sh" not in selected and "test-97-dev-probe.sh" not in selected


def test_run_sh_still_fails_loud_with_nothing_to_run(tmp_path: Path) -> None:
    shutil.copy(ROOT / "verify" / "run.sh", tmp_path / "run.sh")
    (tmp_path / "test-05b-dev-copilot.sh").write_text("exit 99\n")
    done = subprocess.run(["bash", str(tmp_path / "run.sh"), "--list"], capture_output=True, text=True, timeout=30)
    assert done.returncode == 1 and "no verify/test-*.sh found" in done.stdout


def test_phase_itential_brings_the_token_and_the_topology_first() -> None:
    """make up runs phase-itential, whose dev plays need NETBOX_DEV_RO_TOKEN (load_env_dev) and the clab nodes."""
    prerequisites = _make_prerequisites("phase-itential")
    assert prerequisites == ["netbox-token-dev", "clab-dev"], prerequisites
    assert " ".join(_make_prerequisites("up")) == "$(addprefix phase-,$(PHASES))"
    assert "itential" in (ROOT / "Makefile").read_text().split("PHASES := ", 1)[1].split("\n", 1)[0].split()


def test_clab_nodes_give_gateway5_time_for_a_slow_banner_and_login() -> None:
    """S4.3 failed on itential-dev with "Error reading SSH protocol banner" (2026-09-16): the first banner from a
    clab node took 14.6-15.5 s (netmiko's default is 15 s) and a vEOS login took up to 95 s."""
    clab = yaml.safe_load((ROOT / "clab" / "versions.yaml").read_text())
    netmiko = clab["gateway_driver_options"]["netmiko"]
    assert netmiko["banner_timeout"] >= 60 and netmiko["auth_timeout"] >= 120 and netmiko["conn_timeout"] >= 10
    tasks = yaml.safe_load((TASKS / "platform-assets.yml").read_text())
    by_name = {t["name"]: t for t in tasks}
    body = by_name["Inventory nodes from the Containerlab oracle (dev only)"]["ansible.builtin.set_fact"]["inv_nodes"]
    assert "'itential_driver_options': clab.gateway_driver_options" in body


def test_the_inventory_is_replaced_when_driver_options_differ_and_production_still_compares_by_name() -> None:
    task = next(t for t in yaml.safe_load((TASKS / "platform-assets.yml").read_text())
                if t["name"] == "Inventory nodes populated from NetBox (replaces the set when it differs)")
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["zip"] = lambda a, b: list(zip(a, b))
    v = task["vars"]

    def replaces(existing: list[dict], wanted: list[dict]) -> bool:
        have = env.from_string(v["have"]).render(have_nodes=existing)
        want = env.from_string(v["want"]).render(inv_nodes=wanted)
        return have != want

    opts = {"netmiko": {"conn_timeout": 30, "banner_timeout": 150, "auth_timeout": 150}}
    prod = [{"name": "br1-sw01", "attributes": {"itential_host": "10.1.1.1"}}, {"name": "br1-rtr01", "attributes": {}}]
    assert not replaces(prod, list(reversed(prod))), "production: same names, no options -> untouched"
    assert replaces(prod, prod[:1]), "a node removed from NetBox still replaces the set"
    old_dev = [{"name": "clab-sw1", "attributes": {"itential_host": "10.100.2.21"}}]
    new_dev = [{"name": "clab-sw1", "attributes": {"itential_host": "10.100.2.21", "itential_driver_options": opts}}]
    assert replaces(old_dev, new_dev), "an existing dev inventory picks up the driver options"
    assert not replaces(new_dev, new_dev), "and is left alone once it has them"
    assert task["when"] == "have != want"
