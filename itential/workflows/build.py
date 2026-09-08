#!/usr/bin/env python3
"""Generate the Phase 5 workflow documents (Platform 6, canvasVersion 3) into itential/workflows/.

The JSON files are what the play imports and what tests/test_itential.py checks; this generator
is the readable source (task graph, variable references) so the exported documents never have
to be hand-edited. Conventions learned from the platform's own workflowDocument.json schema and
the itentialopensource pre-built automations:
  - workflow inputs are `$var.job.<name>`; a task's output is `$var.<taskId>.<outgoing>`;
    writing an outgoing variable to `$var.job.<name>` sets a job variable (what the API returns)
  - automatic tasks carry actor Pronghorn; manual tasks carry a view and groups
  - transitions: {taskId: {nextId: {state: success|failure|error, type: standard}}}

  itential/workflows/build.py            # writes every workflow file
"""

from __future__ import annotations

import json

import yaml
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLUSTER = "lab"
INVENTORY = "lab"
VERSIONS = yaml.safe_load((HERE.parent / "versions.yaml").read_text())


def task(name: str, app: str, summary: str, incoming: dict, outgoing: dict, *, kind: str = "automatic",
         location: str = "Application", location_type: str | None = None, display: str | None = None,
         view: str | None = None, x: int = 0, y: int = 0) -> dict:
    t = {
        "name": name,
        "canvasName": name,
        "summary": summary,
        "description": summary,
        "location": location,
        "locationType": location_type,
        "app": app,
        "type": kind,
        "displayName": display or app,
        "groups": [],
        "nodeLocation": {"x": x, "y": y},
        "variables": {"incoming": incoming, "outgoing": outgoing, "error": "", "decorators": []},
    }
    if kind == "automatic":
        t["actor"] = "Pronghorn"
        t["scheduled"] = False
    if kind == "manual":
        t["view"] = view
    return t


def workflow(name: str, description: str, inputs: dict, tasks: dict, transitions: dict, outputs: dict | None = None) -> dict:
    tasks = dict(tasks)
    tasks["workflow_start"] = {"name": "workflow_start", "summary": "workflow_start", "groups": [], "nodeLocation": {"x": -600, "y": 0}}
    tasks["workflow_end"] = {"name": "workflow_end", "summary": "workflow_end", "groups": [], "nodeLocation": {"x": 1800, "y": 0}}
    transitions = dict(transitions)
    transitions.setdefault("workflow_end", {})
    return {
        "name": name,
        "type": "automation",
        "description": description,
        "tasks": tasks,
        "transitions": transitions,
        "groups": [],
        "canvasVersion": 3,
        "inputSchema": {"type": "object", "properties": inputs, "required": [k for k, v in inputs.items() if v.get("required")]},
        "outputSchema": {"type": "object", "properties": outputs or {}},
        "tags": [],
        "preAutomationTime": 300000,
        "sla": 600000,
        "font_size": 12,
        "decorators": [],
    }


SNOW_EXPORT = "Servicenow"      # adapter-servicenow pronghorn export
SNOW_INSTANCE_ID = "ServiceNow"  # the adapter instance the play creates
SNOW_TEMPLATE = "b1c8d15147810200e90d87e8dee490f7"  # PDI standard change template "Change VLAN on a Cisco switchport"
SNOW_GROUP = "287ebd7da9fe198100f92cc8d1d2154e"     # PDI assignment group "Network" (the change model requires one)


def snow_call(summary: str, method: str, path_ref: str, body, x: int, y: int = 0, query: dict | None = None) -> dict:
    """adapter-servicenow genericAdapterRequest: the adapter's own change methods return normalised or
    empty documents, the generic request returns ServiceNow's raw result (sys_id.value,
    state.display_value, ...) so the workflow can read ids and states back."""
    return task("genericAdapterRequest", SNOW_EXPORT, summary,
                {"adapter_id": SNOW_INSTANCE_ID, "uriPath": path_ref, "restMethod": method, "queryData": query or {}, "requestBody": body, "addlHeaders": {}},
                {"result": None}, location="Adapter", location_type=SNOW_EXPORT, x=x, y=y)


def snow_state(summary: str, state: str, x: int, y: int, extra: dict | None = None) -> dict:
    return snow_call(summary, "PATCH", "$var.job.change_path", {"state": state, **(extra or {})}, x=x, y=y)


NETBOX_EXPORT = "Netbox"   # the adapter model's pronghorn export (task app / locationType)
NETBOX_INSTANCE = "NetBox"  # the adapter instance the play creates (task input adapter_id)


def nb(name: str, summary: str, incoming: dict, x: int, y: int = 0, outgoing: dict | None = None) -> dict:
    """An adapter task addresses the model by its export and the instance by adapter_id (the
    workflow engine's validateAdapterMethod: app must equal a configured model's export)."""
    return task(name, NETBOX_EXPORT, summary, {"adapter_id": NETBOX_INSTANCE, **incoming}, outgoing or {"result": None},
                location="Adapter", location_type=NETBOX_EXPORT, x=x, y=y)


def selector(device_ref: str, x: int) -> tuple[dict, dict]:
    """Two tasks that render the Gateway Manager inventory selector for one node. $var references
    are substituted only at the top level of a task's inputs, so the node name is spliced into a
    JSON string with Tools.replace and the string is parsed with Tools.parse."""
    template = '[{"inventory": "%s", "nodeNames": ["__DEVICE__"]}]' % INVENTORY
    rep = task("replace", "WorkFlowEngine", "selector JSON with the node name",
               {"str": template, "substr": "__DEVICE__", "newSubstr": device_ref}, {"replacedString": None}, display="Tools", x=x)
    par = task("parse", "WorkFlowEngine", "selector JSON -> object", {"text": "$var.0a.replacedString"}, {"textObject": None},
               display="Tools", x=x + 300)
    return rep, par


def chain(*ids: str) -> dict:
    """workflow_start -> ids... -> workflow_end on success."""
    seq = ["workflow_start", *ids, "workflow_end"]
    return {a: {b: {"state": "success", "type": "standard"}} for a, b in zip(seq, seq[1:])}


