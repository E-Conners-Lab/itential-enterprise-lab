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
    # as IOS XE reads a line: abbreviated, or behind `no` / `default` (code review 2026-10-23)
    "usern evil priv 15 secret x", "user evil", "cry key zer rsa", "enable sec x", "en pass x", "wr", "w mem",
    "conf rep bootflash:x force", "rel", "del bootflash:baseline.cfg", "no username automation", "no usern automation",
    "default username restconf-reader", "priv exec level 1 configure terminal", "kron policy-list x", "arch",
    # leaving configuration mode: the lines after it would run as exec commands
    "end", "exit", " ex", "EXIT",
    # an alias stands for any command
    "alias configure x interface", "alias exec wm show running-config",
    # the management path a restore needs: GigabitEthernet1 in VRF MGMT, its protocols and its logins (owner 2026-10-23)
    "interface GigabitEthernet1", "int gi1", "int g1", "interface Gig 1", "default interface GigabitEthernet1",
    "int gi01", "interface GigabitEthernet 001",
    "interface GigabitEthernet1.100", "interface range gi1 - 3", "int ran g2-3,gi1", "no vrf definition MGMT",
    "ip route vrf MGMT 0.0.0.0 0.0.0.0 Null0", "line vty 0 4", "lin con 0", "no aaa new-model",
    "aaa authentication login default none", "login block-for 600 attempts 1 within 60", "no restconf",
    "no netconf-yang", "no ip http secure-server", "ip http access-class 99", "no ip h secure-server", "ip ssh version 1",
    "ip ssh server algorithm encryption aes128-ctr", "no crypto pki trustpoint TP-self-signed-1", "crypto ca trustpoint X",
    "control-plane", "no ip routing",
    # `default` abbreviated: IOS XE reads `defa line vty 0 4` as `default line vty 0 4`; `d` and `de` stay `do` and
    # `delete` as well
    "defa line vty 0 4", "def aaa new-model", "defau no ip routing", "d sh run", "de bootflash:baseline.cfg", "no d",
])
def test_a_forbidden_line_is_refused_before_any_card(line: str) -> None:
    plan = wf.scratch_push_plan({"device": "skl-r1", "reason": "r", "lines": ["router ospf 1", line]},
                                SKILLS["forbidden_lines"], SKILLS["forbidden_commands"])
    assert plan["ok"] is False and plan["refused"] == [line] and "card" not in plan and "config" not in plan


def test_allowed_lines_are_joined_for_send_config_and_shown_on_the_card() -> None:
    lines = ["router ospf 1", "network 10.0.12.0 0.0.0.3 area 0", "interface GigabitEthernet2", " ip ospf cost 10",
             "no shutdown", "passive-interface default", "default-information originate", "exit-address-family",
             "ip ospf 1 area 0", "logging buffered 64000", "enable", "no ip ospf cost", "def interface GigabitEthernet2"]
    plan = wf.scratch_push_plan({"device": "skl-r2", "reason": "lab", "lines": lines}, SKILLS["forbidden_lines"],
                                SKILLS["forbidden_commands"])
    assert plan == {"ok": True, "refused": [], "config": "\n".join(lines),
                    "card": {"device": "skl-r2", "reason": "lab", "lines": lines}}


