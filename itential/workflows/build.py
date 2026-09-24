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

import itertools
import json
import statistics
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
CLUSTER = "lab"
INVENTORY = "lab"
VERSIONS = yaml.safe_load((HERE.parent / "versions.yaml").read_text())
WF = VERSIONS["workflows"]  # every workflow name comes from here (ADR 0067)


def file_name(name: str) -> str:
    """The document's file: its name in lowercase with dashes ("Add Branch VLAN" -> add-branch-vlan.json)."""
    return name.lower().replace(" ", "-") + ".json"


def task(
    name: str,
    app: str,
    summary: str,
    incoming: dict,
    outgoing: dict,
    *,
    kind: str = "automatic",
    location: str = "Application",
    location_type: str | None = None,
    display: str | None = None,
    view: str | None = None,
    x: int = 0,
    y: int = 0,
) -> dict:
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
        "variables": {
            "incoming": incoming,
            "outgoing": outgoing,
            "error": "",
            "decorators": [],
        },
    }
    if kind == "automatic":
        t["actor"] = "Pronghorn"
        t["scheduled"] = False
    if kind == "manual":
        t["view"] = view
    return t


# --- the canvas ------------------------------------------------------------------------------------------------
# Every workflow reads top to bottom and is laid out from its transitions, not by hand (owner preference,
# 2026-09-24). The x/y a task is given in this file is only a hint: x its order along the flow, y its lane (0 the
# main path, negative a failure branch to the left, positive a side branch to the right).
#   row    = the longest path from workflow_start, so every forward arrow points down (a loop back, a retry or a
#            poll, is the only arrow that may go up); workflow_start is alone on top, workflow_end at the bottom
#   column = two candidates, scored on what Studio draws (straight arrows): the lanes as given, and the rows
#            reordered by the barycenter of their neighbours (long arrows kept as a chain of placeholder slots)
#            then pulled toward the median of their neighbours. The lower score wins, then a local search swaps
#            neighbours and nudges tasks sideways while the score keeps dropping.
#   score  = arrows crossing + 3 x arrows through a task box (an arrow hidden behind a task reads worse)
# Deterministic: the same document always gets the same canvas. tests/test_workflow_layout.py holds the shape.
ROW = 150  # down the page from one row to the next (a task is about 50 units tall)
COL = 300  # across the page between tasks in a row (a task is about 220 units wide)
NUDGE = 150  # the local search's sideways step
HALF_W, HALF_H = 100, 20  # a task box for the score, a little inside its drawn 220 x 50


def _rows(tasks: dict, transitions: dict) -> tuple[dict, dict, list]:
    hint = {tid: t.get("nodeLocation") or {"x": 0, "y": 0} for tid, t in tasks.items()}
    hint["workflow_start"] = {"x": float("-inf"), "y": 0}
    hint["workflow_end"] = {"x": float("inf"), "y": 0}
    succ = {n: sorted((d for d in transitions.get(n, {}) if d in hint), key=lambda d: hint[d]["x"]) for n in hint}

    # depth-first from the start: finishing order gives a topological order once the back edges are set aside
    back, done, active, finish = set(), set(), set(), []

    def visit(n: str) -> None:
        active.add(n)
        for d in succ[n]:
            if d in active:
                back.add((n, d))
            elif d not in done:
                visit(d)
        active.discard(n)
        done.add(n)
        finish.append(n)

    visit("workflow_start")
    unreachable = sorted(set(hint) - done - {"workflow_end"})
    assert not unreachable, f"tasks no transition reaches: {unreachable}"

    row = dict.fromkeys(hint, 0)
    for n in reversed(finish):
        for d in succ[n]:
            if (n, d) not in back:
                row[d] = max(row[d], row[n] + 1)
    row["workflow_end"] = max(r for n, r in row.items() if n != "workflow_end") + 1
    edges = [(s, d) for s in succ for d in succ[s]]
    return hint, row, edges


def _pack(order: list, want: dict) -> dict:
    """x for one row: keep the order, keep COL apart, stay as close to `want` as possible."""
    left, right = [], [0.0] * len(order)
    for i, n in enumerate(order):
        left.append(want[n] if i == 0 else max(want[n], left[-1] + COL))
    for i in range(len(order) - 1, -1, -1):
        right[i] = want[order[i]] if i == len(order) - 1 else min(want[order[i]], right[i + 1] - COL)
    x = {}
    for i, n in enumerate(order):
        x[n] = (left[i] + right[i]) / 2 if i == 0 else max((left[i] + right[i]) / 2, x[order[i - 1]] + COL)
    return x


def _lane_columns(hint: dict, row: dict) -> dict:
    x = {}
    for r in sorted(set(row.values())):
        order = sorted((n for n in row if row[n] == r), key=lambda n: (hint[n]["y"], hint[n]["x"]))
        x.update(_pack(order, {n: hint[n]["y"] for n in order}))
    return x


def _ordered_columns(hint: dict, row: dict, edges: list) -> dict:
    rank, key = dict(row), {n: (hint[n]["y"], hint[n]["x"]) for n in hint}
    up, down = {n: [] for n in rank}, {n: [] for n in rank}
    for s, d in edges:
        if row[d] <= row[s]:
            continue  # a loop back takes no part in the ordering
        prev = s
        for r in range(row[s] + 1, row[d]):  # placeholder slots for an arrow spanning several rows
            v = f"~{s}>{d}@{r}"
            rank[v], up[v], down[v] = r, [], []
            key[v] = (hint[s]["y"] if row[d] - r > r - row[s] else hint[d]["y"], hint[s]["x"])
            down[prev].append(v)
            up[v].append(prev)
            prev = v
        down[prev].append(d)
        up[d].append(prev)
    layers = [sorted((n for n in rank if rank[n] == r), key=lambda n: key[n]) for r in range(max(rank.values()) + 1)]

    def crossings(ls: list) -> int:
        total = 0
        for a, b in itertools.pairwise(ls):
            pos = {n: i for i, n in enumerate(b)}
            segs = sorted((i, pos[d]) for i, n in enumerate(a) for d in down[n])
            total += sum(1 for (i1, j1), (i2, j2) in itertools.combinations(segs, 2) if i1 < i2 and j1 > j2)
        return total

    best, best_c = [list(layer) for layer in layers], crossings(layers)
    for sweep in range(24):
        downward = sweep % 2 == 0
        for r in range(1, len(layers)) if downward else range(len(layers) - 2, -1, -1):
            pos = {n: i for i, n in enumerate(layers[r - 1] if downward else layers[r + 1])}
            nbrs = up if downward else down
            cur = {n: i for i, n in enumerate(layers[r])}
            layers[r].sort(key=lambda n: statistics.mean(pos[m] for m in nbrs[n]) if nbrs[n] else cur[n])
        c = crossings(layers)
        if c < best_c:
            best, best_c = [list(layer) for layer in layers], c

    x = {}
    for layer in best:
        pivot = min(range(len(layer)), key=lambda i: (abs(key[layer[i]][0]), i))
        x.update({n: (i - pivot) * COL for i, n in enumerate(layer)})
    for sweep in range(8):
        for layer in best if sweep % 2 == 0 else best[::-1]:
            want = {}
            for n in layer:
                around = [x[m] for m in up[n] + down[n] if m != "workflow_end"]
                want[n] = statistics.median(around) if around else x[n]
            x.update(_pack(layer, want))
    return {n: x[n] - x["workflow_start"] for n in hint}


def _crosses(p1: tuple, p2: tuple, p3: tuple, p4: tuple) -> bool:
    def ccw(a: tuple, b: tuple, c: tuple) -> float:
        return (c[1] - a[1]) * (b[0] - a[0]) - (b[1] - a[1]) * (c[0] - a[0])

    d1, d2, d3, d4 = ccw(p3, p4, p1), ccw(p3, p4, p2), ccw(p1, p2, p3), ccw(p1, p2, p4)
    return (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0) and 0 not in (d1, d2, d3, d4)


def _through_box(p: tuple, q: tuple, c: tuple) -> bool:
    """Liang-Barsky: does the straight arrow p -> q enter the task box centred on c?"""
    t0, t1 = 0.0, 1.0
    dx, dy = q[0] - p[0], q[1] - p[1]
    for pk, qk in ((-dx, p[0] - c[0] + HALF_W), (dx, c[0] + HALF_W - p[0]), (-dy, p[1] - c[1] + HALF_H), (dy, c[1] + HALF_H - p[1])):
        if pk == 0:
            if qk < 0:
                return False
        elif pk < 0:
            t0 = max(t0, qk / pk)
        else:
            t1 = min(t1, qk / pk)
        if t0 > t1:
            return False
    return True


def drawn_cost(where: dict, edges: list) -> tuple[int, int]:
    """(arrows crossing, arrows through a task box) for the canvas as Studio draws it."""
    at = {n: (v["x"], v["y"]) for n, v in where.items()}
    crossing = sum(
        1
        for i, (a, b) in enumerate(edges)
        for c, d in edges[i + 1 :]
        if len({a, b, c, d}) == 4 and _crosses(at[a], at[b], at[c], at[d])
    )
    through = sum(1 for a, b in edges for n, c in at.items() if n not in (a, b) and _through_box(at[a], at[b], c))
    return crossing, through


def _polish(where: dict, edges: list) -> dict:
    def cost() -> int:
        crossing, through = drawn_cost(where, edges)
        return crossing + 3 * through

    best = cost()
    for _ in range(12):
        improved = False
        rows: dict[int, list] = {}
        for n, v in where.items():
            if n not in ("workflow_start", "workflow_end"):
                rows.setdefault(v["y"], []).append(n)
        for y in sorted(rows):
            members = sorted(rows[y], key=lambda n: where[n]["x"])
            for a, b in itertools.pairwise(members):  # swap neighbours
                where[a]["x"], where[b]["x"] = where[b]["x"], where[a]["x"]
                c = cost()
                if c < best:
                    best, improved = c, True
                else:
                    where[a]["x"], where[b]["x"] = where[b]["x"], where[a]["x"]
            members = sorted(rows[y], key=lambda n: where[n]["x"])
            for i, n in enumerate(members):  # nudge sideways, keeping COL to both neighbours
                for step in (-NUDGE, NUDGE, -2 * NUDGE, 2 * NUDGE):
                    nx = where[n]["x"] + step
                    if i > 0 and nx - where[members[i - 1]]["x"] < COL:
                        continue
                    if i < len(members) - 1 and where[members[i + 1]]["x"] - nx < COL:
                        continue
                    old, where[n]["x"] = where[n]["x"], nx
                    c = cost()
                    if c < best:
                        best, improved = c, True
                        break
                    where[n]["x"] = old
        if not improved:
            break
    return where


def layout(tasks: dict, transitions: dict) -> dict[str, dict]:
    hint, row, edges = _rows(tasks, transitions)
    candidates = [
        {n: {"x": int(round(x / 10) * 10), "y": row[n] * ROW} for n, x in columns.items()}
        for columns in (_lane_columns(hint, row), _ordered_columns(hint, row, edges))
    ]
    best = min(candidates, key=lambda w: (lambda c: c[0] + 3 * c[1])(drawn_cost(w, edges)))
    return _polish(best, edges)


def workflow(
    name: str,
    description: str,
    inputs: dict,
    tasks: dict,
    transitions: dict,
    outputs: dict | None = None,
) -> dict:
    where = layout(tasks, transitions)
    tasks = {tid: {**t, "nodeLocation": where[tid]} for tid, t in tasks.items()}
    tasks["workflow_start"] = {
        "name": "workflow_start",
        "summary": "workflow_start",
        "groups": [],
        "nodeLocation": where["workflow_start"],
    }
    tasks["workflow_end"] = {
        "name": "workflow_end",
        "summary": "workflow_end",
        "groups": [],
        "nodeLocation": where["workflow_end"],
    }
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
        "inputSchema": {
            "type": "object",
            "properties": inputs,
            "required": [k for k, v in inputs.items() if v.get("required")],
        },
        "outputSchema": {"type": "object", "properties": outputs or {}},
        "tags": [],
        "preAutomationTime": 300000,
        "sla": 600000,
        "font_size": 12,
        "decorators": [],
    }


SNOW_TEMPLATE = "b1c8d15147810200e90d87e8dee490f7"  # PDI standard change template "Change VLAN on a Cisco switchport"
SNOW_GROUP = "287ebd7da9fe198100f92cc8d1d2154e"  # PDI assignment group "Network" (the change model requires one)


_SN_MODEL = VERSIONS["integrations"]["models"]["servicenow"]
SNOW_INTEGRATION = f"{_SN_MODEL['title']}:{_SN_MODEL['version']}"
SNOW_INTEGRATION_INSTANCE = _SN_MODEL["instance"]  # servicenow-api