# --- wf-netbox-device-count-v1 (S4.2): NetBox adapter page -> job variable device_count -----------
def device_count() -> dict:
    tasks = {
        "1a": nb("getDcimDevices", "One page of devices (count comes with it)", {"limit": 1, "offset": 0}, x=0,
                 outgoing={"result": "$var.job.devices"}),
    }
    return workflow("wf-netbox-device-count-v1", "Reads the NetBox device list through the NetBox adapter and returns its count (PID S4.2)",
                    {}, tasks, chain("1a"), {"devices": {"type": "object"}})


# --- wf-show-version-v1 (S4.3): Gateway 5 send-command on one inventory node -> job variable output --
def show_version() -> dict:
    tasks = {
        "1a": task("sendCommand", "GatewayManager", "show version through Gateway 5",
                   {"clusterId": CLUSTER, "commands": ["show version"], "inventory": "$var.0b.textObject"},
                   {"result": "$var.job.show_version"}, x=600),
    }
    tasks["0a"], tasks["0b"] = selector("$var.job.device", x=-300)
    return workflow("wf-show-version-v1", "Runs 'show version' on one inventory node through Gateway 5 and returns the raw output (PID S4.3)",
                    {"device": {"type": "string", "required": True, "description": "Inventory node name, e.g. br1-sw01"}},
                    tasks, chain("0a", "0b", "1a"), {"show_version": {"type": "object"}})


def replace(summary: str, template: str, marker: str, value_ref: str, x: int, y: int = 0) -> dict:
    return task("replace", "WorkFlowEngine", summary, {"str": template, "substr": marker, "newSubstr": value_ref},
                {"replacedString": None}, display="Tools", x=x, y=y)


def parse(summary: str, text_ref: str, x: int, y: int = 0) -> dict:
    return task("parse", "WorkFlowEngine", summary, {"text": text_ref}, {"textObject": None}, display="Tools", x=x, y=y)


def jq(summary: str, obj_ref: str, path: str, x: int, y: int = 0, to_job: str | None = None, optional: bool = False) -> dict:
    """Tools.query (json-query) extracts a nested value; optionally also published as a job variable.
    optional=True lets a missing value pass as null instead of failing the task."""
    return task("query", "WorkFlowEngine", summary, {"pass_on_null": optional, "query": path, "obj": obj_ref},
                {"return_data": f"$var.job.{to_job}" if to_job else None}, kind="operation", display="WorkFlowEngine", x=x, y=y)


def evaluate(summary: str, task_id: str, variable: str, path: str, operator: str, value, x: int, y: int = 0) -> dict:
    return task("evaluation", "WorkFlowEngine", summary,
                {"all_true_flag": True, "evaluation_groups": [{"all_true_flag": True, "evaluations": [
                    {"query": path, "operand_1": {"variable": variable, "task": task_id}, "operator": operator,
                     "operand_2": {"variable": value, "task": "static"}}]}], "options": {}},
                {"return_value": None}, kind="operation", display="WorkFlowEngine", x=x, y=y)


def num2str(summary: str, num_ref: str, x: int, y: int = 0) -> dict:
    """Tools.numberToString: replace() insists on string operands, NetBox ids and VIDs are numbers."""
    return task("numberToString", "WorkFlowEngine", summary, {"num": num_ref, "radix": 10}, {"numToString": None}, display="Tools", x=x, y=y)


def flag(summary: str, value: str, job_var: str, x: int, y: int = 0) -> dict:
    return task("makeData", "WorkFlowEngine", summary, {"input": value, "outputType": "boolean", "variables": ""},
                {"output": f"$var.job.{job_var}"}, display="Tools", x=x, y=y)


def view(summary: str, header: str, message_ref: str, body_ref: str, ok: str, cancel: str, x: int, y: int = 0) -> dict:
    return task("ViewData", "WorkFlowEngine", summary,
                {"header": header, "message": message_ref, "body": body_ref, "variables": {}, "btn_success": ok, "btn_failure": cancel},
                {}, kind="manual", display="Tools", view="/workflow_engine/task/ViewData", x=x, y=y)


def t(a: str, b: str, state: str = "success") -> dict:
    return {b: {"state": state, "type": "standard"}}


# --- Lifecycle Manager + JSON Forms (S4d.3, ADR 0043/0044) ---------------------------------------------
FORM_NAME = VERSIONS["forms"]["approval"]
FORM_VIEW = "/json-forms/task/ShowJsonForm"  # measured on 6.5.2: app JsonForms, form_id by name, instance_data defaults, export out
CONFIG_PUSH = VERSIONS["workflows"]["config_push"]
# the object Lifecycle Manager stores as the instance (itential/lcm/branch-vlan.yaml schema); every marker is
# filled by one Tools.replace (a $var inside a nested object never resolves) and the string parsed at the end
INSTANCE_TPL = '{"branch": "__B__", "vid": __V__, "vlan_name": "__N__", "switch": "__S__", "netbox_vlan_id": __I__, "status": "__ST__"}'
INSTANCE_MARKERS = ("__B__", "__V__", "__N__", "__S__", "__I__", "__ST__")


def form_task(summary: str, data_ref: str, x: int, y: int = 0) -> dict:
    """JsonForms.ShowJsonForm manual task: the approver sees the form pre-filled from data_ref (a top-level $var
    object) and the submitted form comes back as export -> job variable approval (ADR 0044). The form has no
    reject button: the decision is a field, evaluated by the next task; a failure finish is a reject too."""
    return task("ShowJsonForm", "JsonForms", summary, {"form_id": FORM_NAME, "instance_data": data_ref},
                {"export": "$var.job.approval"}, kind="manual", display="JsonForms", view=FORM_VIEW, x=x, y=y)


