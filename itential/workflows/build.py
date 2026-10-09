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

import base64
import html
import importlib.util
import inspect
import ipaddress
import itertools
import json
import os
import re
import statistics
from datetime import datetime, timedelta, timezone
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
# The canvas search below is the slow part of a build (a 120-task workflow: ~30 s, nearly all in drawn_cost). Only the
# committed JSON needs the finished canvas; a test that asks about tasks, edges or card text does not. The test
# suite turns the search off (tests/conftest.py) except where it compares against the committed files, and
# `python build.py` always runs it. Off: each task sits at its lane column, rows as computed, no polish.
LAYOUT_SEARCH = os.environ.get("LAB_WORKFLOW_LAYOUT", "on").lower() != "off"


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


def _ccw(a: tuple, b: tuple, c: tuple) -> float:
    return (c[1] - a[1]) * (b[0] - a[0]) - (b[1] - a[1]) * (c[0] - a[0])


def _crosses(p1: tuple, p2: tuple, p3: tuple, p4: tuple) -> bool:
    d1, d2, d3, d4 = _ccw(p3, p4, p1), _ccw(p3, p4, p2), _ccw(p1, p2, p3), _ccw(p1, p2, p4)
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
    """(arrows crossing, arrows through a task box) for the canvas as Studio draws it.

    Each arrow carries its bounding box: two arrows whose boxes do not overlap cannot cross, and an arrow cannot enter
    a task box whose centre lies outside the arrow's box grown by the task's half size. Those two cheap tests skip
    nearly every pair before the exact ones (the polish asks for this cost thousands of times per build)."""
    at = {n: (v["x"], v["y"]) for n, v in where.items()}
    segs = []
    for a, b in edges:
        (ax, ay), (bx, by) = at[a], at[b]
        segs.append((a, b, at[a], at[b], min(ax, bx), max(ax, bx), min(ay, by), max(ay, by)))
    crossing = 0
    for i, (a, b, p1, p2, x0, x1, y0, y1) in enumerate(segs):
        for c, d, p3, p4, u0, u1, v0, v1 in segs[i + 1 :]:
            if x1 < u0 or u1 < x0 or y1 < v0 or v1 < y0 or len({a, b, c, d}) != 4:
                continue
            if _crosses(p1, p2, p3, p4):
                crossing += 1
    through = 0
    for a, b, p, q, x0, x1, y0, y1 in segs:
        x0, x1, y0, y1 = x0 - HALF_W, x1 + HALF_W, y0 - HALF_H, y1 + HALF_H
        for n, c in at.items():
            if n in (a, b) or c[0] < x0 or c[0] > x1 or c[1] < y0 or c[1] > y1:
                continue
            if _through_box(p, q, c):
                through += 1
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
    if not LAYOUT_SEARCH:
        return {n: {"x": int(round(x / 10) * 10), "y": row[n] * ROW} for n, x in _lane_columns(hint, row).items()}
    candidates = [
        {n: {"x": int(round(x / 10) * 10), "y": row[n] * ROW} for n, x in columns.items()}
        for columns in (_lane_columns(hint, row), _ordered_columns(hint, row, edges))
    ]
    best = min(candidates, key=lambda w: (lambda c: c[0] + 3 * c[1])(drawn_cost(w, edges)))
    return _polish(best, edges)


# --- input gates (security review 2026-09-29, WEB-12) -----------------------------------------------------------------
# These workflows put job inputs into text with replace(): JSON that is parsed afterwards (node selectors, command
# lists, NetBox bodies) and switch configuration. A quote or newline in an input could then add keys, extra commands
# or extra config lines, and nothing checked the inputs. Each gated workflow now starts by building its inputs as an
# object and checking it against these patterns (validateJsonSchema, then an evaluate on `valid`: the task completes
# even for invalid data, measured 2026-09-29); a refusal ends the job with the reason in `error` before anything runs.
NODE_NAME = {"type": "string", "pattern": r"^[a-z0-9][a-z0-9-]{0,62}$"}
VLAN_NAME = {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,31}$"}  # EOS: 32 characters
# only read commands: `show ...`, optionally one read-only pipe. Never redirect / append / tee (they write files on
# IOS-XE and EOS), never a quote, backslash or newline. Every command the agents and verifies send fits.
SHOW_COMMAND = {
    "type": "string",
    "maxLength": 200,
    # (?!.*://): no URL argument, which would make the device fetch from elsewhere (show archive ... tftp://h/f)
    "pattern": r"^(?!.*://)show [A-Za-z0-9][A-Za-z0-9 ._/:,-]{0,120}( \| (include|exclude|begin|section|count|json)( [A-Za-z0-9 ._/:,^$*-]{1,80})?)?$",
}
# Remove Branch VLAN takes the Lifecycle Manager instance whole; a direct start bypasses Lifecycle Manager's own check,
# so the gate holds the model's bounds (itential/lcm/branch-vlan.yaml) plus the name patterns. null stays allowed:
# the workflow already handles an instance whose create failed (object keywords do not apply to null).
BRANCH_VLAN_INSTANCE = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "required": ["branch", "vid", "vlan_name", "switch", "netbox_vlan_id", "status"],
    "properties": {
        "branch": {"enum": ["br1", "br2"]},
        "vid": {"type": "integer", "minimum": 11, "maximum": 99},
        "vlan_name": VLAN_NAME,
        "switch": NODE_NAME,
        "netbox_vlan_id": {"type": "integer", "minimum": 1},
        "status": {"enum": ["reserved", "active", "deprecated", "deleted"]},
    },
}
# Push Configuration with Revert Timer's checks, as config-push-revert takes them (it checks them again)
IPV4_TEXT = {"type": "string", "pattern": r"^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$"}
CHECK_VRF = {"type": "string", "pattern": r"^[A-Za-z0-9_-]{1,32}$"}
REVERT_CHECKS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "bgp_established": {"type": "array", "maxItems": 20, "items": {"anyOf": [
            IPV4_TEXT,
            {"type": "object", "additionalProperties": False, "required": ["neighbor"],
             "properties": {"neighbor": IPV4_TEXT, "vrf": CHECK_VRF}},
        ]}},
        "pings": {"type": "array", "maxItems": 20, "items": {
            "type": "object", "additionalProperties": False, "required": ["target"],
            "properties": {"target": IPV4_TEXT, "vrf": CHECK_VRF,
                           "source": {"anyOf": [{"type": "string", "pattern": r"^[A-Za-z][A-Za-z0-9/.:-]{0,63}$"},
                                                IPV4_TEXT]},
                           "min_percent": {"type": "integer", "minimum": 1, "maximum": 100}}}},
        "settle_seconds": {"type": "integer", "minimum": 0, "maximum": 780},
    },
}
FABRIC_DEVICES = set(VERSIONS["fabric_bgp"]["host_keys"])
INPUT_GATES = {
    WF["show_version"]: {"device": NODE_NAME},
    WF["show_command"]: {"device": NODE_NAME, "command": SHOW_COMMAND},
    WF["show_all"]: {"command": SHOW_COMMAND},
    # R6 (ADR 0072): what the alert relay sends; the device is the one open target the AWS monitor watches
    WF["diagnose_aws_vpn_outage"]: {
        "alertname": {"type": "string", "enum": ["LabAwsTunnelDown"]},
        "device": {"type": "string", "enum": sorted(n for n, e in VERSIONS["aws_vpn"]["targets"].items()
                                                    if e["window"] == "open" and e["monitor"] == "aws")},
        "interface": {"type": "string", "pattern": r"^Tunnel[0-9]{1,4}$"},
        "starts_at": {"type": "string", "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"},
        "fingerprint": {"type": "string", "pattern": r"^[0-9a-f]{1,32}$"},
    },
    # R10 (ADR 0073): what the alert relay sends for LabBgpSessionDown; the plan then holds the end to a declared session
    WF["diagnose_fabric_bgp_outage"]: {
        "alertname": {"type": "string", "enum": ["LabBgpSessionDown"]},
        "device": {"type": "string", "enum": sorted(FABRIC_DEVICES)},
        "neighbor": IPV4_TEXT,
        "vrf": {"type": "string", "pattern": r"^[A-Za-z0-9_-]{1,32}$"},
        "peer": {"type": "string", "enum": sorted(FABRIC_DEVICES)},
        "starts_at": {"type": "string", "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"},
        "fingerprint": {"type": "string", "pattern": r"^[0-9a-f]{1,32}$"},
    },
    # R10 PR C: the drill; the plan then holds the end to a declared vEOS session
    # R7 (ADR 0076 decision 6): an open target and one of batfish-check's own drill modes (aws_vpn.batfish.drills)
    WF["drill_batfish_gate"]: {
        "target": {"enum": sorted(n for n, t in VERSIONS["aws_vpn"]["targets"].items() if t["window"] == "open")},
        "drill": {"type": "string", "enum": list(VERSIONS["aws_vpn"]["batfish"]["drills"])},
    },
    WF["break_fabric_bgp"]: {
        "device": {"type": "string", "enum": sorted(FABRIC_DEVICES)},
        "neighbor": IPV4_TEXT,
        "fault": {"type": "string", "enum": ["interface-shutdown", "md5-mismatch", "neighbor-shutdown",
                                              "remote-as-mismatch"]},
    },
    # reason is shown on the approval card only; no markup
    WF["config_push"]: {"device": NODE_NAME, "reason": {"type": "string", "maxLength": 500, "pattern": r"^[^<>]*$"}},
    # ADR 0077: the approval card's message (the requester's note, the end time) is rendered on the runner, so Deploy
    # gets the same input gate as every other workflow whose inputs reach text; the note allows no markup
    WF["deploy_aws_vpn"]: {
        "onprem_public_ip": IPV4_TEXT,
        "enable_nat_gateway": {"type": "string", "enum": ["false", "true"]},
        "change_note": {"type": "string", "maxLength": 280, "pattern": r'^[^<>"\\\r\n]{0,280}$'},  # no `\"`: JS u-mode
        "lifetime_hours": {"type": "string", "enum": VERSIONS["aws_vpn"]["lifetime_hours"]["choices"]},
    },
    WF["branch_vlan"]: {
        "branch": {"type": "string", "pattern": r"^[a-z0-9][a-z0-9-]{0,31}$"},
        "vlan_name": VLAN_NAME,
        "switch_override": {"type": "string", "pattern": r"^([a-z0-9][a-z0-9-]{0,62})?$"},  # empty: the NetBox choice
        "change_request": {"type": "boolean"},
    },
    WF["branch_vlan_delete"]: {"instance": BRANCH_VLAN_INSTANCE},
    # only a target whose window is open (itential/versions.yaml aws_vpn.targets): a closed one is refused at the gate
    WF["verify_aws_vpn"]: {"target": {"enum": sorted(n for n, t in VERSIONS["aws_vpn"]["targets"].items()
                                                if t["window"] == "open")}},
    WF["hand_off_aws_vpn"]: {"target": {"enum": sorted(n for n, t in VERSIONS["aws_vpn"]["targets"].items()
                                                  if t["window"] == "open")}},
    # Tear Down may remove only what Hand Off may push: an open target that is also a revert target
    WF["tear_down_aws_vpn"]: {"target": {"enum": sorted(n for n, t in VERSIONS["aws_vpn"]["targets"].items()
                                                   if t["window"] == "open" and n in VERSIONS["revert_push"]["targets"])}},
    # the card shows reason; config and checks go in as data only; the service checks every line and check again
    WF["config_push_revert"]: {
        "device": {"enum": sorted(VERSIONS["revert_push"]["targets"])},
        "reason": {"type": "string", "maxLength": 500, "pattern": r"^[^<>]*$"},
        "config": {"type": "string", "minLength": 1, "maxLength": 30000},
        "revert_minutes": {"type": "integer", "minimum": 2, "maximum": 15},
        "checks": REVERT_CHECKS,
    },
}
# where a refusal is reported: the device workflows already say "did not run" in device_error, which the agents read
GATE_ERROR_VAR = {WF[k]: "device_error" for k in ("show_version", "show_command", "show_all", "config_push")}
# the field copies; then 9a0a validate, 9a0b evaluate, 9a0c refuse
GATE_IDS = ("9a01", "9a02", "9a03", "9a04", "9a05", "9a06", "9a07", "9a08", "9a09")


def with_input_gate(tasks: dict, transitions: dict, fields: dict, error_var: str = "error") -> tuple[dict, dict]:
    """workflow_start -> copy each input into an object -> validate -> valid? -> the workflow's first task; refused ->
    `error` -> workflow_end. The original start edges move behind the gate unchanged."""
    # one copy per field: zip() would drop the fields past the last slot, and the schema would then refuse every call
    # (2026-10-06: Diagnose Fabric BGP Outage's seventh field, fingerprint, on its first real alert)
    assert len(fields) <= len(GATE_IDS), f"{len(fields)} gated fields, {len(GATE_IDS)} copy slots: add slots"
    ids = GATE_IDS[: len(fields)]
    assert not set(ids + ("9a0a", "9a0b", "9a0c")) & set(tasks), "gate ids collide with the workflow's own"
    tasks, tr = dict(tasks), {k: dict(v) for k, v in transitions.items()}
    prev = {}
    for tid, key in zip(ids, fields):
        tasks[tid] = task("setObjectKey", "WorkFlowEngine", f"input to check: {key}",
                          {"obj": prev or {}, "path": [key], "value": f"$var.job.{key}"}, {"object": None},
                          display="Tools")
        prev = f"$var.{tid}.object"
    tasks["9a0a"] = task("validateJsonSchema", "WorkFlowEngine", "check the inputs",
                       {"jsonData": prev, "schema": {"type": "object", "additionalProperties": False,
                                                     "required": list(fields), "properties": fields}},
                       {"result": "$var.job.input_check"}, display="WorkFlowEngine")
    tasks["9a0b"] = evaluate("inputs valid?", "9a0a", "result", "valid", "==", True, x=0)
    tasks["9a0c"] = note("the inputs were refused",
                       "the inputs were refused before anything ran (see input_check for which input and why): "
                       "nothing was sent to a device", error_var, x=0)
    first = tr["workflow_start"]
    tr["workflow_start"] = t("", ids[0])
    for a, b in zip(ids, ids[1:] + ("9a0a",)):
        tr[a] = t("", b)
    tr["9a0a"] = {"9a0b": {"state": "success", "type": "standard"}, "9a0c": {"state": "error", "type": "standard"}}
    tr["9a0b"] = {**first, "9a0c": {"state": "failure", "type": "standard"}}
    tr["9a0c"] = t("", "workflow_end")
    return tasks, tr


def workflow(
    name: str,
    description: str,
    inputs: dict,
    tasks: dict,
    transitions: dict,
    outputs: dict | None = None,
) -> dict:
    if name in INPUT_GATES:
        error_var = GATE_ERROR_VAR.get(name, "error")
        tasks, transitions = with_input_gate(tasks, transitions, INPUT_GATES[name], error_var)
        # a refusal is part of the workflow's answer, so callers (agents, verifies) can see it
        outputs = {**(outputs or {}), error_var: {"type": "string"}, "input_check": {"type": "object"}}
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


# --- Deploy AWS VPN (PID S13 criterion 1, ADR 0068 step 5) ---------------------------------------------------------
# The AWS side of the site-to-site VPN, planned, approved and applied through two Gateway 5 executable services on the
# cloud-devops-pipeline repository (versions.yaml terraform_run): terraform-run plans with a plan ID it mints itself,
# a Work Center card shows the plan summary, apply runs exactly that plan (SHA-256 and commit checked by the service),
# and aws-vpn-psk writes a new PSK to Vault (write-only AppRole) and Secrets Manager. Started by the branded page
# (itential/portal) through an Operations Manager API trigger, or by its manual trigger. Service params are JSON
# strings filled by Tools.replace and parsed (a $var inside the params object never resolves). Every service call has
# an error edge and its result is evaluated (ADR 0066); every path reaches workflow_end with the reason in `error` and
# whether AWS changed in `aws_changed` (unset when the apply failed: the error then says "nothing applied" when the
# service refused before applying; otherwise the apply may have run part-way).
# The plan's fixed parameters. The two user inputs are added with setObjectKey, as data: string-replacing them into
# JSON let an input close the string and add keys ("action": "apply", ...) - security review 2026-09-29, WEB-01.
PLAN_FIXED = '{"action": "plan", "job": "new", "enable_vpn": "true", "timeout": "900"}'
IPV4 = r"^(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])){3}$"
# What the plan step may send, checked before anything runs, whichever trigger (or a direct jobs/start) began the job.
# validateJsonSchema never fails its task (measured on dev 2026-09-29): its result.valid is evaluated after it.
PLAN_INPUTS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "job", "enable_vpn", "timeout", "onprem_public_ip", "enable_nat_gateway", "change_note"],
    "properties": {
        "action": {"const": "plan"},
        "job": {"const": "new"},
        "enable_vpn": {"const": "true"},
        "timeout": {"const": "900"},
        "onprem_public_ip": {"type": "string", "maxLength": 15, "pattern": IPV4},
        "enable_nat_gateway": {"enum": ["false", "true"]},
        "change_note": {"type": "string", "maxLength": 280},
        # R2b: when the deployment ends (expires_from), or none; terraform-run checks it again (future, <= 31 days)
        "expires_at": {"type": "string", "pattern": r"^(none|[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:00Z)$"},
    },
}
LIFETIMES = VERSIONS["aws_vpn"]["lifetime_hours"]


def expires_from(d: dict, now: datetime, choices: list) -> dict:
    """The deployment's end time from the chosen lifetime (R2b): now, to the minute, plus that many hours, as UTC
    YYYY-MM-DDTHH:MM:00Z; `none` keeps it up until Tear Down. Anything not in `choices` gives `invalid`, which the
    input check refuses. Pure: runCode runs this source on the Gateway with the clock and the choices written in."""
    hours = d.get("lifetime_hours")
    if not isinstance(hours, str) or hours not in choices:
        return {"expires_at": "invalid", "lifetime_hours": str(hours)[:10]}
    if hours == "none":
        return {"expires_at": "none", "lifetime_hours": hours, "ends": "none: it stays up until Tear Down"}
    end = (now.replace(second=0, microsecond=0) + timedelta(hours=int(hours))).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"expires_at": end, "lifetime_hours": hours, "ends": f"{end} ({hours} hours)"}


EXPIRES_CODE = ("import json, sys\nfrom datetime import datetime, timedelta, timezone\n\n\nCHOICES = "
                + repr(LIFETIMES["choices"]) + "\n\n\n" + inspect.getsource(expires_from)
                + "\n\nprint(json.dumps(expires_from(json.loads(sys.stdin.read() or \"{}\"), datetime.now(timezone.utc), "
                + "CHOICES)))\n")
APPLY_TPL = '{"action": "apply", "job": "__ID__", "plan_sha256": "__SHA__", "timeout": "1800"}'
DISCARD_TPL = '{"action": "discard", "job": "__ID__", "timeout": "120"}'
# ensure, not write: strongSwan reads the key only at boot, so a redeploy keeps the key it has (step 6 feasibility C7)
PSK_TPL = '{"action": "ensure", "secret_arn": "__ARN__", "timeout": "120"}'


def run_service(summary: str, service: str, params_ref: str, out_job: str, x: int, y: int = 0) -> dict:
    """GatewayManager.runService on this cluster; the published result is the JSON-RPC envelope {id, jsonrpc, result:
    {return_code, stdout, stdout_json, stderr, elapsed_time}} - so paths start `result.` (measured in a workflow 2026-09-29).
    No `inventory`: these services target no node, and an empty list is refused ("inventory must be a non-empty
    array if provided", measured on dev 2026-09-29)."""
    return task(
        "runService",
        "GatewayManager",
        summary,
        {"serviceName": service, "clusterId": CLUSTER, "params": params_ref},
        {"result": f"$var.job.{out_job}"},
        display="GatewayManager",
        x=x,
        y=y,
    )


def set_key(summary: str, obj_ref, key: str, value_ref: str, x: int, y: int = 0) -> dict:
    """WorkFlowEngine.setObjectKey: a value goes in as data, so no input can add or change another key."""
    return task("setObjectKey", "WorkFlowEngine", summary, {"obj": obj_ref, "path": [key], "value": value_ref},
                {"object": None}, display="Tools", x=x, y=y)


def deploy_aws_vpn() -> dict:
    ok, fail, err = "success", "failure", "error"
    tasks = {
        # the inputs, as data, checked before anything runs (WEB-01)
        "1a": parse("plan params: the fixed part", PLAN_FIXED, x=100),
        "1b": set_key("plan params: public IP", "$var.1a.textObject", "onprem_public_ip", "$var.job.onprem_public_ip", x=150),
        "1c": set_key("plan params: NAT gateway", "$var.1b.object", "enable_nat_gateway", "$var.job.enable_nat_gateway", x=200),
        # R2b: the end time, from the chosen lifetime, worked out on the runner and planned as expires_at
        "14": set_key("the lifetime, as data", {}, "lifetime_hours", "$var.job.lifetime_hours", x=210),
        "15": run_code("the end time (Python on the runner)", EXPIRES_CODE, "$var.14.object", "lifetime", x=220),
        "16": jq("the end time", "$var.job.lifetime", "stdout_json.expires_at", x=230, to_job="expires_at"),
        "17": set_key("plan params: end time", "$var.1c.object", "expires_at", "$var.16.return_data", x=240),
        "11": set_key("the inputs to check (params + change note)", "$var.17.object", "change_note", "$var.job.change_note", x=250),
        "12": task("validateJsonSchema", "WorkFlowEngine", "check the inputs",
                   {"jsonData": "$var.11.object", "schema": PLAN_INPUTS_SCHEMA}, {"result": "$var.job.input_check"},
                   display="WorkFlowEngine", x=300),
        "13": evaluate("inputs valid?", "12", "result", "valid", "==", True, x=350),
        "1d": run_service("terraform plan", "terraform-run", "$var.17.object", "plan_result", x=400),
        "1e": evaluate("plan made?", "1d", "result", "result.return_code", "==", 0, x=500),
        "1f": jq("the plan summary", "$var.1d.result", "result.stdout_json", x=600, to_job="plan"),
        "10": jq("the plan ID", "$var.1d.result", "result.stdout_json.job", x=700),
        # approve
        "2a": replace(
            "the card's message",
            "Deploy the AWS side of the site-to-site VPN (costs about $1 a day while it is up). "
            "It ends at __END__: approving this also approves Tear Down Expired AWS VPN removing it then, router "
            "first, with no further card (none: it stays up until Tear Down). "
            "The requester's note (their own words, not checked): __NOTE__",
            "__NOTE__", "$var.job.change_note", x=800,
        ),
        "2c": replace("the card's message: end time", "$var.2a.replacedString", "__END__", "$var.job.expires_at", x=850),
        **approval("2b", "Approve the AWS change", "$var.2c.replacedString", "$var.job.plan", "Approve", "Reject",
                   x=900, var="approval"),
        # apply
        "3a": jq("the approved plan's SHA-256", "$var.1d.result", "result.stdout_json.plan_sha256", x=1000),
        "3b": replace("apply params: plan ID", APPLY_TPL, "__ID__", "$var.10.return_data", x=1100),
        "3c": replace("apply params: SHA-256", "$var.3b.replacedString", "__SHA__", "$var.3a.return_data", x=1200),
        "3d": parse("apply params", "$var.3c.replacedString", x=1300),
        "3e": run_service("terraform apply (the approved plan)", "terraform-run", "$var.3d.textObject", "apply_result", x=1400),
        "3f": evaluate("applied?", "3e", "result", "result.return_code", "==", 0, x=1500),
        "30": jq("the outputs", "$var.3e.result", "result.stdout_json.outputs", x=1600, to_job="outputs"),
        "31": flag("aws_changed = true", "true", "aws_changed", x=1700),
        # PSK
        "4a": jq("the PSK secret's ARN", "$var.3e.result", "result.stdout_json.outputs.psk_secret_arn", x=1800),
        "4b": replace("PSK params", PSK_TPL, "__ARN__", "$var.4a.return_data", x=1900),
        "4c": parse("PSK params", "$var.4b.replacedString", x=2000),
        "4d": run_service("the pre-shared key: Vault, then Secrets Manager", "aws-vpn-psk", "$var.4c.textObject", "psk_result", x=2100),
        "4e": evaluate("key in place?", "4d", "result", "result.return_code", "==", 0, x=2200),
        "4f": jq("the PSK versions (never the PSK)", "$var.4d.result", "result.stdout_json", x=2300, to_job="psk"),
        # close out
        "5a": jq("the strongSwan EIP", "$var.job.outputs", "strongswan_eip", x=2400),
        # the key step's own one-line summary: the version written, or that the key in place was kept
        "5b": jq("what happened to the key", "$var.job.psk", "summary", x=2500),
        "5d": replace("the outcome: EIP", "Deployed: strongSwan at __EIP__; __V__.", "__EIP__", "$var.5a.return_data", x=2700),
        "5e": replace("the outcome", "$var.5d.replacedString", "__V__", "$var.5b.return_data", x=2800),
        # reject: discard the stored plan, end cleanly
        "7a": replace("discard params", DISCARD_TPL, "__ID__", "$var.10.return_data", x=1000, y=400),
        "7b": parse("discard params", "$var.7a.replacedString", x=1100, y=400),
        "7c": run_service("discard the rejected plan", "terraform-run", "$var.7b.textObject", "discard_result", x=1200, y=400),
        "71": evaluate("discarded?", "7c", "result", "result.return_code", "==", 0, x=1250, y=400),
        "7d": flag("rejected = true", "true", "rejected", x=1300, y=400),
        "7e": flag("aws_changed = false (rejected)", "false", "aws_changed", x=1400, y=400),
        "7f": note("the rejection", "rejected in Work Center: the plan was discarded, nothing changed in AWS", "outcome", x=1500, y=400),
        "70": note("the discard did not run", "the rejected plan could not be discarded; it stays in the state bucket until removed", "error", x=1300, y=600),
        # failures
        "8a": note("the plan did not run", "the Gateway could not run terraform-run for the plan; nothing changed in AWS", "error", x=500, y=-300),
        "8b": jq("why the plan was refused", "$var.1d.result", "result.stdout_json.error", x=600, y=-300, to_job="error", optional=True),
        "8c": flag("aws_changed = false (no plan)", "false", "aws_changed", x=700, y=-300),
        "8f": note(
            "the inputs were refused",
            "the inputs were refused before anything ran (see input_check): the on-prem IP must be an IPv4 address "
            "a.b.c.d, the NAT gateway true or false, the change note at most 280 characters, the lifetime one of "
            + ", ".join(LIFETIMES["choices"]) + " hours; nothing changed in AWS",
            "error", x=400, y=-300,
        ),
        "8d": note("the apply did not run", "the Gateway could not run terraform-run for the apply; check apply_result and AWS", "error", x=1500, y=-700),
        "8e": jq("why the apply stopped", "$var.3e.result", "result.stdout_json.error", x=1600, y=-700, to_job="error", optional=True),
        "9a": note("the key step did not run", "applied, but the Gateway could not run aws-vpn-psk; the key may not be in place", "error", x=2200, y=-1100),
        "9b": jq("why the PSK step stopped", "$var.4d.result", "result.stdout_json.error", x=2300, y=-1100, to_job="error", optional=True),
        "80": note("the end time could not be worked out", "the Gateway could not work out the end time from the "
                   "lifetime (see lifetime): nothing changed in AWS", "error", x=220, y=-300),
    }
    tasks["5e"]["variables"]["outgoing"]["replacedString"] = "$var.job.outcome"
    tr = chain("1a", "1b", "1c", "14", "15", "16", "17", "11", "12", "13", "1d", "1e", "1f", "10", "2a", "2c", *approval_ids("2b"), "2b", "3a", "3b", "3c", "3d", "3e", "3f", "30", "31",
               "4a", "4b", "4c", "4d", "4e", "4f", "5a", "5b", "5d", "5e")
    tr["12"] = {"13": {"state": ok, "type": "standard"}, "8f": {"state": err, "type": "standard"}}
    tr["13"] = {"1d": {"state": ok, "type": "standard"}, "8f": {"state": fail, "type": "standard"}}
    tr["8f"] = t("", "8c")
    tr["15"] = {"16": {"state": ok, "type": "standard"}, "80": {"state": err, "type": "standard"}}
    tr["16"] = {"17": {"state": ok, "type": "standard"}, "80": {"state": err, "type": "standard"}}
    tr["80"] = t("", "8c")
    tr["1d"] = {"1e": {"state": ok, "type": "standard"}, "8a": {"state": err, "type": "standard"}}
    tr["1e"] = {"1f": {"state": ok, "type": "standard"}, "8b": {"state": fail, "type": "standard"}}
    tr["2b"] = {"3a": {"state": ok, "type": "standard"}, "7a": {"state": fail, "type": "standard"}}
    tr["3e"] = {"3f": {"state": ok, "type": "standard"}, "8d": {"state": err, "type": "standard"}}
    tr["3f"] = {"30": {"state": ok, "type": "standard"}, "8e": {"state": fail, "type": "standard"}}
    tr["4d"] = {"4e": {"state": ok, "type": "standard"}, "9a": {"state": err, "type": "standard"}}
    tr["4e"] = {"4f": {"state": ok, "type": "standard"}, "9b": {"state": fail, "type": "standard"}}
    # reject: the discard's own failure still ends cleanly (the stored plan expires with the bucket's lifecycle)
    tr["7a"] = t("", "7b")
    tr["7b"] = t("", "7c")
    tr["7c"] = {"71": {"state": ok, "type": "standard"}, "70": {"state": err, "type": "standard"}}
    tr["71"] = {"7d": {"state": ok, "type": "standard"}, "70": {"state": fail, "type": "standard"}}
    tr["70"] = t("", "7d")
    tr["7d"] = t("", "7e")
    tr["7e"] = t("", "7f")
    tr["7f"] = t("", "workflow_end")
    tr["8a"] = t("", "8c")
    tr["8b"] = t("", "8c")
    tr["8c"] = t("", "workflow_end")
    tr["8d"] = t("", "workflow_end")  # aws_changed left unset: the apply may have run part-way
    # the service's own error says "nothing applied" when it refused before applying (hash, commit, stale plan)
    tr["8e"] = t("", "workflow_end")
    tr["9a"] = t("", "workflow_end")  # applied (aws_changed is already true); the PSK step is repeatable
    tr["9b"] = t("", "workflow_end")
    return workflow(
        WF["deploy_aws_vpn"],
        "Deploys the AWS side of the lab's site-to-site VPN: Terraform plans it on the Gateway, a Work Center approval "
        "shows the plan, exactly that plan is applied, and the pre-shared key goes to Vault and Secrets Manager "
        "(PID S13, ADR 0068)",
        {
            "onprem_public_ip": {"type": "string", "required": True,
                                 "description": "The public IPv4 address the tunnel comes from (curl ifconfig.me)"},
            "enable_nat_gateway": {"type": "string", "required": True, "enum": ["false", "true"],
                                   "description": "true also builds the NAT gateway (about $1 a day more); the VPN does not need it"},
            # required in fact: the Platform refuses a start without it (500, metadata.error ["change_note"], measured)
            "change_note": {"type": "string", "required": True, "maxLength": 280,
                            "description": "Why, shown to the approver (may be empty)"},
            "lifetime_hours": {"type": "string", "required": True, "enum": LIFETIMES["choices"],
                               "description": "Hours until Tear Down Expired AWS VPN removes it (R2b); none keeps it "
                                              "up until Tear Down"},
        },
        tasks,
        tr,
        {
            "plan": {"type": "object"}, "approval_card": {"type": "object"},
            "approval_decision": {"type": ["object", "null"]},
            "expires_at": {"type": "string"},
            "lifetime": {"type": "object"},
            "outputs": {"type": "object"},
            "psk": {"type": "object"},
            "aws_changed": {"type": "boolean"},
            "rejected": {"type": "boolean"},
            "outcome": {"type": "string"},
            "error": {"type": "string"},
            "input_check": {"type": "object"},
            "plan_result": {"type": "object"},
            "apply_result": {"type": "object"},
            "psk_result": {"type": "object"},
            "discard_result": {"type": "object"},
        },
    )


# --- Verify AWS VPN and Hand Off AWS VPN (PID S13 criteria 3 and 4, build steps 7 and 8) ---------------------------
# Verify reads only: lab-edge verify (show commands and one ping from Loopback0), aws-vpn-monitor check (the monitor
# Lambda's own swanctl read and the CloudWatch datapoint it writes), terraform-run outputs (state, read). Three
# signals - the router, the AWS monitor (only where a target's monitor is `aws`) and the data plane - judged by `judge`.
# Hand Off runs the very same verify tasks after its push, generated by verify_section: no child job (owner decision
# 2026-10-01: a child that errors leaves its parent waiting, and Itential's guidance is to move away from them).

# what the workflows carry per open target: the pinned values lab-edge checks, the twin's pinned outputs (a target
# monitored by AWS reads its outputs from Terraform instead), the login user and which monitor applies
VERIFY_TARGETS = {
    name: {k: entry[k] for k in ("target", "outputs", "username", "monitor", "netbox") if k in entry}
    for name, entry in VERSIONS["aws_vpn"]["targets"].items()
    if entry["window"] == "open"
}
OUTPUTS_PARAMS = '{"action": "outputs", "timeout": "120"}'
LAB_EDGE_TIMEOUT = "300"  # the reads, a 5 x 2 s ping and the second read, with room for a slow SSH login
MONITOR_TIMEOUT = "240"  # the Lambda (75 s read timeout) plus up to 90 s for its datapoint
PRECHECK_TIMEOUT = "300"
PUSH_TIMEOUT = "600"  # login, precheck again, the block, the post-read, up to 120 s for the tunnel, confirm, the
# post-read again, write memory (a job the Gateway stops leaves the change to the router's revert timer)
READY_TIMEOUT = "120"
# R7 (ADR 0076): login, `show running-config`, the scrub, two Batfish snapshots and seven questions (measured 2026-10-06
# on the Mac: 3.4 s after the read)
BATFISH_TIMEOUT = "300"
BATFISH_HOST = VERSIONS["aws_vpn"]["batfish"]["host"]

# One plan for both workflows: every service's params for this target, or why there is nothing to do.
LAB_EDGE_PLAN_CODE = """import json, sys
d = json.loads(sys.stdin.read() or "{}")
name = d.get("target")
entry = (d.get("targets") or {}).get(name) or {}
monitor = entry.get("monitor")
deployed = entry.get("outputs") if monitor != "aws" else d.get("deployed")
plan = {"target": name, "monitor": monitor, "need_outputs": bool(entry) and monitor == "aws" and deployed is None,
        "ready": False, "reason": "", "lab_edge": None, "monitor_params": None, "precheck": None, "render": None,
        "push": None, "batfish": None, "monitor_ready": None, "netbox": bool(entry.get("netbox"))}
if not entry:
    plan["reason"] = "%%s is not an open target" %% name
elif not plan["need_outputs"] and not (deployed or {}).get("strongswan_eip"):
    plan["reason"] = "nothing is deployed (the outputs have no strongSwan address)"
if entry and deployed and not plan["reason"]:
    target, outputs, user = json.dumps(entry["target"]), json.dumps(deployed), entry["username"]
    plan.update(
        ready=True,
        lab_edge={"action": "verify", "target_json": target, "outputs_json": outputs, "username": user, "timeout": "%s"},
        precheck={"action": "precheck", "target_json": target, "username": user, "timeout": "%s"},
        render={"action": "render", "target_json": target, "outputs_json": outputs, "timeout": "%s"},
        push={"action": "push", "target_json": target, "outputs_json": outputs, "username": user, "timeout": "%s"},
        batfish={"action": "check", "target_json": target, "outputs_json": outputs, "username": user,
                 "batfish_host": "%s", "timeout": "%s"})
    if d.get("drill") and d["drill"] != "none":
        plan["batfish"]["drill"] = d["drill"]
    if monitor == "aws":
        instance = str(deployed.get("strongswan_instance_id") or "")
        plan["monitor_params"] = {"action": "check", "instance_id": instance, "timeout": "%s"}
        plan["monitor_ready"] = {"action": "ready", "instance_id": instance, "timeout": "%s"}
print(json.dumps(plan))
""" % (LAB_EDGE_TIMEOUT, PRECHECK_TIMEOUT, PRECHECK_TIMEOUT, PUSH_TIMEOUT, BATFISH_HOST, BATFISH_TIMEOUT,
       MONITOR_TIMEOUT, READY_TIMEOUT)

def judge(d: dict) -> dict:
    """Verify's verdict from its readings (the spec's rules): every applicable signal up passes; any signal that could
    not be read is "could not check <signal>"; every signal down is "tunnel down"; any other mix is "disagreement",
    naming each reading. The AWS monitor applies only where the target's monitor is `aws`. Pure: runCode runs this
    very function's source on the Gateway, so the unit tests test what runs."""
    states = ("up", "down", "could not check")
    plan = (d.get("plan") or {}).get("stdout_json") or {}

    def service(envelope):  # a runService result {id, jsonrpc, result: {return_code, stdout_json}}, or {} if none ran
        result = (envelope or {}).get("result") or {}
        return result.get("return_code"), result.get("stdout_json") or {}

    rc, edge = service(d.get("lab_edge"))
    signals = {}
    for key in ("router", "data_plane"):
        value = edge.get(key) if rc == 0 else "could not check"
        signals[key] = value if value in states else "could not check"
    applicable = plan.get("monitor") == "aws"
    mrc, mon = service(d.get("monitor"))
    if applicable:
        value = mon.get("monitor") if mrc == 0 else "could not check"
        signals["aws_monitor"] = value if value in states + ("disagreement",) else "could not check"
    label = {"router": "router", "data_plane": "data plane", "aws_monitor": "AWS monitor"}
    values = list(signals.values())
    if all(v == "up" for v in values):
        verdict = "tunnel up: " + ", ".join(f"{label[k]} up" for k in signals)
    elif "could not check" in values:
        verdict = "could not check " + " and ".join(label[k] for k, v in signals.items() if v == "could not check")
        others = [f"{label[k]} {v}" for k, v in signals.items() if v != "could not check"]
        if others:
            verdict += " (" + ", ".join(others) + ")"
    elif all(v == "down" for v in values):
        verdict = "tunnel down: " + ", ".join(f"{label[k]} down" for k in signals)
    else:
        verdict = "disagreement: " + ", ".join(f"{label[k]} {v}" for k, v in signals.items())
    if not applicable:
        verdict += " (AWS monitor not applicable)"
    return {
        "passed": all(v == "up" for v in values),
        "verdict": verdict,
        "signals": {**signals, **({} if applicable else {"aws_monitor": "not applicable"})},
        "readings": {"router": edge.get("readings"), "aws_monitor": mon.get("readings") if applicable else None},
        # the services return an error as a class name or their own refusal text, never device or AWS output
        "errors": {k: v for k, v in (("lab_edge", edge.get("error")), ("aws_monitor", mon.get("error"))) if v},
    }


JUDGE_CODE = ("import json, sys\n\n\n" + inspect.getsource(judge)
              + "\n\nprint(json.dumps(judge(json.loads(sys.stdin.read() or \"{}\"))))\n")


def run_code(summary: str, code: str, data_ref: str, out_job: str, x: int, y: int = 0) -> dict:
    """GatewayManager.runCode: Python on the runner, the data on stdin; its result carries stdout_json."""
    return task("runCode", "GatewayManager", summary,
                {"clusterId": CLUSTER, "language": "python", "code": code, "data": data_ref,
                 "safety": {"timeout": 30}, "packages": []},
                {"result": f"$var.job.{out_job}"}, x=x, y=y)


def empty(summary: str, job_var: str, x: int, y: int = 0) -> dict:
    """{} published as a job variable, so a service that never ran reads as no result (could not check)."""
    t = parse(summary, "{}", x=x, y=y)
    t["variables"]["outgoing"]["textObject"] = f"$var.job.{job_var}"
    return t


def _edge(**states: str) -> dict:
    return {dst: {"state": st, "type": "standard"} for dst, st in states.items()}


def approval_ids(tid: str) -> tuple:
    """The five helper tasks in front of a branded approval `tid` (ADR 0077): header, message, body, render, HTML."""
    return tuple(f"{tid}{i}" for i in "12345")


def approval_edges(tid: str, ok: str = "success") -> dict:
    ids = [*approval_ids(tid), tid]
    return {a: _edge(**{b: ok}) for a, b in zip(ids, ids[1:])}


def verify_section(plan_var: str, passed: str, failed: str, broken: str, judge_broken: str,
                   x: int) -> tuple[dict, dict, str]:
    """The verify tasks, e0-ef, written identically into Verify AWS VPN and Hand Off AWS VPN: lab-edge verify, the AWS
    monitor where it applies, the judge. A service that fails or does not run is still judged (as could not check).
    The judge's evaluation `ef` goes to `passed` or `failed`; `broken` is where a step that leaves nothing to judge
    goes, `judge_broken` where a judge that could not run goes. Reads the job variable `plan_var` (the LAB_EDGE_PLAN_CODE result); publishes lab_edge_result,
    monitor_result, router_note, monitor_note and judgement. Returns (tasks, transitions, the first task id)."""
    ok, fail, err = "success", "failure", "error"
    tasks = {
        # a service that does not run leaves {} behind: the judge reads it as could not check
        "e0": empty("no router reading yet", "lab_edge_result", x=x),
        "e1": empty("no AWS monitor reading yet", "monitor_result", x=x + 50),
        "e2": jq("lab-edge verify's params", f"$var.job.{plan_var}", "stdout_json.lab_edge", x=x + 100),
        "e3": run_service("the router and the data plane (lab-edge verify: show commands, one ping)", "lab-edge",
                          "$var.e2.return_data", "lab_edge_result", x=x + 150),
        "e4": evaluate("lab-edge read the router?", "e3", "result", "result.return_code", "==", 0, x=x + 200),
        # a non-zero exit is a reading too (the judge reads it as could not check), so both outcomes go on
        "e5": note("lab-edge exited non-zero",
                   "lab-edge could not read the router: see lab_edge_result.result.stdout_json.error", "router_note",
                   x=x + 250, y=-300),
        "e6": evaluate("AWS monitor applies?", "job", plan_var, "stdout_json.monitor", "==", "aws", x=x + 300),
        "e7": jq("aws-vpn-monitor's params", f"$var.job.{plan_var}", "stdout_json.monitor_params", x=x + 350, y=300),
        "e8": run_service("the AWS monitor (Lambda + CloudWatch)", "aws-vpn-monitor", "$var.e7.return_data",
                          "monitor_result", x=x + 400, y=300),
        "e9": evaluate("the monitor answered?", "e8", "result", "result.return_code", "==", 0, x=x + 450, y=300),
        "ea": note("aws-vpn-monitor exited non-zero",
                   "aws-vpn-monitor could not check: see monitor_result.result.stdout_json.error", "monitor_note",
                   x=x + 500, y=600),
        "eb": set_key("the readings: what was read", {}, "plan", f"$var.job.{plan_var}", x=x + 550),
        "ec": set_key("the readings: router and data plane", "$var.eb.object", "lab_edge", "$var.job.lab_edge_result",
                      x=x + 600),
        "ed": set_key("the readings: AWS monitor", "$var.ec.object", "monitor", "$var.job.monitor_result", x=x + 650),
        "ee": run_code("the judge (every applicable signal up passes)", JUDGE_CODE, "$var.ed.object", "judgement",
                       x=x + 700),
        "ef": evaluate("passed?", "ee", "result", "stdout_json.passed", "==", True, x=x + 750),
    }
    tr = {
        "e0": _edge(e1=ok),
        "e1": _edge(e2=ok),
        "e2": _edge(**{"e3": ok, broken: err}),
        # a lab-edge that refused or could not log in is still judged
        "e3": _edge(e4=ok, e6=err),
        "e4": _edge(e6=ok, e5=fail),
        "e5": _edge(e6=ok),
        "e6": _edge(e7=ok, eb=fail),
        "e7": _edge(e8=ok, eb=err),
        "e8": _edge(e9=ok, eb=err),
        "e9": _edge(eb=ok, ea=fail),
        "ea": _edge(eb=ok),
        "eb": _edge(ec=ok),
        "ec": _edge(ed=ok),
        "ed": _edge(ee=ok),
        "ee": _edge(**{"ef": ok, judge_broken: err}),
        "ef": _edge(**{passed: ok, failed: fail}),
    }
    return tasks, tr, "e0"


def verify_aws_vpn() -> dict:
    ok, fail, err = "success", "failure", "error"
    tasks = {
        # the target's values (the target is gated: an open one)
        "1a": task("setObjectKey", "WorkFlowEngine", "the target and the pinned values",
                   {"obj": {"targets": VERIFY_TARGETS}, "path": ["target"], "value": "$var.job.target"},
                   {"object": "$var.job.verify_in"}, display="Tools", x=100),
        "1b": run_code("what to read for this target", LAB_EDGE_PLAN_CODE, "$var.job.verify_in", "verify_plan", x=200),
        "1c": evaluate("deployed outputs needed (AWS)?", "1b", "result", "stdout_json.need_outputs", "==", True, x=300),
        # dc1-wan01: the deployed outputs, read from the Terraform state
        "2a": parse("outputs params", OUTPUTS_PARAMS, x=350, y=300),
        "2b": run_service("the deployed outputs (terraform-run, a read)", "terraform-run", "$var.2a.textObject",
                          "outputs_result", x=400, y=300),
        "2c": evaluate("outputs read?", "2b", "result", "result.return_code", "==", 0, x=450, y=300),
        "2d": jq("the outputs", "$var.2b.result", "result.stdout_json.outputs", x=500, y=300),
        "2e": set_key("the outputs, as data", "$var.job.verify_in", "deployed", "$var.2d.return_data", x=550, y=300),
        "2f": run_code("what to read, with the outputs", LAB_EDGE_PLAN_CODE, "$var.2e.object", "verify_plan", x=600, y=300),
        "3e": evaluate("anything to read?", "job", "verify_plan", "stdout_json.ready", "==", True, x=650),
        "3f": jq("why there is nothing to read", "$var.job.verify_plan", "stdout_json.reason", x=700, y=-900,
                 to_job="error"),
        "5f": jq("the verdict", "$var.ee.result", "stdout_json.verdict", x=1500, to_job="outcome"),
        "50": jq("the verdict (not passed)", "$var.ee.result", "stdout_json.verdict", x=1500, y=-300, to_job="error"),
        # failures that leave nothing to judge
        "8a": note("the outputs could not be read",
                   "could not check: the deployed outputs could not be read from the Terraform state (outputs_result); "
                   "nothing was read on the router", "error", x=600, y=600),
        "8b": note("a read could not run", "could not check: the Gateway could not run a step of the check (see the "
                   "job's results); nothing more was read", "error", x=400, y=-600),
        "8c": note("the judge did not run", "could not check: the readings were taken but the judge could not run; "
                   "see lab_edge_result and monitor_result", "error", x=1600, y=-600),
    }
    vtasks, vtr, first = verify_section("verify_plan", passed="5f", failed="50", broken="8b", judge_broken="8c", x=700)
    tasks.update(vtasks)
    tr = {
        "workflow_start": _edge(**{"1a": ok}),
        "1a": _edge(**{"1b": ok}),
        "1b": _edge(**{"1c": ok, "8b": err}),
        "1c": _edge(**{"2a": ok, "3e": fail}),
        "2a": _edge(**{"2b": ok}),
        "2b": _edge(**{"2c": ok, "8a": err}),
        "2c": _edge(**{"2d": ok, "8a": fail}),
        "2d": _edge(**{"2e": ok, "8a": err}),
        "2e": _edge(**{"2f": ok}),
        "2f": _edge(**{"3e": ok, "8b": err}),
        "3e": _edge(**{first: ok, "3f": fail}),
        "3f": _edge(**{"workflow_end": ok, "8b": err}),
        **vtr,
        # a judge that printed no verdict: the reads fail, and still reach the end with a reason
        "5f": _edge(**{"workflow_end": ok, "8c": err}),
        "50": _edge(**{"workflow_end": ok, "8c": err}),
        "8a": _edge(**{"workflow_end": ok}),
        "8b": _edge(**{"workflow_end": ok}),
        "8c": _edge(**{"workflow_end": ok}),
    }
    return workflow(
        WF["verify_aws_vpn"],
        "Verifies the lab's site-to-site VPN to AWS without changing anything: the router (IKEv2 SA, IPsec counters "
        "rising), the AWS monitor (where it applies) and a ping from Loopback0 through the tunnel must all say up "
        "(PID S13 criterion 4, ADR 0068)",
        {"target": {"type": "string", "required": True, "enum": INPUT_GATES[WF["verify_aws_vpn"]]["target"]["enum"],
                    "description": "The lab edge router whose tunnel to check"}},
        tasks,
        tr,
        {
            "outcome": {"type": "string"},
            "error": {"type": "string"},
            "judgement": {"type": "object"},
            "verify_plan": {"type": "object"},
            "lab_edge_result": {"type": "object"},
            "monitor_result": {"type": "object"},
            "outputs_result": {"type": "object"},
            "router_note": {"type": "string"},
            "monitor_note": {"type": "string"},
        },
    )


# --- Hand Off AWS VPN (PID S13 criterion 3, build step 8) -----------------------------------------------------------
# Read AWS (the twin's pinned outputs, or Terraform's) -> readiness (the AWS monitor ran recently, where it applies;
# the router's prerequisites) -> render (SHA-256 + the block with the key masked) -> a Work Center card -> push exactly
# that block with the key from Vault (lab-edge-push: revert timer, post-read, confirm, save) -> the verify tasks
# (verify_section, the same as Verify AWS VPN's). Reject ends clean with nothing sent. The NetBox read-back is
# dc1-wan01's and is added with its records (build step 10): no open target is monitored by AWS until then.
SETTLE_SECONDS = 30  # the router starts IKE once Tunnel10 is up; the verify tasks read after this
APPROVAL_MESSAGE = (
    "Hand Off the AWS VPN to __T__: this block (SHA-256 below) is pushed under a 5-minute revert timer and saved only "
    "if the post-read proves it and the tunnel comes up; otherwise the router rolls it back. If the router already "
    "holds exactly this block and key version, nothing is sent and it is only saved. The pre-shared key comes from "
    "Vault at push time and is never shown here."
)
OUTCOME_TPL = "handed off (router: __R__); Verify: __V__"


def push_summary(d: dict) -> dict:
    """lab-edge-push's answer as Hand Off reads it. The service reports `router` only when it fails, and a push that a
    line rejected or the post-read did not prove is rolled back and still exits 0 (cloud-devops-pipeline at the pin):
    so the outcome comes from saved / rolled_back, never from the exit code alone. `changed` is the service's own
    answer when the push saved (a second Hand Off finds the block in place and sends nothing) and false after a
    rollback; after a failure it is true when lines were sent, and when there is no answer at all - the router may
    differ, check it.
    Pure: runCode runs this function's own source on the Gateway."""
    result = ((d.get("push") or {}).get("result")) or {}
    rc, out = result.get("return_code"), result.get("stdout_json")
    out = out if isinstance(out, dict) else {}
    saved, rolled, sent = out.get("saved") is True, out.get("rolled_back") is True, out.get("sent") is True
    if rc == 0 and saved:
        state, changed = "saved", out.get("changed") is True
    elif rolled:
        state, changed = "rolled back", False
    else:
        state, changed = "failed", out.get("changed") is True or sent or not out
    if not out:
        router = "unknown: no answer from lab-edge-push - check the router (push_result)"
    else:
        router = out.get("router") or (
            "already in place: nothing sent, saved" if saved and out.get("already_in_place") is True
            else "saved" if saved else "rolled back" if rolled else "unchanged: nothing was sent" if not sent
            else "unknown: lines were sent - check the router (push_result)")
    messages = {
        "saved": "",
        "rolled back": "the router rejected a line, the change did not prove out, or the tunnel did not come up in "
                       "time, so it was rolled back (see push_result: rejected, checks, tunnel): nothing was left on "
                       "the router",
        "failed": f"the push did not complete ({out.get('error') or 'no answer from lab-edge-push'}); the router: {router}",
    }
    return {"state": state, "changed": changed, "router": router, "message": messages[state]}

PUSH_SUMMARY_CODE = ("import json, sys\n\n\n" + inspect.getsource(push_summary)
                     + "\n\nprint(json.dumps(push_summary(json.loads(sys.stdin.read() or \"{}\"))))\n")


# --- R7: the Batfish proof between the render and the card (ADR 0076 decisions 3, 4 and 6) -------------------------
BATFISH_CHECKS = ("parses", "aws_reaches_no_lab_address", "lab_reaches_aws_only_from_pinned_prefixes",
                  "inet_in_admits_the_peer_only", "nothing_else_gained_or_lost", "self_zone_drops_aws",
                  "vpc_prefix_list_unchanged")  # cloud-devops-pipeline batfish_candidate.CHECKS, in the card's order
# what each drill mode of batfish-check breaks, and therefore which checks must fail (measured against a real Batfish
# 2026-10-06, cloud-devops-pipeline #42); `none` is the healthy candidate and must pass
DRILL_EXPECTS = {
    "none": [],
    "aws-open": ["aws_reaches_no_lab_address", "self_zone_drops_aws"],
    "acl-any": ["lab_reaches_aws_only_from_pinned_prefixes"],
    "inet-open": ["inet_in_admits_the_peer_only", "nothing_else_gained_or_lost"],
}
NOT_PROVEN = ("not proven: batfish-check could not run, exited non-zero or gave no answer (see batfish_result); "
              "approval stays possible (ADR 0076 decision 4)")


def batfish_summary(d: dict) -> dict:
    """batfish-check's answer as Hand Off reads it: `pass` (every check passed), `fail` (any check failed: Hand Off
    ends with nothing sent) or `not proven` (the service did not run, exited non-zero or answered without checks:
    the card says so and approval stays possible, ADR 0076 decision 4). `card` is the card's entry, `failed` the
    names, `message` the job's error when it ends here. Pure: runCode runs this very function's source."""
    result = ((d.get("batfish") or {}).get("result")) or {}
    rc, out = result.get("return_code"), result.get("stdout_json")
    out = out if isinstance(out, dict) else {}
    checks = out.get("checks") if isinstance(out.get("checks"), dict) else {}
    if rc != 0 or out.get("verdict") not in ("pass", "fail") or not checks:
        why = out.get("error") or ("gave no answer" if not out else "answered without checks")
        return {"verdict": "not proven", "failed": [], "message": "",
                "card": f"not proven: batfish-check {why} (see batfish_result); approval stays possible "
                        "(ADR 0076 decision 4)"}
    failed = [k for k, v in checks.items() if not isinstance(v, dict) or v.get("status") != "pass"]
    parts = []
    for k, v in checks.items():
        v = v if isinstance(v, dict) else {}
        parts.append(f"{k}: {v.get('status', '?')}" + (f" - {v.get('detail')}" if v.get("status") != "pass" else ""))
    drill = f", drill {out['drill']}" if out.get("drill") else ""
    head = (f"{'FAIL' if failed else 'PASS'} ({len(checks)} checks, {out.get('seconds', '?')} s on "
            f"{out.get('batfish_host', '?')}{drill})")
    card = f"Batfish: {head}: " + "; ".join(parts)
    if failed:
        return {"verdict": "fail", "failed": failed, "card": card,
                "message": f"the Batfish proof failed ({', '.join(failed)}): nothing was sent to the router"}
    return {"verdict": "pass", "failed": [], "card": card, "message": ""}


BATFISH_SUMMARY_CODE = ("import json, sys\n\n\n" + inspect.getsource(batfish_summary)
                        + "\n\nprint(json.dumps(batfish_summary(json.loads(sys.stdin.read() or \"{}\"))))\n")


def drill_judge(d: dict) -> dict:
    """Drill Batfish Gate's verdict: `none` must pass; any other mode must fail, with every check the mode breaks among
    the failed ones (more may fail: a check that cannot run on a broken candidate is a failure too). Pure: runCode runs
    this very function's source after a copy of DRILL_EXPECTS."""
    expects = DRILL_EXPECTS
    mode = d.get("drill")
    s = ((d.get("summary") or {}).get("stdout_json")) or {}
    verdict, failed = s.get("verdict"), sorted(s.get("failed") or [])
    if mode not in expects:
        return {"drill_passed": False, "outcome": f"unknown drill mode {mode!r}"}
    want = sorted(expects[mode])
    passed = verdict == "pass" if mode == "none" else (verdict == "fail" and set(want) <= set(failed))
    said = f"Batfish said {verdict}" + (f", failed: {', '.join(failed)}" if failed else "")
    if passed:
        outcome = f"drill {mode} passed: {said}" + (f" (expected {', '.join(want)})" if want else " (the healthy candidate)")
    else:
        outcome = f"drill {mode} FAILED: {said}; expected " + (", ".join(want) + " to fail" if want else "pass")
    return {"drill_passed": passed, "outcome": outcome}


DRILL_JUDGE_CODE = ("import json, sys\n\nDRILL_EXPECTS = " + json.dumps(DRILL_EXPECTS) + "\n\n\n"
                    + inspect.getsource(drill_judge)
                    + "\n\nprint(json.dumps(drill_judge(json.loads(sys.stdin.read() or \"{}\"))))\n")


def batfish_section(after_ok: str, after_fail: str, x: int) -> tuple[dict, dict]:
    """f8, f0-f7, fb: batfish-check with the approved SHA-256 (the plan's params plus the one the card shows), its
    exit code checked, its answer read by batfish_summary. pass or not proven -> after_ok (the card's entry says which);
    fail -> after_fail. Drill Batfish Gate holds an identical copy (tests/test_batfish_gate.py): there both ways lead
    to its judge."""
    ok, fail, err = "success", "failure", "error"
    tasks = {
        "f8": jq("batfish-check's params", "$var.job.handoff_plan", "stdout_json.batfish", x=x, to_job="batfish_params"),
        "f0": set_key("batfish-check's params: the approved SHA-256", "$var.f8.return_data", "sha256",
                      "$var.job.sha256", x=x + 1),
        "f1": run_service("prove the block in Batfish (batfish-check: the running config, scrubbed; seven checks)",
                          "batfish-check", "$var.f0.object", "batfish_result", x=x + 2),
        "fb": evaluate("batfish-check exited 0?", "f1", "result", "result.return_code", "==", 0, x=x + 3),
        "f2": set_key("Batfish's answer", {}, "batfish", "$var.job.batfish_result", x=x + 4),
        "f3": run_code("what Batfish proved (pass, fail or not proven)", BATFISH_SUMMARY_CODE, "$var.f2.object",
                       "batfish_summary", x=x + 5),
        "f4": evaluate("a check failed?", "f3", "result", "stdout_json.verdict", "==", "fail", x=x + 6),
        "f5": jq("the card: Batfish", "$var.f3.result", "stdout_json.card", x=x + 7, to_job="batfish"),
        "f6": jq("the proof failed: why", "$var.f3.result", "stdout_json.message", x=x + 7, y=-600,
                 to_job="batfish_message"),
        "f7": note("not proven", NOT_PROVEN, "batfish", x=x + 6, y=-300),
    }
    tr = {
        "f8": _edge(**{"f0": ok, "f7": err}),
        "f0": _edge(**{"f1": ok}),
        "f1": _edge(**{"fb": ok, "f7": err}),
        "fb": _edge(**{"f2": ok, "f7": fail}),
        "f2": _edge(**{"f3": ok}),
        "f3": _edge(**{"f4": ok, "f7": err}),
        "f4": _edge(**{"f6": ok, "f5": fail}),
        "f5": _edge(**{after_ok: ok, "f7": err}),
        "f6": _edge(**{after_fail: ok}),
        "f7": _edge(**{after_ok: ok}),
    }
    return tasks, tr


# Hand Off's NetBox read-back (spec "NetBox read-back"): the one open target with NetBox records, read view-only from
# production NetBox through the lab-netbox Integration Model before the card. A second such target would need its own
# reads (the reads are generated per target), so this is refused until then rather than guessed.
_NB_TARGETS = {n: e for n, e in VERSIONS["aws_vpn"]["targets"].items() if e["window"] == "open" and e.get("netbox")}
if len(_NB_TARGETS) > 1:
    raise SystemExit(f"NetBox read-back is generated for one target, not {sorted(_NB_TARGETS)}")
NETBOX_WANT = {
    name: {"device": e["target"]["name"], "site": e["netbox"]["site"], "interface": e["netbox"]["interface"],
           "address": e["target"]["router_inner"], "prefixes": dict(e["netbox"]["prefixes"])}
    for name, e in _NB_TARGETS.items()
}
# NetBox answers 400 to a filter naming an object it does not have (a missing site), so the prefixes are read from
# each top-level expected prefix's parent block (`within`) and their scope is compared here, never filtered on
NETBOX_WITHIN = {
    name: sorted({str(ipaddress.ip_network(p).supernet()) for p in want["prefixes"]
                  if not any(ipaddress.ip_network(p) != ipaddress.ip_network(q)
                             and ipaddress.ip_network(p).subnet_of(ipaddress.ip_network(q)) for q in want["prefixes"])})
    for name, want in NETBOX_WANT.items()
}


def netbox_readback(d: dict) -> dict:
    """Which of the target's records production NetBox holds (`exists`), which it does not (`missing`) and which it
    holds otherwise (`differ`), from the reads Hand Off makes before its card. Reads only: nothing is written. Each
    answer comes as the Integration Model gives it (the payload in `body`); an answer that is not a list of results
    counts as not found. Pure: runCode runs this very function's source, so the unit tests test what runs."""
    want = d.get("want") or {}

    def rows(answer) -> list:
        body = answer.get("body", answer) if isinstance(answer, dict) else None
        results = body.get("results") if isinstance(body, dict) else None
        return [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []

    exists, missing, differ = [], [], []

    def record(label: str, found: bool, wrong: str = "") -> None:
        (missing if not found else differ if wrong else exists).append(f"{label}: {wrong}" if found and wrong else label)

    site = want.get("site")
    record(f"site {site}", any(r.get("slug") == site for r in rows(d.get("site"))))
    prefixes = {}
    for key in sorted(k for k in d if k.startswith("prefixes_")):  # one answer per parent block read
        prefixes.update({r.get("prefix"): r for r in rows(d[key])})
    for prefix, scope in sorted((want.get("prefixes") or {}).items()):
        row = prefixes.get(prefix)
        got = ((row or {}).get("scope") or {}).get("slug")
        record(f"prefix {prefix} ({scope})", row is not None, "" if got == scope else f"scoped to {got or 'nothing'}")
    device, interface, address = want.get("device"), want.get("interface"), want.get("address")
    record(f"{device} {interface}", any(r.get("name") == interface for r in rows(d.get("interface"))))
    ips = rows(d.get("address"))
    on_it = [r for r in ips if (r.get("assigned_object") or {}).get("name") == interface]
    wrong = ("" if any(r.get("address") == address for r in on_it)
             else f"holds {on_it[0].get('address')}" if on_it
             else f"assigned to {(ips[0].get('assigned_object') or {}).get('name') or 'nothing'}" if ips else "")
    record(f"{address} on {interface}", bool(ips), wrong)
    return {"agrees": not missing and not differ, "exists": exists, "missing": missing, "differ": differ}


NETBOX_READBACK_CODE = ("import json, sys\n\n\n" + inspect.getsource(netbox_readback)
                        + "\n\nprint(json.dumps(netbox_readback(json.loads(sys.stdin.read() or \"{}\"))))\n")


def netbox_readback_section(name: str, want: dict, within: list[str], card: str, x: int) -> tuple[dict, dict]:
    """The read-back's tasks (d0-df) between the render and the card: five view-only reads, then the comparison. A read
    that throws (NetBox down, a refused filter) does not stop Hand Off: the card says the read-back could not be made,
    and the approver decides. Publishes `netbox` (the card's NetBox entry) and goes on to `card`."""
    ok, fail, err = "success", "failure", "error"
    host = want["address"].split("/")[0]  # without the mask: a record with the wrong mask still answers, and differs
    tasks = {
        "d0": evaluate("NetBox read-back applies? (a target with NetBox records)", "job", "handoff_plan",
                       "stdout_json.netbox", "==", True, x=x),
        "d1": nbi("dcim_sites_list", f"NetBox: site {want['site']}", {"slug": want["site"], "limit": 1}, x=x + 5),
        "d4": nbi("dcim_interfaces_list", f"NetBox: {want['device']} {want['interface']}",
                  {"device": want["device"], "name": want["interface"]}, x=x + 20),
        "d5": nbi("ipam_ip_addresses_list", f"NetBox: {host} on {want['device']}",
                  {"device": want["device"], "address": host}, x=x + 25),
        "d6": parse("the records NetBox should hold", json.dumps({"want": want}), x=x + 30),
        "d7": set_key("read-back: the site", "$var.d6.textObject", "site", "$var.d1.response", x=x + 31),
        "d8": set_key("read-back: the interface", "$var.d7.object", "interface", "$var.d4.response", x=x + 32),
        "d9": set_key("read-back: the address", "$var.d8.object", "address", "$var.d5.response", x=x + 33),
        "dc": run_code("compare NetBox with the records it should hold", NETBOX_READBACK_CODE, "",
                       "netbox_result", x=x + 40),
        "dd": jq("the read-back", "$var.job.netbox_result", "stdout_json", x=x + 42, to_job="netbox"),
        "de": note("NetBox could not be read",
                   "could not read NetBox (a read failed): check site, prefixes, Tunnel10 and its address by hand "
                   "before approving", "netbox", x=x + 44, y=1500),
        "df": note("no NetBox read-back for this target",
                   f"not applicable (only {name} has NetBox records: the clab twin is not a NetBox device)",
                   "netbox", x=x + 44, y=300),
    }
    # the prefixes, one read per parent block (d2, d3), each its own key: a $var resolves only at the top of an input
    if not 1 <= len(within) <= 2:
        raise SystemExit(f"the read-back reads one or two parent blocks, not {within}")
    reads, keyed = ["d2", "d3"][: len(within)], ["da", "db"][: len(within)]
    obj = "$var.d9.object"
    for i, (read, key, block) in enumerate(zip(reads, keyed, within)):
        tasks[read] = nbi("ipam_prefixes_list", f"NetBox: prefixes within {block}", {"within": block, "limit": 100},
                          x=x + 10 + i)
        tasks[key] = set_key(f"read-back: prefixes within {block}", obj, f"prefixes_{i}", f"$var.{read}.response",
                             x=x + 35 + i)
        obj = f"$var.{key}.object"
    tasks["dc"]["variables"]["incoming"]["data"] = obj
    chain_ids = ["d1", *reads, "d4", "d5", "d6", "d7", "d8", "d9", *keyed, "dc", "dd"]
    tr = {"d0": _edge(d1=ok, df=fail)}
    # the reads, the runCode and the query can fail; parse and setObjectKey on these values cannot (as at 6a-6d)
    can_fail = {"d1", *reads, "d4", "d5", "dc", "dd"}
    for a, b in zip(chain_ids, chain_ids[1:]):
        tr[a] = _edge(**{b: ok, "de": err}) if a in can_fail else _edge(**{b: ok})
    tr["dd"] = _edge(**{card: ok, "de": err})
    tr["de"] = _edge(**{card: ok})
    tr["df"] = _edge(**{card: ok})
    return tasks, tr


def hand_off_aws_vpn() -> dict:
    ok, fail, err = "success", "failure", "error"
    tasks = {
        # nothing is sent until the push, which sets `changed` from what the router went through
        "10": flag("changed = false (nothing sent yet)", "false", "changed", x=50),
        # the target's values (the target is gated: an open one)
        "1a": task("setObjectKey", "WorkFlowEngine", "the target and the pinned values",
                   {"obj": {"targets": VERIFY_TARGETS}, "path": ["target"], "value": "$var.job.target"},
                   {"object": "$var.job.handoff_in"}, display="Tools", x=100),
        "1b": run_code("what to read and push for this target", LAB_EDGE_PLAN_CODE, "$var.job.handoff_in",
                       "handoff_plan", x=200),
        "1c": evaluate("deployed outputs needed (AWS)?", "1b", "result", "stdout_json.need_outputs", "==", True, x=300),
        "2a": parse("outputs params", OUTPUTS_PARAMS, x=350, y=300),
        "2b": run_service("the deployed outputs (terraform-run, a read)", "terraform-run", "$var.2a.textObject",
                          "outputs_result", x=400, y=300),
        "2c": evaluate("outputs read?", "2b", "result", "result.return_code", "==", 0, x=450, y=300),
        "2d": jq("the outputs", "$var.2b.result", "result.stdout_json.outputs", x=500, y=300),
        "2e": set_key("the outputs, as data", "$var.job.handoff_in", "deployed", "$var.2d.return_data", x=550, y=300),
        "2f": run_code("what to read and push, with the outputs", LAB_EDGE_PLAN_CODE, "$var.2e.object", "handoff_plan",
                       x=600, y=300),
        "1d": evaluate("anything to hand off?", "job", "handoff_plan", "stdout_json.ready", "==", True, x=650),
        "1e": jq("why there is nothing to hand off", "$var.job.handoff_plan", "stdout_json.reason", x=700, y=-900,
                 to_job="error"),
        # readiness: the AWS end (where a monitor applies), then the router
        "3a": evaluate("AWS monitor applies?", "job", "handoff_plan", "stdout_json.monitor", "==", "aws", x=750),
        "3b": jq("aws-vpn-monitor's params", "$var.job.handoff_plan", "stdout_json.monitor_ready", x=800, y=300),
        "3c": run_service("the AWS end ready? (a CloudWatch read)", "aws-vpn-monitor", "$var.3b.return_data",
                          "ready_result", x=850, y=300),
        "3d": evaluate("the monitor answered?", "3c", "result", "result.return_code", "==", 0, x=900, y=300),
        "3e": evaluate("the AWS end checked in recently?", "3c", "result", "result.stdout_json.ready", "==", True,
                       x=950, y=300),
        "4a": jq("lab-edge precheck's params", "$var.job.handoff_plan", "stdout_json.precheck", x=1000),
        "4b": run_service("the router's prerequisites (lab-edge precheck: show commands)", "lab-edge",
                          "$var.4a.return_data", "precheck_result", x=1050),
        "4c": evaluate("the precheck ran?", "4b", "result", "result.return_code", "==", 0, x=1100),
        "4d": evaluate("the router is ready?", "4b", "result", "result.stdout_json.ready", "==", True, x=1150),
        "4e": jq("what the router is missing", "$var.4b.result", "result.stdout_json.missing", x=1200, y=-600,
                 to_job="precheck_missing", optional=True),
        # render: the exact block, its SHA-256 and the block with the key masked
        "5a": jq("lab-edge render's params", "$var.job.handoff_plan", "stdout_json.render", x=1250),
        "5b": run_service("render the block (lab-edge render: no device)", "lab-edge", "$var.5a.return_data",
                          "render_result", x=1300),
        "5c": evaluate("rendered?", "5b", "result", "result.return_code", "==", 0, x=1350),
        "5d": jq("the block's SHA-256", "$var.5b.result", "result.stdout_json.sha256", x=1400, to_job="sha256"),
        "5e": jq("the block, key masked", "$var.5b.result", "result.stdout_json.block_masked", x=1450,
                 to_job="block_masked"),
        # approve
        "6a": set_key("the card: target", {}, "target", "$var.job.target", x=1500),
        "6b": set_key("the card: SHA-256", "$var.6a.object", "sha256", "$var.job.sha256", x=1550),
        "6c": set_key("the card: the block (key masked)", "$var.6b.object", "block", "$var.job.block_masked", x=1600),
        "6d": set_key("the card: NetBox", "$var.6c.object", "netbox", "$var.job.netbox", x=1650),
        "69": set_key("the card: Batfish", "$var.6d.object", "batfish", "$var.job.batfish", x=1675),
        "6e": replace("the card's message", APPROVAL_MESSAGE, "__T__", "$var.job.target", x=1700),
        **approval("6f", "Approve the router change", "$var.6e.replacedString", "$var.69.object", "Approve", "Reject",
                   x=1750, var="approval"),  # NetBox, then Batfish (R7), on the card
        # the Batfish proof failed (ADR 0076 decision 4): no card, nothing sent
        "f9": replace("why Hand Off stops here", "__M__", "__M__", "$var.job.batfish_message", x=1460, y=-900),
        # push exactly the approved block
        "7a": jq("lab-edge-push's params", "$var.job.handoff_plan", "stdout_json.push", x=1800),
        "7b": set_key("push params: the approved SHA-256", "$var.7a.return_data", "sha256", "$var.job.sha256", x=1850),
        "7c": run_service("push the approved block (lab-edge-push: the key from Vault, revert timer, prove, save)",
                          "lab-edge-push", "$var.7b.object", "push_result", x=1900),
        # what the push did, from saved / rolled_back (never the exit code alone: a rollback exits 0); a non-zero
        # exit is an answer too, so both outcomes go on to the summary
        "74": evaluate("lab-edge-push exited 0?", "7c", "result", "result.return_code", "==", 0, x=1925),
        "75": note("lab-edge-push exited non-zero", "lab-edge-push stopped: push_summary says what the router went "
                   "through", "push_note", x=1940, y=-300),
        "7d": set_key("the push's answer", {}, "push", "$var.job.push_result", x=1950),
        "7e": run_code("what the push did (saved, rolled back or failed)", PUSH_SUMMARY_CODE, "$var.7d.object",
                       "push_summary", x=2000),
        "7f": evaluate("the router may have changed?", "7e", "result", "stdout_json.changed", "==", True, x=2050),
        "70": flag("changed = true", "true", "changed", x=2075, y=300),
        "71": jq("what the router went through", "$var.7e.result", "stdout_json.router", x=2100, to_job="router_state"),
        "72": evaluate("pushed, proved and saved?", "7e", "result", "stdout_json.state", "==", "saved", x=2125),
        "73": jq("why the push did not save", "$var.7e.result", "stdout_json.message", x=2150, y=-900, to_job="error"),
        "8a": task("delay", "WorkFlowEngine", f"wait {SETTLE_SECONDS} s for the tunnel", {"time": SETTLE_SECONDS},
                   {"time_in_milliseconds": None}, kind="operation", display="WorkFlowEngine", x=2100),
        # the outcome: what the router went through and Verify's verdict
        "90": replace("the outcome: router", OUTCOME_TPL, "__R__", "$var.job.router_state", x=2900),
        "91": jq("the verdict", "$var.ee.result", "stdout_json.verdict", x=2950),
        "92": replace("the outcome", "$var.90.replacedString", "__V__", "$var.91.return_data", x=3000),
        "94": replace("the outcome (Verify did not pass): router", OUTCOME_TPL, "__R__", "$var.job.router_state",
                      x=2900, y=-300),
        "95": jq("the verdict (not passed)", "$var.ee.result", "stdout_json.verdict", x=2950, y=-300),
        "96": replace("the outcome (Verify did not pass)", "$var.94.replacedString", "__V__", "$var.95.return_data",
                      x=3000, y=-300),
        # reject: nothing sent
        "a0": flag("rejected = true", "true", "rejected", x=1800, y=600),
        "a2": note("the rejection", "rejected in Work Center: nothing was sent to the router", "outcome", x=1900, y=600),
        # failures before the push: nothing was sent
        "b0": note("the plan could not run", "the Gateway could not work out what to hand off (see handoff_plan): "
                   "nothing was sent to the router", "error", x=700, y=-600),
        "b7": note("the precheck could not start", "the precheck's params could not be read (see handoff_plan): nothing "
                   "was sent to the router", "error", x=1050, y=-600),
        "b8": note("the render could not run", "the Gateway could not run lab-edge render or read its answer (see "
                   "render_result): nothing was sent to the router", "error", x=1400, y=-600),
        "b9": note("the push could not start", "the push's params could not be read (see handoff_plan): nothing was "
                   "sent to the router", "error", x=1850, y=-600),
        "b1": note("the outputs could not be read", "the deployed outputs could not be read from the Terraform state "
                   "(outputs_result): nothing was sent to the router", "error", x=550, y=900),
        "b2": note("the AWS end could not be checked", "could not check whether the AWS end is ready (ready_result): "
                   "nothing was sent to the router", "error", x=950, y=900),
        "b3": note("the AWS end is not ready", "the AWS monitor has not checked the strongSwan box successfully in the "
                   "last 15 minutes: nothing was sent to the router", "error", x=1000, y=900),
        "b4": note("the precheck did not run", "lab-edge precheck refused or could not read the router (precheck_result): "
                   "nothing was sent to the router", "error", x=1150, y=-900),
        "b5": note("the router is not ready", "the router is not ready for Hand Off (precheck_missing lists what is "
                   "missing): nothing was sent to the router", "error", x=1250, y=-600),
        "b6": note("the render refused", "the render refused the values (an output that does not match versions.yaml, "
                   "or nothing deployed; see render_result): nothing was sent to the router", "error", x=1400, y=-900),
        # failures of the push or after it
        "c0": note("the push could not run", "the Gateway could not run lab-edge-push: if any line reached the router, its "
                   "revert timer rolls it back within 5 minutes - check the router", "error", x=1950, y=-600),
        "c4": note("the push's answer could not be read", "lab-edge-push ran but its answer could not be read: check "
                   "the router (push_result)", "error", x=2100, y=-1200),
        "c3": note("the verify steps could not run", "the push was saved (see changed and router_state), but the verify "
                   "steps could not run or gave no verdict: run Verify AWS VPN", "error", x=3000, y=-600),
        "c5": flag("changed = true (the push's answer is unknown)", "true", "changed", x=2100, y=-900),
    }
    tasks["92"]["variables"]["outgoing"]["replacedString"] = "$var.job.outcome"
    tasks["96"]["variables"]["outgoing"]["replacedString"] = "$var.job.error"
    tasks["f9"]["variables"]["outgoing"]["replacedString"] = "$var.job.error"
    vtasks, vtr, first = verify_section("handoff_plan", passed="90", failed="94", broken="c3", judge_broken="c3", x=2150)
    tasks.update(vtasks)
    # R7 (ADR 0076): the Batfish proof after the render, before the NetBox read-back and the card
    btasks, btr = batfish_section(after_ok="d0" if NETBOX_WANT else "6a", after_fail="f9", x=1451)
    tasks.update(btasks)
    # the NetBox read-back before the card (dc1-wan01's records); with no such target open, the card says so itself
    nb_tr = {}
    for name, want in NETBOX_WANT.items():
        nb_tasks, nb_tr = netbox_readback_section(name, want, NETBOX_WITHIN[name], card="6a", x=1455)
        tasks.update(nb_tasks)
    if not NETBOX_WANT:
        tasks["6d"]["variables"]["incoming"]["value"] = "not applicable (no open target has NetBox records)"
    tr = {
        **nb_tr,
        "workflow_start": _edge(**{"10": ok}),
        "10": _edge(**{"1a": ok}),
        "1a": _edge(**{"1b": ok}),
        "1b": _edge(**{"1c": ok, "b0": err}),
        "1c": _edge(**{"2a": ok, "1d": fail}),
        "2a": _edge(**{"2b": ok}),
        "2b": _edge(**{"2c": ok, "b1": err}),
        "2c": _edge(**{"2d": ok, "b1": fail}),
        "2d": _edge(**{"2e": ok, "b1": err}),
        "2e": _edge(**{"2f": ok}),
        "2f": _edge(**{"1d": ok, "b0": err}),
        "1d": _edge(**{"3a": ok, "1e": fail}),
        "1e": _edge(**{"workflow_end": ok, "b0": err}),
        "3a": _edge(**{"3b": ok, "4a": fail}),
        "3b": _edge(**{"3c": ok, "b2": err}),
        "3c": _edge(**{"3d": ok, "b2": err}),
        "3d": _edge(**{"3e": ok, "b2": fail}),
        "3e": _edge(**{"4a": ok, "b3": fail}),
        "4a": _edge(**{"4b": ok, "b7": err}),
        "4b": _edge(**{"4c": ok, "b4": err}),
        "4c": _edge(**{"4d": ok, "b4": fail}),
        "4d": _edge(**{"5a": ok, "4e": fail}),
        "4e": _edge(**{"b5": ok, "b4": err}),  # optional: a missing list that cannot be read is null, still not ready
        "5a": _edge(**{"5b": ok, "b8": err}),
        "5b": _edge(**{"5c": ok, "b8": err}),
        "5c": _edge(**{"5d": ok, "b6": fail}),
        "5d": _edge(**{"5e": ok, "b8": err}),
        "5e": _edge(**{"f8": ok, "b8": err}),
        **btr,
        "f9": _edge(**{"workflow_end": ok}),
        "6a": _edge(**{"6b": ok}),
        "6b": _edge(**{"6c": ok}),
        "6c": _edge(**{"6d": ok}),
        "6d": _edge(**{"69": ok}),
        "69": _edge(**{"6e": ok}),
        "6e": _edge(**{"6f1": ok}), **approval_edges("6f"),
        "6f": _edge(**{"7a": ok, "a0": fail}),
        "a0": _edge(**{"a2": ok}),
        "a2": _edge(**{"workflow_end": ok}),
        "7a": _edge(**{"7b": ok, "b9": err}),
        "7b": _edge(**{"7c": ok}),
        "7c": _edge(**{"74": ok, "c0": err}),
        "74": _edge(**{"7d": ok, "75": fail}),
        "75": _edge(**{"7d": ok}),
        "7d": _edge(**{"7e": ok}),
        "7e": _edge(**{"7f": ok, "c5": err}),
        "7f": _edge(**{"70": ok, "71": fail}),
        "70": _edge(**{"71": ok}),
        "71": _edge(**{"72": ok, "c5": err}),
        "72": _edge(**{"8a": ok, "73": fail}),
        "73": _edge(**{"workflow_end": ok, "c5": err}),
        "8a": _edge(**{first: ok}),
        **vtr,
        "90": _edge(**{"91": ok}),
        "91": _edge(**{"92": ok, "c3": err}),
        "92": _edge(**{"workflow_end": ok}),
        "94": _edge(**{"95": ok}),
        "95": _edge(**{"96": ok, "c3": err}),
        "96": _edge(**{"workflow_end": ok}),
        **{b: _edge(**{"workflow_end": ok}) for b in ("b0", "b1", "b2", "b3", "b4", "b5", "b6", "b7", "b8", "b9")},
        "c0": _edge(**{"workflow_end": ok}),  # its message says to check the router: lines may have reached it
        "c5": _edge(**{"c4": ok}),
        "c4": _edge(**{"workflow_end": ok}),
        "c3": _edge(**{"workflow_end": ok}),
    }
    return workflow(
        WF["hand_off_aws_vpn"],
        "Hands the lab edge router its side of the AWS VPN: checks the deployed values and both ends, shows the exact "
        "block (key masked) for approval, pushes exactly that block with the key from Vault under a revert timer, "
        "proves and saves it, then runs the same checks as Verify AWS VPN (PID S13 criterion 3, ADR 0068)",
        {"target": {"type": "string", "required": True, "enum": INPUT_GATES[WF["hand_off_aws_vpn"]]["target"]["enum"],
                    "description": "The lab edge router to hand the AWS VPN to"}},
        tasks,
        tr,
        {
            "outcome": {"type": "string"},
            "error": {"type": "string"},
            "changed": {"type": "boolean"},
            "rejected": {"type": "boolean"},
            "router_state": {"type": "string"},
            "sha256": {"type": "string"},
            "block_masked": {"type": "string"},
            "precheck_missing": {"type": "array"},
            "handoff_plan": {"type": "object"}, "approval_card": {"type": "object"},
            "approval_decision": {"type": ["object", "null"]},
            "outputs_result": {"type": "object"},
            "ready_result": {"type": "object"},
            "precheck_result": {"type": "object"},
            "render_result": {"type": "object"},
            "push_result": {"type": "object"},
            "push_summary": {"type": "object"},
            "push_note": {"type": "string"},
            "judgement": {"type": "object"},
            "lab_edge_result": {"type": "object"},
            "monitor_result": {"type": "object"},
            "router_note": {"type": "string"},
            "monitor_note": {"type": "string"},
            "netbox": {"type": ["object", "string"]},
            "netbox_result": {"type": "object"},
            "batfish_params": {"type": "object"},
            "batfish_result": {"type": "object"},
            "batfish_summary": {"type": "object"},
            "batfish": {"type": "string"},
            "batfish_message": {"type": "string"},
        },
    )


# --- R7: Drill Batfish Gate (ADR 0076 decision 6) ---------------------------------------------------------------------
# Hand Off's own Batfish section, on a candidate batfish-check breaks in one known way (or `none`: the healthy one), with
# no card and nothing sent: the proof that each check can fail is a run, not a claim. verify/test-13a runs every mode.
def drill_batfish_gate() -> dict:
    ok, fail, err = "success", "failure", "error"
    name = WF["drill_batfish_gate"]
    tasks = {
        "1a": task("setObjectKey", "WorkFlowEngine", "the target and the pinned values",
                   {"obj": {"targets": VERIFY_TARGETS}, "path": ["target"], "value": "$var.job.target"},
                   {"object": None}, display="Tools", x=100),
        "1b": set_key("the drill mode", "$var.1a.object", "drill", "$var.job.drill", x=150),
        "1c": run_code("what to read and check for this target", LAB_EDGE_PLAN_CODE, "$var.job.handoff_in",
                       "handoff_plan", x=200),
        "1d": evaluate("deployed outputs needed (AWS)?", "1c", "result", "stdout_json.need_outputs", "==", True, x=300),
        "2a": parse("outputs params", OUTPUTS_PARAMS, x=350, y=300),
        "2b": run_service("the deployed outputs (terraform-run, a read)", "terraform-run", "$var.2a.textObject",
                          "outputs_result", x=400, y=300),
        "2c": evaluate("outputs read?", "2b", "result", "result.return_code", "==", 0, x=450, y=300),
        "2d": jq("the outputs", "$var.2b.result", "result.stdout_json.outputs", x=500, y=300),
        "2e": set_key("the outputs, as data", "$var.job.handoff_in", "deployed", "$var.2d.return_data", x=550, y=300),
        "2f": run_code("what to read and check, with the outputs", LAB_EDGE_PLAN_CODE, "$var.2e.object", "handoff_plan",
                       x=600, y=300),
        "1e": evaluate("anything to check?", "job", "handoff_plan", "stdout_json.ready", "==", True, x=650),
        "1f": jq("why there is nothing to check", "$var.job.handoff_plan", "stdout_json.reason", x=700, y=-900,
                 to_job="error"),
        # the same render as Hand Off's: the SHA-256 the service must agree with
        "5a": jq("lab-edge render's params", "$var.job.handoff_plan", "stdout_json.render", x=1250),
        "5b": run_service("render the block (lab-edge render: no device)", "lab-edge", "$var.5a.return_data",
                          "render_result", x=1300),
        "5c": evaluate("rendered?", "5b", "result", "result.return_code", "==", 0, x=1350),
        "5d": jq("the block's SHA-256", "$var.5b.result", "result.stdout_json.sha256", x=1400, to_job="sha256"),
        "5e": jq("the block, key masked", "$var.5b.result", "result.stdout_json.block_masked", x=1450,
                 to_job="block_masked"),
        # the judge: did the mode break what it should?
        "a0": set_key("the judge's data: the drill mode", {}, "drill", "$var.job.drill", x=1500),
        "a1": set_key("the judge's data: what Batfish proved", "$var.a0.object", "summary", "$var.job.batfish_summary",
                      x=1550),
        "a2": run_code("did the drill break what it should?", DRILL_JUDGE_CODE, "$var.a1.object", "drill_judgement",
                       x=1600),
        "a3": jq("the outcome", "$var.a2.result", "stdout_json.outcome", x=1650, to_job="outcome"),
        "a4": evaluate("the drill passed?", "a2", "result", "stdout_json.drill_passed", "==", True, x=1700),
        "a5": flag("drill_passed = true", "true", "drill_passed", x=1750),
        "a6": flag("drill_passed = false", "false", "drill_passed", x=1750, y=300),
        "b0": note("the plan could not run", "the Gateway could not work out what to check (see handoff_plan)", "error",
                   x=700, y=-600),
        "b1": note("the outputs could not be read", "the deployed outputs could not be read from the Terraform state "
                   "(outputs_result)", "error", x=550, y=900),
        "b6": note("the render refused", "the render refused the values (see render_result): nothing to prove", "error",
                   x=1400, y=-900),
        "b8": note("the render could not run", "the Gateway could not run lab-edge render or read its answer (see "
                   "render_result)", "error", x=1400, y=-600),
        "c3": note("the judge could not run", "the drill's judge could not run or gave no verdict (see "
                   "drill_judgement, batfish_summary)", "error", x=1650, y=-600),
    }
    tasks["1b"]["variables"]["outgoing"]["object"] = "$var.job.handoff_in"
    btasks, btr = batfish_section(after_ok="a0", after_fail="a0", x=1451)
    tasks.update(btasks)
    tr = {
        "workflow_start": _edge(**{"1a": ok}),
        "1a": _edge(**{"1b": ok}),
        "1b": _edge(**{"1c": ok}),
        "1c": _edge(**{"1d": ok, "b0": err}),
        "1d": _edge(**{"2a": ok, "1e": fail}),
        "2a": _edge(**{"2b": ok}),
        "2b": _edge(**{"2c": ok, "b1": err}),
        "2c": _edge(**{"2d": ok, "b1": fail}),
        "2d": _edge(**{"2e": ok, "b1": err}),
        "2e": _edge(**{"2f": ok}),
        "2f": _edge(**{"1e": ok, "b0": err}),
        "1e": _edge(**{"5a": ok, "1f": fail}),
        "1f": _edge(**{"workflow_end": ok, "b0": err}),
        "5a": _edge(**{"5b": ok, "b8": err}),
        "5b": _edge(**{"5c": ok, "b8": err}),
        "5c": _edge(**{"5d": ok, "b6": fail}),
        "5d": _edge(**{"5e": ok, "b8": err}),
        "5e": _edge(**{"f8": ok, "b8": err}),
        **btr,
        "a0": _edge(**{"a1": ok}),
        "a1": _edge(**{"a2": ok}),
        "a2": _edge(**{"a3": ok, "c3": err}),
        "a3": _edge(**{"a4": ok, "c3": err}),
        "a4": _edge(**{"a5": ok, "a6": fail}),
        "a5": _edge(**{"workflow_end": ok}),
        "a6": _edge(**{"workflow_end": ok}),
        **{b: _edge(**{"workflow_end": ok}) for b in ("b0", "b1", "b6", "b8", "c3")},
    }
    return workflow(
        name,
        "Runs Hand Off AWS VPN's Batfish proof on the router's running configuration with the block applied and broken "
        "in one known way (or none), without a card and without sending anything: proves that each check can fail "
        "(R7, ADR 0076 decision 6)",
        {"target": {"type": "string", "required": True, "enum": INPUT_GATES[name]["target"]["enum"],
                    "description": "The lab edge router whose running configuration the candidate is built on"},
         "drill": {"type": "string", "required": True, "enum": INPUT_GATES[name]["drill"]["enum"],
                   "description": "How batfish-check breaks the candidate (none: the healthy candidate, must pass)"}},
        tasks,
        tr,
        {
            "outcome": {"type": "string"},
            "error": {"type": "string"},
            "drill_passed": {"type": "boolean"},
            "sha256": {"type": "string"},
            "block_masked": {"type": "string"},
            "handoff_in": {"type": "object"},
            "handoff_plan": {"type": "object"},
            "outputs_result": {"type": "object"},
            "render_result": {"type": "object"},
            "batfish_params": {"type": "object"},
            "batfish_result": {"type": "object"},
            "batfish_summary": {"type": "object"},
            "batfish": {"type": "string"},
            "batfish_message": {"type": "string"},
            "drill_judgement": {"type": "object"},
        },
    )

# --- Push Configuration with Revert Timer (step 10, F9 decision 2a) --------------------------------------------------
# Owner choice 2026-10-01: a sibling of Push Configuration with Approval, which stays untouched for its agent and
# Lifecycle Manager callers. The approver sees the router, the reason, the exact lines, the timer and every check; on
# approval cloud-devops-pipeline's config-push-revert runs the checks, pushes under `configure terminal revert timer N`,
# checks again (BGP, pings, a fresh login), and confirms and saves - or rolls back and proves it. The outcome comes
# from saved / rolled_back, never from the exit code alone.
REVERT_TARGETS = VERSIONS["revert_push"]["targets"]
REVERT_APPROVAL_MESSAGE = (
    "Approve this change to __D__. It is pushed under a revert timer and kept only if every check below still passes "
    "afterwards (and a fresh login still works); otherwise the router rolls it back. Nothing is saved before that."
)


def revert_plan(d: dict, targets: dict) -> dict:
    """The service's params and the approval card, from the job's inputs (already through the input gate). The card
    lists exactly what will be pushed and what must hold afterwards; the params carry the same lines.
    Pure: runCode runs this function's own source on the Gateway, with the targets table written in."""
    device = d.get("device")
    target = targets.get(device)
    if not target:
        return {"ok": False, "message": f"{device} is not a router this workflow may change (revert_push.targets)"}
    lines = [line.rstrip() for line in str(d.get("config") or "").splitlines() if line.strip()]
    if not lines:
        return {"ok": False, "message": "no configuration lines to push"}
    checks, minutes = d.get("checks") or {}, d.get("revert_minutes")
    kept_if = []
    for b in checks.get("bgp_established") or []:
        n, vrf = (b.get("neighbor"), b.get("vrf")) if isinstance(b, dict) else (b, None)
        kept_if.append(f"BGP neighbour {n} Established" + (f" (VRF {vrf})" if vrf else ""))
    for p in checks.get("pings") or []:
        kept_if.append(f"ping {p['target']}" + (f" from {p['source']}" if p.get("source") else "")
                       + (f" in VRF {p['vrf']}" if p.get("vrf") else "") + f" at least {p.get('min_percent', 80)}%")
    kept_if.append("a fresh login to the router still works")
    inputs = {
        "target_json": json.dumps({"name": device, "mgmt_host": target["mgmt_host"]}),
        "lines_json": json.dumps(lines),
        "checks_json": json.dumps(checks),
        "revert_minutes": str(minutes),
    }
    # `check` (no device) runs before the card; the push's Gateway timeout is the one `check` answers
    check = {"action": "check", **inputs, "timeout": "120"}  # the lab's shortest service limit (the checkout runs in it)
    params = {"action": "push", **inputs, "username": target["username"]}
    card = {
        "router": device,
        "reason": d.get("reason"),
        "revert timer": f"{minutes} minutes",
        "settle before the checks": f"{checks.get('settle_seconds', 75)} s",
        "kept only if": kept_if,
        "saving": "`write memory` saves the whole running configuration - with any change already on the router and "
                  "not yet saved",
        "lines": lines,
    }
    return {"ok": True, "check": check, "params": params, "card": card}


def revert_summary(d: dict) -> dict:
    """config-push-revert's answer as the workflow reads it: saved, rolled back (proved by the service's read-back),
    nothing sent, or unknown. Only a saved change exits 0, so the exit code alone says nothing about the router; with
    no answer at all the router may hold a pending change (the revert timer rolls it back) - changed is then true.
    Pure: runCode runs this function's own source on the Gateway."""
    result = ((d.get("push") or {}).get("result")) or {}
    rc, out = result.get("return_code"), result.get("stdout_json")
    out = out if isinstance(out, dict) else {}
    if rc == 0 and out.get("saved") is True:
        state, changed = "saved", out.get("changed") is True
    elif out.get("rolled_back") is True:
        state, changed = "rolled back", False
    elif out and out.get("sent") is False:
        state, changed = "unchanged", False
    else:
        state, changed = "unknown", True
    router = out.get("router") or {"saved": "saved", "rolled back": "rolled back", "unchanged": "unchanged: nothing was sent"}.get(
        state, "unknown: the service gave no answer - a change may be pending until the revert timer rolls it back")
    message = "" if state == "saved" else str(out.get("error") or "config-push-revert did not finish (see push_result)")
    return {"state": state, "changed": changed, "router": router, "message": message}


REVERT_PLAN_CODE = ("import json, sys\n\n\nTARGETS = " + repr(REVERT_TARGETS) + "\n\n\n" + inspect.getsource(revert_plan)
                    + "\n\nprint(json.dumps(revert_plan(json.loads(sys.stdin.read() or \"{}\"), TARGETS)))\n")
REVERT_SUMMARY_CODE = ("import json, sys\n\n\n" + inspect.getsource(revert_summary)
                       + "\n\nprint(json.dumps(revert_summary(json.loads(sys.stdin.read() or \"{}\"))))\n")


def config_push_revert() -> dict:
    ok, fail, err = "success", "failure", "error"
    tasks = {
        # nothing has changed until the service says so: every early end leaves this as it is
        "10": flag("changed = false", "false", "changed", x=50),
        # the inputs, as data (never templated), into the plan
        "1a": set_key("plan input: device", {}, "device", "$var.job.device", x=100),
        "1b": set_key("plan input: config", "$var.1a.object", "config", "$var.job.config", x=150),
        "1c": set_key("plan input: reason", "$var.1b.object", "reason", "$var.job.reason", x=200),
        "1d": set_key("plan input: revert_minutes", "$var.1c.object", "revert_minutes", "$var.job.revert_minutes", x=250),
        "1e": set_key("plan input: checks", "$var.1d.object", "checks", "$var.job.checks", x=300),
        "1f": run_code("the plan: the service's params and the approval card (Python on the runner)", REVERT_PLAN_CODE,
                       "$var.1e.object", "revert_plan", x=350),
        "11": evaluate("planned?", "1f", "result", "stdout_json.ok", "==", True, x=400),
        # config-push-revert's own check, with no device: what it would refuse never reaches an approver
        "12": jq("config-push-revert check's params", "$var.1f.result", "stdout_json.check", x=410),
        "13": run_service("would config-push-revert take this? (check: no device)", "config-push-revert",
                          "$var.12.return_data", "check_result", x=420),
        "14": evaluate("config-push-revert takes it?", "13", "result", "result.return_code", "==", 0, x=430),
        "15": jq("why config-push-revert refuses it", "$var.13.result", "result.stdout_json.error", x=430, y=-1500,
                 to_job="error"),
        "18": note("config-push-revert refuses it", "config-push-revert refused these inputs without a reason (see "
                   "check_result; a pinned cloud-devops-pipeline without `check` answers so): nothing was sent to the "
                   "router", "error", x=440, y=-450),
        "b8": note("config-push-revert could not run", "the Gateway could not run config-push-revert (not on this "
                   "Gateway - a dev-tier service until step 10's window - or it could not start, e.g. an alias that did "
                   "not resolve; see check_result): nothing was sent to the router", "error", x=420, y=-1500),
        # approve exactly what will be pushed and what must hold afterwards
        "2a": jq("the card", "$var.1f.result", "stdout_json.card", x=450),
        "2b": replace("the card's message", REVERT_APPROVAL_MESSAGE, "__D__", "$var.job.device", x=500),
        **approval("2c", "Approve the change under a revert timer", "$var.2b.replacedString", "$var.2a.return_data",
                   "Approve", "Reject", x=550, var="approval"),
        # push, check, confirm and save - or roll back
        "17": flag("rejected = false", "false", "rejected", x=570),
        "3a": jq("config-push-revert's params", "$var.1f.result", "stdout_json.params", x=600),
        "16": jq("the timeout a push needs (check's answer)", "$var.13.result", "result.stdout_json.timeout", x=610),
        "34": set_key("push params: the Gateway timeout", "$var.3a.return_data", "timeout", "$var.16.return_data",
                      x=620),
        "3b": run_service("push under the revert timer, check, then confirm and save or roll back (config-push-revert)",
                          "config-push-revert", "$var.34.object", "push_result", x=650),
        "3c": evaluate("config-push-revert exited 0?", "3b", "result", "result.return_code", "==", 0, x=700),
        "3d": note("config-push-revert exited non-zero", "config-push-revert did not save: push_summary says what the "
                   "router went through", "push_note", x=700, y=-300),
        "3e": set_key("the push's answer", {}, "push", "$var.job.push_result", x=750),
        "3f": run_code("what the push did (saved, rolled back, nothing sent or unknown)", REVERT_SUMMARY_CODE,
                       "$var.3e.object", "push_summary", x=800),
        "31": jq("what the router went through", "$var.3f.result", "stdout_json.router", x=850, to_job="router_state"),
        "32": evaluate("the router may have changed?", "3f", "result", "stdout_json.changed", "==", True, x=900),
        "5a": flag("changed = true", "true", "changed", x=950),
        "5b": flag("changed = false", "false", "changed", x=950, y=300),
        "33": evaluate("confirmed and saved?", "3f", "result", "stdout_json.state", "==", "saved", x=1000),
        "4a": note("the outcome: saved", "saved: the change passed every check, was confirmed and saved", "outcome",
                   x=1050),
        "4b": jq("why the change was not kept", "$var.3f.result", "stdout_json.message", x=1050, y=300, to_job="error"),
        # reject: nothing sent
        "a0": flag("rejected = true", "true", "rejected", x=600, y=600),
        "a2": note("the rejection", "rejected in Work Center: nothing was sent to the router", "outcome", x=700, y=300),
        # failures before the push: nothing was sent
        "b0": note("the plan could not run", "the Gateway could not make the plan (see revert_plan): nothing was sent "
                   "to the router", "error", x=400, y=-600),
        "b1": jq("why there is no plan", "$var.1f.result", "stdout_json.message", x=450, y=600, to_job="error"),
        "b3": note("the push could not start", "the push's params could not be read (see revert_plan): nothing was "
                   "sent to the router", "error", x=650, y=-600),
        # the Gateway could not run the service or the summary: the router may hold a pending change
        "b5": note("config-push-revert could not finish", "the Gateway could not run config-push-revert or read its "
                   "answer (push_result): check the router - the change may be live and unsaved, or pending until "
                   "the revert timer rolls it back", "error", x=750, y=-1500),
        "b6": flag("changed = true (unknown)", "true", "changed", x=800, y=-1500),
    }
    tr = {
        "workflow_start": _edge(**{"10": ok}),
        "10": _edge(**{"1a": ok}),
        "1a": _edge(**{"1b": ok}), "1b": _edge(**{"1c": ok}), "1c": _edge(**{"1d": ok}), "1d": _edge(**{"1e": ok}),
        "1e": _edge(**{"1f": ok}),
        "1f": _edge(**{"11": ok, "b0": err}),
        "11": _edge(**{"12": ok, "b1": fail}),
        "12": _edge(**{"13": ok, "b0": err}),
        "13": _edge(**{"14": ok, "b8": err}),
        "14": _edge(**{"2a": ok, "15": fail}),
        "15": _edge(**{"workflow_end": ok, "18": err}),
        "18": _edge(**{"workflow_end": ok}),
        "b8": _edge(**{"workflow_end": ok}),
        "2a": _edge(**{"2b": ok, "b0": err}),
        "2b": _edge(**{"2c1": ok}), **approval_edges("2c"),
        "2c": _edge(**{"17": ok, "a0": fail}),
        "17": _edge(**{"3a": ok}),
        "3a": _edge(**{"16": ok, "b3": err}),
        "16": _edge(**{"34": ok, "b3": err}),
        "34": _edge(**{"3b": ok}),
        "3b": _edge(**{"3c": ok, "b5": err}),
        "3c": _edge(**{"3e": ok, "3d": fail}),
        "3d": _edge(**{"3e": ok}),
        "3e": _edge(**{"3f": ok}),
        "3f": _edge(**{"31": ok, "b5": err}),
        "31": _edge(**{"32": ok, "b5": err}),
        "32": _edge(**{"5a": ok, "5b": fail}),
        "5a": _edge(**{"33": ok}), "5b": _edge(**{"33": ok}),
        "33": _edge(**{"4a": ok, "4b": fail}),
        "4a": _edge(**{"workflow_end": ok}), "4b": _edge(**{"workflow_end": ok}),
        "a0": _edge(**{"a2": ok}), "a2": _edge(**{"workflow_end": ok}),
        "b0": _edge(**{"workflow_end": ok}), "b1": _edge(**{"workflow_end": ok}), "b3": _edge(**{"workflow_end": ok}),
        "b5": _edge(**{"b6": ok}), "b6": _edge(**{"workflow_end": ok}),
    }
    return workflow(
        WF["config_push_revert"],
        "Pushes configuration lines to one router after a Work Center approval, under a revert timer: kept and saved "
        "only if BGP, the pings and a fresh login still pass afterwards, otherwise rolled back and proved (step 10, "
        "F9 decision 2a)",
        {
            "device": {"type": "string", "required": True, "enum": sorted(REVERT_TARGETS),
                       "description": "The router to change (revert_push.targets)"},
            "config": {"type": "string", "required": True, "description": "Configuration lines to push, one per line"},
            "reason": {"type": "string", "required": True, "description": "Why, shown to the approver"},
            "revert_minutes": {"type": "integer", "required": True, "minimum": 2, "maximum": 15,
                               "description": "The revert timer, in minutes"},
            "checks": {"type": "object", "required": True,
                       "description": "What must hold after the change: bgp_established, pings, settle_seconds"},
        },
        tasks,
        tr,
        {
            "outcome": {"type": "string"},
            "error": {"type": "string"},
            "changed": {"type": "boolean"},
            "rejected": {"type": "boolean"},
            "router_state": {"type": "string"},
            "revert_plan": {"type": "object"}, "approval_card": {"type": "object"},
            "approval_decision": {"type": ["object", "null"]},
            "check_result": {"type": "object"},
            "push_result": {"type": "object"},
            "push_summary": {"type": "object"},
            "push_note": {"type": "string"},
        },
    )


# --- Tear Down AWS VPN (PID S13 criterion 5, R2) ----------------------------------------------------------------------
# The inverse of Hand Off, then the inverse of Deploy, inline (no child jobs): lab-edge `removal` renders the router's
# lines (cloud-devops-pipeline lab_edge_render.removal, the exact inverse of the block), config-push-revert pushes them
# under a revert timer after the first Work Center card (it never holds the key), lab-edge `absent` proves nothing of
# the block is left - and only then, for a target deployed in AWS, terraform-run plans the destroy (no variables: a
# destroy plan takes everything in the state), the second card shows it, and exactly that plan is applied. A router step
# that does not end saved and proved stops the job before AWS. The clab twin has no AWS deployment: it ends after the
# router. Proven by hand first (2026-10-02): the same lines, then the same destroy, then a full redeploy.
TEARDOWN_TARGETS = {
    name: {"target": entry["target"], "username": entry["username"], "monitor": entry["monitor"]}
    for name, entry in VERSIONS["aws_vpn"]["targets"].items()
    if entry["window"] == "open" and name in REVERT_TARGETS
}
TEARDOWN_REVERT_MINUTES = 8
DESTROY_PLAN_PARAMS = '{"action": "plan-destroy", "job": "new", "timeout": "900"}'
TEARDOWN_ROUTER_MESSAGE = (
    "Tear Down the AWS VPN on __D__, step 1 of 2: remove the router's AWS block. The lines below are pushed under a "
    "revert timer and kept only if every check still passes (and a fresh login still works); then the router is read "
    "back to prove nothing of the block is left. Only after that is the AWS side planned for destruction, on a second "
    "card. Nothing is saved before the checks pass."
)
TEARDOWN_AWS_MESSAGE = (
    "Tear Down the AWS VPN, step 2 of 2: destroy the AWS side. The router's block is already removed and proved gone. "
    "Exactly this plan is applied (the strongSwan box, its Elastic IP, the key's secret and the VPC go); rejecting "
    "keeps it all, at about $1 a day."
)


def teardown_plan(d: dict, targets: dict, revert_targets: dict, revert_minutes: int, lab_edge_timeout: str) -> dict:
    """Tear Down's plan for one router. First call (no `removal` yet): the params of lab-edge `removal` and `absent`,
    and whether an AWS deployment follows. Second call (with lab-edge removal's answer): config-push-revert's params
    and the first card, through `revert_plan` with this router's own teardown checks. Pure: runCode runs this source
    on the Gateway, with the tables and `revert_plan` written in."""
    name = d.get("target")
    entry, revert = (targets or {}).get(name), (revert_targets or {}).get(name)
    if not entry or not revert:
        return {"ok": False, "message": f"{name} is not a router Tear Down may change (an open target with a revert target)"}
    target_json = json.dumps(entry["target"])
    plan = {
        "ok": True,
        "target": name,
        "aws": entry.get("monitor") == "aws",
        "removal": {"action": "removal", "target_json": target_json, "timeout": "120"},
        "absent": {"action": "absent", "target_json": target_json, "username": entry["username"],
                   "timeout": lab_edge_timeout},
    }
    removal = d.get("removal")
    if removal is None:
        return plan
    out = ((removal or {}).get("result") or {}).get("stdout_json") or {}
    lines = out.get("lines") if (removal.get("result") or {}).get("return_code") == 0 else None
    if not lines or out.get("target") != name:
        return {"ok": False, "message": "lab-edge removal gave no lines for " + str(name) + " (see removal_result)"}
    pushed = revert_plan({"device": name, "config": lines, "revert_minutes": revert_minutes,
                          "reason": "Tear Down AWS VPN: remove the router's AWS block (lab-edge removal "
                                    + str(out.get("sha256", ""))[:12] + ")",
                          "checks": revert["teardown_checks"]}, revert_targets)
    if not pushed.get("ok"):
        return pushed
    pushed["card"]["after this"] = ("the router is read back to prove the block is gone, then the AWS destroy is "
                                    "planned for a second card" if plan["aws"] else
                                    "the router is read back to prove the block is gone; nothing of this target is "
                                    "deployed in AWS")
    return {**plan, **pushed}


def destroy_left(d: dict) -> dict:
    """After the destroy: what terraform-run outputs still reports (nothing, when the state is empty)."""
    result = (d.get("outputs") or {}).get("result") or {}
    outputs = (result.get("stdout_json") or {}).get("outputs")
    return {"read": result.get("return_code") == 0 and isinstance(outputs, dict),
            "empty": result.get("return_code") == 0 and outputs == {}}


TEARDOWN_PLAN_CODE = ("import json, sys\n\n\nTARGETS = " + repr(TEARDOWN_TARGETS) + "\nREVERT_TARGETS = "
                      + repr(REVERT_TARGETS) + "\n\n\n" + inspect.getsource(revert_plan) + "\n\n"
                      + inspect.getsource(teardown_plan)
                      + "\n\nprint(json.dumps(teardown_plan(json.loads(sys.stdin.read() or \"{}\"), TARGETS, REVERT_TARGETS, "
                      + repr(TEARDOWN_REVERT_MINUTES) + ", " + repr(LAB_EDGE_TIMEOUT) + ")))\n")
DESTROY_LEFT_CODE = ("import json, sys\n\n\n" + inspect.getsource(destroy_left)
                     + "\n\nprint(json.dumps(destroy_left(json.loads(sys.stdin.read() or \"{}\"))))\n")


# The cards and the rejection path: a timed teardown (R2b) has neither - its approval was the Deploy card's end time
TEARDOWN_CARDS = ("2a", "2b", "2c", *approval_ids("2c"), "5f", *approval_ids("5f"), "a0", "a1", "a2", "a3", "a4", "a5", "a6", "a7", "a8")
# where a teardown that failed ends: a timed one opens a Work Center task there first
TEARDOWN_FAILED = ("bf", "c1", "c2", "c3", "c4", "c5", "c6", "c7")


def teardown_section(timed: bool, failed_end: str = "workflow_end") -> tuple[dict, dict]:
    """Tear Down's tasks from "10" on, written identically into Tear Down AWS VPN and Tear Down Expired AWS VPN (no
    child jobs). `timed` leaves out the two cards and the rejection path (approved on the Deploy card, R2b) and sends
    every failure to `failed_end`; everything else is the same task. Reads the job variable `target`."""
    ok, fail, err = "success", "failure", "error"
    tasks = {
        "10": flag("router_changed = false", "false", "router_changed", x=40),
        "11": flag("aws_changed = false", "false", "aws_changed", x=60),
        # the plan: which services, with what, and whether AWS follows
        "1a": task("setObjectKey", "WorkFlowEngine", "the router (the target is gated: an open one)",
                   {"obj": {}, "path": ["target"], "value": "$var.job.target"}, {"object": "$var.job.teardown_in"},
                   display="Tools", x=100),
        "1b": run_code("the plan (Python on the runner)", TEARDOWN_PLAN_CODE, "$var.job.teardown_in", "teardown_plan",
                       x=150),
        "1c": evaluate("planned?", "job", "teardown_plan", "stdout_json.ok", "==", True, x=200),
        "1d": jq("lab-edge removal's params", "$var.job.teardown_plan", "stdout_json.removal", x=250),
        "1e": run_service("the removal lines (lab-edge removal: no device)", "lab-edge", "$var.1d.return_data",
                          "removal_result", x=300),
        "17": evaluate("lab-edge removal answered?", "1e", "result", "result.return_code", "==", 0, x=325),
        "1f": set_key("the plan's input, with the removal", "$var.job.teardown_in", "removal", "$var.job.removal_result",
                      x=350),
        "18": run_code("the push's params and the first card (Python on the runner)", TEARDOWN_PLAN_CODE,
                       "$var.1f.object", "push_plan", x=400),
        "19": evaluate("push planned?", "job", "push_plan", "stdout_json.ok", "==", True, x=450),
        # config-push-revert's own check, with no device
        "12": jq("config-push-revert check's params", "$var.job.push_plan", "stdout_json.check", x=500),
        "13": run_service("would config-push-revert take this? (check: no device)", "config-push-revert",
                          "$var.12.return_data", "check_result", x=550),
        "14": evaluate("config-push-revert takes it?", "13", "result", "result.return_code", "==", 0, x=600),
        # card 1: the router
        "2a": jq("the first card", "$var.job.push_plan", "stdout_json.card", x=650),
        "2b": replace("the first card's message", TEARDOWN_ROUTER_MESSAGE, "__D__", "$var.job.target", x=700),
        **approval("2c", "Approve removing the router's AWS block", "$var.2b.replacedString", "$var.2a.return_data",
                   "Approve", "Reject", x=750, var="approval"),
        "15": flag("rejected = false", "false", "rejected", x=775),
        # push, check, confirm and save - or roll back
        "3a": jq("config-push-revert's params", "$var.job.push_plan", "stdout_json.params", x=800),
        "16": jq("the timeout a push needs (check's answer)", "$var.13.result", "result.stdout_json.timeout", x=825),
        "34": set_key("push params: the Gateway timeout", "$var.3a.return_data", "timeout", "$var.16.return_data",
                      x=850),
        "3b": run_service("remove the block under the revert timer (config-push-revert)", "config-push-revert",
                          "$var.34.object", "push_result", x=900),
        "3c": evaluate("config-push-revert exited 0?", "3b", "result", "result.return_code", "==", 0, x=925),
        "3d": note("config-push-revert exited non-zero", "config-push-revert did not save: push_summary says what the "
                   "router went through", "push_note", x=925, y=-300),
        "3e": set_key("the push's answer", {}, "push", "$var.job.push_result", x=950),
        "3f": run_code("what the push did (saved, rolled back, nothing sent or unknown)", REVERT_SUMMARY_CODE,
                       "$var.3e.object", "push_summary", x=1000),
        "31": jq("what the router went through", "$var.3f.result", "stdout_json.router", x=1050, to_job="router_state"),
        "32": evaluate("the router may have changed?", "3f", "result", "stdout_json.changed", "==", True, x=1100),
        "35": flag("router_changed = true", "true", "router_changed", x=1125),
        "33": evaluate("removed and saved?", "3f", "result", "stdout_json.state", "==", "saved", x=1150),
        # prove it gone
        "4a": jq("lab-edge absent's params", "$var.job.teardown_plan", "stdout_json.absent", x=1200),
        "4b": run_service("prove the block is gone (lab-edge absent: show commands)", "lab-edge", "$var.4a.return_data",
                          "absent_result", x=1250),
        "4c": evaluate("the proof ran?", "4b", "result", "result.return_code", "==", 0, x=1300),
        "4d": evaluate("nothing of the block left?", "4b", "result", "result.stdout_json.absent", "==", True, x=1350),
        "4e": evaluate("deployed in AWS?", "job", "teardown_plan", "stdout_json.aws", "==", True, x=1400),
        "4f": note("the outcome: router only", "torn down: the router's AWS block was removed, saved and proved gone; "
                   "nothing of this target is deployed in AWS", "outcome", x=1450, y=600),
        # the AWS side: plan the destroy, card 2, apply exactly that plan
        "5a": parse("the destroy plan's params", DESTROY_PLAN_PARAMS, x=1500),
        "5b": run_service("terraform plan -destroy", "terraform-run", "$var.5a.textObject", "destroy_plan_result",
                          x=1550),
        "5c": evaluate("destroy planned?", "5b", "result", "result.return_code", "==", 0, x=1600),
        "5d": jq("the destroy plan", "$var.5b.result", "result.stdout_json", x=1650, to_job="destroy_plan"),
        "5e": jq("the plan ID", "$var.5b.result", "result.stdout_json.job", x=1700),
        **approval("5f", "Approve destroying the AWS side", TEARDOWN_AWS_MESSAGE, "$var.job.destroy_plan", "Approve",
                   "Reject", x=1750, var="approval_2"),
        "6a": jq("the approved plan's SHA-256", "$var.5b.result", "result.stdout_json.plan_sha256", x=1800),
        "6b": replace("apply params: plan ID", APPLY_TPL, "__ID__", "$var.5e.return_data", x=1850),
        "6c": replace("apply params: SHA-256", "$var.6b.replacedString", "__SHA__", "$var.6a.return_data", x=1900),
        "6d": parse("apply params", "$var.6c.replacedString", x=1950),
        "6e": run_service("terraform apply (the approved destroy)", "terraform-run", "$var.6d.textObject",
                          "apply_result", x=2000),
        "6f": evaluate("destroyed?", "6e", "result", "result.return_code", "==", 0, x=2050),
        "60": flag("aws_changed = true", "true", "aws_changed", x=2075),
        "61": parse("terraform-run outputs' params", OUTPUTS_PARAMS, x=2100),
        "62": run_service("what is left (terraform-run outputs, a state read)", "terraform-run", "$var.61.textObject",
                          "outputs_result", x=2150),
        "67": evaluate("the state read answered?", "62", "result", "result.return_code", "==", 0, x=2175),
        "63": set_key("the outputs' answer", {}, "outputs", "$var.job.outputs_result", x=2200),
        "64": run_code("anything left?", DESTROY_LEFT_CODE, "$var.63.object", "left", x=2250),
        "65": evaluate("nothing left in the state?", "64", "result", "stdout_json.empty", "==", True, x=2300),
        "66": note("the outcome: torn down", "torn down: the router's AWS block was removed, saved and proved gone, "
                   "and the AWS side was destroyed - nothing of the deployment is left", "outcome", x=2350),
        # rejections
        "a0": flag("rejected = true (router)", "true", "rejected", x=800, y=600),
        "a1": note("the router rejection", "rejected in Work Center: nothing was sent to the router and nothing "
                   "changed in AWS", "outcome", x=850, y=600),
        "a2": flag("rejected = true (AWS)", "true", "rejected", x=1800, y=600),
        "a3": replace("discard params", DISCARD_TPL, "__ID__", "$var.5e.return_data", x=1850, y=600),
        "a4": parse("discard params", "$var.a3.replacedString", x=1900, y=600),
        "a5": run_service("discard the rejected destroy plan", "terraform-run", "$var.a4.textObject",
                          "discard_result", x=1950, y=600),
        "a8": evaluate("discarded?", "a5", "result", "result.return_code", "==", 0, x=1975, y=600),
        "a6": note("the AWS rejection", "the router's AWS block was removed and proved gone; the AWS destroy was "
                   "rejected in Work Center, so the AWS side stays (about $1 a day) - Tear Down again to remove it",
                   "outcome", x=2000, y=600),
        "a7": note("the discard did not run", "the rejected destroy plan could not be discarded; it stays in the "
                   "state bucket until removed", "error", x=1950, y=900),
        # failures: before the router changes, nothing was sent
        "b0": note("the plan could not run", "the Gateway could not make Tear Down's plan (see teardown_plan / "
                   "push_plan): nothing was sent to the router, nothing changed in AWS", "error", x=200, y=-600),
        "b1": jq("why there is no plan", "$var.job.teardown_plan", "stdout_json.message", x=250, y=-1200,
                 to_job="error"),
        "b2": note("the removal lines could not be made", "lab-edge removal did not answer (see removal_result): "
                   "nothing was sent to the router, nothing changed in AWS", "error", x=350, y=-600),
        "b3": jq("why there is no push plan", "$var.job.push_plan", "stdout_json.message", x=500, y=-1200,
                 to_job="error"),
        "b4": note("config-push-revert refuses it", "config-push-revert's check refused the removal (see "
                   "check_result): nothing was sent to the router, nothing changed in AWS", "error", x=600, y=-600),
        "bf": flag("router_changed = false (nothing sent)", "false", "router_changed", x=650, y=-900),
        # failures after the push started
        "c0": note("the push could not finish", "the Gateway could not run config-push-revert or read its answer "
                   "(push_result): check the router - the removal may be live and unsaved, or pending until the "
                   "revert timer rolls it back. Nothing changed in AWS", "error", x=950, y=-1200),
        "c1": flag("router_changed = true (unknown)", "true", "router_changed", x=1000, y=-1200),
        "c2": jq("why the removal was not kept", "$var.3f.result", "stdout_json.message", x=1150, y=-600,
                 to_job="error"),
        "c3": note("the proof could not run", "the removal was saved, but lab-edge absent could not read the router "
                   "(absent_result): the AWS side was NOT touched - check the router, then Tear Down again",
                   "error", x=1300, y=-600),
        "c4": note("the block is not gone", "the removal was saved, but lab-edge absent still finds part of the "
                   "block (absent_result.left): the AWS side was NOT touched", "error", x=1350, y=-1200),
        "c5": note("the destroy could not be planned", "the router is clean; the Gateway could not plan the AWS "
                   "destroy (destroy_plan_result): nothing changed in AWS - Tear Down again to remove it", "error",
                   x=1600, y=-600),
        "c6": note("the destroy did not complete", "the router is clean; terraform apply of the destroy did not "
                   "finish (apply_result): part of the AWS side may be left - check it, then Tear Down again",
                   "error", x=2050, y=-600),
        "c7": note("something is left", "the destroy applied, but terraform-run outputs still reports outputs "
                   "(outputs_result): check AWS", "error", x=2300, y=-600),
    }
    tr = {
        "10": _edge(**{"11": ok}), "11": _edge(**{"1a": ok}), "1a": _edge(**{"1b": ok}),
        "1b": _edge(**{"1c": ok, "b0": err}),
        "1c": _edge(**{"1d": ok, "b1": fail}),
        "1d": _edge(**{"1e": ok, "b0": err}),
        "1e": _edge(**{"17": ok, "b2": err}),
        "17": _edge(**{"1f": ok, "b2": fail}),
        "1f": _edge(**{"18": ok}),
        "18": _edge(**{"19": ok, "b0": err}),
        "19": _edge(**{"12": ok, "b3": fail}),
        "12": _edge(**{"13": ok, "b0": err}),
        "13": _edge(**{"14": ok, "b4": err}),
        "14": _edge(**{"2a": ok, "b4": fail}),
        "2a": _edge(**{"2b": ok, "b0": err}),
        "2b": _edge(**{"2c1": ok}), **approval_edges("2c"),
        "2c": _edge(**{"15": ok, "a0": fail}),
        "15": _edge(**{"3a": ok}),
        "3a": _edge(**{"16": ok, "b0": err}),
        "16": _edge(**{"34": ok, "b0": err}),
        "34": _edge(**{"3b": ok}),
        "3b": _edge(**{"3c": ok, "c0": err}),
        "3c": _edge(**{"3e": ok, "3d": fail}),
        "3d": _edge(**{"3e": ok}),
        "3e": _edge(**{"3f": ok}),
        "3f": _edge(**{"31": ok, "c0": err}),
        "31": _edge(**{"32": ok, "c0": err}),
        "32": _edge(**{"35": ok, "33": fail}),
        "35": _edge(**{"33": ok}),
        "33": _edge(**{"4a": ok, "c2": fail}),
        "4a": _edge(**{"4b": ok, "c3": err}),
        "4b": _edge(**{"4c": ok, "c3": err}),
        "4c": _edge(**{"4d": ok, "c3": fail}),
        "4d": _edge(**{"4e": ok, "c4": fail}),
        "4e": _edge(**{"5a": ok, "4f": fail}),
        "4f": _edge(**{"workflow_end": ok}),
        "5a": _edge(**{"5b": ok}),
        "5b": _edge(**{"5c": ok, "c5": err}),
        "5c": _edge(**{"5d": ok, "c5": fail}),
        "5d": _edge(**{"5e": ok, "c5": err}),
        "5e": _edge(**{"5f1": ok, "c5": err}), **approval_edges("5f"),
        "5f": _edge(**{"6a": ok, "a2": fail}),
        "6a": _edge(**{"6b": ok, "c6": err}),
        "6b": _edge(**{"6c": ok}), "6c": _edge(**{"6d": ok}), "6d": _edge(**{"6e": ok}),
        "6e": _edge(**{"6f": ok, "c6": err}),
        "6f": _edge(**{"60": ok, "c6": fail}),
        "60": _edge(**{"61": ok}), "61": _edge(**{"62": ok}),
        "62": _edge(**{"67": ok, "c7": err}),
        "67": _edge(**{"63": ok, "c7": fail}),
        "63": _edge(**{"64": ok}),
        "64": _edge(**{"65": ok, "c7": err}),
        "65": _edge(**{"66": ok, "c7": fail}),
        "66": _edge(**{"workflow_end": ok}),
        "a0": _edge(**{"a1": ok}), "a1": _edge(**{"workflow_end": ok}),
        "a2": _edge(**{"a3": ok}), "a3": _edge(**{"a4": ok}), "a4": _edge(**{"a5": ok}),
        "a5": _edge(**{"a8": ok, "a7": err}),
        "a8": _edge(**{"a6": ok, "a7": fail}),
        "a7": _edge(**{"a6": ok}),
        "a6": _edge(**{"workflow_end": ok}),
        # every failure before the push ends through one task: nothing was sent (one arrow to the end, not five)
        "b0": _edge(**{"bf": ok}), "b1": _edge(**{"bf": ok}), "b2": _edge(**{"bf": ok}), "b3": _edge(**{"bf": ok}),
        "b4": _edge(**{"bf": ok}),
        "bf": _edge(**{"workflow_end": ok}),
        "c0": _edge(**{"c1": ok}), "c1": _edge(**{"workflow_end": ok}), "c2": _edge(**{"workflow_end": ok}),
        "c3": _edge(**{"workflow_end": ok}), "c4": _edge(**{"workflow_end": ok}), "c5": _edge(**{"workflow_end": ok}),
        "c6": _edge(**{"workflow_end": ok}), "c7": _edge(**{"workflow_end": ok}),
    }
    if not timed:
        return tasks, tr
    tasks = {k: v for k, v in tasks.items() if k not in TEARDOWN_CARDS}
    tr = {k: v for k, v in tr.items() if k not in TEARDOWN_CARDS}
    tr["14"] = _edge(**{"15": ok, "b4": fail})  # no router card
    tr["5e"] = _edge(**{"6a": ok, "c5": err})  # no AWS card
    for k in TEARDOWN_FAILED:
        tr[k] = _edge(**{failed_end: ok})
    return tasks, tr


def tear_down_aws_vpn() -> dict:
    tasks, tr = teardown_section(timed=False)
    return workflow(
        WF["tear_down_aws_vpn"],
        "Tears the AWS VPN down: removes the router's AWS block under a revert timer after a Work Center approval, "
        "proves it gone, then - for a target deployed in AWS - plans the destroy, shows it on a second card and "
        "applies exactly that plan (PID S13 criterion 5, R2, ADR 0068)",
        {"target": {"type": "string", "required": True, "enum": INPUT_GATES[WF["tear_down_aws_vpn"]]["target"]["enum"],
                    "description": "The lab edge router whose AWS VPN to tear down"}},
        tasks,
        {"workflow_start": _edge(**{"10": "success"}), **tr},
        {
            "outcome": {"type": "string"},
            "error": {"type": "string"},
            "router_changed": {"type": "boolean"},
            "aws_changed": {"type": "boolean"},
            "rejected": {"type": "boolean"},
            "router_state": {"type": "string"},
            "teardown_in": {"type": "object"},
            "teardown_plan": {"type": "object"},
            "push_plan": {"type": "object"},
            "removal_result": {"type": "object"},
            "check_result": {"type": "object"},
            "push_result": {"type": "object"},
            "push_summary": {"type": "object"},
            "absent_result": {"type": "object"},
            "destroy_plan_result": {"type": "object"},
            "destroy_plan": {"type": "object"}, "approval_card": {"type": "object"},
            "approval_decision": {"type": ["object", "null"]}, "approval_2_card": {"type": "object"},
            "approval_2_decision": {"type": ["object", "null"]},
            "apply_result": {"type": "object"},
            "outputs_result": {"type": "object"},
            "discard_result": {"type": "object"},
            "left": {"type": "object"},
        },
    )


# --- Get AWS VPN Status (A1, Cloud Status; owner decisions 2026-10-04) -------------------------------------------------
# The Cloud Status agent's first tool, read-only: terraform-run outputs (a state read) and nothing else. It reports what
# is deployed, until when, the NAT gateway, when AWS last changed (the state's own metadata, cloud-devops-pipeline #31)
# and an ESTIMATED cost from the unit prices pinned in versions.yaml aws_vpn.prices - said to be an estimate every time.
STATUS_PRICES = VERSIONS["aws_vpn"]["prices"]  # a month is 730 hours in AWS pricing (the function's own literal)


def aws_vpn_status(d: dict, now: datetime, prices: dict) -> dict:
    """The deployment, as the Cloud Status agent reports it. `d` holds terraform-run outputs' answer under `outputs`.
    Pure: runCode runs this source on the Gateway with the clock and the prices written in."""
    result = (d.get("outputs") or {}).get("result") or {}
    answer = result.get("stdout_json") or {}
    outputs = answer.get("outputs")
    if result.get("return_code") != 0 or not isinstance(outputs, dict):
        return {"ok": False, "summary": "The AWS side could not be read: terraform-run outputs did not answer "
                                        "(see outputs_result). Nothing was changed."}
    state = answer.get("state") or {}
    changed = state.get("last_modified") if state.get("read") else None
    hours = None
    if changed:
        then = datetime.strptime(changed, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        hours = round((now - then).total_seconds() / 3600, 1)
    # the state's own metadata: the last Terraform apply, which a no-change apply moves too - not "up since"
    when = (f"last Terraform apply {changed} ({hours} h ago)" if changed
            else "when Terraform last applied it is not known")
    if not outputs:
        return {"ok": True, "deployed": False, "last_changed": changed, "hours_since_change": hours,
                "estimate": {"per_day_usd": 0, "since_change_usd": 0, "basis": prices["basis"]},
                "summary": f"Nothing is deployed in AWS (the state is empty; {when}). Estimated cost: none."}
    end = outputs.get("expires_at")
    ends = ("stays up until torn down" if end in (None, "", "none")
            else f"ends at {end} (Tear Down Expired AWS VPN removes it then)")
    nat = outputs.get("nat_gateway_enabled")
    nat_text = "unknown (recorded by the next Deploy)" if nat is None else ("on" if nat else "off")
    itype = outputs.get("strongswan_instance_type")
    assumed = not itype
    itype = itype or prices["assumed_instance_type"]
    itype_text = f"{itype} (assumed: recorded by the next Deploy)" if assumed else itype
    rate = prices["instance_hour"].get(itype)
    if rate is None:
        per_day, since = None, None
        cost = f"no pinned price for {itype}: add it to versions.yaml aws_vpn.prices"
    else:
        hourly = (rate + prices["public_ipv4_hour"] + (prices["nat_gateway_hour"] if nat else 0)
                  + (prices["secret_month"] + prices["root_volume_month"]) / 730)
        per_day = round(hourly * 24, 2)
        since = round(hourly * hours, 2) if hours is not None else None
        cost = (f"about ${per_day:.2f} a day" + (f", about ${since:.2f} since the last change" if since is not None
                                                 else "") + f" ({prices['basis']})")
    summary = (f"Deployed: strongSwan at {outputs.get('strongswan_eip')} (instance {outputs.get('strongswan_instance_id')}, "
               f"{itype_text}); it {ends}; NAT gateway {nat_text}; {when}. Estimated cost: {cost}.")
    return {"ok": True, "deployed": True, "strongswan_eip": outputs.get("strongswan_eip"),
            "instance_id": outputs.get("strongswan_instance_id"), "instance_type": itype_text,
            "private_ip": outputs.get("strongswan_private_ip"), "vpc_private_prefixes": outputs.get("vpc_private_prefixes"),
            "ends": ends, "nat_gateway": nat_text, "last_changed": changed, "hours_since_change": hours,
            "estimate": {"per_day_usd": per_day, "since_change_usd": since, "basis": prices["basis"]},
            "summary": summary}


STATUS_CODE = ("import json, sys\nfrom datetime import datetime, timezone\n\n\nPRICES = " + repr(STATUS_PRICES)
               + "\n\n\n" + inspect.getsource(aws_vpn_status)
               + "\n\nprint(json.dumps(aws_vpn_status(json.loads(sys.stdin.read() or \"{}\"), datetime.now(timezone.utc), "
               + "PRICES)))\n")


def get_aws_vpn_status() -> dict:
    ok, fail, err = "success", "failure", "error"
    tasks = {
        "1a": parse("terraform-run outputs' params", OUTPUTS_PARAMS, x=100),
        "1b": run_service("the deployment (terraform-run outputs, a state read)", "terraform-run", "$var.1a.textObject",
                          "outputs_result", x=200),
        "1c": evaluate("the state read answered?", "1b", "result", "result.return_code", "==", 0, x=300),
        "1d": set_key("the outputs' answer", {}, "outputs", "$var.job.outputs_result", x=400),
        "1e": run_code("the status (Python on the runner)", STATUS_CODE, "$var.1d.object", "status", x=500),
        "1f": jq("the summary", "$var.job.status", "stdout_json.summary", x=600, to_job="summary"),
        "8a": note("the state could not be read", "the Gateway could not read the deployment (terraform-run outputs, "
                   "see outputs_result): nothing was changed", "error", x=250, y=-300),
        "8b": note("the status could not be made", "the Gateway could not make the status (see status): nothing was "
                   "changed", "error", x=450, y=300),
    }
    tr = {
        "workflow_start": _edge(**{"1a": ok}),
        "1a": _edge(**{"1b": ok}),
        "1b": _edge(**{"1c": ok, "8a": err}),
        "1c": _edge(**{"1d": ok, "8a": fail}),
        "1d": _edge(**{"1e": ok}),
        "1e": _edge(**{"1f": ok, "8b": err}),
        "1f": _edge(**{"workflow_end": ok, "8b": err}),
        "8a": _edge(**{"workflow_end": ok}),
        "8b": _edge(**{"workflow_end": ok}),
    }
    return workflow(
        WF["get_aws_vpn_status"],
        "Reports the AWS side of the lab's site-to-site VPN without changing anything: whether it is deployed, "
        "strongSwan's address, when it ends, the NAT gateway, when AWS last changed and an estimated cost from pinned "
        "list prices (A1, Cloud Status; ADR 0068)",
        {},
        tasks,
        tr,
        {"summary": {"type": "string"}, "status": {"type": "object"}, "error": {"type": "string"},
         "outputs_result": {"type": "object"}},
    )


# --- Check AWS Drift (R3, owner decisions 2026-10-04) -----------------------------------------------------------------
# Once a day (operations_manager.check_aws_drift): terraform-run drift, a refresh-only plan (cloud-devops-pipeline #32)
# that reads every resource from AWS and reports what changed outside Terraform by NAME only. No drift and nothing
# deployed end quietly; drift opens a Work Center task with the two ways back. The check never changes anything; a
# check that could not run ends with the reason (no task, as the hourly timed teardown).
DRIFT_PARAMS = '{"action": "drift", "timeout": "600"}'


def drift_summary(d: dict) -> dict:
    """What the drift check found, in words. `d` holds terraform-run drift's answer under `drift`. Pure: runCode runs
    this source on the Gateway."""
    result = (d.get("drift") or {}).get("result") or {}
    rep = (result.get("stdout_json") or {}).get("drift")
    if result.get("return_code") != 0 or not isinstance(rep, dict):
        return {"ok": False, "found": False, "summary": "The drift check could not run (see drift_result): nothing "
                                                        "was changed."}
    checked, resources = rep.get("resources_checked", 0), rep.get("resources") or []
    if not checked:
        return {"ok": True, "found": False, "drifted": 0, "summary": "nothing is deployed in AWS: nothing to check"}
    if not resources:
        return {"ok": True, "found": False, "drifted": 0,
                "summary": f"no drift: {checked} resources in AWS match the Terraform state"}
    named = [r.get("address", "?") + (" (deleted outside Terraform)" if "delete" in (r.get("actions") or [])
                                      else " (" + ", ".join(r.get("attributes") or ["changed"]) + ")")
             for r in resources]
    return {"ok": True, "found": True, "drifted": len(resources), "resources": resources,
            "summary": f"Drift in AWS: {len(resources)} of {checked} resources changed outside Terraform: "
                       + "; ".join(named) + ". Nothing was changed by this check. Two ways back: Deploy AWS VPN with "
                       "the same inputs puts AWS back to what Terraform wants, or Tear Down AWS VPN removes it all."}


DRIFT_CODE = ("import json, sys\n\n\n" + inspect.getsource(drift_summary)
              + "\n\nprint(json.dumps(drift_summary(json.loads(sys.stdin.read() or \"{}\"))))\n")


def check_aws_drift() -> dict:
    ok, fail, err = "success", "failure", "error"
    tasks = {
        "1a": parse("terraform-run drift's params", DRIFT_PARAMS, x=100),
        "1b": run_service("what changed in AWS outside Terraform (terraform-run drift, a refresh-only plan)",
                          "terraform-run", "$var.1a.textObject", "drift_result", x=200),
        "1c": evaluate("the drift check answered?", "1b", "result", "result.return_code", "==", 0, x=300),
        "1d": set_key("the drift check's answer", {}, "drift", "$var.job.drift_result", x=400),
        "1e": run_code("what was found (Python on the runner)", DRIFT_CODE, "$var.1d.object", "drift", x=500),
        "1f": evaluate("drift found?", "1e", "result", "stdout_json.found", "==", True, x=600),
        "10": jq("the outcome: no drift", "$var.job.drift", "stdout_json.summary", x=700, y=300, to_job="outcome"),
        "2a": jq("the drift, in words", "$var.job.drift", "stdout_json.summary", x=700, to_job="outcome"),
        "2b": view("Work Center task", "Drift in AWS: changed outside Terraform", "$var.job.outcome",
                   "$var.job.drift", "Seen", "Seen", x=800),
        "8a": note("the drift check could not run", "terraform-run drift did not answer (see drift_result): nothing "
                   "was changed", "error", x=250, y=-300),
        "8b": note("the drift could not be read", "the Gateway could not read the drift check's answer (see drift): "
                   "nothing was changed", "error", x=450, y=-300),
    }
    tr = {
        "workflow_start": _edge(**{"1a": ok}),
        "1a": _edge(**{"1b": ok}),
        "1b": _edge(**{"1c": ok, "8a": err}),
        "1c": _edge(**{"1d": ok, "8a": fail}),
        "1d": _edge(**{"1e": ok}),
        "1e": _edge(**{"1f": ok, "8b": err}),
        "1f": _edge(**{"2a": ok, "10": fail}),
        "10": _edge(**{"workflow_end": ok}),
        "2a": _edge(**{"2b": ok}),
        "2b": _edge(**{"workflow_end": ok}),
        "8a": _edge(**{"workflow_end": ok}),
        "8b": _edge(**{"workflow_end": ok}),
    }
    return workflow(
        WF["check_aws_drift"],
        "Reports what changed in AWS outside Terraform without changing anything: a refresh-only plan reads every "
        "resource and names the ones that drifted and their attributes; drift opens a Work Center task (R3, ADR 0068)",
        {},
        tasks,
        tr,
        {"outcome": {"type": "string"}, "drift": {"type": "object"}, "error": {"type": "string"},
         "drift_result": {"type": "object"}},
    )


# --- Tear Down Expired AWS VPN (R2b, owner decisions 2026-10-04) ------------------------------------------------------
# An hourly Operations Manager schedule (platform.yml, tasks/aws-vpn-schedule.yml) runs it. It reads the deployment's
# end time from the Terraform state (`expires_at`, set by Deploy AWS VPN and shown on its approval card - that card is
# the approval) and ends there unless the time is up. When it is, Tear Down's own tasks run without their two cards
# (teardown_section(timed=True)): router first, the AWS destroy only after the block is proved gone. A teardown that
# fails opens a Work Center task (f0); a run that cannot read the end time ends with the reason and opens none, so a
# lasting outage does not leave a task every hour.
EXPIRY_TARGETS = {name: {"monitor": entry["monitor"]} for name, entry in TEARDOWN_TARGETS.items()}


def expiry_due(d: dict, now: datetime, targets: dict) -> dict:
    """Is the deployment's time up? `d` holds terraform-run outputs' answer under `outputs`. Due only for a deployment
    whose `expires_at` is a time not after `now`; then `target` names the one Tear Down target deployed in AWS. `ok`
    is false when the answer cannot be read. Pure: runCode runs this source on the Gateway with the clock and the
    targets written in."""
    result = (d.get("outputs") or {}).get("result") or {}
    if result.get("return_code") != 0:
        return {"ok": False, "message": "terraform-run outputs did not answer (expiry_outputs): the end time could "
                                        "not be read; nothing was changed"}
    outputs = (result.get("stdout_json") or {}).get("outputs")
    if not isinstance(outputs, dict):
        return {"ok": False, "message": "terraform-run outputs gave no outputs (expiry_outputs); nothing was changed"}
    if not outputs:
        return {"ok": True, "due": False, "message": "nothing is deployed in AWS"}
    end = outputs.get("expires_at")
    if end in (None, "", "none"):
        return {"ok": True, "due": False, "expires_at": end,
                "message": "the deployment has no end time (lifetime none, or deployed before R2b): it stays up"}
    try:
        when = datetime.strptime(str(end), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return {"ok": False, "message": f"the deployment's expires_at {str(end)[:40]!r} is not a time; nothing was "
                                        "changed"}
    if now < when:
        return {"ok": True, "due": False, "expires_at": end, "message": f"the deployment ends at {end}"}
    aws = sorted(name for name, t in targets.items() if t.get("monitor") == "aws")
    if len(aws) != 1:
        return {"ok": False, "message": f"exactly one Tear Down target must be deployed in AWS, found {aws}; "
                                        "nothing was changed"}
    return {"ok": True, "due": True, "expires_at": end, "target": aws[0],
            "message": f"the deployment's time was up at {end}: tearing down {aws[0]} and the AWS side"}


EXPIRY_CODE = ("import json, sys\nfrom datetime import datetime, timezone\n\n\nTARGETS = " + repr(EXPIRY_TARGETS)
               + "\n\n\n" + inspect.getsource(expiry_due)
               + "\n\nprint(json.dumps(expiry_due(json.loads(sys.stdin.read() or \"{}\"), datetime.now(timezone.utc), "
               + "TARGETS)))\n")


def tear_down_expired_aws_vpn() -> dict:
    ok, fail, err = "success", "failure", "error"
    section, section_tr = teardown_section(timed=True, failed_end="f0")
    tasks = {
        # is the time up? (a state read; most runs end at t8)
        "d1": parse("terraform-run outputs' params", OUTPUTS_PARAMS, x=-300),
        "d2": run_service("the deployment and its end time (terraform-run outputs, a state read)", "terraform-run",
                          "$var.d1.textObject", "expiry_outputs", x=-250),
        "db": evaluate("the state read answered?", "d2", "result", "result.return_code", "==", 0, x=-225),
        "d3": set_key("the outputs' answer", {}, "outputs", "$var.job.expiry_outputs", x=-200),
        "d4": run_code("the time up? (Python on the runner)", EXPIRY_CODE, "$var.d3.object", "expiry", x=-150),
        "d5": evaluate("the end time read?", "d4", "result", "stdout_json.ok", "==", True, x=-100),
        "d6": evaluate("the time up?", "d4", "result", "stdout_json.due", "==", True, x=-50),
        "d7": jq("the router", "$var.d4.result", "stdout_json.target", x=-25, to_job="target"),
        "d8": jq("nothing to do", "$var.d4.result", "stdout_json.message", x=0, y=600, to_job="outcome"),
        "d9": jq("why the end time could not be read", "$var.d4.result", "stdout_json.message", x=-50, y=-600,
                 to_job="error"),
        "da": note("the check could not run", "the Gateway could not read or check the end time (expiry_outputs / "
                   "expiry): nothing was changed", "error", x=-150, y=-600),
        **section,
        # a teardown that failed: a Work Center task says so (the job's error says where)
        "f0": view("Work Center task", "The timed teardown of the AWS VPN stopped: check the job, then run Tear Down "
                   "AWS VPN by hand", "$var.job.error", "$var.job.expiry", "Seen", "Seen", x=2400, y=-600),
    }
    tr = {
        "workflow_start": _edge(d1=ok),
        "d1": _edge(d2=ok),
        "d2": _edge(db=ok, da=err),
        "db": _edge(d3=ok, da=fail),
        "d3": _edge(d4=ok),
        "d4": _edge(d5=ok, da=err),
        "d5": _edge(d6=ok, d9=fail),
        "d6": _edge(d7=ok, d8=fail),
        "d7": _edge(**{"10": ok, "da": err}),
        "d8": _edge(workflow_end=ok),
        "d9": _edge(workflow_end=ok),
        "da": _edge(workflow_end=ok),
        **section_tr,
        "f0": _edge(workflow_end=ok),
    }
    return workflow(
        WF["tear_down_expired_aws_vpn"],
        "Ends an AWS VPN deployment whose time is up (R2b): run every hour by a schedule, it reads the end time from the "
        "Terraform state and - only when it has passed - removes the router's AWS block under a revert timer, proves "
        "it gone and destroys the AWS side with exactly the planned destroy. The approval was the Deploy card's end "
        "time; a teardown that fails opens a Work Center task (ADR 0068, PID S13 R2b)",
        {},
        tasks,
        tr,
        {
            "outcome": {"type": "string"},
            "error": {"type": "string"},
            "target": {"type": "string"},
            "expiry_outputs": {"type": "object"},
            "expiry": {"type": "object"},
            "router_changed": {"type": "boolean"},
            "aws_changed": {"type": "boolean"},
            "rejected": {"type": "boolean"},
            "router_state": {"type": "string"},
            "teardown_in": {"type": "object"},
            "teardown_plan": {"type": "object"},
            "push_plan": {"type": "object"},
            "removal_result": {"type": "object"},
            "check_result": {"type": "object"},
            "push_result": {"type": "object"},
            "push_summary": {"type": "object"},
            "absent_result": {"type": "object"},
            "destroy_plan_result": {"type": "object"},
            "destroy_plan": {"type": "object"},
            "apply_result": {"type": "object"},
            "outputs_result": {"type": "object"},
            "left": {"type": "object"},
        },
    )


# --- Rotate AWS VPN Key (R4, owner decisions 2026-10-04) --------------------------------------------------------------
# The key between AWS and the one AWS-monitored router is changed in place, only with the tunnel up: aws-vpn-psk write
# (a new version in Vault, then Secrets Manager), aws-vpn-monitor reload (the monitor Lambda's fixed SSM document
# restarts vpn-bootstrap, so strongSwan re-reads Secrets Manager), lab-edge-push (the same push as Hand Off: the new key
# from Vault under a revert timer, a fresh SA, saved), prune (Vault keeps the current and one previous version), then
# Verify's own checks (verify_section). The tunnel is down from the reload until the router takes the key. When the
# router does not take it, or the AWS side fails part-way, the AWS side goes back to the previous key by itself
# (restore-previous, reload) and a Work Center task says what happened. Operations Manager has no monthly repeat
# (minute/hour/day/week), so Rotate AWS VPN Key Monthly runs every day and rotates only on the 1st (UTC); Rotate AWS
# VPN Key is the same section, run on demand (no child jobs: the section is written into both).
PSK_TIMEOUT = "120"
RELOAD_TIMEOUT = "300"  # the Lambda waits on the SSM command: vpn-bootstrap's restart and swanctl --load-creds


def rotation_plan(d: dict, targets: dict) -> dict:
    """What a rotation changes, from terraform-run outputs' answer under `outputs`: the one target the AWS monitor
    watches, each service's params, and `edge_in` for LAB_EDGE_PLAN_CODE (Hand Off's plan: precheck, render, push).
    Pure: runCode runs this source on the Gateway with the targets written in."""
    result = (d.get("outputs") or {}).get("result") or {}
    outputs = (result.get("stdout_json") or {}).get("outputs")
    if result.get("return_code") != 0 or not isinstance(outputs, dict):
        return {"ok": False, "message": "terraform-run outputs did not answer (rotate_outputs): nothing was changed"}
    if not outputs:
        return {"ok": True, "deployed": False, "message": "nothing is deployed in AWS: no key to rotate"}
    aws = sorted(name for name, t in targets.items() if t.get("monitor") == "aws")
    if len(aws) != 1:
        return {"ok": False, "message": f"exactly one open target must be monitored by AWS, found {aws}: nothing was "
                                        "changed"}
    arn, instance = outputs.get("psk_secret_arn"), outputs.get("strongswan_instance_id")
    if not arn or not instance:
        return {"ok": False, "message": "the outputs name no key secret or no strongSwan instance (rotate_outputs): "
                                        "nothing was changed"}
    return {"ok": True, "deployed": True, "target": aws[0],
            "message": f"rotating the pre-shared key between AWS and {aws[0]}",
            "edge_in": {"target": aws[0], "targets": targets, "deployed": outputs},
            "write": {"action": "write", "secret_arn": arn, "timeout": PSK_TIMEOUT},
            "restore": {"action": "restore-previous", "secret_arn": arn, "timeout": PSK_TIMEOUT},
            "prune": {"action": "prune", "timeout": PSK_TIMEOUT},
            "reload": {"action": "reload", "instance_id": instance, "timeout": RELOAD_TIMEOUT}}


def rotation_step(d: dict) -> dict:
    """One step's verdict from its service's answer (`stage`, `answer`): go on (`ok`), roll the AWS side back
    (`rollback`), or stop where it is. The push is read through push_summary, as Hand Off reads it. A push that never
    reached `configure confirm` leaves the router on the old key (its revert timer), so AWS goes back too; a confirmed
    but unsaved one runs the new key, so AWS keeps it. Pure: runCode runs this source (and push_summary's) on the
    Gateway."""
    stage = d.get("stage")
    result = (d.get("answer") or {}).get("result") or {}
    rc, out = result.get("return_code"), result.get("stdout_json")
    out = out if isinstance(out, dict) else {}
    error = out.get("error") or ("no answer" if not out else f"exit code {rc}")

    def verdict(ok: bool, rollback: bool, message: str) -> dict:
        return {"stage": stage, "ok": ok, "rollback": rollback, "message": message}

    if stage == "write":
        if rc == 0 and out.get("written") is True:
            return verdict(True, False, f"a new key (Vault version {out.get('vault_version')}) is in Vault and "
                                        "Secrets Manager")
        if out.get("vault_version"):
            return verdict(False, True, f"the new key reached Vault but not Secrets Manager ({error})")
        return verdict(False, False, f"the new key could not be written ({error}): nothing was changed")
    if stage in ("reload", "reload_back"):
        done = rc == 0 and out.get("reloaded") is True and out.get("instance_matches") is not False
        if stage == "reload":
            return verdict(done, not done, "strongSwan reloaded the new key" if done
                           else f"strongSwan did not reload the new key ({error})")
        return verdict(done, False, "strongSwan reloaded the previous key: the tunnel runs on the previous key again "
                                    "(run Verify AWS VPN)" if done
                       else f"strongSwan did not reload the previous key ({error}): check the strongSwan box "
                            "(vpn-bootstrap.service) by hand")
    if stage == "push":
        s = push_summary({"push": d.get("answer")})
        router = out.get("target") or "the router"
        if s["state"] == "saved" and out.get("key_sent") is True:
            return verdict(True, False, f"{router} holds the new key, proved on a fresh SA and saved")
        if s["state"] == "saved":
            # its recorded version already matched: it was handed the old key, so it kept it
            return verdict(False, True, f"{router} was sent no key (it already records the version it was handed): "
                                        "the router keeps the previous key")
        if s["state"] == "failed" and out.get("confirmed") is True:
            return verdict(False, False, f"{router} runs the new key but did not save it ({error}): AWS keeps the new "
                                         "key; run `write memory` on the router by hand")
        return verdict(False, True, f"{router} did not take the new key: {s['message'] or s['router']}")
    if stage == "restore":
        if rc == 0 and out.get("restored") is True:
            return verdict(True, False, "the previous key is current again in Vault and Secrets Manager")
        return verdict(False, False, f"the previous key could not be restored ({error}): restore it by hand")
    return verdict(False, False, f"unknown step {stage!r}: nothing more was done")


def rotation_due(now: datetime) -> dict:
    """The monthly run rotates on the 1st of the month (UTC) and on no other day."""
    if now.day == 1:
        return {"due": True, "message": "the 1st of the month: rotating the key"}
    return {"due": False, "message": f"the key is rotated on the 1st of each month (UTC); today is day {now.day}: "
                                      "nothing to do"}


ROTATE_PLAN_CODE = ("import json, sys\n\n\nTARGETS = " + repr(VERIFY_TARGETS) + "\nPSK_TIMEOUT = " + repr(PSK_TIMEOUT)
                    + "\nRELOAD_TIMEOUT = " + repr(RELOAD_TIMEOUT) + "\n\n\n" + inspect.getsource(rotation_plan)
                    + "\n\nprint(json.dumps(rotation_plan(json.loads(sys.stdin.read() or \"{}\"), TARGETS)))\n")
ROTATE_STEP_CODE = ("import json, sys\n\n\n" + inspect.getsource(push_summary) + "\n\n"
                    + inspect.getsource(rotation_step)
                    + "\n\nprint(json.dumps(rotation_step(json.loads(sys.stdin.read() or \"{}\"))))\n")
ROTATE_DUE_CODE = ("import json\nfrom datetime import datetime, timezone\n\n\n" + inspect.getsource(rotation_due)
                   + "\n\nprint(json.dumps(rotation_due(datetime.now(timezone.utc))))\n")
ROTATED_TPL = "Rotated: __S__. Verify: __V__"
UNVERIFIED_TPL = ("Rotated (AWS and the router hold the new key, saved), but Verify did not pass: __V__. Run Verify AWS "
                  "VPN")
ROLLBACK_TPL = "__E__. Rolled back: __R__"


def rotation_section() -> tuple[dict, dict, str]:
    """The rotation, written identically into Rotate AWS VPN Key and Rotate AWS VPN Key Monthly. Every failure ends at
    the Work Center task f0, with the reason in `error`. Returns (tasks, transitions, the first task id)."""
    ok, fail, err = "success", "failure", "error"
    nothing = "nothing was changed"
    tasks = {
        # a service that does not run leaves {} behind: its step reads it as no answer
        "01": empty("no write answer yet", "write_result", x=0),
        "02": empty("no reload answer yet", "reload_result", x=20),
        "03": empty("no push answer yet", "push_result", x=40),
        "04": empty("no restore answer yet", "restore_result", x=60),
        "05": empty("no reload-back answer yet", "reload_back_result", x=80),
        "06": empty("no step yet", "step", x=90),
        # the plan: the deployment, its target and every service's params
        "1a": parse("terraform-run outputs' params", OUTPUTS_PARAMS, x=100),
        "1b": run_service("the deployment (terraform-run outputs, a state read)", "terraform-run", "$var.1a.textObject",
                          "rotate_outputs", x=150),
        "1c": evaluate("the state read answered?", "1b", "result", "result.return_code", "==", 0, x=200),
        "1d": set_key("the outputs' answer", {}, "outputs", "$var.job.rotate_outputs", x=250),
        "1e": run_code("what the rotation changes (Python on the runner)", ROTATE_PLAN_CODE, "$var.1d.object",
                       "rotate_plan", x=300),
        "1f": evaluate("the plan made?", "1e", "result", "stdout_json.ok", "==", True, x=350),
        "19": jq("why there is no plan", "$var.1e.result", "stdout_json.message", x=400, y=-600, to_job="error"),
        "10": evaluate("anything deployed?", "1e", "result", "stdout_json.deployed", "==", True, x=400),
        "11": jq("nothing to rotate", "$var.1e.result", "stdout_json.message", x=450, y=600, to_job="outcome"),
        "12": jq("the router's plan input", "$var.1e.result", "stdout_json.edge_in", x=450),
        "13": run_code("what to read and push on the router (Hand Off's plan)", LAB_EDGE_PLAN_CODE,
                       "$var.12.return_data", "rotate_edge", x=500),
        "14": evaluate("the router's plan ready?", "job", "rotate_edge", "stdout_json.ready", "==", True, x=550),
        # the tunnel must be up before anything changes
        "2a": jq("lab-edge verify's params", "$var.job.rotate_edge", "stdout_json.lab_edge", x=600),
        "2b": run_service("the tunnel up now? (lab-edge verify: show commands, one ping)", "lab-edge",
                          "$var.2a.return_data", "precheck_result", x=650),
        "2c": evaluate("lab-edge read the router?", "2b", "result", "result.return_code", "==", 0, x=700),
        "2d": evaluate("the router says up?", "2b", "result", "result.stdout_json.router", "==", "up", x=750),
        "2e": evaluate("the data plane says up?", "2b", "result", "result.stdout_json.data_plane", "==", "up", x=800),
        # the block the push sends (the key is a marker in it, so its SHA-256 does not change with the key)
        "3a": jq("lab-edge render's params", "$var.job.rotate_edge", "stdout_json.render", x=850),
        "3b": run_service("render the block (lab-edge render: no device)", "lab-edge", "$var.3a.return_data",
                          "render_result", x=900),
        "3c": evaluate("rendered?", "3b", "result", "result.return_code", "==", 0, x=950),
        "3d": jq("the block's SHA-256", "$var.3b.result", "result.stdout_json.sha256", x=1000, to_job="sha256"),
        # 1. a new key in Vault, then Secrets Manager
        "4a": jq("aws-vpn-psk write's params", "$var.job.rotate_plan", "stdout_json.write", x=1050),
        "4b": run_service("a new key: Vault, then Secrets Manager (aws-vpn-psk write)", "aws-vpn-psk",
                          "$var.4a.return_data", "write_result", x=1100),
        "47": evaluate("aws-vpn-psk write exited 0?", "4b", "result", "result.return_code", "==", 0, x=1120, y=0),
        "48": note("aws-vpn-psk write exited non-zero", "aws-vpn-psk write exited non-zero: its step says what that means", "write_note",
                   x=1130, y=-300),
        "4c": set_key("the write's answer", {"stage": "write"}, "answer", "$var.job.write_result", x=1150),
        "4d": run_code("go on, roll back or stop?", ROTATE_STEP_CODE, "$var.4c.object", "step", x=1200),
        "4e": evaluate("written to both stores?", "4d", "result", "stdout_json.ok", "==", True, x=1250),
        "4f": evaluate("roll back?", "4d", "result", "stdout_json.rollback", "==", True, x=1300, y=-300),
        "40": jq("why the write stopped", "$var.4d.result", "stdout_json.message", x=1350, y=-600, to_job="error"),
        # 2. strongSwan re-reads Secrets Manager (the tunnel is down from here until the router takes the key)
        "5a": jq("aws-vpn-monitor reload's params", "$var.job.rotate_plan", "stdout_json.reload", x=1350),
        "5b": run_service("strongSwan reloads the key (aws-vpn-monitor reload: the Lambda's SSM document)",
                          "aws-vpn-monitor", "$var.5a.return_data", "reload_result", x=1400),
        "57": evaluate("aws-vpn-monitor reload exited 0?", "5b", "result", "result.return_code", "==", 0, x=1420, y=0),
        "58": note("aws-vpn-monitor reload exited non-zero", "aws-vpn-monitor reload exited non-zero: its step says what that means", "reload_note",
                   x=1430, y=-300),
        "5c": set_key("the reload's answer", {"stage": "reload"}, "answer", "$var.job.reload_result", x=1450),
        "5d": run_code("go on or roll back?", ROTATE_STEP_CODE, "$var.5c.object", "step", x=1500),
        "5e": evaluate("reloaded?", "5d", "result", "stdout_json.ok", "==", True, x=1550),
        "5f": evaluate("roll back?", "5d", "result", "stdout_json.rollback", "==", True, x=1600, y=-300),
        "50": jq("why the reload stopped", "$var.5d.result", "stdout_json.message", x=1650, y=-600, to_job="error"),
        # 3. the router takes the key: Hand Off's push, the same block, the new key from Vault
        "6a": jq("lab-edge-push's params", "$var.job.rotate_edge", "stdout_json.push", x=1650),
        "6b": set_key("push params: the block's SHA-256", "$var.6a.return_data", "sha256", "$var.job.sha256", x=1700),
        "6c": run_service("the router takes the new key (lab-edge-push: revert timer, a fresh SA, save)",
                          "lab-edge-push", "$var.6b.object", "push_result", x=1750),
        "67": evaluate("lab-edge-push exited 0?", "6c", "result", "result.return_code", "==", 0, x=1770, y=0),
        "68": note("lab-edge-push exited non-zero", "lab-edge-push exited non-zero: its step says what that means", "push_note",
                   x=1780, y=-300),
        "6d": set_key("the push's answer", {"stage": "push"}, "answer", "$var.job.push_result", x=1800),
        "6e": run_code("go on, roll back or stop?", ROTATE_STEP_CODE, "$var.6d.object", "step", x=1850),
        "6f": evaluate("saved on the router?", "6e", "result", "stdout_json.ok", "==", True, x=1900),
        "60": evaluate("roll back?", "6e", "result", "stdout_json.rollback", "==", True, x=1950, y=-300),
        "61": jq("why the push stopped", "$var.6e.result", "stdout_json.message", x=2000, y=-600, to_job="error"),
        # 4. Vault keeps the current and the one previous version; a failed prune is caught up by the next rotation
        "7a": jq("aws-vpn-psk prune's params", "$var.job.rotate_plan", "stdout_json.prune", x=2000),
        "7b": run_service("keep two key versions in Vault (aws-vpn-psk prune)", "aws-vpn-psk", "$var.7a.return_data",
                          "prune_result", x=2050),
        "7c": evaluate("pruned?", "7b", "result", "result.return_code", "==", 0, x=2100),
        "70": note("the prune did not run", "older key versions were not pruned (prune_result): the next rotation "
                   "prunes them", "prune_note", x=2150, y=300),
        # the outcome: the rotation and Verify's verdict
        "90": jq("the verdict", "$var.ee.result", "stdout_json.verdict", x=2950),
        "91": jq("what the push said", "$var.job.step", "stdout_json.message", x=3000),
        "92": replace("the outcome: the rotation", ROTATED_TPL, "__S__", "$var.91.return_data", x=3050),
        "93": replace("the outcome", "$var.92.replacedString", "__V__", "$var.90.return_data", x=3100),
        "94": jq("the verdict (not passed)", "$var.ee.result", "stdout_json.verdict", x=2950, y=-300),
        "95": replace("why the rotation needs a look", UNVERIFIED_TPL, "__V__", "$var.94.return_data", x=3000, y=-300),
        # the rollback: the previous key current again, then strongSwan reloads it
        "b0": jq("why it is rolled back", "$var.job.step", "stdout_json.message", x=2000, y=-900, to_job="error"),
        "b1": jq("aws-vpn-psk restore-previous's params", "$var.job.rotate_plan", "stdout_json.restore", x=2050,
                 y=-900),
        "b2": run_service("the previous key current again (aws-vpn-psk restore-previous)", "aws-vpn-psk",
                          "$var.b1.return_data", "restore_result", x=2100, y=-900),
        "bd": evaluate("aws-vpn-psk restore-previous exited 0?", "b2", "result", "result.return_code", "==", 0, x=2120, y=-900),
        "be": note("aws-vpn-psk restore-previous exited non-zero", "aws-vpn-psk restore-previous exited non-zero: its step says what that means", "restore_note",
                   x=2130, y=-1200),
        "b3": set_key("the restore's answer", {"stage": "restore"}, "answer", "$var.job.restore_result", x=2150,
                      y=-900),
        "b4": run_code("restored?", ROTATE_STEP_CODE, "$var.b3.object", "rollback_step", x=2200, y=-900),
        "b5": evaluate("the previous key current again?", "b4", "result", "stdout_json.ok", "==", True, x=2250, y=-900),
        "b6": jq("aws-vpn-monitor reload's params", "$var.job.rotate_plan", "stdout_json.reload", x=2300, y=-900),
        "b7": run_service("strongSwan reloads the previous key (aws-vpn-monitor reload)", "aws-vpn-monitor",
                          "$var.b6.return_data", "reload_back_result", x=2350, y=-900),
        "c0": evaluate("aws-vpn-monitor reload (back) exited 0?", "b7", "result", "result.return_code", "==", 0, x=2370, y=-900),
        "c1": note("aws-vpn-monitor reload (back) exited non-zero", "aws-vpn-monitor reload (back) exited non-zero: its step says what that means", "reload_back_note",
                   x=2380, y=-1200),
        "b8": set_key("the reload's answer", {"stage": "reload_back"}, "answer", "$var.job.reload_back_result",
                      x=2400, y=-900),
        "b9": run_code("reloaded?", ROTATE_STEP_CODE, "$var.b8.object", "rollback_step", x=2450, y=-900),
        "ba": jq("what the rollback did", "$var.job.rollback_step", "stdout_json.message", x=2500, y=-900),
        "bb": replace("the reason and the rollback", ROLLBACK_TPL, "__E__", "$var.job.error", x=2550, y=-900),
        "bc": replace("the reason and the rollback, in full", "$var.bb.replacedString", "__R__", "$var.ba.return_data",
                      x=2600, y=-900),
        # failures before the write change nothing; after it, the job says where it stopped
        "8a": note("the deployment could not be read", f"terraform-run outputs could not be read (rotate_outputs): "
                   f"{nothing}", "error", x=200, y=-600),
        "8b": note("a step could not run", f"the Gateway could not run a step before the new key (see rotate_plan, "
                   f"rotate_edge): {nothing}", "error", x=500, y=-600),
        "8c": note("the tunnel could not be read", f"lab-edge could not read the router before the rotation "
                   f"(precheck_result): {nothing}", "error", x=700, y=-600),
        "8d": note("the tunnel is not up", f"the tunnel was not up before the rotation (precheck_result: router, "
                   f"data_plane): {nothing}; run Verify AWS VPN", "error", x=800, y=-600),
        "8e": note("the render failed", f"lab-edge could not render the block (render_result): {nothing}", "error",
                   x=950, y=-600),
        "8f": note("a step after the write could not run", "the Gateway could not run a step after the new key was "
                   "written (see step, rollback_step and each *_result): Vault, Secrets Manager, strongSwan and the "
                   "router may hold different keys - run Verify AWS VPN and check by hand", "error", x=1800, y=-1200),
        "c3": note("the verify steps could not run", "the key was rotated (see step), but the verify steps could not "
                   "run or gave no verdict: run Verify AWS VPN", "error", x=3000, y=-600),
        "f0": view("Work Center task", "Rotate AWS VPN Key did not finish: check the job", "$var.job.error",
                   "$var.job.step", "Seen", "Seen", x=3200, y=0),
    }
    tasks["93"]["variables"]["outgoing"]["replacedString"] = "$var.job.outcome"
    tasks["95"]["variables"]["outgoing"]["replacedString"] = "$var.job.error"
    tasks["bc"]["variables"]["outgoing"]["replacedString"] = "$var.job.error"
    vtasks, vtr, first = verify_section("rotate_edge", passed="90", failed="94", broken="c3", judge_broken="c3",
                                        x=2200)
    tasks.update(vtasks)
    tr = {
        "01": _edge(**{"02": ok}),
        "02": _edge(**{"03": ok}),
        "03": _edge(**{"04": ok}),
        "04": _edge(**{"05": ok}),
        "05": _edge(**{"06": ok}),
        "06": _edge(**{"1a": ok}),
        "1a": _edge(**{"1b": ok}),
        "1b": _edge(**{"1c": ok, "8a": err}),
        "1c": _edge(**{"1d": ok, "8a": fail}),
        "1d": _edge(**{"1e": ok}),
        "1e": _edge(**{"1f": ok, "8b": err}),
        "1f": _edge(**{"10": ok, "19": fail}),
        "19": _edge(**{"f0": ok, "8b": err}),
        "10": _edge(**{"12": ok, "11": fail}),
        "11": _edge(**{"workflow_end": ok, "8b": err}),
        "12": _edge(**{"13": ok, "8b": err}),
        "13": _edge(**{"14": ok, "8b": err}),
        "14": _edge(**{"2a": ok, "8b": fail}),
        "2a": _edge(**{"2b": ok, "8b": err}),
        "2b": _edge(**{"2c": ok, "8c": err}),
        "2c": _edge(**{"2d": ok, "8c": fail}),
        "2d": _edge(**{"2e": ok, "8d": fail}),
        "2e": _edge(**{"3a": ok, "8d": fail}),
        "3a": _edge(**{"3b": ok, "8b": err}),
        "3b": _edge(**{"3c": ok, "8e": err}),
        "3c": _edge(**{"3d": ok, "8e": fail}),
        "3d": _edge(**{"4a": ok, "8e": err}),
        "4a": _edge(**{"4b": ok, "8b": err}),
        # a service that exits non-zero, or that the Gateway could not run, is an answer too: its step judges it
        "4b": _edge(**{"47": ok, "4c": err}),
        "47": _edge(**{"4c": ok, "48": fail}),
        "48": _edge(**{"4c": ok}),
        "4c": _edge(**{"4d": ok}),
        "4d": _edge(**{"4e": ok, "8f": err}),
        "4e": _edge(**{"5a": ok, "4f": fail}),
        "4f": _edge(**{"b0": ok, "40": fail}),
        "40": _edge(**{"f0": ok, "8f": err}),
        "5a": _edge(**{"5b": ok, "8f": err}),
        "5b": _edge(**{"57": ok, "5c": err}),
        "57": _edge(**{"5c": ok, "58": fail}),
        "58": _edge(**{"5c": ok}),
        "5c": _edge(**{"5d": ok}),
        "5d": _edge(**{"5e": ok, "8f": err}),
        "5e": _edge(**{"6a": ok, "5f": fail}),
        "5f": _edge(**{"b0": ok, "50": fail}),
        "50": _edge(**{"f0": ok, "8f": err}),
        "6a": _edge(**{"6b": ok, "8f": err}),
        "6b": _edge(**{"6c": ok}),
        "6c": _edge(**{"67": ok, "6d": err}),
        "67": _edge(**{"6d": ok, "68": fail}),
        "68": _edge(**{"6d": ok}),
        "6d": _edge(**{"6e": ok}),
        "6e": _edge(**{"6f": ok, "8f": err}),
        "6f": _edge(**{"7a": ok, "60": fail}),
        "60": _edge(**{"b0": ok, "61": fail}),
        "61": _edge(**{"f0": ok, "8f": err}),
        "7a": _edge(**{"7b": ok, "70": err}),
        "7b": _edge(**{"7c": ok, "70": err}),
        "7c": _edge(**{first: ok, "70": fail}),
        "70": _edge(**{first: ok}),
        **vtr,
        "90": _edge(**{"91": ok, "c3": err}),
        "91": _edge(**{"92": ok, "c3": err}),
        "92": _edge(**{"93": ok}),
        "93": _edge(**{"workflow_end": ok}),
        "94": _edge(**{"95": ok, "c3": err}),
        "95": _edge(**{"f0": ok}),
        "b0": _edge(**{"b1": ok, "8f": err}),
        "b1": _edge(**{"b2": ok, "8f": err}),
        "b2": _edge(**{"bd": ok, "b3": err}),
        "bd": _edge(**{"b3": ok, "be": fail}),
        "be": _edge(**{"b3": ok}),
        "b3": _edge(**{"b4": ok}),
        "b4": _edge(**{"b5": ok, "8f": err}),
        "b5": _edge(**{"b6": ok, "ba": fail}),
        "b6": _edge(**{"b7": ok, "8f": err}),
        "b7": _edge(**{"c0": ok, "b8": err}),
        "c0": _edge(**{"b8": ok, "c1": fail}),
        "c1": _edge(**{"b8": ok}),
        "b8": _edge(**{"b9": ok}),
        "b9": _edge(**{"ba": ok, "8f": err}),
        "ba": _edge(**{"bb": ok, "8f": err}),
        "bb": _edge(**{"bc": ok}),
        "bc": _edge(**{"f0": ok}),
        **{n: _edge(**{"f0": ok}) for n in ("8a", "8b", "8c", "8d", "8e", "8f", "c3")},
        "f0": _edge(**{"workflow_end": ok}),
    }
    return tasks, tr, "01"


ROTATE_OUTPUTS = {
    "outcome": {"type": "string"},
    "error": {"type": "string"},
    "sha256": {"type": "string"},
    "prune_note": {"type": "string"},
    "rotate_outputs": {"type": "object"},
    "rotate_plan": {"type": "object"},
    "rotate_edge": {"type": "object"},
    "precheck_result": {"type": "object"},
    "render_result": {"type": "object"},
    "write_result": {"type": "object"},
    "reload_result": {"type": "object"},
    "push_result": {"type": "object"},
    "prune_result": {"type": "object"},
    "restore_result": {"type": "object"},
    "reload_back_result": {"type": "object"},
    "write_note": {"type": "string"},
    "reload_note": {"type": "string"},
    "push_note": {"type": "string"},
    "restore_note": {"type": "string"},
    "reload_back_note": {"type": "string"},
    "step": {"type": "object"},
    "rollback_step": {"type": "object"},
    "judgement": {"type": "object"},
    "lab_edge_result": {"type": "object"},
    "monitor_result": {"type": "object"},
    "router_note": {"type": "string"},
    "monitor_note": {"type": "string"},
}
ROTATE_DESCRIPTION = (
    "Changes the pre-shared key between AWS and the lab edge router in place, only with the tunnel up: a new key in "
    "Vault and Secrets Manager, strongSwan reloads it, the router takes it under a revert timer and saves it, Vault "
    "keeps two versions, then Verify's checks. When the router does not take it the AWS side goes back to the previous "
    "key by itself; a failure opens a Work Center task (R4, ADR 0068)"
)


def rotate_aws_vpn_key() -> dict:
    tasks, tr, first = rotation_section()
    return workflow(WF["rotate_aws_vpn_key"], ROTATE_DESCRIPTION + ". Run on demand", {}, tasks,
                    {"workflow_start": _edge(**{first: "success"}), **tr}, ROTATE_OUTPUTS)


def rotate_aws_vpn_key_monthly() -> dict:
    ok, fail, err = "success", "failure", "error"
    section, section_tr, first = rotation_section()
    tasks = {
        "d1": run_code("the 1st of the month? (the runner's UTC clock)", ROTATE_DUE_CODE, "{}", "due", x=-150),
        "d2": evaluate("due?", "d1", "result", "stdout_json.due", "==", True, x=-100),
        "d3": jq("nothing to do", "$var.d1.result", "stdout_json.message", x=-50, y=600, to_job="outcome"),
        # a clock that cannot be read opens no task: the next day's run tries again
        "d4": note("the due check could not run", "the Gateway could not run the due check (due): nothing was changed",
                   "error", x=-100, y=-600),
        **section,
    }
    tr = {
        "workflow_start": _edge(d1=ok),
        "d1": _edge(d2=ok, d4=err),
        "d2": _edge(**{first: ok, "d3": fail}),
        "d3": _edge(workflow_end=ok),
        "d4": _edge(workflow_end=ok),
        **section_tr,
    }
    return workflow(WF["rotate_aws_vpn_key_monthly"], ROTATE_DESCRIPTION + ". Run every day by a schedule; it rotates "
                    "on the 1st of the month (UTC) only", {}, tasks, tr, {**ROTATE_OUTPUTS, "due": {"type": "object"}})


# --- Diagnose AWS VPN Outage (R6 + A3, owner decisions 2026-10-05; ADR 0072) ---------------------------------------
# Prometheus sees dc1-wan01's Tunnel10 down (LabAwsTunnelDown); Alertmanager posts to the in-cluster alert relay, which
# starts this workflow through its endpoint trigger. It reads the deployment, finds an open incident for the same
# outage (correlation_id) and only notes it, or gathers the evidence (Verify's own checks and the AWS tunnel-down
# alarm), opens ONE ServiceNow incident, runs the tunnel-diagnostics agent (runAgent), and shows the one fix the agent
# picked from the fixed menu on a Work Center card. Only after approval does it run that fix - reset the IKE SA
# (lab-edge-push reset-sa), restart strongSwan (aws-vpn-monitor restart) or re-push the router block (lab-edge render
# + lab-edge-push push) - then reads the tunnel again and resolves the incident, or notes it and opens a task.
OUTAGE_FIXES = ("repush-router-block", "restart-strongswan", "reset-ike", "escalate")
AGENT_MARKER = "__AGENT_ID:tunnel-diagnostics__"  # the agent's UUID, filled in at import (tasks/workflow-agent-ids.yml)
ALARM_PARAMS = '{"action": "alarm", "timeout": "120"}'
OUTAGE_RECHECKS = (("a0", "a1", "a2", 30), ("a3", "a4", "a5", 45), ("a6", "a7", "a8", 45), ("a9", "aa", "ab", 60))


def outage_plan(d: dict, targets: dict) -> dict:
    """What the outage loop may act on: the deployment (terraform-run outputs' answer under `outputs`), the alerting
    `device` (an open target the AWS monitor watches), each service's params and the incident's correlation_id.
    Pure: runCode runs this source on the Gateway with the targets written in."""
    result = (d.get("outputs") or {}).get("result") or {}
    outputs = (result.get("stdout_json") or {}).get("outputs")
    if result.get("return_code") != 0 or not isinstance(outputs, dict):
        return {"ok": False, "message": "terraform-run outputs did not answer (outage_outputs): nothing was changed"}
    if not outputs:
        return {"ok": True, "deployed": False, "message": "nothing is deployed in AWS: no AWS VPN to diagnose"}
    device = d.get("device")
    entry = (targets or {}).get(device) or {}
    if entry.get("monitor") != "aws":
        return {"ok": False, "message": f"{device} is not an open target the AWS monitor watches: nothing was changed"}
    instance = outputs.get("strongswan_instance_id")
    if not instance:
        return {"ok": False, "message": "the outputs name no strongSwan instance (outage_outputs): nothing was changed"}
    correlation = f"aws-vpn-{device}"
    return {"ok": True, "deployed": True, "target": device, "correlation_id": correlation,
            # open = New, In Progress or On Hold: a Resolved incident stays active=true until ServiceNow closes it
            "open_query": f"correlation_id={correlation}^stateIN1,2,3",
            "edge_in": {"target": device, "targets": targets, "deployed": outputs},
            "restart": {"action": "restart", "instance_id": instance, "timeout": RELOAD_TIMEOUT}}


def open_incident(d: dict) -> dict:
    """The open incident for this outage, from listIncidents' response (`open`), if there is one. `alert` names the
    alert in the still-down note (the tunnel-down alert when absent, R6)."""
    rows = (((d.get("open") or {}).get("body") or {}).get("result")) or []
    row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
    found = bool(row.get("sys_id"))
    alert = d.get("alert") or "the tunnel-down alert"
    return {"open": found, "sys_id": row.get("sys_id"), "number": row.get("number"),
            "note": {"work_notes": f"Still down: {alert} fired again ({d.get('starts_at')}). "
                                   "Itential did not open another incident."}}


def outage_summary(d: dict) -> dict:
    """The incident from Verify's verdict (`judgement`, the judge's runCode result), the alarm (`alarm`, aws-vpn-monitor
    alarm's envelope) and the alert (`device`, `starts_at`); its body for createIncident, and the agent's evidence."""
    judge = ((d.get("judgement") or {}).get("stdout_json")) or {}
    alarm_out = (((d.get("alarm") or {}).get("result") or {}).get("stdout_json")) or {}
    alarm = alarm_out.get("state") if alarm_out.get("found") else "not read"
    signals = judge.get("signals") or {}
    device = d.get("device")
    evidence = (f"Verify says {judge.get('verdict', 'nothing (it did not run)')}; signals router "
                f"{signals.get('router', '?')}, data plane {signals.get('data_plane', '?')}, AWS monitor "
                f"{signals.get('aws_monitor', '?')}; the AWS tunnel-down alarm is {alarm}.")
    return {
        "evidence": evidence,
        "incident": {
            "short_description": f"AWS VPN down: {device} Tunnel10",
            "description": (f"Prometheus alert LabAwsTunnelDown since {d.get('starts_at')}. {evidence} Opened by "
                            "Itential's Diagnose AWS VPN Outage; the tunnel-diagnostics agent notes its diagnosis here "
                            "and the fix runs only after a Work Center approval."),
            "correlation_id": f"aws-vpn-{device}",
            "correlation_display": "Itential AWS VPN outage loop",
            "urgency": "2", "impact": "2", "category": "network",
        },
    }


def outage_request(d: dict) -> dict:
    """The agent's request: the new incident (createIncident's response, `created`) and the evidence (`summary`)."""
    row = (((d.get("created") or {}).get("body") or {}).get("result")) or {}
    summary = ((d.get("summary") or {}).get("stdout_json")) or {}
    # ADR 0075: a loop whose agent infers hands it facts only; the AWS VPN loop still hands it evidence
    body = f"Facts: {summary['facts']}" if summary.get("facts") else f"Evidence: {summary.get('evidence', '')}"
    ok = bool(row.get("sys_id") and row.get("number"))
    return {"ok": ok, "sys_id": row.get("sys_id"), "number": row.get("number"),
            "request": f"Incident {row.get('number')} (sys_id {row.get('sys_id')}). {body}"}


def outage_fix(d: dict) -> dict:
    """The fix the agent picked (`agent`: runAgent's result {sessionId, sessionStatus, lastMessage}), held to the menu:
    the last line of its answer must be JSON naming one of OUTAGE_FIXES, or the answer is escalate."""
    agent = d.get("agent") or {}
    text = str(agent.get("lastMessage") or "")
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    answer: dict = {}
    if agent.get("sessionStatus") == "COMPLETE" and lines:
        try:
            parsed = json.loads(lines[-1])
            answer = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            answer = {}
    fix = answer.get("fix") if answer.get("fix") in OUTAGE_FIXES else "escalate"
    why = (str(answer.get("cause") or "unknown")[:80] if answer else
           ("the agent did not finish" if agent.get("sessionStatus") != "COMPLETE" else "the agent's answer was not the JSON line"))
    evidence = str(answer.get("evidence") or "")[:400]
    return {"fix": fix, "cause": why, "evidence": evidence, "card": fix != "escalate",
            "session": str(agent.get("sessionId") or "")[:64],
            "escalation": {"work_notes": f"Escalated to a person: no fix from the menu applies ({why}). {evidence}".strip()}}


def outage_result(d: dict) -> dict:
    """After the approved fix (`fix` name, its service's envelope under `answer`) and the tunnel read again (`check`,
    lab-edge verify's envelope): fixed only when router and data plane both say up. Resolves or notes the incident."""
    fix = d.get("fix")
    answer = (((d.get("answer") or {}).get("result")) or {}).get("stdout_json") or {}
    check = (((d.get("check") or {}).get("result")) or {}).get("stdout_json") or {}
    up = check.get("router") == "up" and check.get("data_plane") == "up"
    ran = {"reset-ike": answer.get("cleared") is True, "restart-strongswan": answer.get("restarted") is True,
           "repush-router-block": answer.get("saved") is True}.get(fix, False)
    note = decision_note(d.get("decision"))
    said = (f"Approved in Work Center ({'note: ' + note if note else 'no note'}). Itential ran the approved fix {fix} "
            f"({'it ran' if ran else 'it did not complete: ' + str(answer.get('error') or 'no answer')})")
    if up:
        message = f"{said}; the tunnel is up again (router and data plane)."
        return {"fixed": True, "message": message,
                "resolve": {"state": "6", "close_code": "Solution provided", "close_notes": message,
                            "work_notes": message}}
    message = (f"{said}; the tunnel is still not up (router {check.get('router', 'not read')}, data plane "
               f"{check.get('data_plane', 'not read')}). The incident stays open for a person.")
    return {"fixed": False, "message": message, "note": {"work_notes": message}}


def decision_note(decision) -> str:
    """The engineer's note from the card's export ({"decision": {"note": <the textarea>}}, P6), trimmed; "" if none."""
    note = ((decision or {}).get("decision") or {}).get("note") if isinstance(decision, dict) else None
    return " ".join(str(note or "").split())[:1000]


def outage_rejected(d: dict) -> dict:
    """The incident's work note when the card is rejected (`d` is the card's export)."""
    note = decision_note(d)
    return {"work_notes": "The proposed fix was rejected in Work Center: nothing was run. The incident stays open for "
                          f"a person. The engineer's note: {note or 'no note'}."}


# The outage card (ADR 0072 amendment, 2026-10-05): an HTML page in Work Center's InteractiveHTML task, in the lab's
# portal design (itential/portal/deploy-aws-vpn). P6/P6b on production: the body renders in an iframe that keeps
# <style>, @keyframes, the form and its textarea but strips <svg>, so each drawing is an <img> with an SVG data URI.
# The flying theme (ADR 0077): a night cockpit lit by its instruments. The meaning colours exist here once: green
# for Established, agrees and passed; amber for waiting and timers; red for down, refused and disagrees; sky for the
# one action. The CSS, the drawings and the code the runner executes all read them (CARD_PALETTE_SRC).
CARD_NIGHT, CARD_PANEL, CARD_EDGE, CARD_DEEP = "#0B1322", "#121D31", "#24344D", "#070D18"
CARD_TEXT, CARD_MUTED, CARD_GREY = "#E6EDF7", "#9FB0C8", "#7C8EA6"
CARD_GREEN, CARD_AMBER, CARD_RED, CARD_SKY = "#39C27A", "#F2B134", "#FF5A5F", "#4FA3F7"
CARD_PALETTE = {"CARD_NIGHT": CARD_NIGHT, "CARD_PANEL": CARD_PANEL, "CARD_EDGE": CARD_EDGE, "CARD_DEEP": CARD_DEEP,
                "CARD_TEXT": CARD_TEXT, "CARD_MUTED": CARD_MUTED, "CARD_GREY": CARD_GREY, "CARD_GREEN": CARD_GREEN,
                "CARD_AMBER": CARD_AMBER, "CARD_RED": CARD_RED, "CARD_SKY": CARD_SKY}
CARD_PALETTE_SRC = "".join(f"{k} = {v!r}\n" for k, v in CARD_PALETTE.items())
OUTAGE_CARD_CSS = """
body { margin: 0; padding: 4px; background: transparent; }
.oc { --night: #0B1322; --panel: #121D31; --edge: #24344D; --deep: #070D18; --ink: #E6EDF7; --ink-soft: #9FB0C8;
  --sky: #4FA3F7; --green: #39C27A; --amber: #F2B134; --red: #FF5A5F; --line: #24344D; --on-sky: #06101E;
  --display: "Avenir Next Condensed", "Avenir Next", "Segoe UI Semibold", "Arial Narrow", sans-serif;
  --mono: "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace;
  font-family: "Avenir Next", "Segoe UI", system-ui, -apple-system, sans-serif; color: var(--ink);
  background: var(--night); font-size: 15px; line-height: 1.5; max-width: 980px; margin: 0 auto;
  border-radius: 14px; overflow: hidden; box-shadow: 0 18px 40px -24px rgba(0, 0, 0, .75);
  font-variant-numeric: tabular-nums; }
.oc * { box-sizing: border-box; }
.oc p { margin: 0; }
.oc .band { background: var(--deep); color: var(--ink); padding: 22px 28px 24px; display: grid;
  grid-template-columns: 1fr auto; gap: 18px 24px; align-items: start; border-bottom: 1px solid var(--sky); }
.oc .brand { display: flex; align-items: center; gap: 10px; font-size: 13px; color: var(--ink-soft); grid-column: 1 / -1;
  letter-spacing: .02em; }
.oc .brand img { width: 26px; height: 26px; flex: none; display: block; }
.oc .brand b { color: #fff; font-family: var(--display); font-weight: 700; font-size: 15px; }
.oc h1 { font-family: var(--display); font-weight: 700; font-size: 34px; line-height: 1.05; margin: 0 0 8px; color: #fff; }
.oc .lede { color: var(--ink-soft); max-width: 58ch; }
.oc .lede strong { color: #fff; font-weight: 600; }
.oc .status { text-align: right; }
.oc .pill { display: inline-flex; align-items: center; gap: 8px; background: rgba(242, 177, 52, .14); color: var(--amber);
  border: 1px solid rgba(242, 177, 52, .55); border-radius: 999px; padding: 5px 12px 5px 10px; font-size: 13px;
  font-weight: 600; white-space: nowrap; }
.oc .pill::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: var(--amber);
  animation: oc-blink 1.8s ease-in-out infinite; }
@keyframes oc-blink { 0%, 100% { opacity: 1; } 50% { opacity: .3; } }
.oc .elapsed { margin-top: 12px; font-family: var(--mono); font-size: 36px; font-weight: 600; line-height: 1;
  letter-spacing: .02em; color: var(--amber); }
.oc .elapsed small { display: block; font-family: var(--display); font-size: 12px; font-weight: 500; color: var(--ink-soft);
  margin-top: 4px; letter-spacing: 0; }
.oc .body { padding: 22px 28px 28px; display: grid; gap: 18px; }
.oc .panel { background: var(--panel); border: 1px solid var(--edge); border-radius: 12px; padding: 18px 20px; }
.oc h2 { font-family: var(--display); font-weight: 700; font-size: 19px; margin: 0 0 12px; color: #fff; }
.oc .topo { padding: 16px 20px 6px; }
.oc .topo img { display: block; width: 100%; height: auto; }
.oc .ledger { display: grid; grid-template-columns: repeat(4, 1fr); border-top: 1px solid var(--line); margin-top: 6px; }
.oc .reading { padding: 14px 16px 4px 14px; border-left: 3px solid var(--c); }
.oc .src, .oc .why { font-size: 13px; color: var(--ink-soft); }
.oc .val { font-family: var(--mono); font-size: 19px; font-weight: 600; color: var(--c); line-height: 1.2;
  margin: 2px 0; }
.oc .steps { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(5, 1fr); }
.oc .steps li { position: relative; padding: 26px 8px 0 0; font-size: 13px; color: var(--ink-soft); }
.oc .steps li::before { content: ""; position: absolute; top: 6px; left: 0; width: 13px; height: 13px;
  border-radius: 50%; background: var(--green); box-shadow: 0 0 0 3px var(--panel), 0 0 0 4px var(--green); }
.oc .steps li::after { content: ""; position: absolute; top: 12px; left: 20px; right: 6px; height: 2px;
  background: var(--green); }
.oc .steps li:last-child::after { display: none; }
.oc .steps .who { display: block; font-family: var(--display); font-weight: 700; font-size: 15px; color: var(--ink); }
.oc .steps b { display: block; color: var(--ink); font-size: 14px; font-weight: 600; }
.oc .steps .now::before { background: var(--amber); box-shadow: 0 0 0 3px var(--panel), 0 0 0 5px var(--amber); }
.oc .steps .now::after, .oc .steps .next::after {
  background: repeating-linear-gradient(90deg, #3A4C66 0 6px, transparent 6px 11px); }
.oc .steps .now b { color: var(--amber); }
.oc .steps .next::before { background: var(--panel); box-shadow: 0 0 0 2px #3A4C66; }
.oc .diag { display: grid; grid-template-columns: 1fr 1.05fr; gap: 20px; align-items: start; }
.oc .cause { font-family: var(--display); font-size: 25px; font-weight: 700; line-height: 1.15; margin: 2px 0 10px; color: #fff; }
.oc .by { display: inline-flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--ink-soft);
  background: var(--night); border-radius: 999px; padding: 4px 10px 4px 6px; margin-bottom: 12px; }
.oc .by i { width: 18px; height: 18px; border-radius: 50%; display: inline-block;
  box-shadow: inset 0 0 0 4px var(--panel), inset 0 0 0 9px var(--sky); background: var(--sky); }
.oc .term { background: var(--deep); color: #C9D6E8; border: 1px solid var(--edge); border-radius: 10px;
  padding: 12px 16px 14px; font-family: var(--mono); font-size: 12.3px; line-height: 1.6; }
.oc .term .h { color: var(--sky); margin-top: 8px; }
.oc .term .h:first-child { margin-top: 0; }
.oc .term .row { display: flex; justify-content: space-between; gap: 12px; padding-left: 14px; }
.oc .term .ok { color: var(--green); }
.oc .term .bad { color: var(--red); }
.oc .term .unk { color: var(--ink-soft); }
.oc .action { background: var(--sky); color: var(--on-sky); border: 0; display: grid; grid-template-columns: 1fr auto;
  gap: 6px 24px; padding: 22px 24px; }
.oc .kicker { font-size: 13px; color: #0B2A4A; }
.oc .action h2 { font-size: 28px; margin: 2px 0 8px; color: var(--on-sky); }
.oc .what { color: #0B2A4A; max-width: 56ch; }
.oc .scope { align-self: start; text-align: right; font-size: 12.5px; color: #0B2A4A; }
.oc .scope b { display: block; font-family: var(--display); font-size: 20px; color: var(--on-sky); }
.oc .promises { grid-column: 1 / -1; list-style: none; margin: 14px 0 0; padding: 14px 0 0;
  border-top: 1px solid rgba(6, 16, 30, .28); display: grid; grid-template-columns: repeat(3, 1fr);
  gap: 10px 18px; font-size: 13.5px; color: var(--on-sky); }
.oc .promise { padding-left: 24px; position: relative; }
.oc .promise::before { content: ""; position: absolute; left: 2px; top: 4px; width: 7px; height: 11px;
  border: solid var(--on-sky); border-width: 0 2.5px 2.5px 0; transform: rotate(40deg); }
.oc .decide { display: grid; grid-template-columns: 1fr 1.3fr; gap: 20px; align-items: start; }
.oc .decide p { color: var(--ink-soft); font-size: 14px; }
.oc .decide p + p { margin-top: 8px; }
.oc label { display: block; font-weight: 600; font-size: 14px; margin-bottom: 6px; color: var(--ink); }
.oc textarea { width: 100%; min-height: 88px; font: inherit; font-size: 14px; padding: 10px 12px; color: var(--ink);
  border: 1px solid var(--edge); border-radius: 8px; background: #0E1828; resize: vertical; }
.oc textarea:focus-visible { outline: 3px solid rgba(79, 163, 247, .4); border-color: var(--sky); }
.oc .foot { padding: 0 28px 20px; font-size: 12px; color: var(--ink-soft); }
.oc a { color: var(--sky); }
@media (max-width: 760px) {
  .oc .band, .oc .action, .oc .decide, .oc .diag { grid-template-columns: 1fr; }
  .oc .status, .oc .scope { text-align: left; }
  .oc .ledger { grid-template-columns: 1fr 1fr; }
  .oc .steps, .oc .promises { grid-template-columns: 1fr; }
  .oc .steps li::after { display: none; }
}
"""

# each menu fix as the card says it: title, what happens, scope (value, note), the three promises. {d} is the device.
OUTAGE_FIX_COPY = {
    "reset-ike": ("Reset the IKE session on {d}",
                  "Itential clears the router's IKE session to the AWS peer so both ends build a fresh one, then reads "
                  "the tunnel again.",
                  ("1 router", "no configuration change"),
                  ("Runs one clear command, nothing typed by the agent",
                   "Changes no configuration on {d} and nothing in AWS",
                   "Proven afterwards: up resolves the incident, down keeps it open")),
    "restart-strongswan": ("Restart strongSwan in AWS",
                           "Itential asks the VPN monitor to run its one fixed restart command on the strongSwan "
                           "instance, then reads the tunnel again.",
                           ("1 EC2 instance", "about 30 seconds"),
                           ("Runs a single pre-approved command, nothing typed by the agent",
                            "Touches nothing on {d} or anywhere else in AWS",
                            "Proven afterwards: up resolves the incident, down keeps it open")),
    "repush-router-block": ("Re-push the AWS VPN block to {d}",
                            "Itential renders the router's AWS VPN block from the source of truth, as Hand Off does, "
                            "and pushes it under a revert timer, then reads the tunnel again.",
                            ("1 router", "configuration, with a revert timer"),
                            ("Pushes only the block Hand Off renders, checked by its SHA-256",
                             "Rolls back by itself unless a fresh IKE session comes up",
                             "Proven afterwards: up resolves the incident, down keeps it open")),
}


# the agent's cause tags (itential/agents/tunnel-diagnostics.yaml, its fixed list) as the card's headline. {d} is the
# device; a tag off the list is shown as the agent wrote it. `unknown` always escalates, so it never reaches a card.
OUTAGE_CAUSES = {
    "tunnel-shut": "Tunnel10 is shut on {d}",
    "ike-blocked": "INET-IN no longer admits IKE from AWS",
    "strongswan-down": "strongSwan is not answering in AWS",
    "stale-ike": "The IKE session to AWS is stale",
}


def outage_card_image(svg: str, alt: str, width: int, height: int) -> str:
    """An SVG drawing as an <img> (Work Center strips <svg>, P6): base64 in a data URI."""
    data = base64.b64encode(svg.encode()).decode()
    return (f'<img alt="{html.escape(alt)}" width="{width}" height="{height}" '
            f'src="data:image/svg+xml;base64,{data}">')


def card_clock(starts_at, now) -> tuple:
    """(start, now, elapsed) for a card: the alert's start and the card's time as UTC datetimes (now: the runner's clock
    when absent or unreadable; start: None when unreadable) and the outage's age as text ("" when unknown)."""
    def when(value):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None

    start, now = when(starts_at), when(now) or datetime.now(timezone.utc)
    minutes = int((now - start).total_seconds() // 60) if start and now >= start else None
    elapsed = ("" if minutes is None else f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60:02d} min")
    return start, now, elapsed


def card_mark() -> str:
    """The lab's mark for a card's band (ADR 0077): an attitude indicator, sky over ground behind the aircraft
    reference symbol. Drawn once here and once in the portal page; never a callsign."""
    return outage_card_image(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40" width="40" height="40"><rect width="40" '
        'height="40" rx="10" fill="#070D18"/><clipPath id="c"><circle cx="20" cy="20" r="14"/></clipPath>'
        '<g clip-path="url(#c)"><rect x="4" y="4" width="32" height="16" fill="#4FA3F7"/><rect x="4" y="20" '
        'width="32" height="16" fill="#8A5A1E"/><path d="M6 20h28" stroke="#fff" stroke-width="1.2"/>'
        '<path d="M14 15h4M22 15h4M16 25h2M22 25h2" stroke="#fff" stroke-width="1" opacity=".8"/></g>'
        '<path d="M9 21h8l3 3 3-3h8" fill="none" stroke="#F2B134" stroke-width="2.2" stroke-linecap="round" '
        'stroke-linejoin="round"/><circle cx="20" cy="21" r="1.6" fill="#F2B134"/>'
        '<circle cx="20" cy="20" r="14" fill="none" stroke="#fff" stroke-width="1.4" opacity=".9"/></svg>', "", 26, 26)


def card_page(title: str, headline: str, lede: str, elapsed: str, since: str, sections: str, number: str,
              foot: str, extra_css: str = "", decide: str = "", note_label: str = "Note for the incident") -> str:
    """A Work Center card's page (R6, R10): the band (title, lede, waiting pill, the outage's age), the card's own
    sections, the decision with the engineer's note, the foot. `lede`, `sections` and `foot` are HTML the caller built
    from escaped values; every other argument is escaped here. `extra_css` adds rules after the shared ones; `decide`
    (HTML) replaces the decision's two lines, which speak of an incident."""
    e = html.escape
    decide = decide or (f"<p>Approve runs only this fix. Reject runs nothing and leaves {e(number)} open for a person.</p>\n"
                        "<p>Either way, your note goes into the incident's work notes.</p>")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{e(title)}</title>
<style>{OUTAGE_CARD_CSS}{extra_css}</style></head>
<body><div class="oc">
<header class="band">
<div class="brand">{card_mark()}<span><b>Elliot's Itential Lab</b>&nbsp; outage response</span></div>
<div><h1>{e(headline)}</h1>
<p class="lede">{lede}</p></div>
<div class="status"><span class="pill">Waiting for your approval</span>
<p class="elapsed">{e(elapsed)}<small>{e(since)}</small></p></div>
</header>
<div class="body">
{sections}
<section class="panel decide" aria-labelledby="t-decide"><div><h2 id="t-decide">Your decision</h2>
{decide}</div>
<form name="decision"><label for="oc-note">{e(note_label)}</label>
<textarea id="oc-note" name="note" placeholder="Why you approve or reject, or what you checked first"></textarea>
</form></section>
</div>
<p class="foot">{foot}</p>
</div></body></html>"""


APPROVAL_CARD_CSS = """
.oc .facts { display: grid; grid-template-columns: max-content 1fr; gap: 6px 18px; margin: 0; font-size: 14px; }
.oc .facts dt { color: var(--ink-soft); }
.oc .facts dd { margin: 0; font-family: var(--mono); font-size: 13.5px; word-break: break-word; }
.oc pre.block { margin: 0; font-family: var(--mono); font-size: 13px; line-height: 1.55; white-space: pre-wrap;
  color: var(--ink); background: var(--deep); border: 1px solid var(--edge); border-radius: 10px; padding: 12px 14px;
  max-height: 520px; overflow: auto; }
"""
APPROVAL_BLOCK_CHARS = 20_000
APPROVAL_HELPER_OFFSET = (0, -300)  # canvas only: the helper row above the approval (measured: Revert 2 arrows through, Deploy and Tear Down within bounds)
# the object keys the ViewData used to show raw, as a person reads them; anything else is capitalised as is
APPROVAL_LABELS = {"block": "The block (key masked)", "lines": "The lines pushed", "netbox": "NetBox, as read back",
                   "plan": "The plan", "resources": "Resources in the plan", "summary": "Plan summary",
                   "kept only if": "Kept only if", "after this": "After this", "details": "Details"}


def approval_card(d: dict) -> dict:
    """A form-based approval as the branded page (ADR 0077): the approval's message as the lede, its object laid out
    as facts (scalars) and blocks (lines, the block, a plan), the same two buttons and the note. `header`, `message`
    and `body` come from the workflow; everything is escaped here, blocks are capped. Pure: runCode runs this
    function's own source on the Gateway."""
    e = html.escape
    header = str(d.get("header") or "Approve this change")
    message = str(d.get("message") or "")
    body = d.get("body")
    facts, blocks = [], []
    if isinstance(body, dict):
        for key, value in body.items():
            if isinstance(value, list) and all(isinstance(i, str) for i in value):
                blocks.append((str(key), "\n".join(value)))
            elif isinstance(value, (dict, list)):
                blocks.append((str(key), json.dumps(value, indent=2, sort_keys=True)))
            elif isinstance(value, str) and ("\n" in value or len(value) > 90):
                blocks.append((str(key), value))
            else:
                facts.append((str(key), "" if value is None else str(value)))
    elif isinstance(body, str):
        blocks.append(("details", body))
    elif body is not None:
        blocks.append(("details", json.dumps(body, indent=2, sort_keys=True)))
    sections = ""
    if facts:
        rows = "".join(f"<dt>{e(k)}</dt><dd>{e(v)}</dd>" for k, v in facts)
        sections += f'<section class="panel" aria-labelledby="t-facts"><h2 id="t-facts">What this approval covers</h2>\n<dl class="facts">{rows}</dl></section>\n'
    for n, (key, text) in enumerate(blocks):
        text = text if len(text) <= APPROVAL_BLOCK_CHARS else text[:APPROVAL_BLOCK_CHARS] + "\n... (cut)"
        label = APPROVAL_LABELS.get(key, key.replace("_", " ").capitalize())
        sections += (f'<section class="panel" aria-labelledby="t-b{n}"><h2 id="t-b{n}">{e(label)}</h2>\n'
                     f'<pre class="block">{e(text)}</pre></section>\n')
    try:
        now = datetime.fromisoformat(str(d.get("now")).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        now = datetime.now(timezone.utc)
    decide = ("<p>Approve runs exactly what this card shows. Reject runs nothing and ends the job.</p>\n"
              "<p>Your note stays on the job, for the record.</p>")
    foot = f"Prepared by Itential at {now:%H:%M} UTC from the job's own data; the pre-shared key is never on a card."
    page = card_page(header, header, e(message), "", "", sections, "", foot, extra_css=APPROVAL_CARD_CSS,
                     decide=decide, note_label="Note for the record")
    return {"html": page}


def approval(tid: str, header: str, message_ref: str, body_ref: str, ok: str, cancel: str, x: int, var: str,
             y: int = 0) -> dict:
    """An approval as a branded page (ADR 0077): three setObjectKey tasks gather header, message and body, runCode
    renders approval_card on the Gateway, a query takes the HTML, and the InteractiveHTML task `tid` shows it with the
    same two buttons the ViewData had. `tid` keeps its id, so the workflow's edges out of the approval are unchanged;
    the five helper tasks are tid1..tid5 (approval_edges wires them)."""
    a, b, c, d, f = (f"{tid}{i}" for i in "12345")
    hx, hy = APPROVAL_HELPER_OFFSET  # the five helpers sit on their own row, so the straight arrows miss the other tasks
    return {
        a: set_key("the card: header", {}, "header", header, x=x + hx, y=y + hy),
        b: set_key("the card: message", f"$var.{a}.object", "message", message_ref, x=x + hx + 50, y=y + hy),
        c: set_key("the card: body", f"$var.{b}.object", "body", body_ref, x=x + hx + 100, y=y + hy),
        d: run_code("the card's page (Python on the runner)", APPROVAL_CARD_CODE, f"$var.{c}.object", f"{var}_card",
                    x=x + hx + 150, y=y + hy),
        f: jq("the card's HTML", f"$var.{d}.result", "stdout_json.html", x=x + hx + 200, y=y + hy),
        tid: task("InteractiveHTML", "WorkCenter", f"approval: {header}",
                  {"header": header, "body": f"$var.{f}.return_data", "variables": {}, "btn_success": ok,
                   "btn_failure": cancel},
                  {"export": f"$var.job.{var}_decision"}, kind="manual", display="Work Center",
                  view="/work-center/task/InteractiveHTML", x=x, y=y),
    }


def outage_card_topology(device: str, vpc: str, tunnel: tuple, router: tuple, aws: tuple) -> str:
    """The path as an engineer draws it: the router in DC1, Tunnel10, strongSwan in AWS. Each of tunnel, router and
    aws is (colour, label); a tunnel that is not up is drawn broken, with a pulsing fracture."""
    e = html.escape
    (t_col, t_label), (r_col, r_label), (a_col, a_label) = tunnel, router, aws
    broken = t_col != CARD_GREEN
    pipe = "#4A2430" if broken else "#15402F"
    fracture = (f'<g class="f"><path d="M346 98l12 14-10 6 14 22" stroke="{CARD_RED}" stroke-width="8" fill="none" '
                f'filter="url(#g)" opacity=".6"/><path d="M346 98l12 14-10 6 14 22" stroke="{CARD_RED}" '
                'stroke-width="3.2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></g>') if broken else ""
    dash = ' stroke-dasharray="7 6"' if broken else ""
    pipes = "M206 118H336M384 118H514" if broken else "M206 118H514"
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 720 230" width="720" height="230">'
        '<style>text{font-family:"Avenir Next","Segoe UI",system-ui,sans-serif}'
        '.f{transform-origin:360px 118px;animation:p 1.8s ease-in-out infinite}'
        '@keyframes p{0%,100%{opacity:1}50%{opacity:.35}}'
        '@media (prefers-reduced-motion:reduce){.f{animation:none}}</style>'
        '<defs><pattern id="d" width="16" height="16" patternUnits="userSpaceOnUse"><path d="M16 0H0V16" '
        'fill="none" stroke="#1A2740"/></pattern><filter id="g" x="-50%" y="-50%" width="200%" height="200%">'
        '<feGaussianBlur stdDeviation="5"/></filter></defs>'
        f'<rect x="10" y="22" width="240" height="190" rx="14" fill="url(#d)" stroke="{CARD_EDGE}"/>'
        f'<text x="28" y="48" font-size="13" fill="{CARD_MUTED}">DC1 on-prem</text>'
        '<rect x="470" y="22" width="240" height="190" rx="14" fill="#161A2A" stroke="#5E4B22" stroke-dasharray="6 5"/>'
        f'<text x="488" y="48" font-size="13" fill="#C9A45C">AWS, lab VPC {e(vpc)}</text>'
        f'<path d="{pipes}" stroke="{pipe}" stroke-width="14" stroke-linecap="round"/>'
        f'<path d="{pipes}" stroke="{t_col}" stroke-width="3"{dash} stroke-linecap="round"/>'
        f'{fracture}'
        f'<text x="360" y="86" font-size="14" font-weight="700" fill="{CARD_TEXT}" text-anchor="middle">Tunnel10, IKEv2</text>'
        f'<text x="360" y="164" font-size="13" fill="{t_col}" text-anchor="middle">{e(t_label)}</text>'
        f'<rect x="60" y="80" width="146" height="76" rx="12" fill="{CARD_DEEP}" stroke="{r_col}" stroke-width="2.5"/>'
        f'<circle cx="90" cy="118" r="15" fill="{CARD_DEEP}" stroke="{r_col}" stroke-width="2"/>'
        f'<path d="M82 114h16m-4-4 4 4-4 4M98 122H82m4-4-4 4 4 4" stroke="{r_col}" stroke-width="2" fill="none" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
        f'<text x="114" y="113" font-size="15" font-weight="700" fill="{CARD_TEXT}">{e(device)}</text>'
        f'<text x="114" y="132" font-size="12.5" fill="{r_col}">{e(r_label)}</text>'
        f'<text x="60" y="182" font-size="12.5" fill="{CARD_MUTED}">the edge router</text>'
        f'<rect x="514" y="80" width="160" height="76" rx="12" fill="{CARD_DEEP}" stroke="{a_col}" stroke-width="2.5"/>'
        f'<rect x="531" y="103" width="30" height="30" rx="7" fill="{CARD_DEEP}" stroke="{a_col}" stroke-width="2"/>'
        f'<path d="M546 109l9 4v6c0 5-4 8-9 10-5-2-9-5-9-10v-6z" fill="none" stroke="{a_col}" stroke-width="1.8" '
        'stroke-linejoin="round"/>'
        f'<text x="571" y="113" font-size="15" font-weight="700" fill="{CARD_TEXT}">strongSwan</text>'
        f'<text x="571" y="132" font-size="12.5" fill="{a_col}">{e(a_label)}</text>'
        '<text x="514" y="182" font-size="12.5" fill="#C9A45C">on EC2, watched by the monitor</text>'
        '</svg>'
    )


def outage_card(d: dict) -> dict:
    """The Work Center card as one HTML page: the outage (`device`, `starts_at`, `number`), the readings (`judgement`,
    Verify's runCode result; `alarm`, aws-vpn-monitor alarm's envelope), the fix (`fix`, outage_fix's runCode result),
    the VPC (`plan`, outage_plan's) and the time it is drawn (`now`, ISO; the runner's clock when absent). Every value
    from outside is escaped. Pure: runCode runs this source on the Gateway."""
    e = html.escape
    kelp, buoy, flare, grey = CARD_GREEN, CARD_RED, CARD_AMBER, CARD_GREY
    device = str(d.get("device") or "the router")
    number = str(d.get("number") or "the incident")
    fix_out = (d.get("fix") or {}).get("stdout_json") or {}
    title, what, (scope, scope_note), promises = OUTAGE_FIX_COPY[fix_out.get("fix")]
    judge = (d.get("judgement") or {}).get("stdout_json") or {}
    signals = judge.get("signals") or {}
    readings = judge.get("readings") or {}
    edge = readings.get("router") or {}
    mon = readings.get("aws_monitor") or {}
    alarm = (((d.get("alarm") or {}).get("result")) or {}).get("stdout_json") or {}
    deployed = ((((d.get("plan") or {}).get("stdout_json") or {}).get("edge_in")) or {}).get("deployed") or {}
    prefixes = deployed.get("vpc_private_prefixes") or []
    vpc = str(prefixes[0]) if prefixes and re.fullmatch(r"[0-9./]{7,18}", str(prefixes[0])) else ""

    start, now, elapsed = card_clock(d.get("starts_at"), d.get("now"))
    since = f"Tunnel10 down since {start:%H:%M} UTC" if start else "Tunnel10 down: start time not read"

    # the drawing
    router_sig, data_sig, aws_sig = signals.get("router"), signals.get("data_plane"), signals.get("aws_monitor")
    ping = edge.get("ping_success_percent")
    if router_sig not in ("up", "down"):
        tunnel = (flare, "not read")
    elif not edge.get("ike_sa_ready"):
        tunnel = (buoy, "no IKE session")
    elif not edge.get("packets_rising"):
        tunnel = (buoy, "IKE up, no packets")
    elif data_sig != "up":
        tunnel = (buoy, "pings fail")
    else:
        tunnel = (kelp, "up")
    router = (kelp, "answers") if router_sig in ("up", "down") else (grey, "not read")
    aws = {"up": (kelp, "reports the tunnel"), "down": (buoy, "reports no tunnel"),
           "disagreement": (flare, "readings disagree")}.get(aws_sig, (flare, "no reading"))
    drawing = outage_card_image(outage_card_topology(device, vpc, tunnel, router, aws),
                                f"{device} in DC1, Tunnel10 {tunnel[1]}, strongSwan in AWS {aws[1]}", 720, 230)

    # one reading per source, coloured by what it says
    alarm_val = {"ALARM": ("In alarm", buoy), "OK": ("OK", kelp), "INSUFFICIENT_DATA": ("No data", flare)}.get(
        alarm.get("state"), ("Not read", grey))
    alarm_since = card_clock(alarm.get("since"), None)[0]
    ledger = (
        ("Router's view", *{"up": ("Up", kelp), "down": ("Down", buoy)}.get(router_sig, ("Not read", grey)),
         "IKE session to AWS ready" if edge.get("ike_sa_ready") else "no IKE session to AWS"),
        ("Traffic", f"{ping}%" if isinstance(ping, int) else "Not read", kelp if data_sig == "up" else buoy,
         "pings through the tunnel"),
        ("AWS monitor", *{"up": ("Up", kelp), "down": ("Down", buoy), "disagreement": ("Disagrees", flare)}.get(
            aws_sig, ("No reading", flare)), "what strongSwan reports"),
        ("CloudWatch", *alarm_val, f"since {alarm_since:%H:%M} UTC" if alarm_since else "the tunnel-down alarm"),
    )
    ledger_html = "".join(
        f'<div class="reading" style="--c:{c}"><p class="src">{e(src)}</p><p class="val">{e(val)}</p>'
        f'<p class="why">{e(why)}</p></div>' for src, val, c, why in ledger)

    def value(v, bad_when_positive=False):
        if v is None:
            return '<span class="unk">not read</span>'
        if isinstance(v, bool):
            return f'<span class="{"ok" if v else "bad"}">{"yes" if v else "no"}</span>'
        if bad_when_positive:
            return f'<span class="{"bad" if v else "ok"}">{e(str(v))}</span>'
        return f'<span class="ok">{e(str(v))}</span>'

    rows = (
        ("h", f"{device}, read by lab-edge verify"),
        ("IKE session to AWS ready", value(edge.get("ike_sa_ready"))),
        ("identities match", value(edge.get("identities_match"))),
        ("PFS configured", value(edge.get("pfs_configured"))),
        ("packets rising both ways", value(edge.get("packets_rising"))),
        ("pings through the tunnel", value(None) if not isinstance(ping, int) else
         f'<span class="{"ok" if ping > 50 else "bad"}">{ping}%</span>'),
        ("drops from AWS to the router", value(edge.get("dropped_aws_to_self"), True)),
        ("h", "strongSwan, read by the AWS VPN monitor"),
        ("monitor answered", value(mon.get("lambda_answered"))),
        ("its check succeeded", value(mon.get("check_succeeded"))),
        ("strongSwan says established", value(mon.get("lambda_says_established"))),
        ("h", "CloudWatch"),
        ("tunnel-down alarm", f'<span class="{"bad" if alarm.get("state") == "ALARM" else "ok" if alarm.get("state") == "OK" else "unk"}">'
                              f'{e(str(alarm.get("state") or "not read"))}</span>'),
    )
    term = "".join(f'<p class="h">{e(b)}</p>' if a == "h" else f'<p class="row"><span>{e(a)}</span>{b}</p>'
                   for a, b in rows)

    tag = str(fix_out.get("cause") or "no cause given")
    cause = OUTAGE_CAUSES[tag].format(d=device) if tag in OUTAGE_CAUSES else tag
    session = str(fix_out.get("session") or "")[:8]
    agent_by = "tunnel-diagnostics agent" + (f", session {session}" if session else "")
    evidence = str(fix_out.get("evidence") or "")
    promises_html = "".join(f'<li class="promise">{e(p.format(d=device))}</li>' for p in promises)
    sections = f"""<section class="panel topo" aria-labelledby="t-where"><h2 id="t-where">Where the tunnel breaks</h2>{drawing}
<div class="ledger">{ledger_html}</div></section>
<section class="panel" aria-labelledby="t-when"><h2 id="t-when">What has happened so far</h2>
<ol class="steps">
<li><span class="who">Prometheus</span><b>Alert fired</b>{e(since[len("Tunnel10 down "):] if start else "Tunnel10 down")}</li>
<li><span class="who">ServiceNow</span><b>Incident opened</b>{e(number)}, with the evidence</li>
<li><span class="who">The agent</span><b>Cause found</b>one fix picked from a fixed menu</li>
<li class="now"><span class="who">You</span><b>Approve or reject</b>nothing has run yet</li>
<li class="next"><span class="who">Itential</span><b>Fix and prove</b>reads the tunnel, then resolves</li>
</ol></section>
<section class="panel diag" aria-labelledby="t-found"><div>
<h2 id="t-found">What the agent found</h2><span class="by"><i></i>{e(agent_by)}</span>
<p class="cause">{e(cause)}</p>
<p>{e(evidence) if evidence else "The agent's full diagnosis is in the incident's work notes."}</p></div>
<div class="term" role="group" aria-label="What Verify read when the incident opened">{term}</div></section>
<section class="panel action" aria-labelledby="t-fix"><div><p class="kicker">The proposed fix</p>
<h2 id="t-fix">{e(title.format(d=device))}</h2><p class="what">{e(what)}</p></div>
<p class="scope">Scope<b>{e(scope)}</b>{e(scope_note)}</p>
<ul class="promises">{promises_html}</ul></section>"""
    lede = (f"Itential opened <strong>{e(number)}</strong> and the tunnel-diagnostics agent found the likely cause.\n"
            "One fix is ready, and nothing runs until you approve it.")
    foot = (f"Prepared by Itential's Diagnose AWS VPN Outage workflow at {now:%H:%M} UTC, from live reads of\n"
            f"{e(device)}, the AWS VPN monitor and CloudWatch.")
    page = card_page(f"AWS VPN outage: {number}", f"The AWS VPN is down on {device}", lede, elapsed, since, sections,
                     number, foot)
    return {"html": page}


def _source(*fns, call: str, extra: str = "") -> str:
    return ("import json, sys\n\n\n" + extra + "\n\n".join(inspect.getsource(f) for f in fns)
            + f"\n\nprint(json.dumps({call}))\n")


OUTAGE_PLAN_CODE = _source(outage_plan, call='outage_plan(json.loads(sys.stdin.read() or "{}"), TARGETS)',
                           extra="TARGETS = " + repr(VERIFY_TARGETS) + "\nRELOAD_TIMEOUT = " + repr(RELOAD_TIMEOUT) + "\n\n\n")
OPEN_INCIDENT_CODE = _source(open_incident, call='open_incident(json.loads(sys.stdin.read() or "{}"))')
OUTAGE_SUMMARY_CODE = _source(outage_summary, call='outage_summary(json.loads(sys.stdin.read() or "{}"))')
OUTAGE_REQUEST_CODE = _source(outage_request, call='outage_request(json.loads(sys.stdin.read() or "{}"))')
OUTAGE_FIX_CODE = _source(outage_fix, call='outage_fix(json.loads(sys.stdin.read() or "{}"))',
                          extra="OUTAGE_FIXES = " + repr(OUTAGE_FIXES) + "\n\n\n")
OUTAGE_RESULT_CODE = _source(decision_note, outage_result, call='outage_result(json.loads(sys.stdin.read() or "{}"))')
OUTAGE_REJECTED_CODE = _source(decision_note, outage_rejected,
                               call='outage_rejected(json.loads(sys.stdin.read() or "{}"))')
APPROVAL_CARD_CODE = _source(outage_card_image, card_mark, card_page, approval_card,
                             call='approval_card(json.loads(sys.stdin.read() or "{}"))',
                             extra=CARD_PALETTE_SRC + "import base64\nimport html\nfrom datetime import datetime, timezone\n\n"
                             "OUTAGE_CARD_CSS = " + repr(OUTAGE_CARD_CSS) + "\nAPPROVAL_CARD_CSS = "
                             + repr(APPROVAL_CARD_CSS) + "\nAPPROVAL_BLOCK_CHARS = " + repr(APPROVAL_BLOCK_CHARS)
                             + "\nAPPROVAL_LABELS = " + repr(APPROVAL_LABELS) + "\n\n\n")
OUTAGE_CARD_CODE = _source(outage_card_image, card_clock, card_mark, card_page, outage_card_topology, outage_card,
                           call='outage_card(json.loads(sys.stdin.read() or "{}"))',
                           extra=CARD_PALETTE_SRC + "import base64\nimport html\nimport re\nfrom datetime import datetime, timezone\n\n"
                                 "OUTAGE_CARD_CSS = " + repr(OUTAGE_CARD_CSS) + "\nOUTAGE_FIX_COPY = "
                                 + repr(OUTAGE_FIX_COPY) + "\nOUTAGE_CAUSES = " + repr(OUTAGE_CAUSES) + "\n\n\n")


def run_agent(summary: str, agent_marker: str, request_ref: str, out_job: str, x: int, y: int = 0) -> dict:
    """AgentSessionManager.runAgent (P5, production 2026-10-05): `agent` is the agent's UUID - a name is refused - so the
    document carries a marker the import fills in; the result is {sessionId, sessionStatus, lastMessage}. The engine
    adds its own callback signature: the task must not name one."""
    return task("runAgent", "AgentSessionManager", summary, {"agent": agent_marker, "inputs": {"request": request_ref}},
                {"result": f"$var.job.{out_job}"}, display="Agent Sessions", x=x, y=y)


def diagnose_aws_vpn_outage() -> dict:
    ok, fail, err = "success", "failure", "error"
    nothing = "nothing was changed"
    tasks = {
        "01": empty("no alarm reading yet", "alarm_result", x=0),
        "02": empty("no agent answer yet", "agent_result", x=20),
        "03": empty("no fix answer yet", "fix_result", x=40),
        "04": empty("no tunnel reading after the fix yet", "check_result", x=60),
        # the deployment and the plan
        "1a": parse("terraform-run outputs' params", OUTPUTS_PARAMS, x=100),
        "1b": run_service("the deployment (terraform-run outputs, a state read)", "terraform-run", "$var.1a.textObject",
                          "outage_outputs", x=150),
        "1c": evaluate("the state read answered?", "1b", "result", "result.return_code", "==", 0, x=200),
        "1d": set_key("the outputs' answer", {}, "outputs", "$var.job.outage_outputs", x=250),
        "1e": set_key("the alerting device", "$var.1d.object", "device", "$var.job.device", x=275),
        "1f": run_code("what the loop may act on (Python on the runner)", OUTAGE_PLAN_CODE, "$var.1e.object",
                       "outage_plan", x=300),
        "10": evaluate("the plan made?", "1f", "result", "stdout_json.ok", "==", True, x=350),
        "19": jq("why there is no plan", "$var.1f.result", "stdout_json.message", x=400, y=-600, to_job="error"),
        "11": evaluate("anything deployed?", "1f", "result", "stdout_json.deployed", "==", True, x=400),
        "12": jq("nothing to diagnose", "$var.1f.result", "stdout_json.message", x=450, y=600, to_job="outcome"),
        "13": jq("the router's plan input", "$var.1f.result", "stdout_json.edge_in", x=450),
        "14": run_code("what to read and push on the router (Hand Off's plan)", LAB_EDGE_PLAN_CODE,
                       "$var.13.return_data", "outage_edge", x=500),
        "15": evaluate("the router's plan ready?", "job", "outage_edge", "stdout_json.ready", "==", True, x=550),
        # one incident per outage: an open one is only noted
        "2a": jq("the open-incident query", "$var.job.outage_plan", "stdout_json.open_query", x=600),
        "2b": sni("listIncidents", "an open incident for this outage? (correlation_id)",
                  {"sysparm_query": "$var.2a.return_data", "sysparm_fields": "sys_id,number", "sysparm_limit": 1},
                  x=650, outgoing={"response": "$var.job.outage_open"}),
        "2c": set_key("the open-incident answer", {}, "open", "$var.job.outage_open", x=700),
        "2d": set_key("when the alert started", "$var.2c.object", "starts_at", "$var.job.starts_at", x=725),
        "2e": run_code("is one open? (Python on the runner)", OPEN_INCIDENT_CODE, "$var.2d.object", "outage_dedupe",
                       x=750),
        "2f": evaluate("already open?", "2e", "result", "stdout_json.open", "==", True, x=800),
        "20": jq("the open incident", "$var.2e.result", "stdout_json.sys_id", x=850, y=600),
        "21": jq("the still-down note", "$var.2e.result", "stdout_json.note", x=875, y=600),
        "22": sni("updateIncident", "note the open incident: still down",
                  {"sys_id": "$var.20.return_data", "sysparm_fields": "number", **nbi_body("$var.21.return_data")},
                  x=900, y=600),
        "23": note("still down: noted", "the outage already has an open incident: noted it, opened none", "outcome",
                   x=925, y=600),
        # the evidence: the AWS alarm, then Verify's own checks (verify_section, its third copy)
        "3a": parse("aws-vpn-monitor alarm's params", ALARM_PARAMS, x=950),
        "3b": run_service("the AWS tunnel-down alarm (aws-vpn-monitor alarm, a CloudWatch read)", "aws-vpn-monitor",
                          "$var.3a.textObject", "alarm_result", x=1000),
        "3e": evaluate("the alarm read?", "3b", "result", "result.return_code", "==", 0, x=1025),
        "3f": note("the alarm could not be read", "the AWS tunnel-down alarm could not be read: the incident says so",
                   "alarm_note", x=1050, y=300),
        "30": note("the tunnel is up again", "the tunnel was up again when Itential checked: no incident opened, "
                   "nothing was changed", "outcome", x=1800, y=600),
        # the incident
        "31": set_key("the incident: Verify's verdict", {}, "judgement", "$var.job.judgement", x=1800),
        "32": set_key("the incident: the alarm", "$var.31.object", "alarm", "$var.job.alarm_result", x=1825),
        "33": set_key("the incident: the device", "$var.32.object", "device", "$var.job.device", x=1850),
        "34": set_key("the incident: when it started", "$var.33.object", "starts_at", "$var.job.starts_at", x=1875),
        "35": run_code("the incident's text (Python on the runner)", OUTAGE_SUMMARY_CODE, "$var.34.object",
                       "outage_summary", x=1900),
        "36": jq("the incident's body", "$var.35.result", "stdout_json.incident", x=1925),
        "37": sni("createIncident", "open one ServiceNow incident for the outage",
                  {"sysparm_fields": "sys_id,number", **nbi_body("$var.36.return_data")}, x=1950,
                  outgoing={"response": "$var.job.incident_created"}),
        "38": set_key("the new incident", {}, "created", "$var.job.incident_created", x=1975),
        "39": set_key("the evidence for the agent", "$var.38.object", "summary", "$var.job.outage_summary", x=2000),
        "3c": run_code("the agent's request (Python on the runner)", OUTAGE_REQUEST_CODE, "$var.39.object",
                       "outage_request", x=2025),
        "3d": evaluate("the incident opened?", "3c", "result", "stdout_json.ok", "==", True, x=2050),
        # A3: the agent diagnoses, notes the incident and picks one fix
        "4a": jq("the agent's request", "$var.job.outage_request", "stdout_json.request", x=2100),
        "4b": run_agent("tunnel-diagnostics: read both ends, note the incident, pick one fix", AGENT_MARKER,
                        "$var.4a.return_data", "agent_result", x=2150),
        "45": note("the agent did not run", "the tunnel-diagnostics session did not run: the loop escalates",
                   "agent_note", x=2175, y=-300),
        "4c": set_key("the agent's answer", {}, "agent", "$var.job.agent_result", x=2200),
        "4d": jq("the incident number", "$var.job.outage_request", "stdout_json.number", x=2225),
        "4e": set_key("the incident number for the card", "$var.4c.object", "number", "$var.4d.return_data", x=2250),
        "4f": run_code("the fix, held to the menu (Python on the runner)", OUTAGE_FIX_CODE, "$var.4e.object",
                       "outage_fix", x=2275),
        "40": evaluate("a fix to approve?", "4f", "result", "stdout_json.card", "==", True, x=2300),
        "41": jq("the incident", "$var.job.outage_request", "stdout_json.sys_id", x=2325, y=600),
        "42": jq("the escalation note", "$var.4f.result", "stdout_json.escalation", x=2350, y=600),
        "43": sni("updateIncident", "note the incident: escalated to a person",
                  {"sys_id": "$var.41.return_data", "sysparm_fields": "number", **nbi_body("$var.42.return_data")},
                  x=2375, y=600),
        "44": note("escalated", "the agent found no fix from the menu: the incident is escalated to a person", "error",
                   x=2400, y=600),
        # the card: one HTML page from the outage's own readings (outage_card), the engineer's note comes back
        "5a": set_key("the card: the fix", "$var.34.object", "fix", "$var.job.outage_fix", x=2400),
        "5b": set_key("the card: the incident number", "$var.5a.object", "number", "$var.4d.return_data", x=2410),
        "5d": set_key("the card: the deployment", "$var.5b.object", "plan", "$var.job.outage_plan", x=2420),
        "5e": run_code("the card's page (Python on the runner)", OUTAGE_CARD_CODE, "$var.5d.object", "outage_card",
                       x=2430),
        "5f": jq("the card's HTML", "$var.5e.result", "stdout_json.html", x=2440),
        "5c": task("InteractiveHTML", "WorkCenter", "approval: the outage card",
                   {"header": "Approve the fix for the AWS VPN outage", "body": "$var.5f.return_data", "variables": {},
                    "btn_success": "Approve and run the fix", "btn_failure": "Reject"},
                   {"export": "$var.job.card_decision"}, kind="manual", display="Work Center",
                   view="/work-center/task/InteractiveHTML", x=2450),
        "50": jq("the incident (rejected)", "$var.job.outage_request", "stdout_json.sys_id", x=2500, y=900),
        "58": run_code("the rejection note, with the engineer's (Python on the runner)", OUTAGE_REJECTED_CODE,
                       "$var.job.card_decision", "outage_rejected", x=2510, y=900),
        "59": jq("the rejection note", "$var.58.result", "stdout_json", x=2520, y=900),
        "51": sni("updateIncident", "note the incident: the fix was rejected",
                  {"sys_id": "$var.50.return_data", "sysparm_fields": "number", **nbi_body("$var.59.return_data")},
                  x=2525, y=900),
        "52": note("rejected", "the fix was rejected in Work Center: nothing was run, the incident stays open",
                   "outcome", x=2550, y=900),
        # the approved fix, exactly one
        "60": evaluate("fix: reset the IKE SA?", "job", "outage_fix", "stdout_json.fix", "==", "reset-ike", x=2550),
        "61": jq("lab-edge-push's params", "$var.job.outage_edge", "stdout_json.push", x=2600, y=-300),
        "62": set_key("reset-sa, not push", "$var.61.return_data", "action", "reset-sa", x=2625, y=-300),
        "63": run_service("reset the IKE SA (lab-edge-push reset-sa: clear, a fresh SA, no config)", "lab-edge-push",
                          "$var.62.object", "fix_result", x=2650, y=-300),
        "64": evaluate("fix: restart strongSwan?", "job", "outage_fix", "stdout_json.fix", "==", "restart-strongswan",
                       x=2600),
        "65": jq("aws-vpn-monitor restart's params", "$var.job.outage_plan", "stdout_json.restart", x=2650),
        "66": run_service("restart strongSwan (aws-vpn-monitor restart: the Lambda's fixed document)", "aws-vpn-monitor",
                          "$var.65.return_data", "fix_result", x=2675),
        "67": evaluate("fix: re-push the router block?", "job", "outage_fix", "stdout_json.fix", "==",
                       "repush-router-block", x=2650, y=300),
        "68": jq("lab-edge render's params", "$var.job.outage_edge", "stdout_json.render", x=2675, y=300),
        "69": run_service("render the block (lab-edge render: no device)", "lab-edge", "$var.68.return_data",
                          "render_result", x=2700, y=300),
        "6a": evaluate("rendered?", "69", "result", "result.return_code", "==", 0, x=2725, y=300),
        "6b": jq("the block's SHA-256", "$var.69.result", "result.stdout_json.sha256", x=2750, y=300),
        "6c": jq("lab-edge-push's params", "$var.job.outage_edge", "stdout_json.push", x=2775, y=300),
        "6d": set_key("push params: the SHA-256", "$var.6c.return_data", "sha256", "$var.6b.return_data", x=2800, y=300),
        "6e": run_service("re-push the block (lab-edge-push: revert timer, a fresh SA, save)", "lab-edge-push",
                          "$var.6d.object", "fix_result", x=2825, y=300),
        "53": evaluate("reset-sa exited 0?", "63", "result", "result.return_code", "==", 0, x=2675, y=-300),
        "55": evaluate("restart exited 0?", "66", "result", "result.return_code", "==", 0, x=2700),
        "57": evaluate("the push exited 0?", "6e", "result", "result.return_code", "==", 0, x=2850, y=300),
        "54": note("the fix did not complete", "the approved fix exited non-zero or could not run (fix_result): the "
                   "tunnel is read again", "fix_note", x=2875),
        # the tunnel again, then the incident
        "70": jq("lab-edge verify's params", "$var.job.outage_edge", "stdout_json.lab_edge", x=2900),
        "80": note("the tunnel could not be read", "lab-edge could not read the tunnel after the fix (check_result)",
                   "check_note", x=2940, y=-300),
        "72": jq("the fix that ran", "$var.job.outage_fix", "stdout_json.fix", x=2950),
        "73": set_key("the result: which fix", {}, "fix", "$var.72.return_data", x=2975),
        "74": set_key("the result: its answer", "$var.73.object", "answer", "$var.job.fix_result", x=3000),
        "75": set_key("the result: the tunnel now", "$var.74.object", "check", "$var.job.check_result", x=3025),
        "81": set_key("the result: the engineer's note", "$var.75.object", "decision", "$var.job.card_decision",
                      x=3037),
        "76": run_code("fixed? (Python on the runner)", OUTAGE_RESULT_CODE, "$var.81.object", "outage_result", x=3050),
        "77": jq("the incident", "$var.job.outage_request", "stdout_json.sys_id", x=3075),
        "78": evaluate("fixed?", "76", "result", "stdout_json.fixed", "==", True, x=3100),
        "79": jq("the resolution", "$var.76.result", "stdout_json.resolve", x=3125),
        "7a": sni("updateIncident", "resolve the incident (Solution provided)",
                  {"sys_id": "$var.77.return_data", "sysparm_fields": "number,state", **nbi_body("$var.79.return_data")},
                  x=3150),
        "7b": jq("the outcome", "$var.76.result", "stdout_json.message", x=3175, to_job="outcome"),
        "7c": jq("the not-fixed note", "$var.76.result", "stdout_json.note", x=3125, y=-300),
        "7d": sni("updateIncident", "note the incident: still not up after the fix",
                  {"sys_id": "$var.77.return_data", "sysparm_fields": "number", **nbi_body("$var.7c.return_data")},
                  x=3150, y=-300),
        "7e": jq("why it needs a look", "$var.76.result", "stdout_json.message", x=3175, y=-300, to_job="error"),
        # failures: the reason, then one Work Center task
        "8a": note("the deployment could not be read", f"terraform-run outputs could not be read (outage_outputs): "
                   f"{nothing}", "error", x=200, y=-600),
        "8b": note("a step could not run", f"the Gateway could not run a step before the evidence (see outage_plan, "
                   f"outage_edge): {nothing}", "error", x=500, y=-600),
        "8c": note("the evidence could not be read", f"Verify's checks could not run or gave no verdict (see "
                   f"lab_edge_result, monitor_result): {nothing}", "error", x=1700, y=-900),
        "8d": note("ServiceNow did not answer", f"ServiceNow did not answer (outage_open, incident_created): "
                   f"{nothing}; the outage has no incident", "error", x=1950, y=-600),
        "8e": note("a step after the incident could not run", "a step after the incident was opened could not run "
                   "(see outage_request, agent_result, outage_fix, fix_result): check the job and the incident",
                   "error", x=2300, y=-900),
        "f0": view("Work Center task", "Diagnose AWS VPN Outage needs a look", "$var.job.error", "$var.job.outage_fix",
                   "Seen", "Seen", x=3300, y=0),
    }
    vtasks, vtr, first = verify_section("outage_edge", passed="30", failed="31", broken="8c", judge_broken="8c",
                                        x=1100)
    tasks.update(vtasks)
    # the tunnel after the fix (2026-10-05 run 2: the router needs ~2 minutes to rebuild IKE after a strongSwan restart,
    # and one immediate read said down): up to four reads over about three minutes, unrolled (no cycle), the first read
    # that is up goes to the result; the last read, up or not, is check_result either way
    recheck_tr = {}
    for i, (delay, read, up, secs) in enumerate(OUTAGE_RECHECKS):
        nxt = OUTAGE_RECHECKS[i + 1][0] if i + 1 < len(OUTAGE_RECHECKS) else "72"
        x = 2900 + i * 30
        tasks[delay] = task("delay", "WorkFlowEngine", f"wait {secs} s for the tunnel (read {i + 1})", {"time": secs},
                            {"time_in_milliseconds": None}, kind="operation", display="WorkFlowEngine", x=x)
        tasks[read] = run_service(f"the tunnel now, read {i + 1} (lab-edge verify: show commands, one ping)", "lab-edge",
                                  "$var.70.return_data", "check_result", x=x + 10)
        tasks[up] = evaluate(f"the tunnel up? (read {i + 1})", read, "result", "result.return_code", "==", 0, x=x + 20)
        tasks[up]["variables"]["incoming"]["evaluation_groups"][0]["evaluations"] += [
            {"query": f"result.stdout_json.{signal}", "operand_1": {"variable": "result", "task": read},
             "operator": "==", "operand_2": {"variable": "up", "task": "static"}} for signal in ("router", "data_plane")]
        recheck_tr[delay] = _edge(**{read: ok})
        # a read that fails is the next attempt's to retry; after the last one, the result says not read
        recheck_tr[read] = _edge(**{up: ok, (nxt if nxt != "72" else "80"): err})
        recheck_tr[up] = _edge(**{"72": ok, (nxt if nxt != "72" else "ac"): fail})
    tasks["ac"] = note("still not up after the last read", "the tunnel was still not up after the last read (about "
                       f"{sum(r[3] for r in OUTAGE_RECHECKS)} s after the fix): the result says so", "check_note",
                       x=3020, y=300)
    recheck_tr["ac"] = _edge(**{"72": ok})
    tr = {
        "workflow_start": _edge(**{"01": ok}),
        "01": _edge(**{"02": ok}), "02": _edge(**{"03": ok}), "03": _edge(**{"04": ok}), "04": _edge(**{"1a": ok}),
        "1a": _edge(**{"1b": ok}),
        "1b": _edge(**{"1c": ok, "8a": err}),
        "1c": _edge(**{"1d": ok, "8a": fail}),
        "1d": _edge(**{"1e": ok}),
        "1e": _edge(**{"1f": ok}),
        "1f": _edge(**{"10": ok, "8b": err}),
        "10": _edge(**{"11": ok, "19": fail}),
        "19": _edge(**{"f0": ok, "8b": err}),
        "11": _edge(**{"13": ok, "12": fail}),
        "12": _edge(**{"workflow_end": ok, "8b": err}),
        "13": _edge(**{"14": ok, "8b": err}),
        "14": _edge(**{"15": ok, "8b": err}),
        "15": _edge(**{"2a": ok, "8b": fail}),
        "2a": _edge(**{"2b": ok, "8b": err}),
        "2b": _edge(**{"2c": ok, "8d": err}),
        "2c": _edge(**{"2d": ok}),
        "2d": _edge(**{"2e": ok}),
        "2e": _edge(**{"2f": ok, "8b": err}),
        "2f": _edge(**{"20": ok, "3a": fail}),
        "20": _edge(**{"21": ok, "8d": err}),
        "21": _edge(**{"22": ok, "8d": err}),
        "22": _edge(**{"23": ok, "8d": err}),
        "23": _edge(**{"workflow_end": ok}),
        "3a": _edge(**{"3b": ok}),
        # an alarm that cannot be read is evidence too ("not read"): Verify's checks go on
        "3b": _edge(**{"3e": ok, "3f": err}),
        "3e": _edge(**{first: ok, "3f": fail}),
        "3f": _edge(**{first: ok}),
        **vtr,
        "30": _edge(**{"workflow_end": ok}),
        "31": _edge(**{"32": ok}), "32": _edge(**{"33": ok}), "33": _edge(**{"34": ok}), "34": _edge(**{"35": ok}),
        "35": _edge(**{"36": ok, "8e": err}),
        "36": _edge(**{"37": ok, "8e": err}),
        "37": _edge(**{"38": ok, "8d": err}),
        "38": _edge(**{"39": ok}),
        "39": _edge(**{"3c": ok}),
        "3c": _edge(**{"3d": ok, "8e": err}),
        "3d": _edge(**{"4a": ok, "8d": fail}),
        "4a": _edge(**{"4b": ok, "8e": err}),
        # an agent that fails is an answer too: no fix from it means escalate
        "4b": _edge(**{"4c": ok, "45": err}),
        "45": _edge(**{"4c": ok}),
        "4c": _edge(**{"4d": ok}),
        "4d": _edge(**{"4e": ok, "8e": err}),
        "4e": _edge(**{"4f": ok}),
        "4f": _edge(**{"40": ok, "8e": err}),
        "40": _edge(**{"5a": ok, "41": fail}),
        "41": _edge(**{"42": ok, "8e": err}),
        "42": _edge(**{"43": ok, "8e": err}),
        "43": _edge(**{"44": ok, "8e": err}),
        "44": _edge(**{"f0": ok}),
        "5a": _edge(**{"5b": ok}), "5b": _edge(**{"5d": ok}), "5d": _edge(**{"5e": ok}),
        "5e": _edge(**{"5f": ok, "8e": err}),
        "5f": _edge(**{"5c": ok, "8e": err}),
        "5c": _edge(**{"60": ok, "50": fail}),
        "50": _edge(**{"58": ok, "8e": err}),
        "58": _edge(**{"59": ok, "8e": err}),
        "59": _edge(**{"51": ok, "8e": err}),
        "51": _edge(**{"52": ok, "8e": err}),
        "52": _edge(**{"workflow_end": ok}),
        "60": _edge(**{"61": ok, "64": fail}),
        "61": _edge(**{"62": ok, "8e": err}),
        "62": _edge(**{"63": ok}),
        "63": _edge(**{"53": ok, "54": err}),
        "53": _edge(**{"70": ok, "54": fail}),
        "54": _edge(**{"70": ok}),
        "64": _edge(**{"65": ok, "67": fail}),
        "65": _edge(**{"66": ok, "8e": err}),
        "66": _edge(**{"55": ok, "54": err}),
        "55": _edge(**{"70": ok, "54": fail}),
        "67": _edge(**{"68": ok, "8e": fail}),
        "68": _edge(**{"69": ok, "8e": err}),
        "69": _edge(**{"6a": ok, "8e": err}),
        "6a": _edge(**{"6b": ok, "8e": fail}),
        "6b": _edge(**{"6c": ok, "8e": err}),
        "6c": _edge(**{"6d": ok, "8e": err}),
        "6d": _edge(**{"6e": ok}),
        "6e": _edge(**{"57": ok, "54": err}),
        "57": _edge(**{"70": ok, "54": fail}),
        "70": _edge(**{OUTAGE_RECHECKS[0][0]: ok, "8e": err}),
        "80": _edge(**{"72": ok}),
        **recheck_tr,
        "72": _edge(**{"73": ok, "8e": err}),
        "73": _edge(**{"74": ok}), "74": _edge(**{"75": ok}),
        "75": _edge(**{"81": ok}), "81": _edge(**{"76": ok}),
        "76": _edge(**{"77": ok, "8e": err}),
        "77": _edge(**{"78": ok, "8e": err}),
        "78": _edge(**{"79": ok, "7c": fail}),
        "79": _edge(**{"7a": ok, "8e": err}),
        "7a": _edge(**{"7b": ok, "8e": err}),
        "7b": _edge(**{"workflow_end": ok}),
        "7c": _edge(**{"7d": ok, "8e": err}),
        "7d": _edge(**{"7e": ok, "8e": err}),
        "7e": _edge(**{"f0": ok}),
        **{n: _edge(**{"f0": ok}) for n in ("8a", "8b", "8c", "8d", "8e")},
        "f0": _edge(**{"workflow_end": ok}),
    }
    return workflow(
        WF["diagnose_aws_vpn_outage"],
        "The AWS VPN's outage loop (R6 + A3, ADR 0072): started by the tunnel-down alert, it opens one ServiceNow "
        "incident with the evidence, lets the tunnel-diagnostics agent pick one fix from a fixed menu, and runs that "
        "fix only after a Work Center approval - then reads the tunnel again and resolves the incident",
        {k: {"type": "string", "required": True} for k in ("alertname", "device", "interface", "starts_at", "fingerprint")},
        tasks,
        tr,
        {
            "outcome": {"type": "string"}, "error": {"type": "string"},
            "outage_outputs": {"type": "object"}, "outage_plan": {"type": "object"}, "outage_edge": {"type": "object"},
            "outage_open": {"type": "object"}, "outage_dedupe": {"type": "object"}, "alarm_result": {"type": "object"},
            "judgement": {"type": "object"}, "lab_edge_result": {"type": "object"}, "monitor_result": {"type": "object"},
            "router_note": {"type": "string"}, "monitor_note": {"type": "string"},
            "outage_summary": {"type": "object"}, "incident_created": {"type": "object"},
            "outage_request": {"type": "object"}, "agent_result": {"type": "object"}, "outage_fix": {"type": "object"},
            "render_result": {"type": "object"}, "fix_result": {"type": "object"}, "check_result": {"type": "object"},
            "outage_result": {"type": "object"}, "outage_card": {"type": "object"},
            "card_decision": {"type": ["object", "null"]}, "outage_rejected": {"type": "object"},
        },
    )


# --- Diagnose Fabric BGP Outage (R10 + A6, owner decisions 2026-10-05; ADR 0073) -------------------------------------
# Prometheus sees a declared BGP session not Established (LabBgpSessionDown, k8s/observability/manifests/bgp-rules.yaml:
# gNMIc on vEOS, SNMP on the C8000v routers); Alertmanager groups both ends of the session by device pair and posts one
# alert to the relay, which starts this workflow. It finds an open incident for the pair and only notes it, or reads
# BOTH ends with fabric-bgp (cloud-devops-pipeline), opens ONE ServiceNow incident, runs the fabric-diagnostics agent,
# and shows the one fix the agent picked - on one end of the session - on an HTML card in Work Center. Only after
# approval does fabric-bgp run that fix and prove it Established; the loop reads the session again and resolves the
# incident, or notes it and opens a Work Center task. The sessions and both ends come from the topology at build time
# (topology/derive.py: the intent NetBox is seeded from); the devices' host keys from versions.yaml fabric_bgp.
FABRIC_FIXES = ("no-shut-neighbor", "no-shut-interface", "clear-session", "escalate")
FABRIC_AGENT_MARKER = "__AGENT_ID:fabric-diagnostics__"
FABRIC = VERSIONS["fabric_bgp"]
FABRIC_READ_TIMEOUT = "150"  # fabric-bgp read: the login and two reads (its own floor is 90 s)
FABRIC_FIX_TIMEOUT = "300"  # a fix: 90 s before, the wait, 60 s for the save (fabric-bgp's floor for a 90 s wait: 240)
FABRIC_RECHECKS = (("b0", "b1", "b2", 15), ("b3", "b4", "b5", 30), ("b6", "b7", "b8", 45), ("b9", "ba", "bb", 60))
# the interfaces fabric-bgp accepts for no-shut-interface (its IFNAME): the fabric /31s and the WAN links. A session over
# a Tunnel interface carries none, so a shut tunnel is the agent's escalate, never a card (a cdp follow-up)
FABRIC_IFNAME = re.compile(r"(Ethernet|GigabitEthernet)[0-9]{1,4}(/[0-9]{1,4}){0,2}")


def fabric_tables() -> tuple[dict, dict]:
    """(sessions, targets) from the topology. sessions: "<device>|<neighbor>" -> one end: device, neighbor, vrf,
    local_as, remote_as, peer, pair, far (the other end's key), and interface + via (the interface toward the peer and
    the peer's address on that link) when fabric-bgp may no-shut it. targets: device -> fabric-bgp's --target_json."""
    spec = importlib.util.spec_from_file_location("derive", HERE.parent.parent / "topology" / "derive.py")
    derive = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(derive)
    topo = derive.load_topology()
    addrs = {dev: [(r["name"], ipaddress.IPv4Interface(r["address"])) for r in rows if r.get("address")]
             for dev, rows in derive.interfaces(topo).items()}
    owner = {str(i.ip): dev for dev, rows in addrs.items() for _, i in rows}
    sessions, local = {}, {}
    for dev in topo["nodes"]:
        bgp = derive.device_context(topo, dev).get("bgp")
        for n in (bgp or {}).get("neighbors", []):
            nb, peer = ipaddress.IPv4Address(n["neighbor"]), owner[n["neighbor"]]
            direct = [(name, i) for name, i in addrs[dev] if nb in i.network and i.network.prefixlen < 32]
            if direct:
                (iface, mine), via = direct[0], str(nb)
                local[(dev, n["neighbor"])] = str(mine.ip)
            else:  # a loopback session (EVPN): the link to the peer, and the session's source is the router id
                links = [(name, str(pi.ip)) for name, i in addrs[dev] for _, pi in addrs[peer]
                         if i.network == pi.network and i.ip != pi.ip and i.network.prefixlen >= 30]
                (iface, via) = links[0]
                local[(dev, n["neighbor"])] = bgp["router_id"]
            end = {"device": dev, "neighbor": n["neighbor"], "vrf": n.get("vrf") or "default", "local_as": bgp["asn"],
                   "remote_as": n["remote_as"], "peer": peer, "pair": "--".join(sorted((dev, peer)))}
            if FABRIC_IFNAME.fullmatch(iface):
                end.update(interface=iface, via=via)
            sessions[f"{dev}|{n['neighbor']}"] = end
    for (dev, neighbor), source in local.items():
        sessions[f"{dev}|{neighbor}"]["far"] = f"{sessions[f'{dev}|{neighbor}']['peer']}|{source}"
    platforms = {"veos": "eos", "c8000v": "ios-xe"}
    targets = {dev: {"name": dev, "mgmt_host": topo["nodes"][dev]["mgmt_ip"].split("/")[0],
                     "platform": platforms[topo["nodes"][dev]["platform"]], "host_keys": [FABRIC["host_keys"][dev]]}
               for dev in sorted({e["device"] for e in sessions.values()})}
    return sessions, targets


FABRIC_SESSIONS, FABRIC_TARGETS = fabric_tables()


def fabric_service_session(end: dict) -> dict:
    """One end as fabric-bgp's --session_json takes it."""
    return {k: end[k] for k in ("neighbor", "vrf", "local_as", "remote_as", "interface", "via") if k in end}


def fabric_plan(d: dict, sessions: dict, targets: dict, fabric: dict) -> dict:
    """What the loop may act on: the alerting end (`device`, `neighbor`, `vrf`, `peer`, the relay's checked labels)
    must be a declared session; its far end, both ends' fabric-bgp read params and the incident's correlation_id
    (one per device pair). Pure: runCode runs this source on the Gateway with the tables written in."""
    key = f"{d.get('device')}|{d.get('neighbor')}"
    near = sessions.get(key)
    if not near or near["vrf"] != d.get("vrf") or near["peer"] != d.get("peer"):
        return {"ok": False, "message": f"{key} (VRF {d.get('vrf')}, peer {d.get('peer')}) is not a session the "
                                        "topology declares: nothing was changed"}
    far = sessions[near["far"]]

    def read(end: dict) -> dict:
        return {"action": "read", "target_json": json.dumps(targets[end["device"]]),
                "session_json": json.dumps(fabric_service_session(end)), "username": fabric["username"],
                "timeout": FABRIC_READ_TIMEOUT}

    correlation = f"bgp-{near['pair']}"
    return {"ok": True, "pair": near["pair"], "correlation_id": correlation,
            # open = New, In Progress or On Hold (R6: a Resolved incident stays active=true until ServiceNow closes it)
            "open_query": f"correlation_id={correlation}^stateIN1,2,3",
            "near": near, "far": far, "near_read": read(near), "far_read": read(far),
            "platforms": {end["device"]: targets[end["device"]]["platform"] for end in (near, far)}}


def fabric_reading(envelope) -> dict:
    """A fabric-bgp read's answer (runService's envelope) as {read, state, admin_shutdown, matches_intent, local_as,
    remote_as, interface}; read False when it did not answer."""
    result = ((envelope or {}).get("result")) or {}
    out = result.get("stdout_json") or {}
    session = out.get("session") or {}
    if result.get("return_code") != 0 or not session:
        return {"read": False, "state": "not read", "admin_shutdown": None, "matches_intent": None,
                "local_as": None, "remote_as": None, "interface": None}
    return {"read": True, "state": session.get("state"), "admin_shutdown": session.get("admin_shutdown"),
            "matches_intent": session.get("matches_intent"), "local_as": session.get("local_as"),
            "remote_as": session.get("remote_as"), "interface": out.get("interface")}


def fabric_commands(end: dict, platform: str) -> list[str]:
    """The agent's confirming reads of one end through Run Show Command on a Device (its gate: show + one pipe)."""
    if platform == "eos":
        vrf = "" if end["vrf"] == "default" else f" vrf {end['vrf']}"
        cmds = [f"show ip bgp neighbors {end['neighbor']}{vrf} | include BGP state"]
    else:
        table = "show ip bgp summary" if end["vrf"] == "default" else f"show ip bgp vpnv4 vrf {end['vrf']} summary"
        cmds = [f"{table} | include {end['neighbor']}"]
    if end.get("interface"):
        cmds.append(f"show interfaces {end['interface']} | include line protocol")
    return cmds


def fabric_summary(d: dict) -> dict:
    """The incident from both ends' fabric-bgp reads (`near`, `far`, runService envelopes), the plan (`plan`, its
    runCode result) and the alert (`starts_at`): the body for createIncident, and the agent's evidence with the exact
    reads it should confirm with."""
    plan = (d.get("plan") or {}).get("stdout_json") or {}
    near, far = plan.get("near") or {}, plan.get("far") or {}

    def end_text(end: dict, reading: dict) -> str:
        if not reading["read"]:
            return f"{end.get('device')} could not be read"
        words = [f"{end['device']} -> {end['neighbor']} (VRF {end['vrf']}) reads {reading['state']}"]
        if reading["admin_shutdown"]:
            words.append("the neighbor is administratively shut down")
        if reading["matches_intent"] is False:
            words.append(f"the device runs AS {reading['local_as']} -> {reading['remote_as']}, NetBox intends "
                         f"{end['local_as']} -> {end['remote_as']}")
        iface = reading["interface"] or {}
        if iface:
            admin = {True: "up", False: "administratively down"}.get(iface.get("admin_up"), "not read")
            line = {True: "up", False: "down"}.get(iface.get("oper_up"), "not read")
            words.append(f"{iface.get('name')} toward {end['peer']} is {admin}, line protocol {line}")
        return "; ".join(words)

    readings = (fabric_reading(d.get("near")), fabric_reading(d.get("far")))
    evidence = f"{end_text(near, readings[0])}. {end_text(far, readings[1])}."
    platforms = plan.get("platforms") or {}

    def intent(end: dict) -> str:
        vrf_ = "" if end.get("vrf") in (None, "default") else f" in VRF {end['vrf']}"
        link = f", toward {end.get('peer')} over {end['interface']}" if end.get("interface") else ""
        return (f"{end.get('device')} ({platforms.get(end.get('device'), 'eos')}) AS {end.get('local_as')} peers with "
                f"{end.get('neighbor')} AS {end.get('remote_as')}{vrf_}{link}")

    # ADR 0075 (owner 2026-10-06): the agent gets the alert and NetBox's intent - never what the reads concluded
    facts = (f"Prometheus raised LabBgpSessionDown at {d.get('starts_at')} for {near.get('device')} -> "
             f"{near.get('neighbor')} (VRF {near.get('vrf')}), peer {near.get('peer')}. NetBox intends: "
             f"{intent(near)}; {intent(far)}. Nothing has been read from the devices for you: run the reads you need.")
    reads = "; ".join(f"on {end['device']} " + " and then ".join(f'"{c}"' for c in fabric_commands(end, platforms.get(
        end["device"], "eos"))) for end in (near, far) if end)
    device, peer, pair = near.get("device"), near.get("peer"), plan.get("pair")
    vrf = "" if near.get("vrf") in (None, "default") else f", VRF {near.get('vrf')}"
    return {
        "evidence": f"{evidence} Confirm with these reads: {reads}.",
        "facts": facts,
        "readings": {"near": readings[0], "far": readings[1]},
        "incident": {
            "short_description": f"BGP session down: {device} <-> {peer} ({near.get('neighbor')}{vrf})",
            "description": (f"Prometheus alert LabBgpSessionDown since {d.get('starts_at')}. {evidence} Opened by "
                            "Itential's Diagnose Fabric BGP Outage; the fabric-diagnostics agent notes its diagnosis "
                            "here and the fix runs only after a Work Center approval."),
            "correlation_id": f"bgp-{pair}",
            "correlation_display": "Itential fabric BGP outage loop",
            "urgency": "2", "impact": "2", "category": "network",
        },
    }


def fabric_agreement(fix: str, device, plan: dict, near, far) -> dict:
    """Whether the workflow's own fabric-bgp reads (`near`, `far` envelopes - the agent never sees them) back the
    agent's answer (ADR 0075): a fix needs its own condition on the end it names; escalate is backed unless an end
    shows a condition a menu fix covers. Shown on the card; a disagreement is red but never blocks (owner)."""
    ends = {(plan.get("near") or {}).get("device"): fabric_reading(near), (plan.get("far") or {}).get("device"):
            fabric_reading(far)}
    if not all(r["read"] for r in ends.values()):
        return {"agree": None, "why": "an end could not be read: no independent check"}
    shut = [dev for dev, r in ends.items() if r["admin_shutdown"]]
    down = [dev for dev, r in ends.items() if (r["interface"] or {}).get("admin_up") is False]
    drift = [dev for dev, r in ends.items() if r["matches_intent"] is False]
    if fix == "no-shut-neighbor":
        ok = device in shut
        why = f"fabric-bgp reads {device}'s neighbor shut down" if ok else f"fabric-bgp does not read {device}'s neighbor shut"
    elif fix == "no-shut-interface":
        ok = device in down
        why = f"fabric-bgp reads {device}'s interface admin down" if ok else f"fabric-bgp reads {device}'s interface not admin down"
    elif fix == "clear-session":
        ok = not (shut or down or drift)
        why = ("fabric-bgp reads nothing shut and both ends as NetBox intends" if ok else
               "fabric-bgp reads " + ", ".join(f"{dev}: " + ("neighbor shut" if dev in shut else "interface admin down"
                                                              if dev in down else "AS differs from NetBox")
                                               for dev in dict.fromkeys(shut + down + drift)))
    else:
        ok = not (shut or down)
        why = ("fabric-bgp reads no condition a menu fix covers" + (": the AS differs from NetBox on " + ", ".join(drift)
                                                                     if drift else "") if ok else
               "fabric-bgp reads a fixable condition: " + ", ".join(f"{dev}: neighbor shut" for dev in shut)
               + ("; " if shut and down else "") + ", ".join(f"{dev}: interface admin down" for dev in down))
    return {"agree": ok, "why": why}


def fabric_fix(d: dict) -> dict:
    """The fix the agent picked (`agent`: runAgent's result), held to the menu AND to the session's two ends (`plan`):
    the last line of its answer must be JSON naming one of FABRIC_FIXES and, for a fix, `device` one of the two ends;
    no-shut-interface needs an interface fabric-bgp may change. Anything else is escalate. A fix carries fabric-bgp's
    params for exactly that end."""
    agent = d.get("agent") or {}
    plan = (d.get("plan") or {}).get("stdout_json") or {}
    text = str(agent.get("lastMessage") or "")
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    answer: dict = {}
    if agent.get("sessionStatus") == "COMPLETE" and lines:
        try:
            parsed = json.loads(lines[-1])
            answer = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            answer = {}
    ends = {e.get("device"): (e, plan.get(f"{side}_read")) for side, e in (("near", plan.get("near") or {}),
                                                                          ("far", plan.get("far") or {}))}
    fix = answer.get("fix") if answer.get("fix") in FABRIC_FIXES else "escalate"
    why = (str(answer.get("cause") or "unknown")[:80] if answer else
           ("the agent did not finish" if agent.get("sessionStatus") != "COMPLETE" else "the agent's answer was not the JSON line"))
    device = answer.get("device")
    if fix != "escalate" and device not in ends:
        fix, why = "escalate", f"the agent named {str(device)[:40]!r}, not one end of the session"
    if fix == "no-shut-interface" and "interface" not in (ends.get(device, ({}, None))[0]):
        fix, why = "escalate", f"{device}'s link toward the peer is not an interface fabric-bgp may change"
    evidence = str(answer.get("evidence") or "")[:400]

    def capped(key: str, most: int) -> list:
        items = answer.get(key) if isinstance(answer.get(key), list) else []
        return [str(x)[:200] for x in items if isinstance(x, (str, int, float))][:most]

    # ADR 0075: the agent's own account of how it got there, for the card - capped, escaped where it is drawn
    findings, ruled_out = capped("findings", 6), capped("ruled_out", 4)
    kb = str(answer.get("kb") or "none")[:64]
    agreement = fabric_agreement(fix, device if fix != "escalate" else None, plan, d.get("near"), d.get("far"))
    check = {True: "agrees", False: "DISAGREES", None: "is not available"}[agreement["agree"]]
    out = {"fix": fix, "device": device if fix != "escalate" else None, "cause": why, "evidence": evidence,
           "findings": findings, "ruled_out": ruled_out, "kb": kb, "agreement": agreement,
           "card": fix != "escalate", "session": str(agent.get("sessionId") or "")[:64],
           "escalation": {"work_notes": (f"Escalated to a person: no fix from the menu applies ({why}). {evidence} "
                                         f"Itential's independent check {check}: {agreement['why']}. KB: {kb}").strip()}}
    if fix != "escalate":
        end, read = ends[device]
        out["end"] = end
        out["params"] = {**read, "action": fix, "wait_seconds": str(FABRIC_WAIT), "timeout": FABRIC_FIX_TIMEOUT}
        # the card's exact lines: fabric-bgp plan (no device, no login: it takes no username)
        out["plan_params"] = {"action": "plan", "target_json": read["target_json"], "session_json": read["session_json"],
                              "plan_for": fix, "timeout": "60"}
    return out


def fabric_result(d: dict) -> dict:
    """After the approved fix (`fix`, fabric_fix's runCode result; its fabric-bgp envelope under `answer`) and the
    session read again at the alerting end (`check`, a fabric-bgp read envelope): fixed only when that read says
    Established. Resolves or notes the incident; the engineer's note (`decision`) goes in either way."""
    fix_out = (d.get("fix") or {}).get("stdout_json") or {}
    fix, device = fix_out.get("fix"), fix_out.get("device")
    answer = (((d.get("answer") or {}).get("result")) or {}).get("stdout_json") or {}
    check = fabric_reading(d.get("check"))
    up = check["state"] == "Established"
    proved = "it proved Established" if answer.get("established") else (
        "it did not complete: " + str(answer.get("error") or "no answer"))
    saved = " and saved the configuration" if answer.get("saved") else ""
    note = decision_note(d.get("decision"))
    said = (f"Approved in Work Center ({'note: ' + note if note else 'no note'}). Itential ran the approved fix {fix} on "
            f"{device} ({proved}{saved})")
    if up:
        message = f"{said}; the session reads Established again."
        return {"fixed": True, "message": message,
                "resolve": {"state": "6", "close_code": "Solution provided", "close_notes": message,
                            "work_notes": message}}
    message = f"{said}; the session still reads {check['state']}. The incident stays open for a person."
    return {"fixed": False, "message": message, "note": {"work_notes": message}}


# each menu fix as the card says it: title, what happens, scope (value, note), the three promises. {d} is the device the
# fix runs on, {p} its peer, {i} the interface toward the peer
FABRIC_FIX_COPY = {
    "no-shut-neighbor": ("Bring the BGP neighbor back on {d}",
                         "Itential removes the neighbor's shutdown on {d}, waits for the session to reach Established, "
                         "then saves the configuration.",
                         ("1 BGP neighbor", "one line of configuration"),
                         ("Sends one line, nothing typed by the agent",
                          "Refused unless the neighbor is shut and {d} matches NetBox",
                          "Saved only once the session is Established")),
    "no-shut-interface": ("Bring {i} back up on {d}",
                          "Itential removes the shutdown on {i}, the interface toward {p}, waits for the session to "
                          "reach Established, then saves the configuration.",
                          ("1 interface", "one line of configuration"),
                          ("Sends one line, nothing typed by the agent",
                           "Refused unless {i} is shut and its address faces {p}",
                           "Saved only once the session is Established")),
    "clear-session": ("Reset the BGP session on {d}",
                      "Itential clears this one session on {d} so both ends rebuild it, then waits for Established.",
                      ("1 BGP session", "no configuration change"),
                      ("Runs one clear command for this neighbor only",
                       "Refused if the neighbor or its interface is shut: that needs another fix",
                       "Proven afterwards: Established resolves the incident")),
}
# the exact lines a card shows (R10 PR C, owner 2026-10-06): fabric-bgp's own `plan`, inside the blue fix panel
CARD_LINES_CSS = """
.oc .lines { grid-column: 1 / -1; margin-top: 14px; background: rgba(6, 16, 30, .78); border-radius: 10px;
  padding: 10px 14px 12px; }
.oc .lines .cap { font-size: 12.5px; color: #9FB0C8; margin-bottom: 6px; }
.oc .lines pre { margin: 0; font-family: var(--mono); font-size: 14px; line-height: 1.55; color: #fff;
  white-space: pre-wrap; }
"""
CARD_LINES_MODE = {
    "configure": "configuration mode; saved only once the session is Established",
    "exec": "one command; no configuration change",
    "configure session": "a configuration session under a commit timer; never saved",
}


def card_lines(envelope) -> str:
    """fabric-bgp plan's answer (runService's envelope) as the card's lines block; every value escaped."""
    e = html.escape
    out = ((((envelope or {}).get("result")) or {}).get("stdout_json") or {}).get("plan") or {}
    lines = [str(line) for line in out.get("lines") or []]
    if not lines:
        return '<div class="lines"><p class="cap">The exact lines could not be read</p></div>'
    mode = CARD_LINES_MODE.get(out.get("mode"), str(out.get("mode") or ""))
    block = (f'<div class="lines"><p class="cap">Exactly what runs on {e(str(out.get("device") or ""))} - {e(mode)}</p>'
             f'<pre>{e(chr(10).join(lines))}</pre>')
    # a drill that must reset the session once its change is committed (the MD5 drill, cdp #43) says so: `then`
    then = [str(line) for line in out.get("then") or []]
    if then:
        block += f'<p class="cap">then, once, as one command - EOS applies a BGP password to new connections only</p><pre>{e(chr(10).join(then))}</pre>'
    return block + "</div>"


# the agent's cause tags (itential/agents/fabric-diagnostics.yaml, its fixed list) as the card's headline
FABRIC_CAUSES = {
    "neighbor-shut": "The BGP neighbor {n} is shut down on {d}",
    "interface-shut": "{i} toward {p} is shut down on {d}",
    "stuck-session": "The session is stuck with both links up",
}


def fabric_card_drawing(left: dict, right: dict, link: tuple) -> str:
    """The session as an engineer draws it: two devices with their AS, the link between them. Each end is {name, asn,
    state (colour, label), iface (colour, label)}; link is (colour, label); a link that is not up is drawn broken."""
    e = html.escape
    l_col, l_label = link
    broken = l_col != CARD_GREEN
    pipe = "#4A2430" if broken else "#15402F"
    fracture = (f'<g class="f"><path d="M346 98l12 14-10 6 14 22" stroke="{CARD_RED}" stroke-width="8" fill="none" '
                f'filter="url(#g)" opacity=".6"/><path d="M346 98l12 14-10 6 14 22" stroke="{CARD_RED}" '
                'stroke-width="3.2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></g>') if broken else ""
    dash = ' stroke-dasharray="7 6"' if broken else ""
    pipes = "M222 118H336M384 118H498" if broken else "M222 118H498"

    def box(x: int, end: dict, align: str) -> str:
        s_col, s_label = end["state"]
        i_col, i_label = end["iface"]
        return (f'<rect x="{x}" y="72" width="176" height="92" rx="12" fill="{CARD_DEEP}" stroke="{s_col}" stroke-width="2.5"/>'
                f'<circle cx="{x + 30}" cy="104" r="14" fill="{CARD_DEEP}" stroke="{s_col}" stroke-width="2"/>'
                f'<path d="M{x + 22} 100h16m-4-4 4 4-4 4M{x + 38} 108H{x + 22}m4-4-4 4 4 4" stroke="{s_col}" '
                'stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
                f'<text x="{x + 52}" y="100" font-size="15" font-weight="700" fill="{CARD_TEXT}">{e(end["name"])}</text>'
                f'<text x="{x + 52}" y="118" font-size="12" fill="{CARD_MUTED}">AS {e(str(end["asn"]))}</text>'
                f'<text x="{x + 16}" y="148" font-size="12.5" fill="{s_col}">{e(s_label)}</text>'
                f'<text x="{x + (0 if align == "start" else 176)}" y="186" font-size="12" fill="{i_col}" '
                f'text-anchor="{align}">{e(i_label)}</text>')

    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 720 230" width="720" height="230">'
        '<style>text{font-family:"Avenir Next","Segoe UI",system-ui,sans-serif}'
        '.f{transform-origin:360px 118px;animation:p 1.8s ease-in-out infinite}'
        '@keyframes p{0%,100%{opacity:1}50%{opacity:.35}}'
        '@media (prefers-reduced-motion:reduce){.f{animation:none}}</style>'
        '<defs><pattern id="d" width="16" height="16" patternUnits="userSpaceOnUse"><path d="M16 0H0V16" '
        'fill="none" stroke="#1A2740"/></pattern><filter id="g" x="-50%" y="-50%" width="200%" height="200%">'
        '<feGaussianBlur stdDeviation="5"/></filter></defs>'
        f'<rect x="10" y="22" width="700" height="190" rx="14" fill="url(#d)" stroke="{CARD_EDGE}"/>'
        f'<text x="28" y="48" font-size="13" fill="{CARD_MUTED}">the BGP session, as each end reads it</text>'
        f'<path d="{pipes}" stroke="{pipe}" stroke-width="14" stroke-linecap="round"/>'
        f'<path d="{pipes}" stroke="{l_col}" stroke-width="3"{dash} stroke-linecap="round"/>'
        f'{fracture}'
        f'<text x="360" y="92" font-size="14" font-weight="700" fill="{CARD_TEXT}" text-anchor="middle">BGP</text>'
        f'<text x="360" y="150" font-size="13" fill="{l_col}" text-anchor="middle">{e(l_label)}</text>'
        f'{box(46, left, "start")}{box(498, right, "end")}'
        '</svg>'
    )


def fabric_card(d: dict) -> dict:
    """The Work Center card as one HTML page: the plan (`plan`), both ends' reads (`near`, `far`: fabric-bgp envelopes),
    the fix (`fix`, fabric_fix's runCode result), the incident (`number`), the alert's start (`starts_at`) and the time
    it is drawn (`now`). Every value from outside is escaped. Pure: runCode runs this source on the Gateway."""
    e = html.escape
    kelp, buoy, flare, grey = CARD_GREEN, CARD_RED, CARD_AMBER, CARD_GREY
    plan = (d.get("plan") or {}).get("stdout_json") or {}
    near_end, far_end = plan.get("near") or {}, plan.get("far") or {}
    number = str(d.get("number") or "the incident")
    fix_out = (d.get("fix") or {}).get("stdout_json") or {}
    fix_end = fix_out.get("end") or {}
    fmt = {"d": str(fix_out.get("device") or ""), "p": str(fix_end.get("peer") or ""),
           "i": str(fix_end.get("interface") or ""), "n": str(fix_end.get("neighbor") or "")}
    title, what, (scope, scope_note), promises = FABRIC_FIX_COPY[fix_out.get("fix")]
    reads = {"near": fabric_reading(d.get("near")), "far": fabric_reading(d.get("far"))}
    device, peer = str(near_end.get("device") or "the device"), str(near_end.get("peer") or "its peer")
    start, now, elapsed = card_clock(d.get("starts_at"), d.get("now"))
    since = f"BGP down since {start:%H:%M} UTC" if start else "BGP down: start time not read"

    def state(reading: dict) -> tuple:
        if not reading["read"]:
            return grey, "not read"
        if reading["state"] == "Established":
            return kelp, "Established"
        if reading["admin_shutdown"]:
            return buoy, f"{reading['state']}: neighbor shut"
        return buoy, str(reading["state"])

    def iface(reading: dict, end: dict) -> tuple:
        i = reading["interface"] or {}
        if not end.get("interface"):
            return grey, "link: not checked"
        if i.get("admin_up") is False:
            return buoy, f"{end['interface']} admin down"
        if i.get("admin_up") is True:
            return (kelp, f"{end['interface']} up") if i.get("oper_up") else (flare, f"{end['interface']} line down")
        return grey, f"{end['interface']} not read"

    ends = []
    for side, end in (("near", near_end), ("far", far_end)):
        ends.append({"name": str(end.get("device") or "?"), "asn": end.get("local_as", "?"),
                     "state": state(reads[side]), "iface": iface(reads[side], end)})
    up = all(r["state"] == "Established" for r in reads.values())
    vrf = "" if near_end.get("vrf") in (None, "default") else f"VRF {near_end.get('vrf')}, "
    link = (kelp, f"{vrf}up") if up else (buoy, f"{vrf}down")
    drawing = outage_card_image(fabric_card_drawing(ends[0], ends[1], link),
                                f"{device} and {peer}: BGP {link[1]}", 720, 230)

    ledger = (
        (f"{device}'s view", ends[0]["state"][1], ends[0]["state"][0], f"to {near_end.get('neighbor', '?')}"),
        (f"{peer}'s view", ends[1]["state"][1], ends[1]["state"][0], f"to {far_end.get('neighbor', '?')}"),
        ("Links", "Up" if all(e_["iface"][0] == kelp for e_ in ends) else "Check", kelp if all(
            e_["iface"][0] == kelp for e_ in ends) else buoy, "the interfaces toward each other"),
        ("NetBox intent", *(("Matches", kelp) if all(r["matches_intent"] for r in reads.values()) else
                            ("Differs", buoy) if any(r["matches_intent"] is False for r in reads.values()) else
                            ("Not read", grey)), "AS numbers and the neighbor"),
    )
    ledger_html = "".join(
        f'<div class="reading" style="--c:{c}"><p class="src">{e(src)}</p><p class="val">{e(val)}</p>'
        f'<p class="why">{e(why)}</p></div>' for src, val, c, why in ledger)

    def yn(v, good=True):
        if v is None:
            return '<span class="unk">not read</span>'
        return f'<span class="{"ok" if bool(v) == good else "bad"}">{"yes" if v else "no"}</span>'

    def asn(have, want):
        if have is None:
            return '<span class="unk">not read</span>'
        return f'<span class="{"ok" if have == want else "bad"}">{e(str(have))} (NetBox {e(str(want))})</span>'

    rows = []
    for side, end in (("near", near_end), ("far", far_end)):
        r = reads[side]
        i = r["interface"] or {}
        rows += [("h", f"{end.get('device', '?')} -> {end.get('neighbor', '?')}, Itential's own read (the agent never saw it)"),
                 ("session state", f'<span class="{"ok" if r["state"] == "Established" else "bad" if r["read"] else "unk"}">'
                                   f'{e(str(r["state"]))}</span>'),
                 ("neighbor shut down", yn(r["admin_shutdown"], good=False)),
                 ("local AS", asn(r["local_as"], end.get("local_as"))),
                 ("remote AS", asn(r["remote_as"], end.get("remote_as")))]
        if end.get("interface"):
            rows.append((f"{end['interface']} admin up", yn(i.get("admin_up") if i else None)))
    term = "".join(f'<p class="h">{e(b)}</p>' if a == "h" else f'<p class="row"><span>{e(a)}</span>{b}</p>'
                   for a, b in rows)

    tag = str(fix_out.get("cause") or "no cause given")
    cause = FABRIC_CAUSES[tag].format(**fmt) if tag in FABRIC_CAUSES else tag
    session_id = str(fix_out.get("session") or "")
    session = session_id[:8]
    agent_by = "fabric-diagnostics agent" + (f", session {session}" if session else "")
    evidence = str(fix_out.get("evidence") or "")
    # ADR 0075: the agent's own account, the knowledge it cited, and Itential's independent check of it
    agreement = fix_out.get("agreement") or {}
    badge_c, badge_t = {True: (kelp, "Itential's independent check agrees"),
                        False: (buoy, "Itential's independent check DISAGREES"),
                        None: (grey, "No independent check")}[agreement.get("agree")]
    account = "".join(f"<li>{e(str(x))}</li>" for x in fix_out.get("findings") or [])
    ruled = "".join(f"<li>{e(str(x))}</li>" for x in fix_out.get("ruled_out") or [])
    kb = str(fix_out.get("kb") or "none")
    trace = (f'<a href="/agent-sessions/#/sessions/{e(session_id)}">every command it ran, and the raw output</a>'
             if session_id else "the agent's session")
    promises_html = "".join(f'<li class="promise">{e(p.format(**fmt))}</li>' for p in promises)
    sections = f"""<section class="panel topo" aria-labelledby="t-where"><h2 id="t-where">Where the session breaks</h2>{drawing}
<div class="ledger">{ledger_html}</div></section>
<section class="panel" aria-labelledby="t-when"><h2 id="t-when">What has happened so far</h2>
<ol class="steps">
<li><span class="who">Prometheus</span><b>Alert fired</b>{e(since[len("BGP down "):] if start else "BGP down")}</li>
<li><span class="who">ServiceNow</span><b>Incident opened</b>{e(number)}, with both ends' readings</li>
<li><span class="who">The agent</span><b>Cause worked out</b>from its own reads, one fix from the menu</li>
<li class="now"><span class="who">You</span><b>Approve or reject</b>nothing has run yet</li>
<li class="next"><span class="who">Itential</span><b>Fix and prove</b>Established, then resolves</li>
</ol></section>
<section class="panel diag" aria-labelledby="t-found"><div>
<h2 id="t-found">What the agent found</h2><span class="by"><i></i>{e(agent_by)}</span>
<p class="cause">{e(cause)}</p>
<p>{e(evidence) if evidence else "The agent's full diagnosis is in the incident's work notes."}</p>
{f'<p><b>What it read and found</b></p><ul class="account">{account}</ul>' if account else ""}
{f'<p><b>What it ruled out</b></p><ul class="account">{ruled}</ul>' if ruled else ""}
<p class="kb">Knowledge cited: <b>{e(kb)}</b></p>
<p class="check" style="border-left:4px solid {badge_c};padding:6px 10px;color:{badge_c}"><b>{e(badge_t)}</b>
{e(str(agreement.get("why") or ""))}</p>
<p class="trace">The agent chose its own reads: {trace}.</p></div>
<div class="term" role="group" aria-label="Itential's independent check: what fabric-bgp read at both ends">{term}</div></section>
<section class="panel action" aria-labelledby="t-fix"><div><p class="kicker">The proposed fix</p>
<h2 id="t-fix">{e(title.format(**fmt))}</h2><p class="what">{e(what.format(**fmt))}</p></div>
<p class="scope">Scope<b>{e(scope)}</b>{e(scope_note)}</p>
{card_lines(d.get("lines"))}
<ul class="promises">{promises_html}</ul></section>"""
    lede = (f"Itential opened <strong>{e(number)}</strong> and the fabric-diagnostics agent found the likely cause.\n"
            "One fix is ready, and nothing runs until you approve it.")
    foot = (f"Prepared by Itential's Diagnose Fabric BGP Outage workflow at {now:%H:%M} UTC, from live reads of\n"
            f"{e(device)} and {e(peer)}, checked against NetBox.")
    page = card_page(f"BGP outage: {number}", f"A BGP session is down: {device} and {peer}", lede, elapsed, since,
                     sections, number, foot, extra_css=CARD_LINES_CSS)
    return {"html": page}


# --- Break Fabric BGP (R10 PR C, owner decisions 2026-10-05 / 2026-10-06; ADR 0073 amendment) -----------------------
# The drill for the fabric BGP outage loop: one fault - a neighbor shutdown or the interface toward the peer shut - on a
# healthy, declared vEOS session, behind its own HTML approval card that shows the exact lines (fabric-bgp plan). The
# fault goes in under an EOS commit timer (fabric-bgp inject-*): unless confirmed, EOS rolls it back by itself after
# DRILL_REVERT_MINUTES. The workflow then waits for the outage loop to bring the session back, and confirms the drill
# session (cancels the timer) only then; a session still down at the last read is left to the timer, and read again
# after it to prove the rollback. It never runs the fix itself: that is the outage loop's, after its own approval.
DRILL_FAULTS = {"neighbor-shutdown": "inject-neighbor-shutdown", "interface-shutdown": "inject-interface-shutdown",
                "remote-as-mismatch": "inject-remote-as-mismatch", "md5-mismatch": "inject-md5-mismatch"}
# Faults the outage loop must NOT fix (owner 2026-10-06), with what the agent should find: the right answer is
# escalate; the commit timer, not a fix, ends the drill, and the read after it proves the rollback. (An agent that
# proposes clear-session for the MD5 fault is refused nothing; the loop proves the session did not come back.)
DRILL_ESCALATES = {"remote-as-mismatch": "config drift: a peer AS NetBox does not intend",
                   "md5-mismatch": "an authentication mismatch: a password on one end only"}
DRILL_REVERT_MINUTES = 20
DRILL_READS = (("d0", "d1", "d2", 300), ("d3", "d4", "d5", 300), ("d6", "d7", "d8", 300), ("d9", "da", "db", 180))
DRILL_AFTER_TIMER = DRILL_REVERT_MINUTES * 60 - sum(r[3] for r in DRILL_READS) + 120  # the timer, plus margin
DRILL_INJECT_TIMEOUT = "300"
DRILL_CONFIRM_TIMEOUT = "180"


def drill_plan(d: dict, sessions: dict, targets: dict, fabric: dict) -> dict:
    """What the drill may break: a declared session end on vEOS (`device`, `neighbor`), the fault (`fault`), both ends'
    read params, the plan / inject params and the confirm params (the drill session's name is added after the
    inject). Pure: runCode runs this source on the Gateway with the tables written in."""
    key = f"{d.get('device')}|{d.get('neighbor')}"
    near = sessions.get(key)
    action = DRILL_FAULTS.get(d.get("fault"))
    if not near or not action:
        return {"ok": False, "message": f"{key} is not a session the topology declares, or {d.get('fault')!r} is not a "
                                        "drill fault: nothing was changed"}
    target = targets[near["device"]]
    if target["platform"] != "eos":
        return {"ok": False, "message": f"{near['device']} is not vEOS: drills run on the fabric only (the IOS-XE revert "
                                        "timer needs archive): nothing was changed"}
    if action == "inject-interface-shutdown" and "interface" not in near:
        return {"ok": False, "message": f"{key} has no interface the drill may shut: nothing was changed"}
    far = sessions[near["far"]]

    def end_params(end: dict) -> dict:
        return {"target_json": json.dumps(targets[end["device"]]), "session_json": json.dumps(fabric_service_session(end))}

    near_p = end_params(near)
    read = lambda end: {**end_params(end), "action": "read", "username": fabric["username"],  # noqa: E731
                        "timeout": FABRIC_READ_TIMEOUT}
    minutes = str(DRILL_REVERT_MINUTES)
    return {"ok": True, "near": near, "far": far, "fault": d.get("fault"), "action": action,
            "revert_minutes": DRILL_REVERT_MINUTES, "near_read": read(near), "far_read": read(far),
            "plan_params": {**near_p, "action": "plan", "plan_for": action, "revert_minutes": minutes, "timeout": "60"},
            "inject": {**near_p, "action": action, "revert_minutes": minutes, "username": fabric["username"],
                       "timeout": DRILL_INJECT_TIMEOUT},
            "confirm": {**near_p, "action": "confirm-drill", "username": fabric["username"],
                        "timeout": DRILL_CONFIRM_TIMEOUT}}


DRILL_COPY = {
    "neighbor-shutdown": ("Shut the BGP neighbor {n} on {d}", "1 BGP neighbor"),
    "interface-shutdown": ("Shut {i} on {d}, the link toward {p}", "1 interface"),
    "remote-as-mismatch": ("Give {d} the wrong peer AS for {n} (config drift)", "1 BGP neighbor"),
    "md5-mismatch": ("Set a BGP password for {n} on {d} only (authentication mismatch)", "1 BGP neighbor"),
}


def drill_card(d: dict) -> dict:
    """The drill's approval card as one HTML page: the plan (`plan`), both ends' reads now (`near`, `far`), the exact
    lines (`lines`, fabric-bgp plan's envelope) and the time it is drawn (`now`). Every value from outside is escaped.
    Pure: runCode runs this source on the Gateway."""
    e = html.escape
    kelp, buoy, grey = CARD_GREEN, CARD_RED, CARD_GREY
    plan = (d.get("plan") or {}).get("stdout_json") or {}
    near, far = plan.get("near") or {}, plan.get("far") or {}
    fault = str(plan.get("fault") or "")
    title, scope = DRILL_COPY.get(fault, ("Break a BGP session", "1 session"))
    fmt = {"d": str(near.get("device") or ""), "n": str(near.get("neighbor") or ""), "i": str(near.get("interface") or ""),
           "p": str(near.get("peer") or "")}
    _, now, _ = card_clock(None, d.get("now"))
    # the commit timer starts when the inject commits, after the approval: the card cannot know that clock time
    minutes = int(plan.get("revert_minutes") or 20)
    reads = {"near": fabric_reading(d.get("near")), "far": fabric_reading(d.get("far"))}
    escalates = fault in DRILL_ESCALATES
    loop_step = (f'<li class="next"><span class="who">The loop</span><b>Incident, agent</b>the agent should escalate '
                 f'{e(DRILL_ESCALATES.get(fault, ""))} - no automatic fix</li>' if escalates else
                 '<li class="next"><span class="who">The loop</span><b>Incident, agent, card</b>your second approval '
                 'fixes it</li>')

    def end(reading: dict, side: dict) -> dict:
        ok = reading["state"] == "Established"
        iface = side.get("interface")
        return {"name": str(side.get("device") or "?"), "asn": side.get("local_as", "?"),
                "state": (kelp, "Established") if ok else ((grey, "not read") if not reading["read"] else
                                                           (buoy, str(reading["state"]))),
                "iface": (kelp, f"{iface} up") if iface else (grey, "link: not checked")}

    drawing = outage_card_image(fabric_card_drawing(end(reads["near"], near), end(reads["far"], far), (kelp, "up now")),
                                f"{fmt['d']} and {fmt['p']}: BGP up now", 720, 230)
    sections = f"""<section class="panel topo" aria-labelledby="t-where"><h2 id="t-where">The session to break, healthy now</h2>{drawing}</section>
<section class="panel" aria-labelledby="t-when"><h2 id="t-when">What will happen</h2>
<ol class="steps">
<li class="now"><span class="who">You</span><b>Approve the drill</b>nothing has run yet</li>
<li class="next"><span class="who">Itential</span><b>Breaks it</b>under a {minutes}-minute commit timer</li>
<li class="next"><span class="who">Prometheus</span><b>Alert</b>about 4 minutes later</li>
{loop_step}
<li class="next"><span class="who">EOS</span><b>Safety net</b>rolls back by itself {minutes} min after you approve</li>
</ol></section>
<section class="panel action" aria-labelledby="t-fix"><div><p class="kicker">The drill</p>
<h2 id="t-fix">{e(title.format(**fmt))}</h2><p class="what">{e(
        "Itential puts this one change on " + fmt["d"] + " in a configuration session committed with a commit timer, "
        + ("then lets the fabric BGP outage loop find it and diagnose it. The agent should find "
           + DRILL_ESCALATES.get(fault, "") + ", which has no automatic fix: it should escalate it, and the commit timer "
           "ends the drill."
           if escalates else "then lets the fabric BGP outage loop find it, diagnose it and fix it after your approval."))}</p></div>
<p class="scope">Scope<b>{e(scope)}</b>{e(f"rolls back by itself after {minutes} min")}</p>
{card_lines(d.get("lines"))}
<ul class="promises"><li class="promise">{e("Refused unless the session is Established and matches NetBox")}</li>
<li class="promise">{e(f"Never saved: EOS rolls it back {minutes} min after you approve" + ("" if escalates else
                         ", unless the loop fixed it first"))}</li>
<li class="promise">{e("Read again after the timer, to prove the rollback" if escalates else
                       "Confirmed only once the session is Established again")}</li></ul></section>"""
    lede = ("A drill for the fabric BGP outage loop. Nothing changes until you approve, and the device undoes it by itself "
            "if nothing else does.")
    decide = ("<p>Approve breaks this one session on purpose. Reject changes nothing.</p>\n"
              "<p>Your note stays with this drill's job.</p>")
    foot = (f"Prepared by Itential's Break Fabric BGP workflow at {now:%H:%M} UTC, from live reads of {e(fmt['d'])} and "
            f"{e(fmt['p'])}, checked against NetBox.")
    page = card_page(f"BGP drill: {fmt['d']}", f"Break a BGP session on purpose: {fmt['d']} and {fmt['p']}", lede, "",
                     f"rolls back {minutes} min after approval", sections, "", foot, extra_css=CARD_LINES_CSS, decide=decide,
                     note_label="Note for this drill")
    return {"html": page}


def drill_result(d: dict) -> dict:
    """The drill's outcome from the inject (`inject`), the last read of the session (`check`), the confirm
    (`confirm`, when it ran) and the read after the commit timer (`after_timer`, when it ran)."""
    inject = (((d.get("inject") or {}).get("result")) or {}).get("stdout_json") or {}
    check = fabric_reading(d.get("check"))
    confirm = (((d.get("confirm") or {}).get("result")) or {}).get("stdout_json") or {}
    after = fabric_reading(d.get("after_timer"))
    name = inject.get("drill_session") or "the drill session"
    if confirm.get("confirmed"):
        return {"ok": True, "message": f"Drill done: the session broke, the outage loop brought it back (Established), "
                                       f"and {name} was confirmed - the commit timer is cancelled."}
    if after["read"]:
        state = after["state"]
        if inject.get("action") in ("inject-remote-as-mismatch", "inject-md5-mismatch"):
            # escalated, never fixed: this IS the designed ending (owner 2026-10-06)
            what = (f"the wrong peer AS ({inject.get('wrong_as')}) is config drift"
                    if inject.get("action") == "inject-remote-as-mismatch" else
                    "a password on one end only is an authentication mismatch")
            return {"ok": state == "Established",
                    "message": f"Drill done as designed: {what}, which the outage loop escalates instead of fixing; the "
                               f"commit timer rolled {name} back: the session now reads {state}."}
        return {"ok": state == "Established",
                "message": f"The session was not back by the last read; the commit timer rolled {name} back: the session "
                           f"now reads {state}."}
    return {"ok": False, "message": f"The drill's ending could not be proved ({name}): check the session and the timer."}


FABRIC_WAIT = int(FABRIC["wait_seconds"])
_FABRIC_CONSTANTS = ("FABRIC_READ_TIMEOUT = " + repr(FABRIC_READ_TIMEOUT) + "\nFABRIC_FIX_TIMEOUT = "
                     + repr(FABRIC_FIX_TIMEOUT) + "\nFABRIC_WAIT = " + repr(FABRIC_WAIT) + "\nFABRIC_FIXES = "
                     + repr(FABRIC_FIXES) + "\n\n\n")
FABRIC_PLAN_CODE = _source(fabric_service_session, fabric_plan,
                           call='fabric_plan(json.loads(sys.stdin.read() or "{}"), SESSIONS, TARGETS, FABRIC)',
                           extra=_FABRIC_CONSTANTS + "SESSIONS = " + repr(FABRIC_SESSIONS) + "\nTARGETS = "
                           + repr(FABRIC_TARGETS) + "\nFABRIC = " + repr({"username": FABRIC["username"]}) + "\n\n\n")
FABRIC_OPEN_CODE = _source(open_incident, call='open_incident(json.loads(sys.stdin.read() or "{}"))')
FABRIC_SUMMARY_CODE = _source(fabric_reading, fabric_commands, fabric_summary,
                              call='fabric_summary(json.loads(sys.stdin.read() or "{}"))')
FABRIC_FIX_CODE = _source(fabric_reading, fabric_agreement, fabric_fix,
                          call='fabric_fix(json.loads(sys.stdin.read() or "{}"))', extra=_FABRIC_CONSTANTS)
FABRIC_RESULT_CODE = _source(fabric_reading, decision_note, fabric_result,
                             call='fabric_result(json.loads(sys.stdin.read() or "{}"))')
FABRIC_CARD_CODE = _source(outage_card_image, card_clock, card_mark, card_page, card_lines, fabric_reading,
                           fabric_card_drawing, fabric_card, call='fabric_card(json.loads(sys.stdin.read() or "{}"))',
                           extra=CARD_PALETTE_SRC + "import base64\nimport html\nfrom datetime import datetime, timezone\n\n"
                                 "OUTAGE_CARD_CSS = " + repr(OUTAGE_CARD_CSS) + "\nFABRIC_FIX_COPY = "
                                 + repr(FABRIC_FIX_COPY) + "\nFABRIC_CAUSES = " + repr(FABRIC_CAUSES)
                                 + "\nCARD_LINES_CSS = " + repr(CARD_LINES_CSS) + "\nCARD_LINES_MODE = "
                                 + repr(CARD_LINES_MODE) + "\n\n\n")
DRILL_PLAN_CODE = _source(fabric_service_session, drill_plan,
                          call='drill_plan(json.loads(sys.stdin.read() or "{}"), SESSIONS, TARGETS, FABRIC)',
                          extra=_FABRIC_CONSTANTS + "DRILL_FAULTS = " + repr(DRILL_FAULTS) + "\nDRILL_REVERT_MINUTES = "
                          + repr(DRILL_REVERT_MINUTES) + "\nDRILL_INJECT_TIMEOUT = " + repr(DRILL_INJECT_TIMEOUT)
                          + "\nDRILL_CONFIRM_TIMEOUT = " + repr(DRILL_CONFIRM_TIMEOUT) + "\nSESSIONS = "
                          + repr(FABRIC_SESSIONS) + "\nTARGETS = " + repr(FABRIC_TARGETS) + "\nFABRIC = "
                          + repr({"username": FABRIC["username"]}) + "\n\n\n")
DRILL_CARD_CODE = _source(outage_card_image, card_clock, card_mark, card_page, card_lines, fabric_reading,
                          fabric_card_drawing, drill_card, call='drill_card(json.loads(sys.stdin.read() or "{}"))',
                          extra=CARD_PALETTE_SRC + "import base64\nimport html\nfrom datetime import datetime, timezone\n\n"
                                "OUTAGE_CARD_CSS = " + repr(OUTAGE_CARD_CSS) + "\nCARD_LINES_CSS = "
                                + repr(CARD_LINES_CSS) + "\nCARD_LINES_MODE = " + repr(CARD_LINES_MODE)
                                + "\nDRILL_COPY = " + repr(DRILL_COPY) + "\nDRILL_ESCALATES = " + repr(DRILL_ESCALATES)
                                + "\n\n\n")
DRILL_RESULT_CODE = _source(fabric_reading, drill_result, call='drill_result(json.loads(sys.stdin.read() or "{}"))')


def diagnose_fabric_bgp_outage() -> dict:
    ok, fail, err = "success", "failure", "error"
    nothing = "nothing was changed"
    tasks = {
        "01": empty("no far-end reading yet", "far_result", x=0),
        "02": empty("no agent answer yet", "agent_result", x=20),
        "03": empty("no fix answer yet", "fix_result", x=40),
        "04": empty("no session reading after the fix yet", "check_result", x=60),
        # the plan: the alerting end must be a declared session; its far end and both ends' reads
        "1a": set_key("the plan: the alerting device", {}, "device", "$var.job.device", x=100),
        "1b": set_key("the plan: its neighbor", "$var.1a.object", "neighbor", "$var.job.neighbor", x=125),
        "1c": set_key("the plan: the VRF", "$var.1b.object", "vrf", "$var.job.vrf", x=150),
        "1d": set_key("the plan: the peer", "$var.1c.object", "peer", "$var.job.peer", x=175),
        "1e": run_code("what the loop may act on: both ends of the session (Python on the runner)", FABRIC_PLAN_CODE,
                       "$var.1d.object", "fabric_plan", x=200),
        "10": evaluate("a declared session?", "1e", "result", "stdout_json.ok", "==", True, x=250),
        "19": jq("why there is no plan", "$var.1e.result", "stdout_json.message", x=300, y=-600, to_job="error"),
        # one incident per device pair: an open one is only noted
        "2a": jq("the open-incident query", "$var.job.fabric_plan", "stdout_json.open_query", x=600),
        "2b": sni("listIncidents", "an open incident for this pair? (correlation_id)",
                  {"sysparm_query": "$var.2a.return_data", "sysparm_fields": "sys_id,number", "sysparm_limit": 1},
                  x=650, outgoing={"response": "$var.job.fabric_open"}),
        "2c": set_key("the open-incident answer", {}, "open", "$var.job.fabric_open", x=700),
        "2d": set_key("when the alert started", "$var.2c.object", "starts_at", "$var.job.starts_at", x=725),
        "24": set_key("which alert", "$var.2d.object", "alert", "the BGP session-down alert", x=737),
        "2e": run_code("is one open? (Python on the runner)", FABRIC_OPEN_CODE, "$var.24.object", "fabric_dedupe",
                       x=750),
        "2f": evaluate("already open?", "2e", "result", "stdout_json.open", "==", True, x=800),
        "20": jq("the open incident", "$var.2e.result", "stdout_json.sys_id", x=850, y=600),
        "21": jq("the still-down note", "$var.2e.result", "stdout_json.note", x=875, y=600),
        "22": sni("updateIncident", "note the open incident: still down",
                  {"sys_id": "$var.20.return_data", "sysparm_fields": "number", **nbi_body("$var.21.return_data")},
                  x=900, y=600),
        "23": note("still down: noted", "the pair already has an open incident: noted it, opened none", "outcome",
                   x=925, y=600),
        # the evidence: both ends, read by fabric-bgp (a far end that cannot be read is evidence too)
        "3a": jq("the alerting end's read params", "$var.job.fabric_plan", "stdout_json.near_read", x=950),
        "3b": run_service("the alerting end (fabric-bgp read: the session, the link, AS against NetBox)", "fabric-bgp",
                          "$var.3a.return_data", "near_result", x=1000),
        "3c": evaluate("the alerting end read?", "3b", "result", "result.return_code", "==", 0, x=1025),
        "3d": jq("the far end's read params", "$var.job.fabric_plan", "stdout_json.far_read", x=1050),
        "3e": run_service("the far end (fabric-bgp read)", "fabric-bgp", "$var.3d.return_data", "far_result", x=1075),
        "3f": note("the far end could not be read", "fabric-bgp could not read the far end: the incident says so",
                   "far_note", x=1100, y=300),
        "c0": evaluate("Established again?", "3b", "result", "result.stdout_json.session.state", "==", "Established",
                       x=1125),
        "30": note("the session is up again", "the session read Established again when Itential checked: no incident "
                   "opened, nothing was changed", "outcome", x=1150, y=600),
        # the incident
        "31": set_key("the incident: the plan", {}, "plan", "$var.job.fabric_plan", x=1200),
        "32": set_key("the incident: the alerting end", "$var.31.object", "near", "$var.job.near_result", x=1225),
        "33": set_key("the incident: the far end", "$var.32.object", "far", "$var.job.far_result", x=1250),
        "34": set_key("the incident: when it started", "$var.33.object", "starts_at", "$var.job.starts_at", x=1275),
        "35": run_code("the incident's text (Python on the runner)", FABRIC_SUMMARY_CODE, "$var.34.object",
                       "fabric_summary", x=1300),
        "36": jq("the incident's body", "$var.35.result", "stdout_json.incident", x=1325),
        "37": sni("createIncident", "open one ServiceNow incident for the session",
                  {"sysparm_fields": "sys_id,number", **nbi_body("$var.36.return_data")}, x=1350,
                  outgoing={"response": "$var.job.incident_created"}),
        "38": set_key("the new incident", {}, "created", "$var.job.incident_created", x=1375),
        "39": set_key("the evidence for the agent", "$var.38.object", "summary", "$var.job.fabric_summary", x=1400),
        "c1": run_code("the agent's request (Python on the runner)", OUTAGE_REQUEST_CODE, "$var.39.object",
                       "fabric_request", x=1425),
        "c2": evaluate("the incident opened?", "c1", "result", "stdout_json.ok", "==", True, x=1450),
        # A6: the agent diagnoses, notes the incident and picks one fix on one end
        "4a": jq("the agent's request", "$var.job.fabric_request", "stdout_json.request", x=1500),
        "4b": run_agent("fabric-diagnostics: confirm both ends, note the incident, pick one fix", FABRIC_AGENT_MARKER,
                        "$var.4a.return_data", "agent_result", x=1550),
        "45": note("the agent did not run", "the fabric-diagnostics session did not run: the loop escalates",
                   "agent_note", x=1575, y=-300),
        "4c": set_key("the agent's answer", {}, "agent", "$var.job.agent_result", x=1600),
        "4d": set_key("the plan for the fix", "$var.4c.object", "plan", "$var.job.fabric_plan", x=1625),
        # ADR 0075: the workflow's own reads - the agent never sees them - check its answer (the card's badge)
        "c3": set_key("the fix check: the alerting end's read", "$var.4d.object", "near", "$var.job.near_result",
                      x=1630),
        "c4": set_key("the fix check: the far end's read", "$var.c3.object", "far", "$var.job.far_result", x=1640),
        "4f": run_code("the fix, held to the menu and the session's ends (Python on the runner)", FABRIC_FIX_CODE,
                       "$var.c4.object", "fabric_fix", x=1650),
        "40": evaluate("a fix to approve?", "4f", "result", "stdout_json.card", "==", True, x=1700),
        "41": jq("the incident", "$var.job.fabric_request", "stdout_json.sys_id", x=1725, y=600),
        "42": jq("the escalation note", "$var.4f.result", "stdout_json.escalation", x=1750, y=600),
        "43": sni("updateIncident", "note the incident: escalated to a person",
                  {"sys_id": "$var.41.return_data", "sysparm_fields": "number", **nbi_body("$var.42.return_data")},
                  x=1775, y=600),
        "44": note("escalated", "the agent found no fix from the menu: the incident is escalated to a person", "error",
                   x=1800, y=600),
        # the card's exact lines: fabric-bgp plan for the picked fix (no device): what the card shows is what runs
        "46": jq("the plan's params (the picked fix)", "$var.job.fabric_fix", "stdout_json.plan_params", x=1720),
        "47": run_service("the fix's exact lines (fabric-bgp plan: no device)", "fabric-bgp", "$var.46.return_data",
                          "fix_plan", x=1740),
        "48": evaluate("the lines read?", "47", "result", "result.return_code", "==", 0, x=1760),
        # the card: one HTML page from both ends' readings (fabric_card), the engineer's note comes back
        "4e": jq("the incident number", "$var.job.fabric_request", "stdout_json.number", x=1800),
        "5a": set_key("the card: the fix", "$var.34.object", "fix", "$var.job.fabric_fix", x=1810),
        "5b": set_key("the card: the incident number", "$var.5a.object", "number", "$var.4e.return_data", x=1820),
        "5e": run_code("the card's page (Python on the runner)", FABRIC_CARD_CODE, "$var.5d.object", "fabric_card",
                       x=1830),
        "5f": jq("the card's HTML", "$var.5e.result", "stdout_json.html", x=1840),
        "5d": set_key("the card: the exact lines", "$var.5b.object", "lines", "$var.job.fix_plan", x=1825),
        "5c": task("InteractiveHTML", "WorkCenter", "approval: the BGP outage card",
                   {"header": "Approve the fix for the BGP outage", "body": "$var.5f.return_data", "variables": {},
                    "btn_success": "Approve and run the fix", "btn_failure": "Reject"},
                   {"export": "$var.job.card_decision"}, kind="manual", display="Work Center",
                   view="/work-center/task/InteractiveHTML", x=1850),
        "50": jq("the incident (rejected)", "$var.job.fabric_request", "stdout_json.sys_id", x=1900, y=900),
        "58": run_code("the rejection note, with the engineer's (Python on the runner)", OUTAGE_REJECTED_CODE,
                       "$var.job.card_decision", "fabric_rejected", x=1910, y=900),
        "59": jq("the rejection note", "$var.58.result", "stdout_json", x=1920, y=900),
        "51": sni("updateIncident", "note the incident: the fix was rejected",
                  {"sys_id": "$var.50.return_data", "sysparm_fields": "number", **nbi_body("$var.59.return_data")},
                  x=1925, y=900),
        "52": note("rejected", "the fix was rejected in Work Center: nothing was run, the incident stays open",
                   "outcome", x=1950, y=900),
        # the approved fix, exactly one, on the end the agent named (fabric-bgp proves it Established, then saves)
        "60": jq("the fix's params (one end, one action)", "$var.job.fabric_fix", "stdout_json.params", x=1950),
        "61": run_service("run the approved fix (fabric-bgp: one change, Established, then save)", "fabric-bgp",
                          "$var.60.return_data", "fix_result", x=2000),
        "62": evaluate("the fix exited 0?", "61", "result", "result.return_code", "==", 0, x=2025),
        "54": note("the fix did not complete", "the approved fix exited non-zero or could not run (fix_result): the "
                   "session is read again", "fix_note", x=2050, y=300),
        # the session again at the alerting end, then the incident
        "70": jq("the alerting end's read params", "$var.job.fabric_plan", "stdout_json.near_read", x=2100),
        "80": note("the session could not be read", "fabric-bgp could not read the session after the fix "
                   "(check_result)", "check_note", x=2140, y=-300),
        "72": set_key("the result: the fix", {}, "fix", "$var.job.fabric_fix", x=2250),
        "74": set_key("the result: its answer", "$var.72.object", "answer", "$var.job.fix_result", x=2275),
        "75": set_key("the result: the session now", "$var.74.object", "check", "$var.job.check_result", x=2300),
        "81": set_key("the result: the engineer's note", "$var.75.object", "decision", "$var.job.card_decision",
                      x=2312),
        "76": run_code("fixed? (Python on the runner)", FABRIC_RESULT_CODE, "$var.81.object", "fabric_result", x=2325),
        "77": jq("the incident", "$var.job.fabric_request", "stdout_json.sys_id", x=2350),
        "78": evaluate("fixed?", "76", "result", "stdout_json.fixed", "==", True, x=2375),
        "79": jq("the resolution", "$var.76.result", "stdout_json.resolve", x=2400),
        "7a": sni("updateIncident", "resolve the incident (Solution provided)",
                  {"sys_id": "$var.77.return_data", "sysparm_fields": "number,state", **nbi_body("$var.79.return_data")},
                  x=2425),
        "7b": jq("the outcome", "$var.76.result", "stdout_json.message", x=2450, to_job="outcome"),
        "7c": jq("the not-fixed note", "$var.76.result", "stdout_json.note", x=2400, y=-300),
        "7d": sni("updateIncident", "note the incident: still not Established after the fix",
                  {"sys_id": "$var.77.return_data", "sysparm_fields": "number", **nbi_body("$var.7c.return_data")},
                  x=2425, y=-300),
        "7e": jq("why it needs a look", "$var.76.result", "stdout_json.message", x=2450, y=-300, to_job="error"),
        # failures: the reason, then one Work Center task
        "8b": note("a step could not run", f"the Gateway could not run a step before the evidence (see fabric_plan): "
                   f"{nothing}", "error", x=500, y=-600),
        "8c": note("the alerting end could not be read", f"fabric-bgp could not read the alerting end (near_result): "
                   f"{nothing}", "error", x=1050, y=-900),
        "8d": note("ServiceNow did not answer", f"ServiceNow did not answer (fabric_open, incident_created): "
                   f"{nothing}; the outage has no incident", "error", x=1350, y=-600),
        "8e": note("a step after the incident could not run", "a step after the incident was opened could not run "
                   "(see fabric_request, agent_result, fabric_fix, fix_result): check the job and the incident",
                   "error", x=1700, y=-900),
        "f0": view("Work Center task", "Diagnose Fabric BGP Outage needs a look", "$var.job.error", "$var.job.fabric_fix",
                   "Seen", "Seen", x=2600, y=0),
    }
    # the session after the fix: fabric-bgp already waited for Established before it saved, so these reads confirm it
    # from the alerting end - up to four over about two and a half minutes, unrolled (no cycle); the first that reads
    # Established goes to the result, the last read is check_result either way
    recheck_tr = {}
    for i, (delay, read, up, secs) in enumerate(FABRIC_RECHECKS):
        nxt = FABRIC_RECHECKS[i + 1][0] if i + 1 < len(FABRIC_RECHECKS) else "72"
        x = 2150 + i * 25
        tasks[delay] = task("delay", "WorkFlowEngine", f"wait {secs} s for the session (read {i + 1})", {"time": secs},
                            {"time_in_milliseconds": None}, kind="operation", display="WorkFlowEngine", x=x)
        tasks[read] = run_service(f"the session now, read {i + 1} (fabric-bgp read)", "fabric-bgp",
                                  "$var.70.return_data", "check_result", x=x + 8)
        tasks[up] = evaluate(f"Established? (read {i + 1})", read, "result", "result.stdout_json.session.state", "==",
                             "Established", x=x + 16)
        recheck_tr[delay] = _edge(**{read: ok})
        recheck_tr[read] = _edge(**{up: ok, (nxt if nxt != "72" else "80"): err})
        recheck_tr[up] = _edge(**{"72": ok, (nxt if nxt != "72" else "bc"): fail})
    tasks["bc"] = note("still not Established after the last read", "the session was still not Established after the "
                       f"last read (about {sum(r[3] for r in FABRIC_RECHECKS)} s after the fix): the result says so",
                       "check_note", x=2240, y=300)
    recheck_tr["bc"] = _edge(**{"72": ok})
    tr = {
        "workflow_start": _edge(**{"01": ok}),
        "01": _edge(**{"02": ok}), "02": _edge(**{"03": ok}), "03": _edge(**{"04": ok}), "04": _edge(**{"1a": ok}),
        "1a": _edge(**{"1b": ok}), "1b": _edge(**{"1c": ok}), "1c": _edge(**{"1d": ok}), "1d": _edge(**{"1e": ok}),
        "1e": _edge(**{"10": ok, "8b": err}),
        "10": _edge(**{"2a": ok, "19": fail}),
        "19": _edge(**{"f0": ok, "8b": err}),
        "2a": _edge(**{"2b": ok, "8b": err}),
        "2b": _edge(**{"2c": ok, "8d": err}),
        "2c": _edge(**{"2d": ok}), "2d": _edge(**{"24": ok}), "24": _edge(**{"2e": ok}),
        "2e": _edge(**{"2f": ok, "8b": err}),
        "2f": _edge(**{"20": ok, "3a": fail}),
        "20": _edge(**{"21": ok, "8d": err}),
        "21": _edge(**{"22": ok, "8d": err}),
        "22": _edge(**{"23": ok, "8d": err}),
        "23": _edge(**{"workflow_end": ok}),
        "3a": _edge(**{"3b": ok, "8b": err}),
        "3b": _edge(**{"3c": ok, "8c": err}),
        "3c": _edge(**{"3d": ok, "8c": fail}),
        "3d": _edge(**{"3e": ok, "8b": err}),
        # a far end that cannot be read is evidence too ("not read"): the incident goes on
        "3e": _edge(**{"c0": ok, "3f": err}),
        "3f": _edge(**{"c0": ok}),
        "c0": _edge(**{"30": ok, "31": fail, "8c": err}),
        "30": _edge(**{"workflow_end": ok}),
        "31": _edge(**{"32": ok}), "32": _edge(**{"33": ok}), "33": _edge(**{"34": ok}), "34": _edge(**{"35": ok}),
        "35": _edge(**{"36": ok, "8e": err}),
        "36": _edge(**{"37": ok, "8e": err}),
        "37": _edge(**{"38": ok, "8d": err}),
        "38": _edge(**{"39": ok}), "39": _edge(**{"c1": ok}),
        "c1": _edge(**{"c2": ok, "8e": err}),
        "c2": _edge(**{"4a": ok, "8d": fail}),
        "4a": _edge(**{"4b": ok, "8e": err}),
        # an agent that fails is an answer too: no fix from it means escalate
        "4b": _edge(**{"4c": ok, "45": err}),
        "45": _edge(**{"4c": ok}),
        "4c": _edge(**{"4d": ok}), "4d": _edge(**{"c3": ok}), "c3": _edge(**{"c4": ok}), "c4": _edge(**{"4f": ok}),
        "4f": _edge(**{"40": ok, "8e": err}),
        "40": _edge(**{"46": ok, "41": fail}),
        "46": _edge(**{"47": ok, "8e": err}),
        # a card without its exact lines would ask for a blind approval: a person looks instead
        "47": _edge(**{"48": ok, "8e": err}),
        "48": _edge(**{"4e": ok, "8e": fail}),
        "41": _edge(**{"42": ok, "8e": err}),
        "42": _edge(**{"43": ok, "8e": err}),
        "43": _edge(**{"44": ok, "8e": err}),
        "44": _edge(**{"f0": ok}),
        "4e": _edge(**{"5a": ok, "8e": err}),
        "5a": _edge(**{"5b": ok}), "5b": _edge(**{"5d": ok}), "5d": _edge(**{"5e": ok}),
        "5e": _edge(**{"5f": ok, "8e": err}),
        "5f": _edge(**{"5c": ok, "8e": err}),
        "5c": _edge(**{"60": ok, "50": fail}),
        "50": _edge(**{"58": ok, "8e": err}),
        "58": _edge(**{"59": ok, "8e": err}),
        "59": _edge(**{"51": ok, "8e": err}),
        "51": _edge(**{"52": ok, "8e": err}),
        "52": _edge(**{"workflow_end": ok}),
        "60": _edge(**{"61": ok, "8e": err}),
        "61": _edge(**{"62": ok, "54": err}),
        "62": _edge(**{"70": ok, "54": fail}),
        "54": _edge(**{"70": ok}),
        "70": _edge(**{FABRIC_RECHECKS[0][0]: ok, "8e": err}),
        "80": _edge(**{"72": ok}),
        **recheck_tr,
        "72": _edge(**{"74": ok}), "74": _edge(**{"75": ok}), "75": _edge(**{"81": ok}), "81": _edge(**{"76": ok}),
        "76": _edge(**{"77": ok, "8e": err}),
        "77": _edge(**{"78": ok, "8e": err}),
        "78": _edge(**{"79": ok, "7c": fail}),
        "79": _edge(**{"7a": ok, "8e": err}),
        "7a": _edge(**{"7b": ok, "8e": err}),
        "7b": _edge(**{"workflow_end": ok}),
        "7c": _edge(**{"7d": ok, "8e": err}),
        "7d": _edge(**{"7e": ok, "8e": err}),
        "7e": _edge(**{"f0": ok}),
        **{n: _edge(**{"f0": ok}) for n in ("8b", "8c", "8d", "8e")},
        "f0": _edge(**{"workflow_end": ok}),
    }
    return workflow(
        WF["diagnose_fabric_bgp_outage"],
        "The fabric BGP outage loop (R10 + A6, ADR 0073): started by the session-down alert, it reads both ends, opens "
        "one ServiceNow incident, lets the fabric-diagnostics agent pick one fix on one end from a fixed menu, and runs "
        "that fix only after a Work Center approval - proved Established - then resolves the incident",
        {k: {"type": "string", "required": True}
         for k in ("alertname", "device", "neighbor", "vrf", "peer", "starts_at", "fingerprint")},
        tasks,
        tr,
        {
            "outcome": {"type": "string"}, "error": {"type": "string"},
            "fabric_plan": {"type": "object"}, "fabric_open": {"type": "object"}, "fabric_dedupe": {"type": "object"},
            "near_result": {"type": "object"}, "far_result": {"type": "object"}, "far_note": {"type": "string"},
            "fabric_summary": {"type": "object"}, "incident_created": {"type": "object"},
            "fabric_request": {"type": "object"}, "agent_result": {"type": "object"}, "agent_note": {"type": "string"},
            "fabric_fix": {"type": "object"}, "fix_plan": {"type": "object"}, "fabric_card": {"type": "object"},
            "card_decision": {"type": ["object", "null"]}, "fabric_rejected": {"type": "object"},
            "fix_result": {"type": "object"}, "fix_note": {"type": "string"}, "check_result": {"type": "object"},
            "check_note": {"type": "string"}, "fabric_result": {"type": "object"},
        },
    )


def break_fabric_bgp() -> dict:
    ok, fail, err = "success", "failure", "error"
    nothing = "nothing was changed"
    tasks = {
        "01": empty("no inject answer yet", "inject_result", x=0),
        "02": empty("no confirm answer yet", "confirm_result", x=20),
        "03": empty("no read after the timer yet", "after_timer_result", x=40),
        "04": empty("no far-end reading yet", "far_result", x=60),
        # the plan: a declared vEOS session end and a drill fault
        "1a": set_key("the plan: the device", {}, "device", "$var.job.device", x=100),
        "1b": set_key("the plan: its neighbor", "$var.1a.object", "neighbor", "$var.job.neighbor", x=125),
        "1c": set_key("the plan: the fault", "$var.1b.object", "fault", "$var.job.fault", x=150),
        "1e": run_code("what the drill may break (Python on the runner)", DRILL_PLAN_CODE, "$var.1c.object",
                       "drill_plan", x=200),
        "10": evaluate("a declared vEOS session?", "1e", "result", "stdout_json.ok", "==", True, x=250),
        "19": jq("why there is no drill", "$var.1e.result", "stdout_json.message", x=300, y=600, to_job="outcome"),
        # both ends now: only a healthy session is broken on purpose
        "2a": jq("the session's read params", "$var.job.drill_plan", "stdout_json.near_read", x=350),
        "2b": run_service("the session now (fabric-bgp read)", "fabric-bgp", "$var.2a.return_data", "near_result", x=400),
        "2c": evaluate("Established now?", "2b", "result", "result.stdout_json.session.state", "==", "Established",
                       x=450),
        "2d": note("not healthy", f"an end of the session is not Established now: a drill never stacks on a fault, "
                   f"{nothing}",
                   "outcome", x=500, y=600),
        "2e": jq("the far end's read params", "$var.job.drill_plan", "stdout_json.far_read", x=500),
        "2f": run_service("the far end now (fabric-bgp read)", "fabric-bgp", "$var.2e.return_data", "far_result", x=550),
        "29": evaluate("the far end Established now?", "2f", "result", "result.stdout_json.session.state", "==",
                       "Established", x=575),
        # the exact lines: fabric-bgp plan (no device)
        "3a": jq("the plan's params", "$var.job.drill_plan", "stdout_json.plan_params", x=600),
        "3b": run_service("the drill's exact lines (fabric-bgp plan: no device)", "fabric-bgp", "$var.3a.return_data",
                          "drill_lines", x=650),
        "3c": evaluate("the lines read?", "3b", "result", "result.return_code", "==", 0, x=700),
        # the card
        "4a": set_key("the card: the plan", {}, "plan", "$var.job.drill_plan", x=750),
        "4b": set_key("the card: the session", "$var.4a.object", "near", "$var.job.near_result", x=775),
        "4c": set_key("the card: the far end", "$var.4b.object", "far", "$var.job.far_result", x=800),
        "4d": set_key("the card: the exact lines", "$var.4c.object", "lines", "$var.job.drill_lines", x=825),
        "4e": run_code("the card's page (Python on the runner)", DRILL_CARD_CODE, "$var.4d.object", "drill_card",
                       x=850),
        "4f": jq("the card's HTML", "$var.4e.result", "stdout_json.html", x=875),
        "47": task("InteractiveHTML", "WorkCenter", "approval: the BGP drill card",
                   {"header": "Approve the BGP drill", "body": "$var.4f.return_data", "variables": {},
                    "btn_success": "Approve and break it", "btn_failure": "Reject"},
                   {"export": "$var.job.card_decision"}, kind="manual", display="Work Center",
                   view="/work-center/task/InteractiveHTML", x=900),
        "48": note("rejected", f"the drill was rejected in Work Center: {nothing}", "outcome", x=950, y=600),
        # the fault, under the commit timer
        "5a": jq("the inject's params", "$var.job.drill_plan", "stdout_json.inject", x=950),
        "5b": run_service("break it (fabric-bgp inject: a configuration session under a commit timer, never saved)",
                          "fabric-bgp", "$var.5a.return_data", "inject_result", x=1000),
        "5c": evaluate("broken?", "5b", "result", "result.return_code", "==", 0, x=1025),
        "5d": note("the drill did not apply", "fabric-bgp did not break the session (inject_result): if its commit "
                   "timer was armed, EOS rolls the change back by itself", "error", x=1050, y=600),
        "5e": jq("the drill session's name", "$var.5b.result", "result.stdout_json.drill_session", x=1075),
        "5f": jq("the confirm's params", "$var.job.drill_plan", "stdout_json.confirm", x=1100),
        "50": set_key("confirm params: the drill session", "$var.5f.return_data", "drill_session", "$var.5e.return_data",
                      x=1125),
        # confirmed only once the outage loop brought it back
        "7a": run_service("cancel the commit timer (fabric-bgp confirm-drill: only when Established)", "fabric-bgp",
                          "$var.50.object", "confirm_result", x=1600),
        "7b": evaluate("confirmed?", "7a", "result", "result.return_code", "==", 0, x=1625),
        # not back by the last read: the timer's, then read again
        "8a": note("not back by the last read", "the session was not Established by the last read: the commit timer "
                   "rolls the drill back", "timer_note", x=1600, y=300),
        "8b": task("delay", "WorkFlowEngine", "wait for the commit timer (and a margin)", {"time": DRILL_AFTER_TIMER},
                   {"time_in_milliseconds": None}, kind="operation", display="WorkFlowEngine", x=1625, y=300),
        "8c": run_service("the session after the timer (fabric-bgp read)", "fabric-bgp", "$var.2a.return_data",
                          "after_timer_result", x=1650, y=300),
        "8d": evaluate("read after the timer?", "8c", "result", "result.return_code", "==", 0, x=1675, y=300),
        # the outcome
        "9a": set_key("the result: the inject", {}, "inject", "$var.job.inject_result", x=1700),
        "9b": set_key("the result: the last read", "$var.9a.object", "check", "$var.job.check_result", x=1725),
        "9c": set_key("the result: the confirm", "$var.9b.object", "confirm", "$var.job.confirm_result", x=1750),
        "9d": set_key("the result: after the timer", "$var.9c.object", "after_timer", "$var.job.after_timer_result",
                      x=1775),
        "9e": run_code("the drill's outcome (Python on the runner)", DRILL_RESULT_CODE, "$var.9d.object",
                       "drill_result", x=1800),
        "9f": evaluate("a clean ending?", "9e", "result", "stdout_json.ok", "==", True, x=1825),
        "90": jq("the outcome", "$var.9e.result", "stdout_json.message", x=1850, to_job="outcome"),
        "91": jq("why it needs a look", "$var.9e.result", "stdout_json.message", x=1850, y=300, to_job="error"),
        "e0": note("a step could not run", f"a step before the card could not run (see drill_plan, near_result, "
                   f"drill_lines): {nothing}", "error", x=650, y=-600),
        "e1": note("a step after the drill could not run", "a step after the drill was applied could not run (see "
                   "inject_result, check_result, confirm_result, after_timer_result): the commit timer still rolls "
                   "the drill back",
                   "error", x=1400, y=-600),
        "f0": view("Work Center task", "Break Fabric BGP needs a look", "$var.job.error", "$var.job.drill_plan", "Seen",
                   "Seen", x=1950, y=0),
    }
    # the patient wait for the outage loop: four reads over 18 minutes, unrolled (no cycle); the first that reads
    # Established goes to the confirm, the last read is check_result either way
    reads_tr = {}
    for i, (delay, read, up, secs) in enumerate(DRILL_READS):
        nxt = DRILL_READS[i + 1][0] if i + 1 < len(DRILL_READS) else "8a"
        x = 1150 + i * 100
        tasks[delay] = task("delay", "WorkFlowEngine", f"wait {secs // 60} min for the outage loop (read {i + 1})",
                            {"time": secs}, {"time_in_milliseconds": None}, kind="operation", display="WorkFlowEngine",
                            x=x)
        tasks[read] = run_service(f"the session now, read {i + 1} (fabric-bgp read)", "fabric-bgp", "$var.2a.return_data",
                                  "check_result", x=x + 25)
        tasks[up] = evaluate(f"back? (read {i + 1})", read, "result", "result.stdout_json.session.state", "==",
                             "Established", x=x + 50)
        reads_tr[delay] = _edge(**{read: ok})
        reads_tr[read] = _edge(**{up: ok, nxt: err})
        reads_tr[up] = _edge(**{"7a": ok, nxt: fail})
    tr = {
        "workflow_start": _edge(**{"01": ok}),
        "01": _edge(**{"02": ok}), "02": _edge(**{"03": ok}), "03": _edge(**{"04": ok}), "04": _edge(**{"1a": ok}),
        "1a": _edge(**{"1b": ok}), "1b": _edge(**{"1c": ok}), "1c": _edge(**{"1e": ok}),
        "1e": _edge(**{"10": ok, "e0": err}),
        "10": _edge(**{"2a": ok, "19": fail}),
        "19": _edge(**{"workflow_end": ok}),
        "2a": _edge(**{"2b": ok, "e0": err}),
        "2b": _edge(**{"2c": ok, "e0": err}),
        "2c": _edge(**{"2e": ok, "2d": fail, "e0": err}),
        "2d": _edge(**{"workflow_end": ok}),
        "2e": _edge(**{"2f": ok, "e0": err}),
        # both ends must read Established: a drill never breaks a session it cannot see whole
        "2f": _edge(**{"29": ok, "e0": err}),
        "29": _edge(**{"3a": ok, "2d": fail, "e0": err}),
        "3a": _edge(**{"3b": ok, "e0": err}),
        "3b": _edge(**{"3c": ok, "e0": err}),
        # a card without its exact lines would ask for a blind approval: a person looks instead
        "3c": _edge(**{"4a": ok, "e0": fail}),
        "4a": _edge(**{"4b": ok}), "4b": _edge(**{"4c": ok}), "4c": _edge(**{"4d": ok}), "4d": _edge(**{"4e": ok}),
        "4e": _edge(**{"4f": ok, "e0": err}),
        "4f": _edge(**{"47": ok, "e0": err}),
        "47": _edge(**{"5a": ok, "48": fail}),
        "48": _edge(**{"workflow_end": ok}),
        "5a": _edge(**{"5b": ok, "e1": err}),
        "5b": _edge(**{"5c": ok, "5d": err}),
        "5c": _edge(**{"5e": ok, "5d": fail}),
        "5d": _edge(**{"f0": ok}),
        "5e": _edge(**{"5f": ok, "e1": err}),
        "5f": _edge(**{"50": ok, "e1": err}),
        "50": _edge(**{DRILL_READS[0][0]: ok}),
        **reads_tr,
        "7a": _edge(**{"7b": ok, "e1": err}),
        "7b": _edge(**{"9a": ok, "e1": fail}),
        "8a": _edge(**{"8b": ok}),
        "8b": _edge(**{"8c": ok}),
        "8c": _edge(**{"8d": ok, "e1": err}),
        "8d": _edge(**{"9a": ok, "e1": fail}),
        "9a": _edge(**{"9b": ok}), "9b": _edge(**{"9c": ok}), "9c": _edge(**{"9d": ok}), "9d": _edge(**{"9e": ok}),
        "9e": _edge(**{"9f": ok, "e1": err}),
        "9f": _edge(**{"90": ok, "91": fail}),
        "90": _edge(**{"workflow_end": ok}),
        "91": _edge(**{"f0": ok}),
        "e0": _edge(**{"f0": ok}), "e1": _edge(**{"f0": ok}),
        "f0": _edge(**{"workflow_end": ok}),
    }
    return workflow(
        WF["break_fabric_bgp"],
        "The fabric BGP outage loop's drill (R10 PR C, ADR 0073): after an HTML approval card with the exact lines, it "
        "breaks one healthy vEOS session - a neighbor or the interface toward the peer shut - under an EOS commit timer, "
        "waits for the outage loop to bring it back and only then cancels the timer; otherwise EOS rolls it back",
        {k: {"type": "string", "required": True} for k in ("device", "neighbor", "fault")},
        tasks,
        tr,
        {
            "outcome": {"type": "string"}, "error": {"type": "string"}, "timer_note": {"type": "string"},
            "drill_plan": {"type": "object"}, "near_result": {"type": "object"}, "far_result": {"type": "object"},
            "drill_lines": {"type": "object"}, "drill_card": {"type": "object"},
            "card_decision": {"type": ["object", "null"]}, "inject_result": {"type": "object"},
            "check_result": {"type": "object"}, "confirm_result": {"type": "object"},
            "after_timer_result": {"type": "object"}, "drill_result": {"type": "object"},
        },
    )


BUILDERS = (device_count, show_version, show_command, show_all, branch_vlan, branch_vlan_delete, config_push,
            compliance_run, compliance_report, netbox_devices, backup_all, deploy_aws_vpn, verify_aws_vpn,
            hand_off_aws_vpn, config_push_revert, tear_down_aws_vpn, tear_down_expired_aws_vpn, get_aws_vpn_status,
            check_aws_drift, rotate_aws_vpn_key, rotate_aws_vpn_key_monthly, diagnose_aws_vpn_outage,
            diagnose_fabric_bgp_outage, break_fabric_bgp, drill_batfish_gate)


if __name__ == "__main__":
    for build in BUILDERS:
        wf = build()
        out = HERE / file_name(wf["name"])
        out.write_text(json.dumps(wf, indent=2) + "\n")
        print(out.relative_to(HERE.parent.parent), len(wf["tasks"]) - 2, "tasks")