def sni(
    name: str,
    summary: str,
    incoming: dict,
    x: int,
    y: int = 0,
    outgoing: dict | None = None,
) -> dict:
    """A lab-servicenow Integration Model task (S4f, ADR 0054). Replaces the adapter's
    genericAdapterRequest: the sys_id is a path parameter, so the workflow no longer builds URL
    strings, and the reply is the HTTP response object (payload in `response.body`)."""
    return task(
        name,
        SNOW_INTEGRATION,
        summary,
        {"adapter_id": SNOW_INTEGRATION_INSTANCE, **incoming},
        outgoing or {"response": None},
        location="Adapter",
        location_type=SNOW_INTEGRATION,
        x=x,
        y=y,
    )


def snow_state_i(summary: str, state: str, x: int, y: int, extra: dict | None = None) -> dict:
    """One step of the change model's state walk, through the Change API."""
    return sni(
        "updateStandardChange",
        summary,
        {"sys_id": "$var.d2.return_data", **nbi_body({"state": state, **(extra or {})})},
        x=x,
        y=y,
    )


NETBOX_EXPORT = (
    "Netbox"  # the adapter model's pronghorn export (task app / locationType)
)
NETBOX_INSTANCE = (
    "NetBox"  # the adapter instance the play creates (task input adapter_id)
)


def nb(
    name: str,
    summary: str,
    incoming: dict,
    x: int,
    y: int = 0,
    outgoing: dict | None = None,
) -> dict:
    """An adapter task addresses the model by its export and the instance by adapter_id (the
    workflow engine's validateAdapterMethod: app must equal a configured model's export)."""
    return task(
        name,
        NETBOX_EXPORT,
        summary,
        {"adapter_id": NETBOX_INSTANCE, **incoming},
        outgoing or {"result": None},
        location="Adapter",
        location_type=NETBOX_EXPORT,
        x=x,
        y=y,
    )


_NB_MODEL = VERSIONS["integrations"]["models"]["netbox"]
NETBOX_INTEGRATION = f"{_NB_MODEL['title']}:{_NB_MODEL['version']}"  # the model id, confirmed on 6.5.2
NETBOX_INTEGRATION_INSTANCE = _NB_MODEL["instance"]  # netbox-api


BODY_JSON = "application/json"


def nbi_body(payload: str | dict) -> dict:
    """The two inputs an Integration Model operation with a request body declares (measured on 6.5.2,
    S4f probe): the payload and its content type. The validator names them if they are missing -
    `Cannot find match for input: "requestBodyPayload" from model`. A $var binds to the payload."""
    return {"bodyContentType": BODY_JSON, "requestBodyPayload": payload}


def nbi(
    name: str,
    summary: str,
    incoming: dict,
    x: int,
    y: int = 0,
    outgoing: dict | None = None,
) -> dict:
    """An Integration Model task (S4f, ADR 0054). An integration is a virtual adapter: the model is
    addressed by `<title>:<version>` and the instance by adapter_id, as an adapter task addresses its
    export and instance (measured on 6.5.2, ADR 0045). `name` is the document's operationId."""
    # Measured on 6.5.2 (S4f probe, 2026-09-10): the platform validates an integration task against the
    # model on import and refuses the workflow as a draft if it disagrees. Two rules an adapter task did
    # not have: every input key must be a parameter the operation declares (an adapter took any key), and
    # the single output is named `response`, not `result` - "Output: \"result\" does not match model
    # output: \"response\"". So downstream tasks read $var.<id>.response, and the adapter's extra
    # `response` wrapper inside the value is gone with it.
    return task(
        name,
        NETBOX_INTEGRATION,
        summary,
        {"adapter_id": NETBOX_INTEGRATION_INSTANCE, **incoming},
        outgoing or {"response": None},
        location="Adapter",
        location_type=NETBOX_INTEGRATION,
        x=x,
        y=y,
    )


def selector(device_ref: str, x: int) -> tuple[dict, dict]:
    """Two tasks that render the Gateway Manager inventory selector for one node. $var references
    are substituted only at the top level of a task's inputs, so the node name is spliced into a
    JSON string with Tools.replace and the string is parsed with Tools.parse."""
    template = '[{"inventory": "%s", "nodeNames": ["__DEVICE__"]}]' % INVENTORY
    rep = task(
        "replace",
        "WorkFlowEngine",
        "selector JSON with the node name",
        {"str": template, "substr": "__DEVICE__", "newSubstr": device_ref},
        {"replacedString": None},
        display="Tools",
        x=x,
    )
    par = task(
        "parse",
        "WorkFlowEngine",
        "selector JSON -> object",
        {"text": "$var.0a.replacedString"},
        {"textObject": None},
        display="Tools",
        x=x + 300,
    )
    return rep, par


def chain(*ids: str) -> dict:
    """workflow_start -> ids... -> workflow_end on success."""
    seq = ["workflow_start", *ids, "workflow_end"]
    return {
        a: {b: {"state": "success", "type": "standard"}} for a, b in zip(seq, seq[1:])
    }


def show_command_transitions() -> dict:
    """chain() plus the one failure edge: sendCommand -> the message task -> workflow_end."""
    tr = chain("0a", "0b", "1a", "1b", "2a", "2b", "3a", "3b", "3c", "4a", "4b", "4c", "4d")
    # ADR 0066: a Gateway JSON-RPC error (a sealed Vault) finishes sendCommand `success`; 2b checks the result
    # and a failure ends the job through 5b (the error edge below only ever caught a THROWN task)
    tr["2b"]["5b"] = {"state": "failure", "type": "standard"}
    tr["5b"] = t("", "workflow_end")
    # The device HAS answered by 3a (raw is already a job variable). The parser is chosen from the node's NetBox
    # platform: a node NetBox does not have (every dev clab node, ADR 0063), a NetBox read that throws (NetBox down, or
    # its token unreadable with Vault sealed, ADR 0066) or a parser that fails on the runner used to dead-end the job at
    # 3c/4a and lose the answer. Each now ends with the raw output and the reason in parse_error (measured on dev
    # 2026-09-23: "Both str and newSubstr parameters must be of type string" at 3c).
    for tid in ("3a", "3b", "3c", "4a"):
        tr[tid]["5c"] = {"state": "error", "type": "standard"}
    tr["5c"] = t("", "workflow_end")
    # "error", not "failure": a Gateway task that 404s lands in state `error`, and a `failure` edge does
    # not fire for it - the job reported "5a could have led to the workflow end task, but did not" while
    # the failure edge sat right there (measured 2026-09-11). branch_vlan already guards its sendConfig
    # this way. `failure` is for a task that COMPLETES unsuccessfully: an evaluate, or a rejected form.
    tr["2a"]["5a"] = {"state": "error", "type": "standard"}
    tr["5a"] = t("", "workflow_end")
    return tr


# --- Count Devices in NetBox (S4.2): NetBox adapter page -> job variable device_count -----------
def device_count() -> dict:
    tasks = {
        "1a": nbi(
            "dcim_devices_list",
            "One page of devices (count comes with it)",
            {"limit": 1},  # `offset` is not a parameter the operation declares, and the platform refuses it
            x=0,
            outgoing={"response": "$var.job.devices"},
        ),
    }
    return workflow(
        WF["device_count"],
        "Reads the NetBox device list through the lab-netbox Integration Model and returns its count (PID S4.2, S4f)",
        {},
        tasks,
        chain("1a"),
        {"devices": {"type": "object"}},
    )


# --- Get Device Software Version (S4.3): Gateway 5 send-command on one inventory node -> job variable output --
def show_version() -> dict:
    tasks = {
        "1a": task(
            "sendCommand",
            "GatewayManager",
            "show version through Gateway 5",
            {
                "clusterId": CLUSTER,
                "commands": ["show version"],
                "inventory": "$var.0b.textObject",
            },
            {"result": "$var.job.show_version"},
            x=600,
        ),
    }
    tasks["0a"], tasks["0b"] = selector("$var.job.device", x=-300)
    # ADR 0066: with Vault sealed the Gateway answers a JSON-RPC error ("Vault is sealed") and sendCommand still
    # finishes `success`, so the job used to end `complete` with the error object as its "show version" - a silent
    # failure (measured on dev 2026-09-23). The result is checked, and a failure ends the job with a message.
    tasks["2a"] = evaluate("did the device answer?", "1a", "result", "result.results[0].success", "==", True, x=900)
    tasks["2b"] = note(
        "the device did not answer",
        "the command did not run on the device (credential or connection error); report this and do not retry",
        "device_error",
        x=900,
        y=-300,
    )
    tr = chain("0a", "0b", "1a", "2a")
    tr["1a"]["2b"] = {"state": "error", "type": "standard"}  # a thrown Gateway error (404 unknown node)
    tr["2a"]["2b"] = {"state": "failure", "type": "standard"}
    tr["2b"] = t("", "workflow_end")
    return workflow(
        WF["show_version"],
        "Runs 'show version' on one inventory node through Gateway 5 and returns the raw output (PID S4.3)",
        {
            "device": {
                "type": "string",
                "required": True,
                "description": "Inventory node name, e.g. br1-sw01",
            }
        },
        tasks,
        tr,
        {"show_version": {"type": "object"}, "device_error": {"type": "string"}},
    )


def replace(
    summary: str, template: str, marker: str, value_ref: str, x: int, y: int = 0
) -> dict:
    return task(
        "replace",
        "WorkFlowEngine",
        summary,
        {"str": template, "substr": marker, "newSubstr": value_ref},
        {"replacedString": None},
        display="Tools",
        x=x,
        y=y,
    )


def parse(summary: str, text_ref: str, x: int, y: int = 0) -> dict:
    return task(
        "parse",
        "WorkFlowEngine",
        summary,
        {"text": text_ref},
        {"textObject": None},
        display="Tools",
        x=x,
        y=y,
    )


def jq(
    summary: str,
    obj_ref: str,
    path: str,
    x: int,
    y: int = 0,
    to_job: str | None = None,
    optional: bool = False,
) -> dict:
    """Tools.query (json-query) extracts a nested value; optionally also published as a job variable.
    optional=True lets a missing value pass as null instead of failing the task."""
    return task(
        "query",
        "WorkFlowEngine",
        summary,
        {"pass_on_null": optional, "query": path, "obj": obj_ref},
        {"return_data": f"$var.job.{to_job}" if to_job else None},
        kind="operation",
        display="WorkFlowEngine",
        x=x,
        y=y,
    )


def evaluate(
    summary: str,
    task_id: str,
    variable: str,
    path: str,
    operator: str,
    value,
    x: int,
    y: int = 0,
) -> dict:
    return task(
        "evaluation",
        "WorkFlowEngine",
        summary,
        {
            "all_true_flag": True,
            "evaluation_groups": [
                {
                    "all_true_flag": True,
                    "evaluations": [
                        {
                            "query": path,
                            "operand_1": {"variable": variable, "task": task_id},
                            "operator": operator,
                            "operand_2": {"variable": value, "task": "static"},
                        }
                    ],
                }
            ],
            "options": {},
        },
        {"return_value": None},
        kind="operation",
        display="WorkFlowEngine",
        x=x,
        y=y,
    )


def num2str(summary: str, num_ref: str, x: int, y: int = 0) -> dict:
    """Tools.numberToString: replace() insists on string operands, NetBox ids and VIDs are numbers."""
    return task(
        "numberToString",
        "WorkFlowEngine",
        summary,
        {"num": num_ref, "radix": 10},
        {"numToString": None},
        display="Tools",
        x=x,
        y=y,
    )


def flag(summary: str, value: str, job_var: str, x: int, y: int = 0) -> dict:
    return task(
        "makeData",
        "WorkFlowEngine",
        summary,
        {"input": value, "outputType": "boolean", "variables": ""},
        {"output": f"$var.job.{job_var}"},
        display="Tools",
        x=x,
        y=y,
    )


def note(summary: str, value: str, job_var: str, x: int, y: int = 0) -> dict:
    """A literal string published as a job variable; `flag`'s sibling for a message rather than a bool."""
    return task(
        "makeData",
        "WorkFlowEngine",
        summary,
        {"input": value, "outputType": "string", "variables": ""},
        {"output": f"$var.job.{job_var}"},
        display="Tools",
        x=x,
        y=y,
    )


def view(
    summary: str,
    header: str,
    message_ref: str,
    body_ref: str,
    ok: str,
    cancel: str,
    x: int,
    y: int = 0,
) -> dict:
    return task(
        "ViewData",
        "WorkFlowEngine",
        summary,
        {
            "header": header,
            "message": message_ref,
            "body": body_ref,
            "variables": {},
            "btn_success": ok,
            "btn_failure": cancel,
        },
        {},
        kind="manual",
        display="Tools",
        view="/workflow_engine/task/ViewData",
        x=x,
        y=y,
    )


def t(a: str, b: str, state: str = "success") -> dict:
    return {b: {"state": state, "type": "standard"}}