def instance_chain(ids: list[str], refs: dict, x: int, y: int = 0, *, to_job: bool = False) -> dict:
    """Six replace tasks filling INSTANCE_TPL (branch, vid string, name, switch, NetBox id string, status) and a
    parse whose textObject is the instance object; to_job publishes it as $var.job.instance (what LCM stores)."""
    tasks, prev = {}, INSTANCE_TPL
    for i, (tid, marker) in enumerate(zip(ids[:6], INSTANCE_MARKERS)):
        tasks[tid] = replace(f"instance: {marker.strip('_').lower()}", prev, marker, refs[marker], x=x + 300 * i, y=y)
        prev = f"$var.{tid}.replacedString"
    tasks[ids[6]] = parse("instance object", prev, x=x + 1800, y=y)
    if to_job:
        tasks[ids[6]]["variables"]["outgoing"] = {"textObject": "$var.job.instance"}
    return tasks


def child_job(summary: str, workflow_name: str, variables: dict, out_job_var: str, x: int, y: int = 0) -> dict:
    """WorkFlowEngine.childJob: starts another workflow by name with {task, value} references as its inputs and
    waits for it; job_details carries the finished child. A child that ends in error errors this task."""
    c = task("childJob", "WorkFlowEngine", summary,
             {"task": "", "workflow": workflow_name, "variables": variables, "data_array": "", "transformation": "", "loopType": ""},
             {"job_details": f"$var.job.{out_job_var}"}, kind="operation", display="WorkFlowEngine", x=x, y=y)
    c["actor"] = "job"
    return c


# --- wf-branch-vlan-v1 (S4.4): reserve a VLAN in NetBox, approve, configure the branch switch ---------
# Inputs: branch (br1|br2), vlan_name. The next free VID in the branch's NetBox VLAN group is chosen
# by a few lines of Python on Gateway 5 (runCode; the NetBox adapter strips the trailing slash the
# available-vlans endpoint needs), the VLAN is created 'reserved' through the adapter, the operator
# approves in a ViewData task, Gateway 5 pushes "vlan N / name X" to <branch>-sw01, the NetBox VLAN
# goes active. A second run with the same name is a no-op (changed=false). A device failure or a
# rejection deletes the reservation and ends the job in error (no transition to the end).
NEXT_VID_CODE = """import json, sys
d = json.loads(sys.stdin.read() or "{}")
used = {v["vid"] for v in (d.get("response") or {}).get("results", [])}
vid = next((n for n in range(11, 100) if n not in used), None)
print(json.dumps({"vid": vid, "used": sorted(used)}))
"""

VLAN_BODY = '{"site": {"slug": "__B__"}, "group": {"slug": "__G__"}, "vid": __V__, "name": "__N__", "status": "reserved"}'


