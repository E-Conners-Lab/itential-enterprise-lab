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

  itential/workflows/build.py            # writes the three workflow files
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLUSTER = "lab"
INVENTORY = "lab"


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


def jq(summary: str, obj_ref: str, path: str, x: int, y: int = 0, to_job: str | None = None) -> dict:
    """Tools.query (json-query) extracts a nested value; optionally also published as a job variable."""
    return task("query", "WorkFlowEngine", summary, {"pass_on_null": False, "query": path, "obj": obj_ref},
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
        # approval
        "4b": replace("summary line", "Reserved VLAN __V__ in NetBox; apply it to the branch switch?", "__V__", "$var.b2.numToString", x=4800, y=-200),
        "4a": view("approval", "Approve VLAN change", "$var.4b.replacedString", "$var.4b.replacedString", "Approve", "Reject", x=5100, y=-200),
        # configure the switch
        "5a": replace("config: vid", "vlan __V__\n   name __N__", "__V__", "$var.b2.numToString", x=5400, y=-200),
        "5b": replace("config: name", "$var.5a.replacedString", "__N__", "$var.job.vlan_name", x=5700, y=-200),
        "5c": task("sendConfig", "GatewayManager", "push the VLAN through Gateway 5",
                   {"clusterId": CLUSTER, "config": "$var.5b.replacedString", "inventory": "$var.1c.textObject"},
                   {"result": "$var.job.config_result"}, x=6000, y=-200),
        "5d": evaluate("config applied?", "5c", "result", "result.results[0].success", "==", True, x=6300, y=-200),
        "6a": nb("patchIpamVlansId", "NetBox VLAN active", {"id": "$var.a2.return_data", "data": {"status": "active"}}, x=6600, y=-200),
        "7a": flag("changed = true", "true", "changed", x=6900, y=-200),
        # rollback: remove the reservation; no transition to the end, so the job ends in error
        "8a": nb("deleteIpamVlansId", "rollback: delete the NetBox reservation", {"id": "$var.a2.return_data"}, x=6300, y=400),
        "8b": flag("rolled_back = true", "true", "rolled_back", x=6600, y=400),
    }
    tasks["1a"]["variables"]["outgoing"] = {"replacedString": "$var.job.switch"}
    order = ["1b", "1c", "1d", "2a", "2b"]
    reserve = ["3a", "3b", "a1", "b2", "3e", "3f", "c1", "c2", "c3", "3d", "a2", "4b", "4a"]
    apply = ["5a", "5b", "5c", "5d", "6a", "7a"]
    tr = {}
    for a, b in zip(["workflow_start", *order], order):
        tr[a] = t("", b)
    tr["workflow_start"] = t("", "0a")
    tr["0a"] = {"0b": {"state": "success", "type": "standard"}, "1a": {"state": "failure", "type": "standard"}}
    tr["0b"] = t("", "1b")
    tr["1a"] = t("", "1b")
    tr["2b"] = {"9a": {"state": "success", "type": "standard"}, "3a": {"state": "failure", "type": "standard"}}
    tr["9a"] = t("", "workflow_end")
    for a, b in zip(reserve, reserve[1:]):
        tr[a] = t("", b)
    tr["4a"] = {"5a": {"state": "success", "type": "standard"}, "8a": {"state": "failure", "type": "standard"}}
    for a, b in zip(apply, apply[1:]):
        tr[a] = t("", b)
    tr["5c"] = {"5d": {"state": "success", "type": "standard"}, "8a": {"state": "error", "type": "standard"}}
    tr["5d"] = {"6a": {"state": "success", "type": "standard"}, "8a": {"state": "failure", "type": "standard"}}
    tr["7a"] = t("", "workflow_end")
    tr["8a"] = t("", "8b")
    tr["8b"] = {}
    return workflow("wf-branch-vlan-v1", "Reserves a VLAN in NetBox for a branch, asks for approval, configures the branch switch through Gateway 5, "
                    "activates the NetBox VLAN; rolls the reservation back on rejection or device failure (PID S4.4)",
                    {"branch": {"type": "string", "required": True, "description": "Branch site slug, e.g. br1"},
                     "vlan_name": {"type": "string", "required": True, "description": "VLAN name to reserve and configure"},
                     "switch_override": {"type": "string", "description": "Inventory node to configure instead of <branch>-sw01 (empty = default; used by verify to force a device failure)"}},
                    tasks, tr, {"changed": {"type": "boolean"}, "vid": {"type": "number"}, "vlan_id": {"type": "number"},
                                "reservation": {"type": "object"}, "config_result": {"type": "object"}, "rolled_back": {"type": "boolean"}})


if __name__ == "__main__":
    for wf in (device_count(), show_version(), branch_vlan()):
        out = HERE / f"{wf['name']}.json"
        out.write_text(json.dumps(wf, indent=2) + "\n")
        print(out.relative_to(HERE.parent.parent), len(wf["tasks"]) - 2, "tasks")