# --- Lifecycle Manager + JSON Forms (S4d.3, ADR 0043/0044) ---------------------------------------------
FORM_NAME = VERSIONS["forms"]["approval"]
FORM_VIEW = "/json-forms/task/ShowJsonForm"  # measured on 6.5.2: app JsonForms, form_id by name, instance_data defaults, export out
CONFIG_PUSH = WF["config_push"]
# the object Lifecycle Manager stores as the instance (itential/lcm/branch-vlan.yaml schema); every marker is
# filled by one Tools.replace (a $var inside a nested object never resolves) and the string parsed at the end
INSTANCE_TPL = '{"branch": "__B__", "vid": __V__, "vlan_name": "__N__", "switch": "__S__", "netbox_vlan_id": __I__, "status": "__ST__"}'
INSTANCE_MARKERS = ("__B__", "__V__", "__N__", "__S__", "__I__", "__ST__")


def form_task(summary: str, data_ref: str, x: int, y: int = 0) -> dict:
    """JsonForms.ShowJsonForm manual task: the approver sees the form pre-filled from data_ref (a top-level $var
    object) and the submitted form comes back as export -> job variable approval (ADR 0044). The form has no
    reject button: the decision is a field, evaluated by the next task; a failure finish is a reject too."""
    return task(
        "ShowJsonForm",
        "JsonForms",
        summary,
        {"form_id": FORM_NAME, "instance_data": data_ref},
        {"export": "$var.job.approval"},
        kind="manual",
        display="JsonForms",
        view=FORM_VIEW,
        x=x,
        y=y,
    )


def instance_chain(
    ids: list[str], refs: dict, x: int, y: int = 0, *, to_job: bool = False
) -> dict:
    """Six replace tasks filling INSTANCE_TPL (branch, vid string, name, switch, NetBox id string, status) and a
    parse whose textObject is the instance object; to_job publishes it as $var.job.instance (what LCM stores)."""
    tasks, prev = {}, INSTANCE_TPL
    for i, (tid, marker) in enumerate(zip(ids[:6], INSTANCE_MARKERS)):
        tasks[tid] = replace(
            f"instance: {marker.strip('_').lower()}",
            prev,
            marker,
            refs[marker],
            x=x + 300 * i,
            y=y,
        )
        prev = f"$var.{tid}.replacedString"
    tasks[ids[6]] = parse("instance object", prev, x=x + 1800, y=y)
    if to_job:
        tasks[ids[6]]["variables"]["outgoing"] = {"textObject": "$var.job.instance"}
    return tasks


def child_job(
    summary: str,
    workflow_name: str,
    variables: dict,
    out_job_var: str,
    x: int,
    y: int = 0,
) -> dict:
    """WorkFlowEngine.childJob: starts another workflow by name with {task, value} references as its inputs and
    waits for it; job_details carries the finished child. A child that ends in error errors this task."""
    c = task(
        "childJob",
        "WorkFlowEngine",
        summary,
        {
            "task": "",
            "workflow": workflow_name,
            "variables": variables,
            "data_array": "",
            "transformation": "",
            "loopType": "",
        },
        {"job_details": f"$var.job.{out_job_var}"},
        kind="operation",
        display="WorkFlowEngine",
        x=x,
        y=y,
    )
    c["actor"] = "job"
    return c


# --- Add Branch VLAN (S4.4): reserve a VLAN in NetBox, approve, configure the branch switch ---------
# Inputs: branch (br1|br2), vlan_name. The next free VID in the branch's NetBox VLAN group is chosen
# by a few lines of Python on Gateway 5 (runCode; the NetBox adapter strips the trailing slash the
# available-vlans endpoint needs), the VLAN is created 'reserved' through the adapter, the operator
# approves in a ViewData task, Gateway 5 pushes "vlan N / name X" to <branch>-sw01, the NetBox VLAN
# goes active. A second run with the same name is a no-op (changed=false). A device failure or a
# rejection deletes the reservation and ends the job in error (no transition to the end).
NEXT_VID_CODE = """import json, sys
d = json.loads(sys.stdin.read() or "{}")
used = {v["vid"] for v in (d.get("body") or {}).get("results", [])}
vid = next((n for n in range(11, 100) if n not in used), None)
print(json.dumps({"vid": vid, "used": sorted(used)}))
"""

JOURNAL_BODY = (
    '{"assigned_object_type": "dcim.device", "assigned_object_id": __D__, '
    '"kind": "info", "comments": "__C__"}'
)
# adapter-netbox 1.0.10 has no journal method and its genericAdapterRequest splits the path on "/" (empty components
# dropped, each one URL-encoded), so it can never send the trailing slash NetBox's API requires (measured 2026-09-08):
# python on the Gateway 5 runner posts the entry with the token from the runner's environment (compose.override.yml).


VLAN_BODY = '{"site": {"slug": "__B__"}, "group": {"slug": "__G__"}, "vid": __V__, "name": "__N__", "status": "reserved"}'


def journal_chain(
    prefix: str,
    switch_ref: str,
    comment: str,
    vid_ref: str,
    name_ref: str,
    x: int,
    y: int = 0,
) -> tuple[list[str], dict]:
    """The NetBox journal entry on the switch (PID S4e.5, ADR 0048; converted by S4f, ADR 0054).

    This used to be python on the Gateway 5 runner, because adapter-netbox 1.0.10 drops the trailing
    slash `extras/journal-entries/` requires, so there was no way to post one through the adapter at all.
    The Integration Model keeps the slash, so the entry is two ordinary operations - look the device up,
    then post - and the runner no longer needs NETBOX_URL and NETBOX_TOKEN in its environment to do it.
    """
    ids = [f"{prefix}{i}" for i in range(1, 9)]
    tasks = {
        ids[0]: nbi(
            "dcim_devices_list",
            "journal: the switch in NetBox",
            {"name": switch_ref, "limit": 1},
            x=x,
            y=y,
        ),
        ids[1]: jq(
            "journal: device id",
            f"$var.{ids[0]}.response",
            "body.results[0].id",
            x=x + 300,
            y=y,
        ),
        ids[2]: num2str("journal: device id as string", f"$var.{ids[1]}.return_data", x=x + 600, y=y),
        ids[3]: replace(
            "journal: device",
            JOURNAL_BODY,
            "__D__",
            f"$var.{ids[2]}.numToString",
            x=x + 900,
            y=y,
        ),
        ids[4]: replace(
            "journal: comment",
            f"$var.{ids[3]}.replacedString",
            "__C__",
            comment,
            x=x + 1200,
            y=y,
        ),
        ids[5]: replace(
            "journal comment: vid",
            f"$var.{ids[4]}.replacedString",
            "__V__",
            vid_ref,
            x=x + 1500,
            y=y,
        ),
        ids[6]: replace(
            "journal comment: name",
            f"$var.{ids[5]}.replacedString",
            "__N__",
            name_ref,
            x=x + 1800,
            y=y,
        ),
        ids[7]: parse(
            "journal body object", f"$var.{ids[6]}.replacedString", x=x + 2100, y=y
        ),
    }
    tasks[f"{prefix}9"] = nbi(
        "extras_journal_entries_create",
        "journal entry on the switch",
        nbi_body(f"$var.{ids[7]}.textObject"),
        x=x + 2400,
        y=y,
    )
    ids.append(f"{prefix}9")
    return ids, tasks


