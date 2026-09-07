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


def nb(name: str, summary: str, incoming: dict, x: int) -> dict:
    return task(name, "NetBox", summary, incoming, {"result": None}, location="Adapter", location_type="Netbox", x=x)


def query(summary: str, obj: str, q: str, to_job: str, x: int) -> dict:
    return task("query", "WorkFlowEngine", summary, {"job_id": "", "pass_on_null": False, "query": q, "obj": obj},
                {"return_data": f"$var.job.{to_job}"}, kind="operation", display="WorkFlowEngine", x=x)


def chain(*ids: str) -> dict:
    """workflow_start -> ids... -> workflow_end on success."""
    seq = ["workflow_start", *ids, "workflow_end"]
    return {a: {b: {"state": "success", "type": "standard"}} for a, b in zip(seq, seq[1:])}


# --- wf-netbox-device-count-v1 (S4.2): NetBox adapter page -> job variable device_count -----------
def device_count() -> dict:
    tasks = {
        "1a": nb("getDcimDevices", "One page of devices (count comes with it)", {"limit": 1, "offset": 0}, x=0),
        "2b": query("device_count = response.count", "$var.1a.result", "count", "device_count", x=600),
    }
    return workflow("wf-netbox-device-count-v1", "Reads the NetBox device list through the NetBox adapter and returns its count (PID S4.2)",
                    {}, tasks, chain("1a", "2b"), {"device_count": {"type": "number"}})


# --- wf-show-version-v1 (S4.3): Gateway 5 send-command on one inventory node -> job variable output --
def show_version() -> dict:
    tasks = {
        "1a": task("sendCommand", "GatewayManager", "show version through Gateway 5",
                   {"clusterId": CLUSTER, "commands": ["show version"], "inventory": [{"inventory": INVENTORY, "nodeNames": ["$var.job.device"]}]},
                   {"result": None}, x=0),
        "2b": query("output = results[0].output", "$var.1a.result", "results[0].output", "output", x=600),
        "3c": query("success = results[0].success", "$var.1a.result", "results[0].success", "success", x=1200),
    }
    return workflow("wf-show-version-v1", "Runs 'show version' on one inventory node through Gateway 5 and returns the raw output (PID S4.3)",
                    {"device": {"type": "string", "required": True, "description": "Inventory node name, e.g. br1-sw01"}},
                    tasks, chain("1a", "2b", "3c"), {"output": {"type": "string"}, "success": {"type": "boolean"}})


if __name__ == "__main__":
    for wf in (device_count(), show_version()):
        out = HERE / f"{wf['name']}.json"
        out.write_text(json.dumps(wf, indent=2) + "\n")
        print(out.relative_to(HERE.parent.parent), len(wf["tasks"]) - 2, "tasks")