@pytest.mark.parametrize("line", [
    "interface GigabitEthernet2", "interface GigabitEthernet10", " ip address 10.1.12.1 255.255.255.252",
    "net 49.0001.0000.0000.0001.00", "router isis", " ip router isis", "ip route 10.9.9.0 255.255.255.0 Null0",
    "vrf definition RED", " rd 65000:1", " ip vrf forwarding RED", " channel-group 1 mode active",
    " glbp 1 ip 10.1.1.254", " standby 1 ip 10.1.1.254", "ip prefix-list P seq 5 permit 10.0.0.0/8 ge 16 le 24",
    " log-adjacency-changes", "router bgp 65001", " neighbor 10.1.12.2 remote-as 65002", " encapsulation dot1Q 10",
    "ip sla 1", "crypto isakmp policy 10", "crypto call admission limit ike sa 10", "ip host r2 10.1.12.2",
    " ip helper-address 10.1.1.1", " ip policy route-map PBR",
])
def test_a_lab_line_beside_the_management_words_is_taken(line: str) -> None:
    """The management refusals leave the CCNP topics a skill teaches: IS-IS's net, FHRPs, port channels, VRF-lite,
    static routes in the global table, IPsec, SLA, the other interfaces."""
    plan = wf.scratch_push_plan({"device": "skl-r3", "reason": "r", "lines": [line]}, SKILLS["forbidden_lines"],
                                SKILLS["forbidden_commands"])
    assert plan["ok"] is True, plan["refused"]


def test_the_runner_code_carries_the_forbidden_set_from_versions() -> None:
    namespace: dict = {}
    code = wf.SCRATCH_PUSH_PLAN_CODE.rsplit("\nprint(", 1)[0]
    exec(compile(code, "plan", "exec"), namespace)
    assert namespace["FORBIDDEN"] == SKILLS["forbidden_lines"]
    assert namespace["COMMANDS"] == SKILLS["forbidden_commands"]


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


def test_the_push_opens_its_job_to_the_approvers_on_every_path_to_its_card() -> None:
    """6.6.0 Work Center shows a card only to its job's groups (the automation's gbac read + write), and either gbac
    list would let eval-approvers start the push (measured on production 2026-10-23): the push adds the group to its
    own job, and the card cannot be reached any other way."""
    doc = _doc("scratch_push")
    add = doc["tasks"]["2d"]
    assert (add["app"], add["name"], add.get("groups", [])) == ("OperationsManager", "addJobGroup", [])
    assert add["variables"]["incoming"] == {
        "jobId": "$var.2f.return_data", "groupId": f"__GROUP_ID:{VERSIONS['eval_access']['approve']['group']}__"}
    # the job's own id, through merge's task/variable form (see the test below)
    merge, query = doc["tasks"]["2e"], doc["tasks"]["2f"]
    assert merge["type"] == "automatic"  # an operation merge makes `_id` a required tool input
    assert merge["variables"]["incoming"] == {
        "data_to_merge": [{"key": "jobId", "value": {"task": "job", "variable": "_id"}}]}
    assert query["variables"]["incoming"]["obj"] == "$var.2e.merged_object"
    assert query["variables"]["incoming"]["query"] == "jobId"
    assert add["variables"]["outgoing"] == {"result": None}  # an unmapped output refuses the job's start (HTTP 400)
    edges = doc["transitions"]
    reached, todo = set(), ["workflow_start"]
    while todo:  # walk the workflow without 2d: neither the card nor its helpers may be reached
        n = todo.pop()
        if n not in reached and n != "2d":
            reached.add(n)
            todo.extend(edges.get(n, {}))
    assert not {"2c", "2c1"} & reached
    assert all(edges[t]["5d"]["state"] == "error" for t in ("2f", "2d")) and list(edges["5d"]) == ["workflow_end"]
    assert query["variables"]["incoming"]["pass_on_null"] is False  # a missing id fails into 5d, never a null jobId
    assert "nothing was sent" in json.dumps(doc["tasks"]["5d"])
    # a forbidden line ends the job before the group is added
    assert "5c" in edges["1e"] and edges["1e"]["5c"]["state"] == "failure"


def _job_refs(value) -> set:
    if isinstance(value, str):
        return set(re.findall(r"^\$var\.job\.(\w+)", value))
    if isinstance(value, dict):
        return set().union(*map(_job_refs, value.values()))
    if isinstance(value, list):
        return set().union(*map(_job_refs, value))
    return set()