def branch_vlan() -> dict:
    tasks = {
        # optional ServiceNow change (S4b): a standard change from the PDI's VLAN template, assigned to
        # the Network group (the change model refuses every state move without one), moved
        # New -> Scheduled -> Implement before the device work, work-noted with the NetBox
        # reservation, and Review -> Closed after it. Every state ServiceNow reports is collected
        # in the job variable change_states (sys_audit is not readable by the integration user).
        "d0": evaluate(
            "change request wanted?",
            "job",
            "change_request",
            "",
            "==",
            True,
            x=-600,
            y=-800,
        ),
        "d1": sni(
            "createStandardChange",
            "create the standard change (PDI VLAN template, Network group)",
            {
                "template_sys_id": SNOW_TEMPLATE,
                **nbi_body(
                    {
                        "short_description": "Add Branch VLAN: branch VLAN change (itential-enterprise-lab)",
                        "assignment_group": SNOW_GROUP,
                    }
                ),
            },
            x=-300,
            y=-800,
        ),
        "d2": jq(
            "change sys_id",
            "$var.d1.response",
            "body.result.sys_id.value",
            x=0,
            y=-800,
            to_job="change_sys_id",
        ),
        "d3": jq(
            "change number",
            "$var.d1.response",
            "body.result.number.value",
            x=0,
            y=-1000,
            to_job="change_number",
        ),
        "d5": snow_state_i("state: Scheduled", "-2", x=600, y=-800),
        "d6": jq(
            "state after scheduled",
            "$var.d5.response",
            "body.result.state.display_value",
            x=900,
            y=-800,
            to_job="change_state_scheduled",
        ),
        "d7": snow_state_i("state: Implement", "-1", x=1200, y=-800),
        "d8": jq(
            "state after implement",
            "$var.d7.response",
            "body.result.state.display_value",
            x=1500,
            y=-800,
            to_job="change_state_implement",
        ),
        # names and selectors: the switch is <branch>-sw01 unless switch_override names another node
        "0a": evaluate(
            "override given?", "job", "switch_override", "", "!=", "", x=-300, y=-200
        ),
        "1a": replace(
            "switch = <branch>-sw01",
            "__B__-sw01",
            "__B__",
            "$var.job.branch",
            x=0,
            y=-200,
        ),
        "0b": task(
            "makeData",
            "WorkFlowEngine",
            "switch = override",
            {
                "input": "$var.job.switch_override",
                "outputType": "string",
                "variables": "",
            },
            {"output": "$var.job.switch"},
            display="Tools",
            x=0,
            y=-600,
        ),
        "1b": replace(
            "selector JSON",
            '[{"inventory": "%s", "nodeNames": ["__D__"]}]' % INVENTORY,
            "__D__",
            "$var.job.switch",
            x=300,
            y=-200,
        ),
        "1c": parse("selector", "$var.1b.replacedString", x=600, y=-200),
        "1d": replace(
            "group slug = <branch>-user",
            "__B__-user",
            "__B__",
            "$var.job.branch",
            x=0,
            y=-400,
        ),
        # idempotency: does the VLAN already exist in the branch?
        "2a": nbi(
            "ipam_vlans_list",
            "VLAN by site + name",
            {"site": "$var.job.branch", "name": "$var.job.vlan_name"},
            x=900,
        ),
        "2b": evaluate(
            "already reserved?", "2a", "response", "body.count", ">", 0, x=1200
        ),
        "9a": flag("changed = false (no-op)", "false", "changed", x=1500, y=400),
        # reservation: next free VID in the branch VLAN group, chosen on Gateway 5
        "3a": nbi(
            "ipam_vlans_list",
            "VLANs already in the branch group",
            {"group": "$var.1d.replacedString", "limit": 200},
            x=1500,
            y=-200,
        ),
        "3b": task(
            "runCode",
            "GatewayManager",
            "next free VID (Python on Gateway 5)",
            {
                "clusterId": CLUSTER,
                "language": "python",
                "code": NEXT_VID_CODE,
                "data": "$var.3a.response",
                "safety": {"timeout": 30},
                "packages": [],
            },
            {"result": None},
            x=1800,
            y=-200,
        ),
        "a1": jq(
            "vid", "$var.3b.result", "stdout_json.vid", x=2100, y=-200, to_job="vid"
        ),
        "b2": num2str("vid as string", "$var.a1.return_data", x=2400, y=-200),
        "3e": replace(
            "body: site", VLAN_BODY, "__B__", "$var.job.branch", x=2700, y=-400
        ),
        "3f": replace(
            "body: group",
            "$var.3e.replacedString",
            "__G__",
            "$var.1d.replacedString",
            x=3000,
            y=-400,
        ),
        "c1": replace(
            "body: vid",
            "$var.3f.replacedString",
            "__V__",
            "$var.b2.numToString",
            x=3300,
            y=-400,
        ),
        "c2": replace(
            "body: name",
            "$var.c1.replacedString",
            "__N__",
            "$var.job.vlan_name",
            x=3600,
            y=-400,
        ),
        "c3": parse("body object", "$var.c2.replacedString", x=3900, y=-400),
        "3d": nbi(
            "ipam_vlans_create",
            "reserve the VLAN in NetBox",
            nbi_body("$var.c3.textObject"),
            x=4200,
            y=-200,
            outgoing={"response": "$var.job.reservation"},
        ),
        "a2": jq(
            "vlan id", "$var.3d.response", "body.id", x=4500, y=-200, to_job="vlan_id"
        ),
        "e0": evaluate(
            "change request wanted? (work note)",
            "job",
            "change_request",
            "",
            "==",
            True,
            x=4600,
            y=-600,
        ),
        "e1": replace(
            "work note text",
            "NetBox reservation: VLAN __V__ (VLAN object id __I__) reserved by Add Branch VLAN",
            "__V__",
            "$var.b2.numToString",
            x=4700,
            y=-800,
        ),
        "e2": num2str("vlan id as string", "$var.a2.return_data", x=4850, y=-1000),
        "e3": replace(
            "work note text (id)",
            "$var.e1.replacedString",
            "__I__",
            "$var.e2.numToString",
            x=5000,
            y=-800,
        ),
        "e4": replace(
            "work note body",
            '{"work_notes": "__N__"}',
            "__N__",
            "$var.e3.replacedString",
            x=5150,
            y=-800,
        ),
        "e5": parse("work note body object", "$var.e4.replacedString", x=5300, y=-800),
        "e6": sni(
            "updateChangeRequest",
            "work note: the NetBox reservation",
            {
                "sys_id": "$var.d2.return_data",
                "sysparm_fields": "number,state",
                **nbi_body("$var.e5.textObject"),
            },
            x=5450,
            y=-800,
        ),
        # approval on the JSON form (ADR 0044): the fields are the instance object with status reserved
        "a3": num2str("vlan id as string", "$var.a2.return_data", x=4700, y=-200),
        "4a": form_task("approval", "$var.a9.textObject", x=7000, y=-200),
        "4c": evaluate(
            "approved on the form?",
            "4a",
            "export",
            "decision",
            "==",
            "approve",
            x=7300,
            y=-200,
        ),
        # the stored instance once the switch is configured and NetBox says active (ADR 0043)
        "7b": replace(
            "instance: status active",
            "$var.a8.replacedString",
            "__ST__",
            "active",
            x=9500,
            y=-200,
        ),
        "7c": parse(
            "instance object (active)", "$var.7b.replacedString", x=9800, y=-200
        ),
        # no-op path: the instance from the VLAN NetBox already has (vid, id, status from the search result)
        "b3": jq(
            "existing vid",
            "$var.2a.response",
            "body.results[0].vid",
            x=1500,
            y=400,
            to_job="vid",
        ),
        "b4": jq(
            "existing NetBox id",
            "$var.2a.response",
            "body.results[0].id",
            x=1800,
            y=400,
            to_job="vlan_id",
        ),
        "b5": jq(
            "existing status",
            "$var.2a.response",
            "body.results[0].status.value",
            x=2100,
            y=400,
        ),
        "b6": num2str("existing vid as string", "$var.b3.return_data", x=2400, y=400),
        "b7": num2str("existing id as string", "$var.b4.return_data", x=2700, y=400),
        # configure the switch
        "5a": replace(
            "config: vid",
            "vlan __V__\n   name __N__",
            "__V__",
            "$var.b2.numToString",
            x=5400,
            y=-200,
        ),
        "5b": replace(
            "config: name",
            "$var.5a.replacedString",
            "__N__",
            "$var.job.vlan_name",
            x=5700,
            y=-200,
        ),
        "5c": task(
            "sendConfig",
            "GatewayManager",
            "push the VLAN through Gateway 5",
            {
                "clusterId": CLUSTER,
                "config": "$var.5b.replacedString",
                "inventory": "$var.1c.textObject",
            },
            {"result": "$var.job.config_result"},
            x=6000,
            y=-200,
        ),
        "5d": evaluate(
            "config applied?",
            "5c",
            "result",
            "result.results[0].success",
            "==",
            True,
            x=6300,
            y=-200,
        ),
        # ADR 0066 (owner decision 2026-09-24): send-config reports success: true for lines the switch REFUSES (measured),
        # so the switch's reply is read on the runner before NetBox is told the VLAN is active - as in Push Configuration with Approval
        "5e": jq("the switch's reply", "$var.5c.result", "result.results[0].output", x=6350, y=-400),
        "5f": task(
            "setObjectKey",
            "WorkFlowEngine",
            "reply for the runner",
            {"obj": {}, "path": ["output"], "value": "$var.5e.return_data"},
            {"object": None},
            display="Tools",
            x=6400,
            y=-400,
        ),
        "50": task(
            "runCode",
            "GatewayManager",
            "did the switch refuse a line? (Python on the runner)",
            {
                "clusterId": CLUSTER,
                "language": "python",
                "code": REPLY_CODE,
                "data": "$var.5f.object",
                "safety": {"timeout": 30},
                "packages": [],
            },
            {"result": None},
            x=6450,
            y=-400,
        ),
        "51": evaluate("every line accepted?", "50", "result", "stdout_json.rejected", "==", False, x=6500, y=-400),
        # a refusal: the lines are named, and the NetBox reservation is rolled back (8a). `vlan <vid>` may have been
        # accepted before `name` was refused, so the switch can hold the VLAN - the message says to check it.
        "52": jq("the refused lines", "$var.50.result", "stdout_json.message", x=6500, y=-600, to_job="device_error"),
        # the reply could not be read: the push most likely worked (the Gateway said so), so NetBox is NOT rolled back;
        # the job ends in error by the workflow's design, with the reason
        "8c": note(
            "the switch's reply could not be checked",
            "the VLAN was sent but the switch's reply could not be checked; NetBox still shows it reserved - check the switch",
            "device_error",
            x=6450,
            y=-700,
        ),
        "6a": nbi(
            "ipam_vlans_partial_update",
            "NetBox VLAN active",
            {"id": "$var.a2.return_data", **nbi_body({"status": "active"})},
            x=6600,
            y=-200,
        ),
        "f0": evaluate(
            "change request wanted? (close)",
            "job",
            "change_request",
            "",
            "==",
            True,
            x=6750,
            y=-600,
        ),
        "f1": snow_state_i("state: Review", "0", x=6900, y=-800),
        "f2": jq(
            "state after review",
            "$var.f1.response",
            "body.result.state.display_value",
            x=7050,
            y=-800,
            to_job="change_state_review",
        ),
        "f3": snow_state_i(
            "state: Closed",
            "3",
            x=7200,
            y=-800,
            extra={
                "close_code": "successful",
                "close_notes": "VLAN configured by Add Branch VLAN; NetBox VLAN active",
            },
        ),
        "f4": jq(
            "state after close",
            "$var.f3.response",
            "body.result.state.display_value",
            x=7350,
            y=-800,
            to_job="change_state_closed",
        ),
        "7a": flag("changed = true", "true", "changed", x=6900, y=-200),
        # rollback: remove the reservation; no transition to the end, so the job ends in error
        "8a": nbi(
            "ipam_vlans_destroy",
            "rollback: delete the NetBox reservation",
            {"id": "$var.a2.return_data"},
            x=6300,
            y=400,
        ),
        "8b": flag("rolled_back = true", "true", "rolled_back", x=6600, y=400),
    }
    tasks["1a"]["variables"]["outgoing"] = {"replacedString": "$var.job.switch"}
    tasks["7c"]["variables"]["outgoing"] = {"textObject": "$var.job.instance"}
    form_ids = ["a4", "a5", "a6", "a7", "a8", "b1", "a9"]
    tasks.update(
        instance_chain(
            form_ids,
            {
                "__B__": "$var.job.branch",
                "__V__": "$var.b2.numToString",
                "__N__": "$var.job.vlan_name",
                "__S__": "$var.job.switch",
                "__I__": "$var.a3.numToString",
                "__ST__": "reserved",
            },
            x=5000,
            y=-200,
        )
    )
    noop_ids = ["b8", "b9", "c4", "c5", "c6", "c7", "c8"]
    tasks.update(
        instance_chain(
            noop_ids,
            {
                "__B__": "$var.job.branch",
                "__V__": "$var.b6.numToString",
                "__N__": "$var.job.vlan_name",
                "__S__": "$var.job.switch",
                "__I__": "$var.b7.numToString",
                "__ST__": "$var.b5.return_data",
            },
            x=3000,
            y=400,
            to_job=True,
        )
    )
    order = ["1b", "1c", "1d", "2a", "2b"]
    reserve = ["3a", "3b", "a1", "b2", "3e", "3f", "c1", "c2", "c3", "3d", "a2", "e0"]
    journal_ids, journal_tasks = journal_chain(
        "ea",
        "$var.job.switch",
        "Lifecycle Manager branch-vlan create: VLAN __V__ (__N__) configured through Gateway 5 by Add Branch VLAN; NetBox VLAN active",
        "$var.b2.numToString",
        "$var.job.vlan_name",
        x=6600,
        y=-1100,
    )
    tasks.update(journal_tasks)
    apply = ["5a", "5b", "5c", "5d", "6a", *journal_ids, "f0"]
    tr = {}
    for a, b in zip(["workflow_start", *order], order):
        tr[a] = t("", b)
    tr["workflow_start"] = t("", "d0")
    tr["d0"] = {
        "d1": {"state": "success", "type": "standard"},
        "0a": {"state": "failure", "type": "standard"},
    }
    for a, b in zip(
        # d4/d9 built the two change URLs by string replacement; S4f made the sys_id a path parameter
        ["d1", "d2", "d3", "d5", "d6", "d7", "d8"],
        ["d2", "d3", "d5", "d6", "d7", "d8", "0a"],
    ):
        tr[a] = t("", b)
    tr["0a"] = {
        "0b": {"state": "success", "type": "standard"},
        "1a": {"state": "failure", "type": "standard"},
    }
    tr["0b"] = t("", "1b")
    tr["1a"] = t("", "1b")
    noop = ["b3", "b4", "b5", "b6", "b7", *noop_ids, "9a"]
    tr["2b"] = {
        noop[0]: {"state": "success", "type": "standard"},
        "3a": {"state": "failure", "type": "standard"},
    }
    for a, b in zip(noop, noop[1:]):
        tr[a] = t("", b)
    tr["9a"] = t("", "workflow_end")
    for a, b in zip(reserve, reserve[1:]):
        tr[a] = t("", b)
    tr["e0"] = {
        "e1": {"state": "success", "type": "standard"},
        "a3": {"state": "failure", "type": "standard"},
    }
    for a, b in zip(
        ["e1", "e2", "e3", "e4", "e5", "e6"], ["e2", "e3", "e4", "e5", "e6", "a3"]
    ):
        tr[a] = t("", b)
    # ADR 0066 (owner decision 2026-09-23): e6 is the one external call between the NetBox reservation (3d) and the
    # push (5c). If it throws - ServiceNow down, or its credential unreadable with Vault sealed - the reservation is
    # rolled back like a reject instead of left behind by a dead-end. Calls before 3d have reserved nothing; calls
    # after 5c must NOT roll back (the VLAN is live on the switch). Both keep the designed error-end.
    tr["e6"]["8a"] = {"state": "error", "type": "standard"}
    form_chain = ["a3", *form_ids, "4a"]
    for a, b in zip(form_chain, form_chain[1:]):
        tr[a] = t("", b)
    # the form: submitted -> the decision decides; a failure finish (API reject) rolls back like a reject
    tr["4a"] = {
        "4c": {"state": "success", "type": "standard"},
        "8a": {"state": "failure", "type": "standard"},
    }
    tr["4c"] = {
        "5a": {"state": "success", "type": "standard"},
        "8a": {"state": "failure", "type": "standard"},
    }
    for a, b in zip(apply, apply[1:]):
        tr[a] = t("", b)
    tr["5c"] = {
        "5d": {"state": "success", "type": "standard"},
        "8a": {"state": "error", "type": "standard"},
    }
    tr["5d"] = {
        "5e": {"state": "success", "type": "standard"},
        "8a": {"state": "failure", "type": "standard"},
    }
    tr["5e"] = {"5f": {"state": "success", "type": "standard"}, "8c": {"state": "error", "type": "standard"}}
    tr["5f"] = {"50": {"state": "success", "type": "standard"}, "8c": {"state": "error", "type": "standard"}}
    tr["50"] = {"51": {"state": "success", "type": "standard"}, "8c": {"state": "error", "type": "standard"}}
    tr["51"] = {"6a": {"state": "success", "type": "standard"}, "52": {"state": "failure", "type": "standard"}}
    tr["52"] = t("", "8a")
    tr["8c"] = {}  # the workflow's designed error-end, without a rollback: the VLAN is probably live
    tr["f0"] = {
        "f1": {"state": "success", "type": "standard"},
        "7b": {"state": "failure", "type": "standard"},
    }
    for a, b in zip(["f1", "f2", "f3", "f4"], ["f2", "f3", "f4", "7b"]):
        tr[a] = t("", b)
    tr["7b"] = t("", "7c")
    tr["7c"] = t("", "7a")
    tr["7a"] = t("", "workflow_end")
    tr["8a"] = t("", "8b")
    tr["8b"] = {}
    return workflow(
        WF["branch_vlan"],
        "Reserves a VLAN in NetBox for a branch, asks for approval on the JSON form %s, configures the branch "
        "switch through Gateway 5, activates the NetBox VLAN and publishes the Lifecycle Manager instance; rolls the reservation "
        "back on rejection or device failure (PID S4.4, S4d.3, ADR 0043/0044)"
        % FORM_NAME,
        {
            "branch": {
                "type": "string",
                "required": True,
                "description": "Branch site slug, e.g. br1",
            },
            "vlan_name": {
                "type": "string",
                "required": True,
                "description": "VLAN name to reserve and configure",
            },
            "switch_override": {
                "type": "string",
                "description": "Inventory node to configure instead of <branch>-sw01 (empty = default; used by verify to force a device failure)",
            },
            "change_request": {
                "type": "boolean",
                "description": "Open, work-note and close a ServiceNow standard change around the work (S4b)",
            },
        },
        tasks,
        tr,
        {
            "changed": {"type": "boolean"},
            "vid": {"type": "number"},
            "vlan_id": {"type": "number"},
            "reservation": {"type": "object"},
            "config_result": {"type": "object"},
            "rolled_back": {"type": "boolean"},
            "change_number": {"type": "string"},
            "change_sys_id": {"type": "string"},
            "change_state_scheduled": {"type": "string"},
            "change_state_implement": {"type": "string"},
            "change_state_review": {"type": "string"},
            "change_state_closed": {"type": "string"},
            "instance": {"type": "object"},
            "approval": {"type": "object"},
        },
    )