def branch_vlan() -> dict:
    tasks = {
        # optional ServiceNow change (S4b): a standard change from the PDI's VLAN template, assigned to
        # the Network group (the change model refuses every state move without one), moved
        # New -> Scheduled -> Implement before the device work, work-noted with the NetBox
        # reservation, and Review -> Closed after it. Every state ServiceNow reports is collected
        # in the job variable change_states (sys_audit is not readable by the integration user).
        "d0": evaluate("change request wanted?", "job", "change_request", "", "==", True, x=-600, y=-800),
        "d1": snow_call("create the standard change (PDI VLAN template, Network group)", "POST", "/sn_chg_rest/change/standard/" + SNOW_TEMPLATE,
                        {"short_description": "wf-branch-vlan-v1: branch VLAN change (itential-enterprise-lab)", "assignment_group": SNOW_GROUP}, x=-300, y=-800),
        "d2": jq("change sys_id", "$var.d1.result", "response.result.sys_id.value", x=0, y=-800, to_job="change_sys_id"),
        "d3": jq("change number", "$var.d1.result", "response.result.number.value", x=0, y=-1000, to_job="change_number"),
        "d4": replace("change API path", "/sn_chg_rest/change/standard/__S__", "__S__", "$var.d2.return_data", x=300, y=-800),
        "d9": replace("change table path", "/now/table/change_request/__S__", "__S__", "$var.d2.return_data", x=300, y=-1000),
        "d5": snow_state("state: Scheduled", "-2", x=600, y=-800),
        "d6": jq("state after scheduled", "$var.d5.result", "response.result.state.display_value", x=900, y=-800, to_job="change_state_scheduled"),
        "d7": snow_state("state: Implement", "-1", x=1200, y=-800),
        "d8": jq("state after implement", "$var.d7.result", "response.result.state.display_value", x=1500, y=-800, to_job="change_state_implement"),
        # names and selectors: the switch is <branch>-sw01 unless switch_override names another node
        "0a": evaluate("override given?", "job", "switch_override", "", "!=", "", x=-300, y=-200),
        "1a": replace("switch = <branch>-sw01", "__B__-sw01", "__B__", "$var.job.branch", x=0, y=-200),
        "0b": task("makeData", "WorkFlowEngine", "switch = override", {"input": "$var.job.switch_override", "outputType": "string", "variables": ""},
                   {"output": "$var.job.switch"}, display="Tools", x=0, y=-600),
        "1b": replace("selector JSON", '[{"inventory": "%s", "nodeNames": ["__D__"]}]' % INVENTORY, "__D__", "$var.job.switch", x=300, y=-200),
        "1c": parse("selector", "$var.1b.replacedString", x=600, y=-200),
        "1d": replace("group slug = <branch>-user", "__B__-user", "__B__", "$var.job.branch", x=0, y=-400),
        # idempotency: does the VLAN already exist in the branch?
        "2a": nb("getIpamVlans", "VLAN by site + name", {"site": "$var.job.branch", "name": "$var.job.vlan_name"}, x=900),
        "2b": evaluate("already reserved?", "2a", "result", "response.count", ">", 0, x=1200),
        "9a": flag("changed = false (no-op)", "false", "changed", x=1500, y=400),
        # reservation: next free VID in the branch VLAN group, chosen on Gateway 5
        "3a": nb("getIpamVlans", "VLANs already in the branch group", {"group": "$var.1d.replacedString", "limit": 200}, x=1500, y=-200),
        "3b": task("runCode", "GatewayManager", "next free VID (Python on Gateway 5)",
                   {"clusterId": CLUSTER, "language": "python", "code": NEXT_VID_CODE, "data": "$var.3a.result", "safety": {"timeout": 30}, "packages": []},
                   {"result": None}, x=1800, y=-200),
        "a1": jq("vid", "$var.3b.result", "stdout_json.vid", x=2100, y=-200, to_job="vid"),
        "b2": num2str("vid as string", "$var.a1.return_data", x=2400, y=-200),
        "3e": replace("body: site", VLAN_BODY, "__B__", "$var.job.branch", x=2700, y=-400),
        "3f": replace("body: group", "$var.3e.replacedString", "__G__", "$var.1d.replacedString", x=3000, y=-400),
        "c1": replace("body: vid", "$var.3f.replacedString", "__V__", "$var.b2.numToString", x=3300, y=-400),
        "c2": replace("body: name", "$var.c1.replacedString", "__N__", "$var.job.vlan_name", x=3600, y=-400),
        "c3": parse("body object", "$var.c2.replacedString", x=3900, y=-400),
        "3d": nb("postIpamVlans", "reserve the VLAN in NetBox", {"data": "$var.c3.textObject"}, x=4200, y=-200, outgoing={"result": "$var.job.reservation"}),
        "a2": jq("vlan id", "$var.3d.result", "response.id", x=4500, y=-200, to_job="vlan_id"),
        "e0": evaluate("change request wanted? (work note)", "job", "change_request", "", "==", True, x=4600, y=-600),
        "e1": replace("work note text", "NetBox reservation: VLAN __V__ (VLAN object id __I__) reserved by wf-branch-vlan-v1", "__V__", "$var.b2.numToString", x=4700, y=-800),
        "e2": num2str("vlan id as string", "$var.a2.return_data", x=4850, y=-1000),
        "e3": replace("work note text (id)", "$var.e1.replacedString", "__I__", "$var.e2.numToString", x=5000, y=-800),
        "e4": replace("work note body", '{"work_notes": "__N__"}', "__N__", "$var.e3.replacedString", x=5150, y=-800),
        "e5": parse("work note body object", "$var.e4.replacedString", x=5300, y=-800),
        "e6": snow_call("work note: the NetBox reservation", "PATCH", "$var.job.change_table_path", "$var.e5.textObject", x=5450, y=-800, query={"sysparm_fields": "number,state"}),
        # approval on the JSON form (ADR 0044): the fields are the instance object with status reserved
        "a3": num2str("vlan id as string", "$var.a2.return_data", x=4700, y=-200),
        "4a": form_task("approval", "$var.a9.textObject", x=7000, y=-200),
        "4c": evaluate("approved on the form?", "4a", "export", "decision", "==", "approve", x=7300, y=-200),
        # the stored instance once the switch is configured and NetBox says active (ADR 0043)
        "7b": replace("instance: status active", "$var.a8.replacedString", "__ST__", "active", x=9500, y=-200),
        "7c": parse("instance object (active)", "$var.7b.replacedString", x=9800, y=-200),
        # no-op path: the instance from the VLAN NetBox already has (vid, id, status from the search result)
        "b3": jq("existing vid", "$var.2a.result", "response.results[0].vid", x=1500, y=400, to_job="vid"),
        "b4": jq("existing NetBox id", "$var.2a.result", "response.results[0].id", x=1800, y=400, to_job="vlan_id"),
        "b5": jq("existing status", "$var.2a.result", "response.results[0].status.value", x=2100, y=400),
        "b6": num2str("existing vid as string", "$var.b3.return_data", x=2400, y=400),
        "b7": num2str("existing id as string", "$var.b4.return_data", x=2700, y=400),
        # configure the switch
        "5a": replace("config: vid", "vlan __V__\n   name __N__", "__V__", "$var.b2.numToString", x=5400, y=-200),
        "5b": replace("config: name", "$var.5a.replacedString", "__N__", "$var.job.vlan_name", x=5700, y=-200),
        "5c": task("sendConfig", "GatewayManager", "push the VLAN through Gateway 5",
                   {"clusterId": CLUSTER, "config": "$var.5b.replacedString", "inventory": "$var.1c.textObject"},
                   {"result": "$var.job.config_result"}, x=6000, y=-200),
        "5d": evaluate("config applied?", "5c", "result", "result.results[0].success", "==", True, x=6300, y=-200),
        "6a": nb("patchIpamVlansId", "NetBox VLAN active", {"id": "$var.a2.return_data", "data": {"status": "active"}}, x=6600, y=-200),
        "f0": evaluate("change request wanted? (close)", "job", "change_request", "", "==", True, x=6750, y=-600),
        "f1": snow_state("state: Review", "0", x=6900, y=-800),
        "f2": jq("state after review", "$var.f1.result", "response.result.state.display_value", x=7050, y=-800, to_job="change_state_review"),
        "f3": snow_state("state: Closed", "3", x=7200, y=-800, extra={"close_code": "successful", "close_notes": "VLAN configured by wf-branch-vlan-v1; NetBox VLAN active"}),
        "f4": jq("state after close", "$var.f3.result", "response.result.state.display_value", x=7350, y=-800, to_job="change_state_closed"),
        "7a": flag("changed = true", "true", "changed", x=6900, y=-200),
        # rollback: remove the reservation; no transition to the end, so the job ends in error
        "8a": nb("deleteIpamVlansId", "rollback: delete the NetBox reservation", {"id": "$var.a2.return_data"}, x=6300, y=400),
        "8b": flag("rolled_back = true", "true", "rolled_back", x=6600, y=400),
    }
    tasks["1a"]["variables"]["outgoing"] = {"replacedString": "$var.job.switch"}
    tasks["d4"]["variables"]["outgoing"] = {"replacedString": "$var.job.change_path"}
    tasks["d9"]["variables"]["outgoing"] = {"replacedString": "$var.job.change_table_path"}
    tasks["7c"]["variables"]["outgoing"] = {"textObject": "$var.job.instance"}
    form_ids = ["a4", "a5", "a6", "a7", "a8", "b1", "a9"]
    tasks.update(instance_chain(form_ids, {"__B__": "$var.job.branch", "__V__": "$var.b2.numToString", "__N__": "$var.job.vlan_name",
                                           "__S__": "$var.job.switch", "__I__": "$var.a3.numToString", "__ST__": "reserved"}, x=5000, y=-200))
    noop_ids = ["b8", "b9", "c4", "c5", "c6", "c7", "c8"]
    tasks.update(instance_chain(noop_ids, {"__B__": "$var.job.branch", "__V__": "$var.b6.numToString", "__N__": "$var.job.vlan_name",
                                           "__S__": "$var.job.switch", "__I__": "$var.b7.numToString", "__ST__": "$var.b5.return_data"},
                                x=3000, y=400, to_job=True))
    order = ["1b", "1c", "1d", "2a", "2b"]
    reserve = ["3a", "3b", "a1", "b2", "3e", "3f", "c1", "c2", "c3", "3d", "a2", "e0"]
    apply = ["5a", "5b", "5c", "5d", "6a", "f0"]
    tr = {}
    for a, b in zip(["workflow_start", *order], order):
        tr[a] = t("", b)
    tr["workflow_start"] = t("", "d0")
    tr["d0"] = {"d1": {"state": "success", "type": "standard"}, "0a": {"state": "failure", "type": "standard"}}
    for a, b in zip(["d1", "d2", "d3", "d4", "d9", "d5", "d6", "d7", "d8"], ["d2", "d3", "d4", "d9", "d5", "d6", "d7", "d8", "0a"]):
        tr[a] = t("", b)
    tr["0a"] = {"0b": {"state": "success", "type": "standard"}, "1a": {"state": "failure", "type": "standard"}}
    tr["0b"] = t("", "1b")
    tr["1a"] = t("", "1b")
    noop = ["b3", "b4", "b5", "b6", "b7", *noop_ids, "9a"]
    tr["2b"] = {noop[0]: {"state": "success", "type": "standard"}, "3a": {"state": "failure", "type": "standard"}}
    for a, b in zip(noop, noop[1:]):
        tr[a] = t("", b)
    tr["9a"] = t("", "workflow_end")
    for a, b in zip(reserve, reserve[1:]):
        tr[a] = t("", b)
    tr["e0"] = {"e1": {"state": "success", "type": "standard"}, "a3": {"state": "failure", "type": "standard"}}
    for a, b in zip(["e1", "e2", "e3", "e4", "e5", "e6"], ["e2", "e3", "e4", "e5", "e6", "a3"]):
        tr[a] = t("", b)
    form_chain = ["a3", *form_ids, "4a"]
    for a, b in zip(form_chain, form_chain[1:]):
        tr[a] = t("", b)
    # the form: submitted -> the decision decides; a failure finish (API reject) rolls back like a reject
    tr["4a"] = {"4c": {"state": "success", "type": "standard"}, "8a": {"state": "failure", "type": "standard"}}
    tr["4c"] = {"5a": {"state": "success", "type": "standard"}, "8a": {"state": "failure", "type": "standard"}}
    for a, b in zip(apply, apply[1:]):
        tr[a] = t("", b)
    tr["5c"] = {"5d": {"state": "success", "type": "standard"}, "8a": {"state": "error", "type": "standard"}}
    tr["5d"] = {"6a": {"state": "success", "type": "standard"}, "8a": {"state": "failure", "type": "standard"}}
    tr["f0"] = {"f1": {"state": "success", "type": "standard"}, "7b": {"state": "failure", "type": "standard"}}
    for a, b in zip(["f1", "f2", "f3", "f4"], ["f2", "f3", "f4", "7b"]):
        tr[a] = t("", b)
    tr["7b"] = t("", "7c")
    tr["7c"] = t("", "7a")
    tr["7a"] = t("", "workflow_end")
    tr["8a"] = t("", "8b")
    tr["8b"] = {}
    return workflow("wf-branch-vlan-v1", "Reserves a VLAN in NetBox for a branch, asks for approval on the JSON form %s, configures the branch "
                    "switch through Gateway 5, activates the NetBox VLAN and publishes the Lifecycle Manager instance; rolls the reservation "
                    "back on rejection or device failure (PID S4.4, S4d.3, ADR 0043/0044)" % FORM_NAME,
                    {"branch": {"type": "string", "required": True, "description": "Branch site slug, e.g. br1"},
                     "vlan_name": {"type": "string", "required": True, "description": "VLAN name to reserve and configure"},
                     "switch_override": {"type": "string", "description": "Inventory node to configure instead of <branch>-sw01 (empty = default; used by verify to force a device failure)"},
                     "change_request": {"type": "boolean", "description": "Open, work-note and close a ServiceNow standard change around the work (S4b)"}},
                    tasks, tr, {"changed": {"type": "boolean"}, "vid": {"type": "number"}, "vlan_id": {"type": "number"},
                                "reservation": {"type": "object"}, "config_result": {"type": "object"}, "rolled_back": {"type": "boolean"},
                                "change_number": {"type": "string"}, "change_sys_id": {"type": "string"},
                                "change_state_scheduled": {"type": "string"}, "change_state_implement": {"type": "string"},
                                "change_state_review": {"type": "string"}, "change_state_closed": {"type": "string"},
                                "instance": {"type": "object"}, "approval": {"type": "object"}})


