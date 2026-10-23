"""ADR 0084's governed path to the scratch routers: the four workflows, their gates, the forbidden lines, the card's
groups, the restore's proof, the skills-lab inventory and who may start what. The generated documents are read as the
play imports them; the runner functions are called directly. No lab access."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = yaml.safe_load((ROOT / "itential" / "versions.yaml").read_text())
SKILLS = VERSIONS["skills_lab"]
WORKFLOWS = ROOT / "itential" / "workflows"
TASKS = ROOT / "ansible" / "playbooks" / "tasks"
PLAYS = ROOT / "ansible" / "playbooks"
KEYS = ["scratch_show", "scratch_restconf", "scratch_push", "scratch_restore"]
NAMES = {k: VERSIONS["workflows"][k] for k in KEYS}
# cloud-devops-pipeline itential/restconf-get.py ALLOWLIST at the pinned commit, written here independently
CDP_ALLOWLIST = [
    "/restconf/data/Cisco-IOS-XE-ospf-oper:ospf-oper-data",
    "/restconf/data/Cisco-IOS-XE-interfaces-oper:interfaces",
    "/restconf/data/Cisco-IOS-XE-native:native/router/Cisco-IOS-XE-ospf:router-ospf",
    "/restconf/data/Cisco-IOS-XE-native:native/interface",
]

_spec = importlib.util.spec_from_file_location("workflow_build", WORKFLOWS / "build.py")
wf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wf)


def _doc(key: str) -> dict:
    return json.loads((WORKFLOWS / wf.file_name(NAMES[key])).read_text())


def _gate(key: str) -> dict:
    return _doc(key)["tasks"]["9a0a"]["variables"]["incoming"]["schema"]


def _tasks(path: Path) -> list[dict]:
    doc = yaml.safe_load(path.read_text())
    if doc and "hosts" in doc[0]:
        return [t for play in doc for t in play.get("tasks", [])]
    return doc


# --- names, endpoints, who may start them ----------------------------------------------------------------------------


def test_the_four_keys_are_the_skills_labs_and_each_has_its_endpoint() -> None:
    assert SKILLS["workflows"] == KEYS
    routes = {}
    for key in KEYS:
        om = VERSIONS["operations_manager"][key]
        assert om["automation"] == NAMES[key]
        assert om["endpoint"]["name"].endswith("from the eval runner")
        assert "manual" not in om and "schedule" not in om, "no form, no schedule: only the runner starts these"
        routes[key] = om["endpoint"]["route"]
    assert routes == {"scratch_show": "skills-lab-show", "scratch_restconf": "skills-lab-restconf",
                      "scratch_push": "skills-lab-push", "scratch_restore": "skills-lab-restore"}
    others = [om.get("endpoint", {}).get("route") for k, om in VERSIONS["operations_manager"].items() if k not in KEYS]
    assert not set(routes.values()) & set(others)


def test_only_the_eval_groups_start_them_and_the_restore_is_the_runners_alone() -> None:
    access = VERSIONS["automation_access"]
    assert access["eval"] == [NAMES["scratch_show"], NAMES["scratch_restconf"], NAMES["scratch_push"]]
    assert access["eval_runner"] == [NAMES["scratch_restore"]]
    for name in access["eval"] + access["eval_runner"]:
        assert name not in access["netclaw"] and name not in access["unmanaged"]
    text = (TASKS / "automation-access.yml").read_text()
    assert 'eval: [admin_group, "{{ eval_access.run.group }}"]' in text
    assert 'eval_runner: [admin_group, "{{ eval_access.approve.group }}"]' in text
    # the PATCH and the final assertion both pick the eval set before the tier's read groups
    assert len(re.findall(r"\bitem\.name in automation_access\.eval\b", text)) == 1
    assert len(re.findall(r"\ba\.name in automation_access\.eval\b", text)) == 1


def test_the_endpoints_and_the_imports_are_production_only() -> None:
    platform = _tasks(PLAYS / "platform.yml")
    trig = next(t for t in platform if t.get("loop") == "{{ skills_lab.workflows }}")
    assert trig["ansible.builtin.include_tasks"] == "tasks/outage-trigger.yml"
    assert trig["when"] == "not (dev_overlay | default(false) | bool)"
    assets = _tasks(TASKS / "platform-assets.yml")
    skip = next(t for t in assets if t["name"] == "The dev tier leaves out the skills lab's workflows")
    assert skip["when"] == "dev_overlay | default(false) | bool"
    files = [wf.file_name(n).removesuffix(".json") for n in NAMES.values()]
    pattern = "/(" + "|".join(files) + ")[.]json$"
    assert all(re.search(pattern, f"/x/{wf.file_name(n)}") for n in NAMES.values())
    assert not re.search(pattern, "/x/" + wf.file_name(VERSIONS["workflows"]["show_command"]))


# --- the gates -------------------------------------------------------------------------------------------------------


def test_every_gate_names_only_the_scratch_routers() -> None:
    for key in KEYS:
        gate = _gate(key)
        assert gate["properties"]["device"] == {"type": "string", "enum": sorted(SKILLS["nodes"])}, key
        assert gate["additionalProperties"] is False and "device" in gate["required"]


def test_the_allowlist_is_restconf_gets_own() -> None:
    assert SKILLS["restconf_allowlist"] == CDP_ALLOWLIST


@pytest.mark.parametrize("path", [
    *CDP_ALLOWLIST,
    "/restconf/data/Cisco-IOS-XE-interfaces-oper:interfaces/interface=GigabitEthernet2",
    "/restconf/data/Cisco-IOS-XE-native:native/interface/GigabitEthernet=0%2F0%2F1",
    "/restconf/data/Cisco-IOS-XE-ospf-oper:ospf-oper-data/ospf-state",
])
def test_the_restconf_gate_takes_an_allowlisted_path(path: str) -> None:
    pattern = _gate("scratch_restconf")["properties"]["path"]["pattern"]
    assert re.search(pattern, path)


@pytest.mark.parametrize("path", [
    "/restconf/data/Cisco-IOS-XE-native:native/username",  # the accounts: never through the Platform
    "/restconf/data/Cisco-IOS-XE-native:native",
    "/restconf/data/Cisco-IOS-XE-native:native/interfaces",  # a prefix may not run on into another node name
    "/restconf/data/Cisco-IOS-XE-native:native/interface/../username",
    "/restconf/data/Cisco-IOS-XE-native:native/interface/%2e%2e/username",
    "/restconf/data/Cisco-IOS-XE-native:native/interface/%252e",
    "/restconf/data/Cisco-IOS-XE-native:native/interface?depth=1",
    "/restconf/data/Cisco-IOS-XE-native:native/interface#x",
    "/restconf/data/Cisco-IOS-XE-native:native/interface/a b",
])
def test_the_restconf_gate_refuses_anything_else(path: str) -> None:
    pattern = _gate("scratch_restconf")["properties"]["path"]["pattern"]
    assert not re.search(pattern, path)


def test_the_gate_patterns_are_valid_javascript_in_unicode_mode() -> None:
    """The Platform checks patterns as JavaScript regular expressions in unicode mode, where an identity escape of a
    character that is not special (Python's re.escape gives `\\-` and `\\:`) is a syntax error."""
    for key in KEYS:
        for prop in _gate(key)["properties"].values():
            for pattern in filter(None, [prop.get("pattern"), prop.get("items", {}).get("pattern")]):
                assert not re.search(r"\\[-:/,=_A-Za-z0-9]", pattern.replace(r"\x", "").replace(r"\.", "")), pattern


@pytest.mark.parametrize("lines,ok", [
    (["router ospf 1", "network 10.0.12.0 0.0.0.3 area 0"], True),
    (["interface GigabitEthernet2", " ip ospf cost 10"], True),
    ([], False),
    (["router ospf 1\nwrite memory"], False),  # a second command smuggled in one item
    (["router ospf 1\rreload"], False),
    (["x" * 201], False),
    (["line"] * 51, False),
])
def test_the_push_gate_takes_a_list_of_single_lines(lines: list, ok: bool) -> None:
    schema = _gate("scratch_push")["properties"]["lines"]
    good = (schema["minItems"] <= len(lines) <= schema["maxItems"]
            and all(len(x) <= schema["items"]["maxLength"] and re.search(schema["items"]["pattern"], x) for x in lines))
    assert good == ok


# --- the push: forbidden lines, the card, never saved ----------------------------------------------------------------


@pytest.mark.parametrize("line", [
    "write memory", "do copy running-config startup-config", "do show run", "reload in 5",
    "configure replace bootflash:baseline.cfg force", "archive", "crypto key generate rsa modulus 2048",
    "username eve privilege 15 secret x", "enable secret x", "copy running-config startup-config",
    "delete bootflash:baseline.cfg", "erase nvram:", "format bootflash:", "boot system bootflash:x.bin",
    "license boot level network-advantage", "event manager applet X", "tclsh", "  DO wr",
])
def test_a_forbidden_line_is_refused_before_any_card(line: str) -> None:
    plan = wf.scratch_push_plan({"device": "skl-r1", "reason": "r", "lines": ["router ospf 1", line]},
                                SKILLS["forbidden_lines"])
    assert plan["ok"] is False and plan["refused"] == [line] and "card" not in plan and "config" not in plan


def test_allowed_lines_are_joined_for_send_config_and_shown_on_the_card() -> None:
    lines = ["router ospf 1", "network 10.0.12.0 0.0.0.3 area 0", "interface GigabitEthernet2", " ip ospf cost 10"]
    plan = wf.scratch_push_plan({"device": "skl-r2", "reason": "lab", "lines": lines}, SKILLS["forbidden_lines"])
    assert plan == {"ok": True, "refused": [], "config": "\n".join(lines),
                    "card": {"device": "skl-r2", "reason": "lab", "lines": lines}}


def test_the_runner_code_carries_the_forbidden_set_from_versions() -> None:
    namespace: dict = {}
    code = wf.SCRATCH_PUSH_PLAN_CODE.rsplit("\nprint(", 1)[0]
    exec(compile(code, "plan", "exec"), namespace)
    assert namespace["FORBIDDEN"] == SKILLS["forbidden_lines"]


def test_the_card_names_eval_approvers_and_admin_group_and_nothing_else_has_groups() -> None:
    doc = _doc("scratch_push")
    cards = {tid: t for tid, t in doc["tasks"].items() if t.get("name") == "InteractiveHTML"}
    assert list(cards) == ["2c"]
    assert cards["2c"]["groups"] == [f"__GROUP_ID:{VERSIONS['eval_access']['approve']['group']}__",
                                     "__GROUP_ID:admin_group__"]
    for key in KEYS:
        for tid, task in _doc(key)["tasks"].items():
            if tid != "2c" or key != "scratch_push":
                assert task.get("groups", []) == [], (key, tid)


def test_the_import_fills_the_group_markers_and_refuses_one_it_cannot() -> None:
    text = (TASKS / "platform-assets.yml").read_text()
    assert "include_tasks: workflow-group-ids.yml" in text
    assert "regex_findall('__GROUP_ID:([A-Za-z0-9_-]+)__')" in text and "replace('__GROUP_ID:' ~ m ~ '__', wgi_ids[m])" in text
    names = [t["name"] for t in _tasks(TASKS / "platform-assets.yml")]
    assert names.index("Every card names groups this Platform has") < names.index("Import the workflows")


def test_nothing_on_the_scratch_path_is_ever_saved() -> None:
    for key in KEYS:
        text = json.dumps(_doc(key))
        assert "write memory" not in text and "copy running-config startup-config" not in text, key
        for task in _doc(key)["tasks"].values():
            if task.get("name") == "sendCommand":
                commands = task["variables"]["incoming"]["commands"]
                assert not isinstance(commands, list) or not any(c.startswith(("write", "copy")) for c in commands)


def test_every_gateway_task_uses_the_skills_lab_inventory_and_never_the_lab() -> None:
    for key in ("scratch_show", "scratch_push", "scratch_restore"):
        doc = _doc(key)
        selector = doc["tasks"]["0a"]["variables"]["incoming"]["str"]
        assert json.loads(selector.replace("__DEVICE__", "x")) == [{"inventory": SKILLS["inventory"], "nodeNames": ["x"]}]
        for task in doc["tasks"].values():
            if task.get("name") in ("sendCommand", "sendConfig"):
                assert task["variables"]["incoming"]["inventory"] == "$var.0b.textObject"
    assert '"inventory": "lab"' not in json.dumps([_doc(k) for k in KEYS])


# --- the RESTCONF read -----------------------------------------------------------------------------------------------


def test_the_read_passes_restconf_get_exactly_its_target_and_the_reader() -> None:
    out = wf.restconf_params({"device": "skl-r3", "path": CDP_ALLOWLIST[0]}, SKILLS["nodes"], "restconf-reader", "60")
    params = out["params"]
    assert set(params) == {"target_json", "path", "username", "timeout"}
    assert json.loads(params["target_json"]) == {"name": "skl-r3", **SKILLS["nodes"]["skl-r3"]}
    assert params["username"] == "restconf-reader" and params["path"] == CDP_ALLOWLIST[0]
    with pytest.raises(KeyError):
        wf.restconf_params({"device": "dc1-wan01", "path": CDP_ALLOWLIST[0]}, SKILLS["nodes"], "restconf-reader", "60")
    services = {s["name"] for s in VERSIONS["terraform_run"]["services"]}
    runs = [t for t in _doc("scratch_restconf")["tasks"].values() if t.get("name") == "runService"]
    assert [t["variables"]["incoming"]["serviceName"] for t in runs] == ["restconf-get"] and "restconf-get" in services


# --- the restore -----------------------------------------------------------------------------------------------------

BASE = "!\n! Last configuration change at 10:00:00 UTC Thu Oct 22 2026 by automation\n!\nversion 17.13\nhostname skl-r1\n!\nend\n"
RUNNING = ("Building configuration...\n\nCurrent configuration : 4242 bytes\n!\n"
           "! Last configuration change at 11:11:11 UTC Fri Oct 23 2026 by automation\n!\nversion 17.13\nhostname skl-r1\n"
           "!\nend\n")


def _two(baseline: str, running: str, ok: bool = True) -> dict:
    return {"result": {"results": [{"success": ok, "output": baseline}, {"success": ok, "output": running}]}}


def test_a_router_equal_to_its_baseline_is_restored_and_only_hashes_leave() -> None:
    out = wf.restore_proof(_two(BASE, RUNNING), wf.RESTORE_VOLATILE)
    assert out["restored"] is True and out["baseline_sha256"] == out["running_sha256"] and out["error"] == ""
    assert "hostname" not in json.dumps(out), "never the configuration"


def test_a_router_that_differs_is_not_restored_and_says_how_much() -> None:
    out = wf.restore_proof(_two(BASE, RUNNING.replace("hostname skl-r1", "hostname skl-r1\nrouter ospf 1")),
                           wf.RESTORE_VOLATILE)
    assert out["restored"] is False and "1 line(s) only in the running configuration" in out["error"]
    assert "router ospf" not in json.dumps(out)


@pytest.mark.parametrize("data", [_two("", RUNNING), _two(BASE, RUNNING, ok=False), {"result": {"results": []}}, {}])
def test_an_empty_or_unread_baseline_is_never_restored(data: dict) -> None:
    out = wf.restore_proof(data, wf.RESTORE_VOLATILE)
    assert out["restored"] is False and out["error"]


@pytest.mark.parametrize("output,done", [
    ("Total number of passes: 1\nRollback Done\n\nskl-r1#", True),
    ("%Error opening bootflash:baseline.cfg (No such file or directory)", False),
    ("", False),
])
def test_the_replace_must_say_rollback_done(output: str, done: bool) -> None:
    out = wf.replace_done({"result": {"results": [{"success": True, "output": output}]}})
    assert out["done"] is done and ("Rollback Done" not in out["error"] or not done)
    assert wf.replace_done({"result": {"results": [{"success": False, "output": "Rollback Done"}]}})["done"] is False


def test_the_restore_is_never_an_agent_tool() -> None:
    for path in (ROOT / "itential" / "agents").glob("*.yaml"):
        tools = [t["reference"] for t in yaml.safe_load(path.read_text())["tools"]]
        assert NAMES["scratch_restore"] not in tools, path.name


# --- the inventory ---------------------------------------------------------------------------------------------------


def test_the_inventory_is_made_once_with_the_eval_groups_from_versions_only() -> None:
    assert SKILLS["inventory"] == "skills-lab"
    assert SKILLS["inventory_groups"] == ["admin_group", VERSIONS["eval_access"]["run"]["group"],
                                          VERSIONS["eval_access"]["approve"]["group"]]
    assets = _tasks(TASKS / "platform-assets.yml")
    mine = [t for t in assets if "skills lab" in t["name"].lower() or "skills_lab.inventory" in json.dumps(t)]
    assert len(mine) >= 6
    for task in mine:
        if task["name"] == "The dev tier leaves out the skills lab's workflows":
            continue
        when = task.get("when")
        whens = when if isinstance(when, list) else [when]
        assert "not (dev_overlay | default(false) | bool)" in whens, task["name"]
    bulk = next(t for t in mine if t["name"].startswith("Skills lab nodes populated"))
    body = bulk["ansible.builtin.uri"]["body"]
    assert body == {"inventory_identifier": "{{ skills_lab.inventory }}", "nodes": "{{ skl_nodes }}"}
    nodes = next(t for t in mine if t["name"].startswith("Skills lab nodes (the scratch"))
    text = json.dumps(nodes)
    assert "$GATEWAYSECRET_(skills-lab-automation-password)" in text and "skills_lab.nodes.items()" in text
    assert "skills-lab-automation-password" in VERSIONS["vault"]["gateway_aliases"]


# --- the probes ------------------------------------------------------------------------------------------------------


def test_the_probes_cover_every_criterion_as_the_runners_accounts_and_read_the_router_as_judge() -> None:
    text = (ROOT / "verify" / "skillslabcheck.py").read_text()
    for check in ("accounts", "inventory", "gbac", "forbidden", "restconf", "hold", "restore", "agents", "profile"):
        assert re.search(rf"^    {check}\s", text, re.M), f"the docstring does not list {check}"
    assert 'ACCESS["run"]["password_env"]' in text and 'ACCESS["approve"]["password_env"]' in text
    # the outcome on the router is read directly, as judge, on a pinned connection - never through the Platform
    assert 'presented == node["cert_sha256"]' in text and "judge:{os.environ['SKILLS_LAB_JUDGE_PASSWORD']}" in text
    # cards are released the way the runner releases them: Work Center claim, then complete
    assert "/claim" in text and "/complete" in text and "/tasks/" not in text
    assert 'choices=["prod"]' in text, "production only"


# --- the agents and their profile (build item 7) ----------------------------------------------------------------------

AGENTS = ROOT / "itential" / "agents"
TOOLS = [NAMES["scratch_show"], NAMES["scratch_restconf"], NAMES["scratch_push"]]


def _agent(name: str) -> dict:
    return yaml.safe_load((AGENTS / f"{name}.yaml").read_text())


def test_the_pair_has_the_three_tools_the_same_generic_instructions_and_no_route() -> None:
    assert SKILLS["agents"] == ["skill-eval", "skill-eval-local"]
    claude, twin = (_agent(n) for n in SKILLS["agents"])
    assert (claude["profile"], twin["profile"]) == ("anthropic-sonnet-5-5", "ollama-mac")
    assert claude["instructions"] == twin["instructions"], "the suite compares models, not prompts"
    for doc in (claude, twin):
        assert [t["reference"] for t in doc["tools"]] == TOOLS and all(t["kind"] == "workflow" for t in doc["tools"])
        assert "expose_route" not in doc, "the runner starts sessions by agent id; no automation, no route"
        assert doc["operator_groups"] == [VERSIONS["eval_access"]["run"]["group"]]
        assert doc["input_schema"]["required"] == ["request"] and doc["instructions"].rstrip().endswith("{{request}}")


def test_the_instructions_name_every_allowlisted_path_and_every_forbidden_command() -> None:
    text = _agent("skill-eval")["instructions"].lower()
    for prefix in SKILLS["restconf_allowlist"]:
        assert prefix.lower() in text, prefix
    for pattern in SKILLS["forbidden_lines"]:
        words = re.findall(r"[a-z]+", pattern.replace("\\b", " ").replace("\\s", " "))
        assert " ".join(words) in text, f"{pattern}: not named in the instructions"
    assert "never ask for or repeat a password, a key or a vault path" in text


def test_the_profile_is_new_and_the_meter_leaves_the_pair_out() -> None:
    profiles = {p["name"]: p for p in VERSIONS["llm"]["profiles"]}
    assert profiles["anthropic-sonnet-5-5"] == {"name": "anthropic-sonnet-5-5", "provider": "anthropic",
                                                "model": "claude-sonnet-5-5", "env_key": "ANTHROPIC_API_KEY"}
    assert profiles["anthropic"]["model"] == "claude-sonnet-5", "the existing profile is not edited"
    assert VERSIONS["llm"]["budget"]["exclude_agents"] == SKILLS["agents"]
    meter = (ROOT / "verify" / "tokens.sh").read_text()
    assert 'budget.get("exclude_agents")' in meter and '(s.get("agentSnapshot") or {}).get("name") in excluded' in meter


def test_no_play_gives_the_pair_to_the_mcp_account_or_builds_it_on_dev() -> None:
    agent = (TASKS / "flowai-agent.yml").read_text()
    assert "or agent.name in skills_lab.agents else [mcp_access.group]" in agent
    assets = (TASKS / "flowai-assets.yml").read_text()
    assert "(skills_lab.agents if dev_overlay | default(false) | bool else [])" in assets
    mcp = next(t for t in _tasks(TASKS / "mcp-access.yml") if t["name"] == "MCP access - the group operates the plays' agents")
    assert "item.name not in skills_lab.agents" in mcp["when"]