# --- Run Show Command on a Device (S4c.7): one show command -> raw text + structured data per vendor --------
# Gateway 5 send-command returns text; a runCode task on the glibc runner (ADR 0038) parses it
# with Genie (Cisco) or TextFSM/ntc-templates (Arista). The engine is chosen from the node's NetBox
# platform slug, which the workflow substitutes into the code (a top-level string input resolves
# `$var`; the nested `data` object would not), and the sendCommand result is the script's stdin.
PARSER_PACKAGES = (
    VERSIONS["parsers"]["cisco"]["packages"] + VERSIONS["parsers"]["arista"]["packages"]
)
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
""".replace(
    "__GENIE__", json.dumps(VERSIONS["parsers"]["cisco"]["netbox_platforms"])
).replace("__TEXTFSM__", json.dumps(VERSIONS["parsers"]["arista"]["netbox_platforms"]))


def show_command() -> dict:
    tasks = {
        # the command list is built from the input (a list literal would not resolve $var inside it)
        "1a": replace(
            "commands JSON", '["__C__"]', "__C__", "$var.job.command", x=0, y=-300
        ),
        "1b": parse("commands list", "$var.1a.replacedString", x=300, y=-300),
        "2a": task(
            "sendCommand",
            "GatewayManager",
            "run the command through Gateway 5",
            {
                "clusterId": CLUSTER,
                "commands": "$var.1b.textObject",
                "inventory": "$var.0b.textObject",
            },
            {"result": "$var.job.raw"},
            x=600,
        ),
        # the parser engine follows the node's NetBox platform
        "3a": nbi(
            "dcim_devices_list",
            "the node in NetBox",
            {"name": "$var.job.device", "limit": 1},
            x=0,
            y=300,
        ),
        "3b": jq(
            "platform slug",
            "$var.3a.response",
            "body.results[0].platform.slug",
            x=300,
            y=300,
        ),
        "3c": replace(
            "parse code for this platform",
            PARSE_CODE,
            "__P__",
            "$var.3b.return_data",
            x=600,
            y=300,
        ),
        "4a": task(
            "runCode",
            "GatewayManager",
            "parse (Genie for Cisco, TextFSM for Arista) on the runner",
            {
                "clusterId": CLUSTER,
                "language": "python",
                "code": "$var.3c.replacedString",
                "data": "$var.2a.result",
                "safety": {"timeout": 180},
                "packages": PARSER_PACKAGES,
            },
            {"result": None},
            x=900,
        ),
        "4b": jq(
            "structured result",
            "$var.4a.result",
            "stdout_json.parsed",
            x=1200,
            to_job="parsed",
        ),
        "4c": jq(
            "parser used",
            "$var.4a.result",
            "stdout_json.parser",
            x=1200,
            y=200,
            to_job="parser",
        ),
        "4d": jq(
            "parse error, if any",
            "$var.4a.result",
            "stdout_json.error",
            x=1200,
            y=400,
            to_job="parse_error",
        ),
    }
    tasks["0a"], tasks["0b"] = selector("$var.job.device", x=-600)
    # Gateway 5 answers 404 "Missing nodes - Inventory 'lab': [X]" for a device that is not there.
    # Without a failure transition the job dead-ends ("Job has no available transitions") and the
    # calling agent session never gets a result back - it hangs for ever, holding Ollama's one slot
    # (measured 2026-09-11: device-ops-local invented "R1"; the job errored in 69 s and the session
    # was still RUNNING 18 minutes later). This ends the job with a message the agent can report.
    tasks["2b"] = evaluate("did the device answer?", "2a", "result", "result.results[0].success", "==", True, x=450)
    tasks["5c"] = note(
        "raw output only: no parser applied",
        "no parser applied: the device's platform could not be read from NetBox (or the parse failed); the raw output is complete",
        "parse_error",
        x=1500,
        y=-300,
    )
    tasks["5b"] = note(
        "the device did not answer",
        "the command did not run on the device (credential or connection error); report this and do not retry",
        "device_error",
        x=900,
        y=-300,
    )
    tasks["5a"] = note(
        "the device is not in the inventory",
        "the device is not in the Inventory Manager inventory 'lab'; check the name and do not retry",
        "device_error",
        x=600,
        y=-300,
    )

    return workflow(
        WF["show_command"],
        "Runs one show command on an inventory node through Gateway 5 and returns the raw output plus structured data: "
        "Genie for Cisco platforms, TextFSM (ntc-templates) for Arista, chosen from the node's NetBox platform (PID S4c.7, ADR 0038)",
        {
            "device": {
                "type": "string",
                "required": True,
                "description": "Inventory node name, e.g. br1-wan01",
            },
            "command": {
                "type": "string",
                "required": True,
                "description": "One show command, e.g. show ip interface brief",
            },
        },
        tasks,
        show_command_transitions(),
        {
            "raw": {"type": "object"},
            "device_error": {"type": "string"},
            "parsed": {"type": ["object", "array", "null"]},
            "parser": {"type": "string"},
            "parse_error": {"type": "string"},
        },
    )  # "" when the parse succeeded


# --- Run Show Command on All Devices (S4d.5, ADR 0046): one show command on every lab device in one call ------------------
# A local model asked about "all devices" invents node names and loops (measured); this workflow is the
# deterministic fan-out: the Configuration Manager device list (the lab inventory), one multi-node
# send-command, and one parse on the runner keyed by device (Genie for cisco_ios, TextFSM for arista_eos).
# Each device's parsed data is capped at 1500 characters so twelve devices fit a small model's context.
SHOW_ALL_CODE = """import json, sys
GENIE = {"cisco_ios": "iosxe"}  # the lab's cisco_ios nodes are IOS-XE (C8000v)
TEXTFSM = {"arista_eos": "arista_eos"}
LIMIT = 1500
d = json.loads(sys.stdin.read() or "{}")
ostype = {x.get("name"): x.get("ostype") for x in (d.get("devices") or {}).get("list") or []}
out = {"devices": [], "results": {}, "parser_errors": {}}
for r in ((d.get("result") or {}).get("results") or []):
    name, output, command = r.get("name"), r.get("output") or "", r.get("command") or ""
    os_ = ostype.get(name)
    entry = {"ostype": os_, "parser": "none", "parsed": None, "truncated": False}
    if not r.get("success", True):
        entry["error"] = "command failed"
        entry["raw_head"] = output[:600]
    else:
        try:
            if os_ in GENIE:
                from genie.conf.base import Device
                dev = Device(name="x", os=GENIE[os_])
                dev.custom.setdefault("abstraction", {"order": ["os"]})
                entry["parsed"], entry["parser"] = dev.parse(command, output=output), "genie"
            elif os_ in TEXTFSM:
                from ntc_templates.parse import parse_output
                entry["parsed"], entry["parser"] = parse_output(platform=TEXTFSM[os_], command=command, data=output), "textfsm"
            else:
                entry["error"] = "no parser for ostype %s" % os_
                entry["raw_head"] = output[:600]
        except Exception as e:  # unsupported command or empty output: keep the head of the raw text
            entry["error"] = type(e).__name__ + ": " + str(e)[:200]
            entry["raw_head"] = output[:600]
    text = json.dumps(entry["parsed"]) if entry["parsed"] is not None else ""
    if len(text) > LIMIT:
        entry["parsed"], entry["truncated"] = text[:LIMIT] + "...", True
    if entry.get("error"):
        out["parser_errors"][name] = entry["error"]
    out["devices"].append(name)
    out["results"][name] = entry
out["devices_checked"] = len(out["devices"])
print(json.dumps(out))
"""


def show_all() -> dict:
    tasks = {
        "1a": task(
            "getDevicesFiltered",
            "ConfigurationManager",
            "every lab device (the inventory through the broker)",
            {"options": {"start": 0, "limit": 500}},
            {"devices": None},
            x=0,
        ),
        "1b": jq(
            "device names", "$var.1a.devices", "list[*].name", x=300, to_job="devices"
        ),
        "1c": task(
            "join",
            "WorkFlowEngine",
            "names joined for the selector",
            {"arr": "$var.1b.return_data", "separator": '","'},
            {"joinedElements": None},
            display="Tools",
            x=600,
        ),
        "1d": replace(
            "selector JSON with every node",
            '[{"inventory": "%s", "nodeNames": ["__N__"]}]' % INVENTORY,
            "__N__",
            "$var.1c.joinedElements",
            x=900,
        ),
        "1e": parse("selector object", "$var.1d.replacedString", x=1200),
        "2a": replace(
            "commands JSON", '["__C__"]', "__C__", "$var.job.command", x=0, y=300
        ),
        "2b": parse("commands list", "$var.2a.replacedString", x=300, y=300),
        "3a": task(
            "sendCommand",
            "GatewayManager",
            "the command on every node in one Gateway 5 call",
            {
                "clusterId": CLUSTER,
                "commands": "$var.2b.textObject",
                "inventory": "$var.1e.textObject",
            },
            {"result": None},
            x=1500,
        ),
        "3b": task(
            "setObjectKey",
            "WorkFlowEngine",
            "results + device list (ostype per device) for the parser",
            {"obj": "$var.3a.result", "path": ["devices"], "value": "$var.1a.devices"},
            {"object": None},
            display="Tools",
            x=1800,
        ),
        "4a": task(
            "runCode",
            "GatewayManager",
            "parse per device on the runner (Genie / TextFSM)",
            {
                "clusterId": CLUSTER,
                "language": "python",
                "code": SHOW_ALL_CODE,
                "data": "$var.3b.object",
                "safety": {"timeout": 240},
                "packages": PARSER_PACKAGES,
            },
            {"result": None},
            x=2100,
        ),
        "4b": jq(
            "results per device",
            "$var.4a.result",
            "stdout_json.results",
            x=2400,
            to_job="results",
        ),
        "4c": jq(
            "devices checked",
            "$var.4a.result",
            "stdout_json.devices_checked",
            x=2400,
            y=200,
            to_job="devices_checked",
        ),
        "4d": jq(
            "parser errors",
            "$var.4a.result",
            "stdout_json.parser_errors",
            x=2400,
            y=400,
            to_job="parser_errors",
        ),
    }
    # ADR 0066: the whole call failing (a sealed Vault: the Gateway answers a JSON-RPC error and sendCommand
    # still finishes `success`) used to reach the parser as if devices had answered. The envelope's status is checked;
    # a single device failing is still per-device data for the parser, as before.
    tasks["3c"] = evaluate("did the Gateway run the command?", "3a", "result", "status", "==", "completed", x=1650, y=-200)
    tasks["5a"] = note(
        "the Gateway could not run the command",
        "the command did not run on any device (credential or Gateway error); report this and do not retry",
        "device_error",
        x=1650,
        y=-400,
    )
    tr = chain("1a", "1b", "1c", "1d", "1e", "2a", "2b", "3a", "3c", "3b", "4a", "4b", "4c", "4d")
    tr["3a"]["5a"] = {"state": "error", "type": "standard"}
    tr["3c"]["5a"] = {"state": "failure", "type": "standard"}
    tr["5a"] = t("", "workflow_end")
    return workflow(
        WF["show_all"],
        "Runs one show command on every lab device in one Gateway 5 call and returns the parsed result per device "
        "(Genie for cisco_ios, TextFSM for arista_eos, each capped at 1500 characters); the agents' fleet-wide read (PID S4d.5, ADR 0046)",
        {
            "command": {
                "type": "string",
                "required": True,
                "description": "One show command, e.g. show ip interface brief",
            }
        },
        tasks,
        tr,
        {
            "devices": {"type": "array"},
            "devices_checked": {"type": "number"},
            "results": {"type": "object"},
            "parser_errors": {"type": "object"},
            "device_error": {"type": "string"},
        },
    )


# --- Push Configuration with Approval (S4d, ADR 0040/0041): the one governed write path ---------------------
# Inputs: device, config (CLI lines), reason. The operator sees device, reason and the exact lines in
# a Work Center approval; on approval Gateway 5 pushes them with send-config and saves the running
# configuration with "write memory" (IOS-XE and EOS both accept it). A rejection ends the job in
# error with nothing touched. The compliance/remediation agents get this workflow as their only
# write tool; Golden Config never remediates on its own (ADR 0040).
# Measured 2026-09-24: Gateway 5 send-config reports success: true for lines the device REJECTS (IOS-XE answered
# "% Invalid input detected" inside `output`). IOS-XE and EOS mark a refused line with a line starting "% " naming the
# error; warnings ("% Warning") are not refusals. This reads the device's reply on the runner (ADR 0066).
REPLY_CODE = """import json, re, sys
d = json.loads(sys.stdin.read() or "{}")
lines = str(d.get("output") or "").splitlines()
refusal = re.compile(r"^% ?(Invalid|Incomplete|Ambiguous|Unknown|Unrecognized|Bad|Error)", re.I)
rejections = []
for i, ln in enumerate(lines):
    if refusal.match(ln.strip()):
        cmd = next((l.split("#", 1)[1].strip() for l in reversed(lines[:i]) if "(config" in l and "#" in l), "")
        rejections.append({"command": cmd, "error": ln.strip()})