# --- wf-show-command-v1 (S4c.7): one show command -> raw text + structured data per vendor --------
# Gateway 5 send-command returns text; a runCode task on the glibc runner (ADR 0038) parses it
# with Genie (Cisco) or TextFSM/ntc-templates (Arista). The engine is chosen from the node's NetBox
# platform slug, which the workflow substitutes into the code (a top-level string input resolves
# `$var`; the nested `data` object would not), and the sendCommand result is the script's stdin.
PARSER_PACKAGES = VERSIONS["parsers"]["cisco"]["packages"] + VERSIONS["parsers"]["arista"]["packages"]
PARSE_CODE = """import json, sys
PLATFORM = "__P__"  # NetBox platform slug of the node (the workflow replaces it)
GENIE = __GENIE__
TEXTFSM = __TEXTFSM__
d = json.loads(sys.stdin.read() or "{}")
r = ((d.get("result") or {}).get("results") or [{}])[0]
command, output = r.get("command", ""), r.get("output", "")
out = {"parser": "none", "platform": PLATFORM, "command": command, "device": r.get("name"), "parsed": None, "error": ""}
try:
    if PLATFORM in GENIE:
        from genie.conf.base import Device
        dev = Device(name="x", os=GENIE[PLATFORM])
        dev.custom.setdefault("abstraction", {"order": ["os"]})
        out["parsed"], out["parser"] = dev.parse(command, output=output), "genie"
    elif PLATFORM in TEXTFSM:
        from ntc_templates.parse import parse_output
        out["parsed"], out["parser"] = parse_output(platform=TEXTFSM[PLATFORM], command=command, data=output), "textfsm"
    else:
        out["error"] = "no parser for platform " + PLATFORM
except Exception as e:  # unsupported command or empty output: report, keep the raw text usable
    out["error"] = type(e).__name__ + ": " + str(e)[:300]
print(json.dumps(out))
""".replace("__GENIE__", json.dumps(VERSIONS["parsers"]["cisco"]["netbox_platforms"])).replace(
    "__TEXTFSM__", json.dumps(VERSIONS["parsers"]["arista"]["netbox_platforms"]))