def _object_job_refs(task: dict) -> set:
    """`{task: "job", variable: X}` in an operation task's incoming: the import infers these as well, and leaves an
    automatic task's out (measured on 6.6.0, 2026-10-24, with the push's merge)."""
    found: set = set()

    def walk(value) -> None:
        if isinstance(value, dict):
            if value.get("task") == "job" and isinstance(value.get("variable"), str):
                found.add(value["variable"])
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    if task.get("type") == "operation":
        walk(task.get("variables", {}).get("incoming", {}))
    return found


AGENT_WORKFLOW_TOOLS = sorted({t["reference"] for p in (ROOT / "itential" / "agents").glob("*.yaml")
                               for t in yaml.safe_load(p.read_text())["tools"] if t.get("kind") == "workflow"})


@pytest.mark.parametrize("name", AGENT_WORKFLOW_TOOLS)
def test_an_agent_tool_asks_only_for_its_declared_inputs(name: str) -> None:
    """The import infers a workflow's stored inputSchema, and so its FlowAI tool's, from the tasks: a `$var.job.X` that a
    task reads and no task writes becomes a required input (measured on 6.6.0, 2026-10-24: the push's `$var.job._id`
    was; the job variables Verify AWS VPN writes and reads back were not). One that is not a declared input is an input
    the agent would have to invent, as `_id` was for the push."""
    doc = json.loads((WORKFLOWS / wf.file_name(name)).read_text())
    variables = [t.get("variables", {}) for t in doc["tasks"].values()]
    read = set().union(*(_job_refs(v.get("incoming", {})) for v in variables),
                       *(_object_job_refs(t) for t in doc["tasks"].values()))
    written = set().union(*(_job_refs(v.get("outgoing", {})) for v in variables))
    assert read - written <= set(doc["inputSchema"]["properties"]), (name, read - written)


def test_the_import_fills_the_group_markers_and_refuses_one_it_cannot() -> None:
    text = (TASKS / "platform-assets.yml").read_text()
    assert "include_tasks: workflow-group-ids.yml" in text
    assert "regex_findall('__GROUP_ID:([A-Za-z0-9_-]+)__')" in text and "replace('__GROUP_ID:' ~ m ~ '__', wgi_ids[m])" in text
    names = [t["name"] for t in _tasks(TASKS / "platform-assets.yml")]
    assert names.index("Every card names groups this Platform has") < names.index("Import the workflows")


def test_a_workflow_with_a_grouped_card_is_created_not_bulk_imported() -> None:
    """6.6.0's bulk import refuses a manual task with groups (measured on production 2026-10-23); create takes it."""
    tasks = _tasks(TASKS / "platform-assets.yml")
    names = [t["name"] for t in tasks]
    imp = tasks[names.index("Import the workflows")]
    create = tasks[names.index("Create each workflow whose cards name groups")]
    assert imp["ansible.builtin.uri"]["body"]["automations"] == "{{ wf_bodies | rejectattr('name', 'in', wf_grouped) | list }}"
    assert create["ansible.builtin.uri"]["url"].endswith("/automation-studio/automations")
    assert create["loop"] == "{{ wf_bodies | selectattr('name', 'in', wf_grouped) | list }}"
    assert names.index("Workflows whose cards name groups") < names.index("Import the workflows") < names.index(
        "Create each workflow whose cards name groups")


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
    for check in ("accounts", "inventory", "gbac", "forbidden", "restconf", "hold", "restore", "agents", "profile",
                  "project"):
        assert re.search(rf"^    {check}\s", text, re.M), f"the docstring does not list {check}"
    assert 'ACCESS["run"]["password_env"]' in text and 'ACCESS["approve"]["password_env"]' in text
    # the outcome on the router is read directly, as judge, on a pinned connection - never through the Platform
    assert 'presented == node["cert_sha256"]' in text and "judge:{os.environ['SKILLS_LAB_JUDGE_PASSWORD']}" in text
    # cards are released the way the runner releases them: Work Center claim, then complete
    assert "/claim" in text and "/complete" in text and "/tasks/" not in text
    assert 'choices=["prod"]' in text, "production only"
    # each scored session is deleted as the runner deletes it, and its absence read back as the administrator
    assert 'evl.req("DELETE", f"/agent-session-manager/sessions/{sid}")' in text
    assert 'adm.req("GET", f"/agent-session-manager/sessions/{sid}")' in text


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
        # the GigabitEthernet1 pattern spells out every abbreviation; the instructions name the interface
        words = (["gigabitethernet1"] if "(?:i(?:g" in pattern
                 else re.findall(r"[a-z]+", pattern.replace("\\b", " ").replace("\\s", " ")))
        assert " ".join(words) in text, f"{pattern}: not named in the instructions"
    for command in SKILLS["forbidden_commands"]:
        assert re.search(rf"\b{' '.join(command)}\b", text), f"{command}: not named in the instructions"
    assert "never ask for or repeat a password, a key or a vault path" in text