msg = ""
if rejections:
    msg = ("the device rejected %d line(s): " % len(rejections)
           + "; ".join("%s (%s)" % (r["command"], r["error"]) for r in rejections)
           + ". Lines it accepted before are in the running configuration and were NOT saved")
print(json.dumps({"rejected": bool(rejections), "rejections": rejections, "message": msg}))
"""


def config_push() -> dict:
    tasks = {
        "1a": replace(
            "summary: device",
            "Push to __D__ (__R__):",
            "__D__",
            "$var.job.device",
            x=0,
            y=-200,
        ),
        "1b": replace(
            "summary: reason",
            "$var.1a.replacedString",
            "__R__",
            "$var.job.reason",
            x=300,
            y=-200,
        ),
        "2a": view(
            "approval",
            "Approve configuration push",
            "$var.1b.replacedString",
            "$var.job.config",
            "Approve",
            "Reject",
            x=600,
        ),
        "3a": task(
            "sendConfig",
            "GatewayManager",
            "push the lines through Gateway 5",
            {
                "clusterId": CLUSTER,
                "config": "$var.job.config",
                "inventory": "$var.0b.textObject",
            },
            {"result": "$var.job.config_result"},
            x=900,
        ),
        "3b": evaluate(
            "config applied?",
            "3a",
            "result",
            "result.results[0].success",
            "==",
            True,
            x=1200,
        ),
        "4a": task(
            "sendCommand",
            "GatewayManager",
            "save the running configuration",
            {
                "clusterId": CLUSTER,
                "commands": ["write memory"],
                "inventory": "$var.0b.textObject",
            },
            {"result": "$var.job.save_result"},
            x=1500,
        ),
        "5a": flag("changed = true", "true", "changed", x=1800),
        "9a": flag("changed = false (rejected)", "false", "changed", x=900, y=400),
        # the device's own reply, read for refused lines before anything is saved
        "3c": jq("the device's reply", "$var.3a.result", "result.results[0].output", x=1250, y=-200),
        "3d": task(
            "setObjectKey",
            "WorkFlowEngine",
            "reply for the runner",
            {"obj": {}, "path": ["output"], "value": "$var.3c.return_data"},
            {"object": None},
            display="Tools",
            x=1300,
            y=-200,
        ),
        "3e": task(
            "runCode",
            "GatewayManager",
            "did the device refuse a line? (Python on the runner)",
            {
                "clusterId": CLUSTER,
                "language": "python",
                "code": REPLY_CODE,
                "data": "$var.3d.object",
                "safety": {"timeout": 30},
                "packages": [],
            },
            {"result": None},
            x=1350,
            y=-200,
        ),
        "3f": evaluate("every line accepted?", "3e", "result", "stdout_json.rejected", "==", False, x=1400, y=-200),
        "8c": jq("the refused lines", "$var.3e.result", "stdout_json.message", x=1400, y=-500, to_job="device_error"),
        "8d": flag("changed = false (lines refused)", "false", "changed", x=1700, y=-500),
        "8e": note(
            "the device's reply could not be checked",
            "the lines were sent but the device's reply could not be checked; nothing was saved - check the device",
            "device_error",
            x=1400,
            y=-700,
        ),
    }
    tasks["0a"], tasks["0b"] = selector("$var.job.device", x=-600)
    tr = chain("0a", "0b", "1a", "1b", "2a", "3a", "3b", "4a", "5a")
    tr["2a"] = {
        "3a": {"state": "success", "type": "standard"},
        "9a": {"state": "failure", "type": "standard"},
    }
    # "config applied?" false - the Gateway answered an error instead of running the lines (ADR 0066: Vault
    # sealed) - used to have no transition, so the job dead-ended and a calling agent hung (ADR 0059). Owner decision
    # 2026-09-23: it now ends cleanly with changed = false and the reason. NOT detected (measured 2026-09-24): lines the
    # DEVICE rejects - send-config still reports success: true while IOS-XE answers "% Invalid input detected".
    tr["3b"] = {
        "3c": {"state": "success", "type": "standard"},
        "8a": {"state": "failure", "type": "standard"},
    }
    # ADR 0066 (owner decision 2026-09-24): a refused line skips the save and ends with the refusal in device_error
    tr["3c"] = {"3d": {"state": "success", "type": "standard"}, "8e": {"state": "error", "type": "standard"}}
    tr["3d"] = {"3e": {"state": "success", "type": "standard"}, "8e": {"state": "error", "type": "standard"}}
    tr["3e"] = {"3f": {"state": "success", "type": "standard"}, "8e": {"state": "error", "type": "standard"}}
    tr["3f"] = {"4a": {"state": "success", "type": "standard"}, "8c": {"state": "failure", "type": "standard"}}
    tr["8c"] = t("", "8d")
    tr["8d"] = t("", "workflow_end")
    tr["8e"] = t("", "8b")
    tr[
        "9a"
    ] = {}  # rejected: no transition to the end, the job ends in error with nothing pushed
    # A device the inventory does not have is NOT the rejection case: sendConfig answers 404 and,
    # with no failure edge, the job dead-ends and the calling agent session hangs for ever (measured
    # 2026-09-11 on Run Show Command on a Device). This ends the job cleanly with changed = false and a message.
    # The reject path above keeps its deliberate error-end: that semantic is a separate decision.
    tasks["6a"] = note(
        "the device is not in the inventory",
        "the device is not in the Inventory Manager inventory 'lab'; nothing was pushed",
        "device_error",
        x=900,
        y=-400,
    )
    tasks["6b"] = flag("changed = false (no such device)", "false", "changed", x=1200, y=-400)
    tr["3a"]["6a"] = {"state": "error", "type": "standard"}  # Gateway error, not a failure finish
    tr["6a"] = t("", "6b")
    tr["6b"] = t("", "workflow_end")
    # `write memory` failing is not the same case: the configuration IS on the device, so the job
    # still ends with changed = true and says the save did not happen. Without this edge it would
    # dead-end and hang the caller exactly like 3a did.
    tasks["7a"] = note(
        "the save did not happen",
        "the configuration was applied but 'write memory' failed; it is not persisted across a reload",
        "save_error",
        x=1500,
        y=-400,
    )
    tr["4a"]["7a"] = {"state": "error", "type": "standard"}
    tr["7a"] = t("", "5a")
    # ADR 0066: the save's result is checked as well - a Gateway error answer finishes sendCommand `success`
    tasks["4b"] = evaluate("saved?", "4a", "result", "result.results[0].success", "==", True, x=1650, y=-200)
    tr["4a"] = {"4b": {"state": "success", "type": "standard"}, "7a": {"state": "error", "type": "standard"}}
    tr["4b"] = {"5a": {"state": "success", "type": "standard"}, "7a": {"state": "failure", "type": "standard"}}
    tasks["8a"] = note(
        "the configuration was not applied",
        "the configuration was not applied (the Gateway could not run it, e.g. its credential could not be read); nothing changed",
        "device_error",
        x=1200,
        y=-600,
    )
    tasks["8b"] = flag("changed = false (not applied)", "false", "changed", x=1500, y=-600)
    tr["8a"] = t("", "8b")
    tr["8b"] = t("", "workflow_end")
    return workflow(
        WF["config_push"],
        "Pushes operator-supplied configuration lines to one inventory node through Gateway 5 after a Work Center "
        "approval and saves the running configuration; the only write path for compliance remediation (PID S4d, ADR 0040)",
        {
            "device": {
                "type": "string",
                "required": True,
                "description": "Inventory node name, e.g. br1-wan01",
            },
            "config": {
                "type": "string",
                "required": True,
                "description": "Configuration lines to push, one per line",
            },
            "reason": {
                "type": "string",
                "required": True,
                "description": "Why, shown to the approver (ticket, compliance report id)",
            },
        },
        tasks,
        tr,
        {
            "changed": {"type": "boolean"},
            "config_result": {"type": "object"},
            "save_result": {"type": "object"},
            "device_error": {"type": "string"},
            "save_error": {"type": "string"},
        },
    )


# --- Run Nightly Compliance Check (S4d.1, ADR 0040): the nightly schedule trigger's target ------------------
# No inputs: Operations Manager schedule triggers on 6.5.2 do not persist formData (measured 2026-09-07,
# PATCH echoes it, GET returns null), so the plan is found by its name from versions.yaml. The search
# matches a regex (an unescaped "-" misses, anchors work). The run is asynchronous; the plan instance
# id and the plan id are published as job variables for the verify script and the agents.
PLAN_NAME = VERSIONS["golden_config"]["plan"]


def compliance_run() -> dict:
    tasks = {
        "1a": task(
            "searchCompliancePlans",
            "ConfigurationManager",
            "the plan by name",
            {"name": "^" + PLAN_NAME + "$", "options": {"start": 0, "limit": 10}},
            {"compliancePlans": None},
            x=0,
        ),
        "1b": jq(
            "plan id", "$var.1a.compliancePlans", "plans[0].id", x=300, to_job="plan_id"
        ),
        "2a": task(
            "runCompliancePlan",
            "ConfigurationManager",
            "run the compliance plan (asynchronous)",
            {"planId": "$var.1b.return_data", "options": {}},
            {"response": "$var.job.run"},
            x=600,
        ),
    }
    return workflow(
        WF["compliance_run"],
        "Runs the Configuration Manager compliance plan %s; scheduled nightly by Operations Manager (PID S4d.1, ADR 0040)"
        % PLAN_NAME,
        {},
        tasks,
        chain("1a", "1b", "2a"),
        {"plan_id": {"type": "string"}, "run": {"type": "object"}},
    )


# --- Back Up All Device Configs (S4d.2, ADR 0042): every Configuration Manager device backed up, nightly ---------
# No inputs (schedule triggers do not persist formData). The device list comes from Configuration Manager
# itself (the InventoryBroker devices, ADR 0039), so a node added to NetBox is backed up on the next run
# with no change here. Loop = WorkFlowEngine forEach: the "loop" transition starts an iteration, a body
# task with no outgoing transition returns to the forEach, "success" fires when the array is exhausted.
def backup_all() -> dict:
    tasks = {
        "1a": task(
            "getDevicesFiltered",
            "ConfigurationManager",
            "every device Configuration Manager knows",
            {"options": {"start": 0, "limit": 500}},
            {"devices": None},
            x=0,
        ),
        "1b": jq(
            "device names", "$var.1a.devices", "list[*].name", x=300, to_job="devices"
        ),
        "2a": task(
            "forEach",
            "WorkFlowEngine",
            "one device at a time",
            {"data_array": "$var.1b.return_data"},
            {"current_item": None},
            kind="operation",
            display="WorkFlowEngine",
            x=600,
        ),
        "3a": task(
            "backUpDevice",
            "ConfigurationManager",
            "backup through the broker (Gateway 5)",
            {
                "name": "$var.2a.current_item",
                "options": {
                    "description": "nightly backup (Back Up All Device Configs)",
                    "notes": "",
                },
            },
            {"status": None},
            x=900,
            y=300,
        ),
    }
    tr = {
        "workflow_start": t("", "1a"),
        "1a": t("", "1b"),
        "1b": t("", "2a"),
        "2a": {
            "3a": {"state": "loop", "type": "standard"},
            "workflow_end": {"state": "success", "type": "standard"},
        },
        "3a": {},
    }
    return workflow(
        WF["backup_all"],
        "Backs up every Configuration Manager device through the InventoryBroker (Gateway 5); "
        "scheduled nightly by Operations Manager (PID S4d.2, ADR 0042)",
        {},
        tasks,
        tr,
        {"devices": {"type": "array"}},
    )


# --- Remove Branch VLAN (S4d.3, ADR 0043): the Lifecycle Manager delete action -------------------
# Input: the instance object LCM passes as the job variable `instance` (branch, vid, vlan_name, switch,
# netbox_vlan_id, status). The switch is read with `show vlan <vid>` and, only when the VLAN is present,
# Push Configuration with Approval runs as a child job with `no vlan <vid>` (its Work Center approval is the gate); the
# NetBox VLAN is deleted after the device, so a rejected push changes nothing. A rejected push leaves the
# push job in error and this job waiting on it (a job in error is retryable on 6.5.2): cancelling the
# execution ends both and keeps the instance. Run on a retired VLAN it is a no-op (changed false, no push).
# An instance without data fails the first read and takes the no-data path.
def branch_vlan_delete() -> dict:
    tasks = {
        "1a": jq("branch", "$var.job.instance", "branch", x=0, y=-300),
        "1b": jq("vid", "$var.job.instance", "vid", x=300, y=-300, to_job="vid"),
        "1c": jq("vlan name", "$var.job.instance", "vlan_name", x=600, y=-300),
        "1d": jq(
            "switch", "$var.job.instance", "switch", x=900, y=-300, to_job="switch"
        ),
        "1e": jq(
            "NetBox VLAN id", "$var.job.instance", "netbox_vlan_id", x=1200, y=-300
        ),
        "1f": num2str("vid as string", "$var.1b.return_data", x=1500, y=-300),
        "2f": num2str("NetBox id as string", "$var.1e.return_data", x=1800, y=-300),
        "2a": flag("changed = false (nothing yet)", "false", "changed", x=2700, y=-300),
        # NetBox: the VLAN by site + name (the same lookup the create uses), deleted when present
        "3a": nbi(
            "ipam_vlans_list",
            "the VLAN in NetBox",
            {"site": "$var.1a.return_data", "name": "$var.1c.return_data"},
            x=3000,
            y=-300,
        ),
        "3b": evaluate(
            "still in NetBox?", "3a", "response", "body.count", ">", 0, x=3300, y=-300
        ),
        "3c": jq(
            "its NetBox id", "$var.3a.response", "body.results[0].id", x=3600, y=-500
        ),
        "3d": nbi(
            "ipam_vlans_destroy",
            "delete the NetBox VLAN",
            {"id": "$var.3c.return_data"},
            x=3900,
            y=-500,
        ),
        "3e": flag("netbox_deleted = true", "true", "netbox_deleted", x=4200, y=-500),
        "2b": flag("changed = true", "true", "changed", x=4500, y=-500),
        "3f": flag(
            "netbox_deleted = false (absent)", "false", "netbox_deleted", x=3900, y=-100
        ),
        # the switch: read before write; `no vlan` is pushed only when the VLAN is configured
        "4a": replace(
            "show vlan command list",
            '["show vlan __V__"]',
            "__V__",
            "$var.1f.numToString",
            x=4800,
            y=-300,
        ),
        "4b": parse("command list", "$var.4a.replacedString", x=5100, y=-300),
        "4c": task(
            "sendCommand",
            "GatewayManager",
            "show vlan <vid> through Gateway 5",
            {
                "clusterId": CLUSTER,
                "commands": "$var.4b.textObject",
                "inventory": "$var.0b.textObject",
            },
            {"result": "$var.job.show_vlan"},
            x=5400,
            y=-300,
        ),
        "4d": evaluate(
            "VLAN absent on the switch?",
            "4c",
            "result",
            "result.results[0].output",
            "contains",
            "not found",
            x=5700,
            y=-300,
        ),
        "5a": replace(
            "config: no vlan <vid>",
            "no vlan __V__",
            "__V__",
            "$var.1f.numToString",
            x=6000,
            y=-500,
        ),
        "5b": replace(
            "reason: vid",
            "Lifecycle Manager branch-vlan delete: remove VLAN __V__ (__N__) from __S__",
            "__V__",
            "$var.1f.numToString",
            x=6300,
            y=-500,
        ),
        "5c": replace(
            "reason: name",
            "$var.5b.replacedString",
            "__N__",
            "$var.1c.return_data",
            x=6600,
            y=-500,
        ),
        "5d": replace(
            "reason: switch",
            "$var.5c.replacedString",
            "__S__",
            "$var.1d.return_data",
            x=6900,
            y=-500,
        ),
        "5e": child_job(
            "remove the VLAN through the governed push (approval in Work Center)",
            CONFIG_PUSH,
            {
                "device": {"task": "1d", "value": "return_data"},
                "config": {"task": "5a", "value": "replacedString"},
                "reason": {"task": "5d", "value": "replacedString"},
            },
            "push_job",
            x=7200,
            y=-500,
        ),
        "2c": flag("switch_changed = true", "true", "switch_changed", x=7500, y=-500),
        "2d": flag("changed = true", "true", "changed", x=7800, y=-500),
        "6a": flag(
            "switch_changed = false (absent)", "false", "switch_changed", x=6000, y=-100
        ),
        # no data on the instance (a rejected create): nothing to delete, LCM retires it
        "9a": flag(
            "changed = false (no instance data)", "false", "changed", x=300, y=300
        ),
    }
    tasks["0a"], tasks["0b"] = selector("$var.1d.return_data", x=2100)
    tasks["0a"]["nodeLocation"]["y"] = tasks["0b"]["nodeLocation"]["y"] = -300
    final_ids = ["7a", "7b", "7c", "7d", "7e", "7f", "2e"]
    tasks.update(
        instance_chain(
            final_ids,
            {
                "__B__": "$var.1a.return_data",
                "__V__": "$var.1f.numToString",
                "__N__": "$var.1c.return_data",
                "__S__": "$var.1d.return_data",
                "__I__": "$var.2f.numToString",
                "__ST__": "deleted",
            },
            x=8100,
            y=-300,
            to_job=True,
        )
    )
    # order: read the switch, push through the approval, and only then touch NetBox, so a rejected push changes nothing
    head = [
        "1a",
        "1b",
        "1c",
        "1d",
        "1e",
        "1f",
        "2f",
        "0a",
        "0b",
        "2a",
        "4a",
        "4b",
        "4c",
        "4d",
    ]
    tr = {"workflow_start": t("", "1a")}
    for a, b in zip(head, head[1:]):
        tr[a] = t("", b)
    tr["1a"] = {
        "1b": {"state": "success", "type": "standard"},
        "9a": {"state": "failure", "type": "standard"},
    }
    tr["4d"] = {
        "6a": {"state": "success", "type": "standard"},
        "5a": {"state": "failure", "type": "standard"},
    }
    journal_ids, journal_tasks = journal_chain(
        "eb",
        "$var.1d.return_data",
        "Lifecycle Manager branch-vlan delete: VLAN __V__ (__N__) removed through %s by Remove Branch VLAN"
        % CONFIG_PUSH,
        "$var.1f.numToString",
        "$var.1c.return_data",
        x=7200,
        y=-900,
    )
    tasks.update(journal_tasks)
    push_path = ["5a", "5b", "5c", "5d", "5e", *journal_ids, "2c"]
    for a, b in zip(push_path, push_path[1:] + ["2d"]):
        tr[a] = t(
            "", b
        )  # 5e waits while the push job is in error (a job in error is retryable); cancelling ends both
    tr["2d"] = t("", "3a")
    tr["6a"] = t("", "3a")
    tr["3a"] = t("", "3b")
    tr["3b"] = {
        "3c": {"state": "success", "type": "standard"},
        "3f": {"state": "failure", "type": "standard"},
    }
    for a, b in zip(["3c", "3d", "3e", "2b"], ["3d", "3e", "2b", final_ids[0]]):
        tr[a] = t("", b)
    tr["3f"] = t("", final_ids[0])
    for a, b in zip(final_ids, final_ids[1:]):
        tr[a] = t("", b)
    tr[final_ids[-1]] = t("", "workflow_end")
    tr["9a"] = t("", "workflow_end")
    return workflow(
        WF["branch_vlan_delete"],
        "Lifecycle Manager delete action for branch-vlan: deletes the NetBox VLAN and removes it from the branch switch only "
        "through %s (Work Center approval); no-op when both are already gone (PID S4d.3, ADR 0043)"
        % CONFIG_PUSH,
        {
            "instance": {
                "type": "object",
                "description": "The branch-vlan instance data (branch, vid, vlan_name, switch, netbox_vlan_id, status); "
                "Lifecycle Manager passes it for a delete action",
            }
        },
        tasks,
        tr,
        {
            "changed": {"type": "boolean"},
            "netbox_deleted": {"type": "boolean"},
            "switch_changed": {"type": "boolean"},
            "vid": {"type": "number"},
            "switch": {"type": "string"},
            "show_vlan": {"type": "object"},
            "push_job": {"type": "object"},
            "instance": {"type": "object"},
        },
    )


# --- Summarize Compliance Results (S4d.5, ADR 0046): one cheap compliance tool for the agents ----------------
# Input run=true starts the plan and waits for it (four unrolled delay + search attempts, no cycle in the graph);
# run=false takes the newest complete instance. Either way the batch reports are reduced on the Gateway 5 runner
# to one compact object per device (errors, warnings, passes, the issue lines) published as `summary`, with
# `compliant` beside it. Reading the raw report tools directly cost an agent 638k input tokens (measured).
PICK_CODE = """import json, sys
d = json.loads(sys.stdin.read() or "{}")
plans = d.get("plans") or [p for g in d.get("groups", []) for p in g.get("plans", [])]
done = [p for p in plans if p.get("jobStatus") == "complete" and p.get("batchId")]
done.sort(key=lambda p: str(p.get("started") or p.get("triggeredAt") or p.get("id") or ""))
p = done[-1] if done else {}
print(json.dumps({"batchId": p.get("batchId"), "instanceId": p.get("id") or p.get("_id"), "startTime": p.get("startTime")}))
"""
SUMMARY_CODE = """import json, sys
d = json.loads(sys.stdin.read() or "{}")
reports = d.get("reports") if isinstance(d, dict) else d
if isinstance(reports, dict):
    reports = reports.get("complianceHistory") or reports.get("list") or []