def show_command() -> dict:
    tasks = {
        # the command list is built from the input (a list literal would not resolve $var inside it)
        "1a": replace("commands JSON", '["__C__"]', "__C__", "$var.job.command", x=0, y=-300),
        "1b": parse("commands list", "$var.1a.replacedString", x=300, y=-300),
        "2a": task("sendCommand", "GatewayManager", "run the command through Gateway 5",
                   {"clusterId": CLUSTER, "commands": "$var.1b.textObject", "inventory": "$var.0b.textObject"},
                   {"result": "$var.job.raw"}, x=600),
        # the parser engine follows the node's NetBox platform
        "3a": nb("getDcimDevices", "the node in NetBox", {"name": "$var.job.device", "limit": 1}, x=0, y=300),
        "3b": jq("platform slug", "$var.3a.result", "response.results[0].platform.slug", x=300, y=300),
        "3c": replace("parse code for this platform", PARSE_CODE, "__P__", "$var.3b.return_data", x=600, y=300),
        "4a": task("runCode", "GatewayManager", "parse (Genie for Cisco, TextFSM for Arista) on the runner",
                   {"clusterId": CLUSTER, "language": "python", "code": "$var.3c.replacedString", "data": "$var.2a.result",
                    "safety": {"timeout": 180}, "packages": PARSER_PACKAGES},
                   {"result": None}, x=900),
        "4b": jq("structured result", "$var.4a.result", "stdout_json.parsed", x=1200, to_job="parsed"),
        "4c": jq("parser used", "$var.4a.result", "stdout_json.parser", x=1200, y=200, to_job="parser"),
        "4d": jq("parse error, if any", "$var.4a.result", "stdout_json.error", x=1200, y=400, to_job="parse_error"),
    }
    tasks["0a"], tasks["0b"] = selector("$var.job.device", x=-600)
    return workflow("wf-show-command-v1",
                    "Runs one show command on an inventory node through Gateway 5 and returns the raw output plus structured data: "
                    "Genie for Cisco platforms, TextFSM (ntc-templates) for Arista, chosen from the node's NetBox platform (PID S4c.7, ADR 0038)",
                    {"device": {"type": "string", "required": True, "description": "Inventory node name, e.g. br1-wan01"},
                     "command": {"type": "string", "required": True, "description": "One show command, e.g. show ip interface brief"}},
                    tasks, chain("0a", "0b", "1a", "1b", "2a", "3a", "3b", "3c", "4a", "4b", "4c", "4d"),
                    {"raw": {"type": "object"}, "parsed": {"type": ["object", "array", "null"]}, "parser": {"type": "string"},
                     "parse_error": {"type": "string"}})  # "" when the parse succeeded


# --- wf-config-push-v1 (S4d, ADR 0040/0041): the one governed write path ---------------------
# Inputs: device, config (CLI lines), reason. The operator sees device, reason and the exact lines in
# a Work Center approval; on approval Gateway 5 pushes them with send-config and saves the running
# configuration with "write memory" (IOS-XE and EOS both accept it). A rejection ends the job in
# error with nothing touched. The compliance/remediation agents get this workflow as their only
# write tool; Golden Config never remediates on its own (ADR 0040).
def config_push() -> dict:
    tasks = {
        "1a": replace("summary: device", "Push to __D__ (__R__):", "__D__", "$var.job.device", x=0, y=-200),
        "1b": replace("summary: reason", "$var.1a.replacedString", "__R__", "$var.job.reason", x=300, y=-200),
        "2a": view("approval", "Approve configuration push", "$var.1b.replacedString", "$var.job.config", "Approve", "Reject", x=600),
        "3a": task("sendConfig", "GatewayManager", "push the lines through Gateway 5",
                   {"clusterId": CLUSTER, "config": "$var.job.config", "inventory": "$var.0b.textObject"},
                   {"result": "$var.job.config_result"}, x=900),
        "3b": evaluate("config applied?", "3a", "result", "result.results[0].success", "==", True, x=1200),
        "4a": task("sendCommand", "GatewayManager", "save the running configuration",
                   {"clusterId": CLUSTER, "commands": ["write memory"], "inventory": "$var.0b.textObject"},
                   {"result": "$var.job.save_result"}, x=1500),
        "5a": flag("changed = true", "true", "changed", x=1800),
        "9a": flag("changed = false (rejected)", "false", "changed", x=900, y=400),
    }
    tasks["0a"], tasks["0b"] = selector("$var.job.device", x=-600)
    tr = chain("0a", "0b", "1a", "1b", "2a", "3a", "3b", "4a", "5a")
    tr["2a"] = {"3a": {"state": "success", "type": "standard"}, "9a": {"state": "failure", "type": "standard"}}
    tr["3b"] = {"4a": {"state": "success", "type": "standard"}}  # failure: no transition, the job ends in error
    tr["9a"] = {}  # rejected: no transition to the end, the job ends in error with nothing pushed
    return workflow("wf-config-push-v1",
                    "Pushes operator-supplied configuration lines to one inventory node through Gateway 5 after a Work Center "
                    "approval and saves the running configuration; the only write path for compliance remediation (PID S4d, ADR 0040)",
                    {"device": {"type": "string", "required": True, "description": "Inventory node name, e.g. br1-wan01"},
                     "config": {"type": "string", "required": True, "description": "Configuration lines to push, one per line"},
                     "reason": {"type": "string", "required": True, "description": "Why, shown to the approver (ticket, compliance report id)"}},
                    tasks, tr, {"changed": {"type": "boolean"}, "config_result": {"type": "object"}, "save_result": {"type": "object"}})