def test_the_profile_is_new_and_the_meter_leaves_the_pair_out() -> None:
    profiles = {p["name"]: p for p in VERSIONS["llm"]["profiles"]}
    assert profiles["anthropic-sonnet-5-5"] == {"name": "anthropic-sonnet-5-5", "provider": "anthropic",
                                                "model": "claude-sonnet-5-5", "env_key": "ANTHROPIC_API_KEY",
                                                "model_variables": {"thinking": {"type": "between_tools"}}}
    assert profiles["anthropic"]["model"] == "claude-sonnet-5", "the existing profile is not edited"
    assert VERSIONS["llm"]["budget"]["exclude_agents"] == SKILLS["agents"]
    meter = (ROOT / "verify" / "tokens.sh").read_text()
    assert 'budget.get("exclude_agents")' in meter and '(s.get("agentSnapshot") or {}).get("name") in excluded' in meter


def test_the_pair_has_a_project_of_its_own_that_only_production_makes_and_no_group_edits() -> None:
    assert SKILLS["project"] != VERSIONS["agents"]["project"]
    in_project = {p.stem for p in AGENTS.glob("*.yaml") if _agent(p.stem)["project"] == SKILLS["project"]}
    assert in_project == set(SKILLS["agents"])
    assets = _tasks(TASKS / "flowai-assets.yml")
    create = next(t for t in assets if t["name"] == "The skills lab's agent project, production only: "
                                                     "{{ skills_lab.project }}")
    assert "not (dev_overlay | default(false) | bool)" in create["when"]
    guard = next(t for t in assets if t["name"].startswith("The skills lab's agent project exists once"))
    assert "selectattr('type', 'equalto', 'group')" in json.dumps(guard) and "dev_overlay" in guard["when"]
    assert assets.index(create) < assets.index(guard)
    # the MCP server's group is made an editor of agents.project alone
    assert "skills_lab.project" not in (TASKS / "mcp-access.yml").read_text()


def test_no_play_gives_the_pair_to_the_mcp_account_or_builds_it_on_dev() -> None:
    agent = (TASKS / "flowai-agent.yml").read_text()
    assert "or agent.name in skills_lab.agents else [mcp_access.group]" in agent
    assets = (TASKS / "flowai-assets.yml").read_text()
    assert "(skills_lab.agents if dev_overlay | default(false) | bool else [])" in assets
    mcp = next(t for t in _tasks(TASKS / "mcp-access.yml") if t["name"] == "MCP access - the group operates the plays' agents")
    assert "item.name not in skills_lab.agents" in mcp["when"]
    # ... and the proof after it does not count the pair as missing the group (else every replay after the first fails)
    proof = next(t for t in _tasks(TASKS / "mcp-access.yml") if t["name"].startswith("MCP access - the group is on"))
    assert "if a.name not in skills_lab.agents" in proof["vars"]["agent_missing"]