reports = reports or []
devices = []
for r in reports:
    t = r.get("totals") or {}
    issues = [" ".join(w.get("value", "") for w in (i.get("spec") or {}).get("words", [])).strip() for i in r.get("issues") or []]
    devices.append({"device": r.get("deviceName"), "report_id": r.get("id") or r.get("_id"), "errors": t.get("errors", 0),
                    "warnings": t.get("warnings", 0), "passes": t.get("passes", 0), "issues": issues})
devices.sort(key=lambda x: str(x["device"]))
bad = [x["device"] for x in devices if x["errors"] or x["warnings"]]
# A device with nothing evaluated (no pass, error or warning - its configuration could not be read, e.g. a sealed
# Vault) was not checked, and no device at all is no answer: neither may ever read as compliant (ADR 0066).
not_checked = [x["device"] for x in devices if not (x["passes"] or x["errors"] or x["warnings"])]
out = {"compliant": bool(devices) and not bad and not not_checked, "devices_checked": len(devices) - len(not_checked),
       "devices_with_issues": bad, "devices_not_checked": not_checked, "devices": devices}
if not devices:
    out["error"] = "no device report in this run: compliance could not be evaluated"
print(json.dumps(out))
"""


def compliance_report() -> dict:
    def search_instance(tid: str, x: int, y: int) -> dict:
        return task(
            "searchCompliancePlanInstances",
            "ConfigurationManager",
            "the run's instance",
            {"searchParams": "$var.2d.textObject"},
            {"compliancePlanInstances": None},
            x=x,
            y=y,
        )

    tasks = {
        "1a": task(
            "searchCompliancePlans",
            "ConfigurationManager",
            "the plan by name",
            {"name": "^" + PLAN_NAME + "$", "options": {"start": 0, "limit": 10}},
            {"compliancePlans": None},
            x=0,
        ),
        "1b": jq(
            "plan id", "$var.1a.compliancePlans", "plans[0].id", x=300, to_job="plan_id"
        ),
        "1c": evaluate("new run requested?", "job", "run", "", "==", True, x=600),
        # run=true: start the plan and wait for the instance to complete
        "2a": task(
            "runCompliancePlan",
            "ConfigurationManager",
            "run the plan",
            {"planId": "$var.1b.return_data", "options": {}},
            {"response": None},
            x=900,
            y=-300,
        ),
        "2b": jq(
            "instance id",
            "$var.2a.response",
            "instanceId",
            x=1200,
            y=-300,
            to_job="instance_id",
        ),
        "2c": replace(
            "search params",
            '{"instanceId": "__I__"}',
            "__I__",
            "$var.2b.return_data",
            x=1500,
            y=-300,
        ),
        "2d": parse("search params object", "$var.2c.replacedString", x=1800, y=-300),
        # run=false: the newest complete instance of the plan
        # the search pages at ten unsorted instances by default (measured): newest first, up to a hundred
        "4a": task(
            "searchCompliancePlanInstances",
            "ConfigurationManager",
            "every instance of the plan, newest first",
            {
                "searchParams": {
                    "planName": PLAN_NAME,
                    "sort": {"started": -1},
                    "limit": 100,
                }
            },
            {"compliancePlanInstances": None},
            x=900,
            y=300,
        ),
        "4b": task(
            "runCode",
            "GatewayManager",
            "newest complete instance (Python on the runner)",
            {
                "clusterId": CLUSTER,
                "language": "python",
                "code": PICK_CODE,
                "data": "$var.4a.compliancePlanInstances",
                "safety": {"timeout": 30},
                "packages": [],
            },
            {"result": None},
            x=1200,
            y=300,
        ),
        "4c": jq(
            "its batch id",
            "$var.4b.result",
            "stdout_json.batchId",
            x=1500,
            y=300,
            to_job="batch_id",
        ),
        "4d": jq(
            "its instance id",
            "$var.4b.result",
            "stdout_json.instanceId",
            x=1800,
            y=300,
            to_job="instance_id",
        ),
        # the reports of the batch, reduced to one compact summary (runCode's data must be an object: the array is wrapped)
        "6a": task(
            "getComplianceReportsByBatch",
            "ConfigurationManager",
            "the batch's reports",
            {"batchId": "$var.job.batch_id"},
            {"complianceHistory": None},
            x=4200,
        ),
        "6e": task(
            "setObjectKey",
            "WorkFlowEngine",
            "reports wrapped in an object for the runner",
            {"obj": {}, "path": ["reports"], "value": "$var.6a.complianceHistory"},
            {"object": None},
            display="Tools",
            x=4350,
        ),
        "6b": task(
            "runCode",
            "GatewayManager",
            "summary per device (Python on the runner)",
            {
                "clusterId": CLUSTER,
                "language": "python",
                "code": SUMMARY_CODE,
                "data": "$var.6e.object",
                "safety": {"timeout": 30},
                "packages": [],
            },
            {"result": None},
            x=4500,
        ),
        "6c": jq("summary", "$var.6b.result", "stdout_json", x=4800, to_job="summary"),
        "6d": jq(
            "compliant?",
            "$var.6b.result",
            "stdout_json.compliant",
            x=5100,
            to_job="compliant",
        ),
    }
    tr = {
        "workflow_start": t("", "1a"),
        "1a": t("", "1b"),
        "1b": t("", "1c"),
        "1c": {
            "2a": {"state": "success", "type": "standard"},
            "4a": {"state": "failure", "type": "standard"},
        },
        "2a": t("", "2b"),
        "2b": t("", "2c"),
        "2c": t("", "2d"),
        "4a": t("", "4b"),
        "4b": t("", "4c"),
        "4c": t("", "4d"),
        "4d": t("", "6a"),
        "6a": t("", "6e"),
        "6e": t("", "6b"),
        "6b": t("", "6c"),
        "6c": t("", "6d"),
        "6d": t("", "workflow_end"),
    }
    # four attempts: delay, search, read the status, evaluate; complete -> batch id -> reports; else the next attempt
    attempts = [
        ("a1", "a2", "a3", "a4", "a5", 30),
        ("b1", "b2", "b3", "b4", "b5", 30),
        ("c1", "c2", "c3", "c4", "c5", 30),
        ("d1", "d2", "d3", "d4", "d5", 60),
    ]
    tr["2d"] = t("", attempts[0][0])
    for i, (dl, se, st, ev, bt, secs) in enumerate(attempts):
        x = 2100 + i * 500
        tasks[dl] = task(
            "delay",
            "WorkFlowEngine",
            f"wait {secs} s (attempt {i + 1})",
            {"time": secs},
            {"time_in_milliseconds": None},
            kind="operation",
            display="WorkFlowEngine",
            x=x,
            y=-300,
        )
        tasks[se] = search_instance(se, x + 100, -300)
        tasks[st] = jq(
            "instance status",
            f"$var.{se}.compliancePlanInstances",
            "plans[0].jobStatus",
            x=x + 200,
            y=-300,
        )
        tasks[ev] = evaluate(
            "complete?", st, "return_data", "", "==", "complete", x=x + 300, y=-300
        )
        tasks[bt] = jq(
            "batch id",
            f"$var.{se}.compliancePlanInstances",
            "plans[0].batchId",
            x=x + 400,
            y=-500,
            to_job="batch_id",
        )
        tr[dl] = t("", se)
        tr[se] = t("", st)
        tr[st] = t("", ev)
        tr[ev] = {
            bt: {"state": "success", "type": "standard"}
        }  # failure: the next attempt; after the last, e2 ends the job with a reason
        tr[bt] = t("", "6a")
    for (_, _, _, ev, _, _), nxt in zip(attempts, attempts[1:]):
        tr[ev][nxt[0]] = {"state": "failure", "type": "standard"}
    # ADR 0059/0066 (owner decision 2026-09-23): a read-only agent tool ends cleanly with a reason. A Configuration
    # Manager or runner call that throws, and a run still not complete after the last attempt, used to dead-end the
    # job ("the job ends in error"), which hangs the calling agent.
    tasks["e1"] = note(
        "the compliance report could not be produced",
        "the compliance report could not be produced (a Configuration Manager or runner call failed); report this and do not retry",
        "report_error",
        x=2400,
        y=400,
    )
    tasks["e2"] = note(
        "the run did not finish in time",
        "the compliance run did not finish within the wait; it may still complete - read it later with run=false",
        "report_error",
        x=4100,
        y=-700,
    )
    # 6c/6d are queries that refuse a null (pass_on_null false) and 6e wraps the reports: a runner reply that is not the
    # expected JSON would make them throw after the calls themselves succeeded, so they end through e1 as well
    for tid in ("1a", "2a", "4a", "4b", "6a", "6b", "6c", "6d", "6e", *(a[1] for a in attempts)):
        tr[tid]["e1"] = {"state": "error", "type": "standard"}
    tr[attempts[-1][3]]["e2"] = {"state": "failure", "type": "standard"}
    tr["e1"] = t("", "workflow_end")
    tr["e2"] = t("", "workflow_end")
    return workflow(
        WF["compliance_report"],
        "Runs (run=true) or reads (run=false) the %s compliance plan and returns one compact summary per device: "
        "errors, warnings, passes and the issue lines; the agents' compliance tool (PID S4d.5, ADR 0046)"
        % PLAN_NAME,
        {
            "run": {
                "type": "boolean",
                "required": True,
                "description": "true: start a new run of the plan and wait for it; "
                "false: summarise the newest complete run",
            }
        },
        tasks,
        tr,
        {
            "plan_id": {"type": "string"},
            "instance_id": {"type": "string"},
            "batch_id": {"type": "string"},
            "summary": {"type": "object"},
            "compliant": {"type": "boolean"},
            "report_error": {"type": "string"},
        },
    )


# --- List Devices from NetBox (S4d.5, ADR 0046): one cheap NetBox inventory tool for the local twins -------
# Measured 2026-09-11: `dcim_devices_list` handed straight to a 7B model returns NetBox device objects of
# 52 fields each - five devices at br1 are 17.9 kB, and with the tool schemas the prompt reached 7,823
# tokens. qwen2.5:7b on the four CPU cores of tools-01 ingests at ~22 tokens/sec, so the Platform's
# inference timeout fired at around 350 s and reported "ollama model invocation failed" while Ollama was
# still working (it finished the same request at 16:55:13, truncated = 0). This is the Summarize Compliance
# Results shape applied to inventory: read the list once, reduce it on the Gateway 5 runner, and hand
# the model six fields per device. All 21 lab devices reduce to 2.8 kB; br1 alone to about 700 bytes.
#
# The filters are workflow inputs rather than integration parameters on purpose. The operation is called
# with `limit` only, so there is no filter to leave out and no null to reject (ADR 0054), and the
# matching happens in Python where an absent filter is simply an absent filter.
DEVICES_CODE = """import json, sys
d = json.loads(sys.stdin.read() or "{}")
raw = d.get("filter")
# A 7B model hands a single string input an object: measured 2026-09-11, filter arrived as
# {"site": "br1", "summary": "Devices at site br1"}. The Platform does not type-check a workflow
# input, so it arrives here intact - this is the boundary, so coerce it here rather than trust it.
coerced = False
if isinstance(raw, dict):
    coerced = True
    for k in ("filter", "value", "name", "site", "role", "device", "q"):
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            raw = v
            break
    else:
        vals = [v for v in raw.values() if isinstance(v, str) and v.strip() and " " not in v.strip()]
        raw = vals[0] if len(vals) == 1 else ""