# --- wf-compliance-run-v1 (S4d.1, ADR 0040): the nightly schedule trigger's target ------------------
# No inputs: Operations Manager schedule triggers on 6.5.2 do not persist formData (measured 2026-09-07,
# PATCH echoes it, GET returns null), so the plan is found by its name from versions.yaml. The search
# matches a regex (an unescaped "-" misses, anchors work). The run is asynchronous; the plan instance
# id and the plan id are published as job variables for the verify script and the agents.
PLAN_NAME = VERSIONS["golden_config"]["plan"]


def compliance_run() -> dict:
    tasks = {
        "1a": task("searchCompliancePlans", "ConfigurationManager", "the plan by name",
                   {"name": "^" + PLAN_NAME + "$", "options": {"start": 0, "limit": 10}}, {"compliancePlans": None}, x=0),
        "1b": jq("plan id", "$var.1a.compliancePlans", "plans[0].id", x=300, to_job="plan_id"),
        "2a": task("runCompliancePlan", "ConfigurationManager", "run the compliance plan (asynchronous)",
                   {"planId": "$var.1b.return_data", "options": {}}, {"response": "$var.job.run"}, x=600),
    }
    return workflow("wf-compliance-run-v1", "Runs the Configuration Manager compliance plan %s; scheduled nightly by Operations Manager (PID S4d.1, ADR 0040)" % PLAN_NAME,
                    {}, tasks, chain("1a", "1b", "2a"), {"plan_id": {"type": "string"}, "run": {"type": "object"}})

# --- wf-backup-all-v1 (S4d.2, ADR 0042): every Configuration Manager device backed up, nightly ---------
# No inputs (schedule triggers do not persist formData). The device list comes from Configuration Manager
# itself (the InventoryBroker devices, ADR 0039), so a node added to NetBox is backed up on the next run
# with no change here. Loop = WorkFlowEngine forEach: the "loop" transition starts an iteration, a body
# task with no outgoing transition returns to the forEach, "success" fires when the array is exhausted.
def backup_all() -> dict:
    tasks = {
        "1a": task("getDevicesFiltered", "ConfigurationManager", "every device Configuration Manager knows",
                   {"options": {"start": 0, "limit": 500}}, {"devices": None}, x=0),
        "1b": jq("device names", "$var.1a.devices", "list[*].name", x=300, to_job="devices"),
        "2a": task("forEach", "WorkFlowEngine", "one device at a time", {"data_array": "$var.1b.return_data"},
                   {"current_item": None}, kind="operation", display="WorkFlowEngine", x=600),
        "3a": task("backUpDevice", "ConfigurationManager", "backup through the broker (Gateway 5)",
                   {"name": "$var.2a.current_item", "options": {"description": "nightly backup (wf-backup-all-v1)", "notes": ""}},
                   {"status": None}, x=900, y=300),
    }
    tr = {"workflow_start": t("", "1a"), "1a": t("", "1b"), "1b": t("", "2a"),
          "2a": {"3a": {"state": "loop", "type": "standard"}, "workflow_end": {"state": "success", "type": "standard"}},
          "3a": {}}
    return workflow("wf-backup-all-v1", "Backs up every Configuration Manager device through the InventoryBroker (Gateway 5); "
                    "scheduled nightly by Operations Manager (PID S4d.2, ADR 0042)",
                    {}, tasks, tr, {"devices": {"type": "array"}})


# --- wf-branch-vlan-delete-v1 (S4d.3, ADR 0043): the Lifecycle Manager delete action -------------------
# Input: the instance object LCM passes as the job variable `instance` (branch, vid, vlan_name, switch,
# netbox_vlan_id, status). The switch is read with `show vlan <vid>` and, only when the VLAN is present,
# wf-config-push-v1 runs as a child job with `no vlan <vid>` (its Work Center approval is the gate); the
# NetBox VLAN is deleted after the device, so a rejected push changes nothing. A rejected push leaves the
# push job in error and this job waiting on it (a job in error is retryable on 6.5.2): cancelling the
# execution ends both and keeps the instance. Run on a retired VLAN it is a no-op (changed false, no push).
# An instance without data fails the first read and takes the no-data path.
def branch_vlan_delete() -> dict:
    tasks = {
        "1a": jq("branch", "$var.job.instance", "branch", x=0, y=-300),
        "1b": jq("vid", "$var.job.instance", "vid", x=300, y=-300, to_job="vid"),
        "1c": jq("vlan name", "$var.job.instance", "vlan_name", x=600, y=-300),
        "1d": jq("switch", "$var.job.instance", "switch", x=900, y=-300, to_job="switch"),
        "1e": jq("NetBox VLAN id", "$var.job.instance", "netbox_vlan_id", x=1200, y=-300),
        "1f": num2str("vid as string", "$var.1b.return_data", x=1500, y=-300),
        "2f": num2str("NetBox id as string", "$var.1e.return_data", x=1800, y=-300),
        "2a": flag("changed = false (nothing yet)", "false", "changed", x=2700, y=-300),
        # NetBox: the VLAN by site + name (the same lookup the create uses), deleted when present
        "3a": nb("getIpamVlans", "the VLAN in NetBox", {"site": "$var.1a.return_data", "name": "$var.1c.return_data"}, x=3000, y=-300),
        "3b": evaluate("still in NetBox?", "3a", "result", "response.count", ">", 0, x=3300, y=-300),
        "3c": jq("its NetBox id", "$var.3a.result", "response.results[0].id", x=3600, y=-500),
        "3d": nb("deleteIpamVlansId", "delete the NetBox VLAN", {"id": "$var.3c.return_data"}, x=3900, y=-500),
        "3e": flag("netbox_deleted = true", "true", "netbox_deleted", x=4200, y=-500),
        "2b": flag("changed = true", "true", "changed", x=4500, y=-500),
        "3f": flag("netbox_deleted = false (absent)", "false", "netbox_deleted", x=3900, y=-100),
        # the switch: read before write; `no vlan` is pushed only when the VLAN is configured
        "4a": replace("show vlan command list", '["show vlan __V__"]', "__V__", "$var.1f.numToString", x=4800, y=-300),
        "4b": parse("command list", "$var.4a.replacedString", x=5100, y=-300),
        "4c": task("sendCommand", "GatewayManager", "show vlan <vid> through Gateway 5",
                   {"clusterId": CLUSTER, "commands": "$var.4b.textObject", "inventory": "$var.0b.textObject"},
                   {"result": "$var.job.show_vlan"}, x=5400, y=-300),
        "4d": evaluate("VLAN absent on the switch?", "4c", "result", "result.results[0].output", "contains", "not found", x=5700, y=-300),
        "5a": replace("config: no vlan <vid>", "no vlan __V__", "__V__", "$var.1f.numToString", x=6000, y=-500),
        "5b": replace("reason: vid", "Lifecycle Manager branch-vlan delete: remove VLAN __V__ (__N__) from __S__", "__V__", "$var.1f.numToString", x=6300, y=-500),
        "5c": replace("reason: name", "$var.5b.replacedString", "__N__", "$var.1c.return_data", x=6600, y=-500),
        "5d": replace("reason: switch", "$var.5c.replacedString", "__S__", "$var.1d.return_data", x=6900, y=-500),
        "5e": child_job("remove the VLAN through the governed push (approval in Work Center)", CONFIG_PUSH,
                        {"device": {"task": "1d", "value": "return_data"}, "config": {"task": "5a", "value": "replacedString"},
                         "reason": {"task": "5d", "value": "replacedString"}}, "push_job", x=7200, y=-500),
        "2c": flag("switch_changed = true", "true", "switch_changed", x=7500, y=-500),
        "2d": flag("changed = true", "true", "changed", x=7800, y=-500),
        "6a": flag("switch_changed = false (absent)", "false", "switch_changed", x=6000, y=-100),
        # no data on the instance (a rejected create): nothing to delete, LCM retires it
        "9a": flag("changed = false (no instance data)", "false", "changed", x=300, y=300),
    }
    tasks["0a"], tasks["0b"] = selector("$var.1d.return_data", x=2100)
    tasks["0a"]["nodeLocation"]["y"] = tasks["0b"]["nodeLocation"]["y"] = -300
    final_ids = ["7a", "7b", "7c", "7d", "7e", "7f", "2e"]
    tasks.update(instance_chain(final_ids, {"__B__": "$var.1a.return_data", "__V__": "$var.1f.numToString", "__N__": "$var.1c.return_data",
                                            "__S__": "$var.1d.return_data", "__I__": "$var.2f.numToString", "__ST__": "deleted"},
                                x=8100, y=-300, to_job=True))
    # order: read the switch, push through the approval, and only then touch NetBox, so a rejected push changes nothing
    head = ["1a", "1b", "1c", "1d", "1e", "1f", "2f", "0a", "0b", "2a", "4a", "4b", "4c", "4d"]
    tr = {"workflow_start": t("", "1a")}
    for a, b in zip(head, head[1:]):
        tr[a] = t("", b)
    tr["1a"] = {"1b": {"state": "success", "type": "standard"}, "9a": {"state": "failure", "type": "standard"}}
    tr["4d"] = {"6a": {"state": "success", "type": "standard"}, "5a": {"state": "failure", "type": "standard"}}
    for a, b in zip(["5a", "5b", "5c", "5d", "5e", "2c"], ["5b", "5c", "5d", "5e", "2c", "2d"]):
        tr[a] = t("", b)  # 5e waits while the push job is in error (a job in error is retryable); cancelling ends both
    tr["2d"] = t("", "3a")
    tr["6a"] = t("", "3a")
    tr["3a"] = t("", "3b")
    tr["3b"] = {"3c": {"state": "success", "type": "standard"}, "3f": {"state": "failure", "type": "standard"}}
    for a, b in zip(["3c", "3d", "3e", "2b"], ["3d", "3e", "2b", final_ids[0]]):
        tr[a] = t("", b)
    tr["3f"] = t("", final_ids[0])
    for a, b in zip(final_ids, final_ids[1:]):
        tr[a] = t("", b)
    tr[final_ids[-1]] = t("", "workflow_end")
    tr["9a"] = t("", "workflow_end")
    return workflow("wf-branch-vlan-delete-v1",
                    "Lifecycle Manager delete action for branch-vlan: deletes the NetBox VLAN and removes it from the branch switch only "
                    "through %s (Work Center approval); no-op when both are already gone (PID S4d.3, ADR 0043)" % CONFIG_PUSH,
                    {"instance": {"type": "object", "description": "The branch-vlan instance data (branch, vid, vlan_name, switch, netbox_vlan_id, status); "
                                                                   "Lifecycle Manager passes it for a delete action"}},
                    tasks, tr, {"changed": {"type": "boolean"}, "netbox_deleted": {"type": "boolean"}, "switch_changed": {"type": "boolean"},
                                "vid": {"type": "number"}, "switch": {"type": "string"}, "show_vlan": {"type": "object"},
                                "push_job": {"type": "object"}, "instance": {"type": "object"}})


if __name__ == "__main__":
    for wf in (device_count(), show_version(), show_command(), branch_vlan(), branch_vlan_delete(), config_push(), compliance_run(), backup_all()):
        out = HERE / f"{wf['name']}.json"
        out.write_text(json.dumps(wf, indent=2) + "\n")
        print(out.relative_to(HERE.parent.parent), len(wf["tasks"]) - 2, "tasks")