elif isinstance(raw, list):
    coerced = True
    raw = next((v for v in raw if isinstance(v, str) and v.strip()), "")
f = str(raw or "").strip().lower()
out = []
for x in d.get("devices") or []:
    def slug(field):
        v = x.get(field) or {}
        return (v.get("slug") or v.get("value") or "") if isinstance(v, dict) else str(v or "")
    row = {
        "name": x.get("name") or "",
        "site": slug("site"),
        "role": slug("role"),
        "platform": slug("platform"),
        "status": slug("status"),
        "primary_ip4": ((x.get("primary_ip4") or {}) or {}).get("address") or "",
    }
    if f and f not in (row["name"].lower(), row["site"].lower(), row["role"].lower()):
        continue
    out.append(row)
out.sort(key=lambda r: r["name"])
matched = ""
if f and out:
    matched = ("name" if f == out[0]["name"].lower()
               else "site" if f == out[0]["site"].lower() else "role")
res = {"count": len(out), "filter": f, "matched": matched, "devices": out}
if coerced:
    # never let a malformed filter read as "no such device": that answer is confidently wrong
    res["filter_was_not_a_string"] = True
    if not out:
        res["error"] = ("filter must be a plain string such as 'br1'; it arrived as "
                        + type(d.get("filter")).__name__ + ". Retry with one string value.")
print(json.dumps(res))
"""


def netbox_devices() -> dict:
    def put(tid: str, key: str, obj_ref: str, value: str, x: int) -> dict:
        return task(
            "setObjectKey",
            "WorkFlowEngine",
            f"{key} for the runner",
            {"obj": obj_ref, "path": [key], "value": value},
            {"object": None},
            display="Tools",
            x=x,
        )

    tasks = {
        # no filter parameters: the whole list once, reduced below. 21 devices today; limit is the guard.
        "1a": nbi("dcim_devices_list", "every device in NetBox", {"limit": 500}, x=0),
        "1b": jq("the device list", "$var.1a.response", "body.results", x=300),
        "1c": put("1c", "devices", {}, "$var.1b.return_data", 600),
        "1d": put("1d", "filter", "$var.1c.object", "$var.job.filter", 900),
        "2a": task(
            "runCode",
            "GatewayManager",
            "filter and reduce to six fields per device (Python on the runner)",
            {
                "clusterId": CLUSTER,
                "language": "python",
                "code": DEVICES_CODE,
                "data": "$var.1d.object",
                "safety": {"timeout": 30},
                "packages": [],
            },
            {"result": None},
            x=1800,
        ),
        "2b": jq("summary", "$var.2a.result", "stdout_json", x=2100, to_job="summary"),
        "2c": jq("count", "$var.2a.result", "stdout_json.count", x=2400, to_job="count"),
        # ADR 0059/0066: a NetBox read that throws (a 403, or the Platform unable to resolve the token because
        # Vault is sealed) took state `error` with no edge out, and the job dead-ended "2c could have led to the
        # workflow end task" (measured on dev 2026-09-23) - the hang that holds an agent twin for ever.
        "3a": note(
            "NetBox could not be read",
            "NetBox could not be read (credential or connection); report this and do not retry",
            "netbox_error",
            x=300,
            y=-300,
        ),
    }
    transitions = chain("1a", "1b", "1c", "1d", "2a", "2b", "2c")
    transitions["1a"]["3a"] = {"state": "error", "type": "standard"}
    transitions["3a"] = t("", "workflow_end")
    return workflow(
        WF["netbox_devices"],
        "Lists NetBox devices reduced to name, site, role, platform, status and primary_ip4, "
        "optionally filtered by one value matched against name, site or role; the local twins' inventory "
        "tool, because the raw "
        "device objects are 52 fields each and overrun a CPU model's inference timeout (PID S4d.5, ADR 0046)",
        {
            # ONE input on purpose. Operations Manager `jobs/start` refuses a job unless EVERY declared
            # input is supplied - `required` does not make one optional (measured 2026-09-11: starting
            # this workflow with site alone answered 500, metadata.error ["name", "role"]). Three
            # declared filters therefore meant three keys on every call, which is the null-filling
            # fragility this workflow exists to avoid. One value, matched against name, site or role
            # in Python; the slugs do not collide in this lab.
            "filter": {
                "type": "string",
                "required": True,
                "description": "One value: a device name (br2-sw01), a site slug (br1) or a role "
                "slug (leaf). Empty string for every device.",
            },
        },
        tasks,
        transitions,
        {"summary": {"type": "object"}, "count": {"type": "number"}, "netbox_error": {"type": "string"}},
    )


if __name__ == "__main__":
    for wf in (
        device_count(),
        show_version(),
        show_command(),
        show_all(),
        branch_vlan(),
        branch_vlan_delete(),
        config_push(),
        compliance_run(),
        compliance_report(),
        netbox_devices(),
        backup_all(),
    ):
        out = HERE / file_name(wf["name"])
        out.write_text(json.dumps(wf, indent=2) + "\n")
        print(out.relative_to(HERE.parent.parent), len(wf["tasks"]) - 2, "tasks")
