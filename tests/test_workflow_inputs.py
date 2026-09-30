"""No job input reaches parsed JSON, a device command or switch configuration unchecked (security review 2026-09-29,
WEB-01 and WEB-12). Runs over EVERY generated workflow, so a new one that templates an input without a gate fails."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / "itential" / "workflows").glob("*.json"))
_SPEC = importlib.util.spec_from_file_location("wf_build", ROOT / "itential" / "workflows" / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)

# text a person reads on an approval card is not parsed or executed; everything else is a sink
DISPLAY_ONLY = {("ViewData", "message")}
# tasks whose output is only a transformation of their input: taint flows through them
PURE = {"replace", "parse", "query", "setObjectKey", "makeData", "numberToString", "stringConcat", "arrayPush",
        "merge", "deepmerge", "validateJsonSchema", "evaluation"}
HOSTILE = ['x"', "x\\", "x\ny", "x\ry", "x\n", 'x", "configure terminal']


def _tasks(wf: dict) -> dict:
    return {tid: t for tid, t in wf["tasks"].items() if isinstance(t, dict) and "variables" in t}


def _refs(val):
    """Every $var reference in a task input, however deeply nested."""
    if isinstance(val, str):
        if val.startswith("$var."):
            yield val
    elif isinstance(val, dict):
        for v in val.values():
            yield from _refs(v)
    elif isinstance(val, list):
        for v in val:
            yield from _refs(v)


def _spliced(wf: dict, val, as_text: bool = False, seen: frozenset = frozenset()) -> set[str]:
    """The workflow INPUTS that reach `val` by being spliced into text (replace's newSubstr), followed back through
    every pure task and every job variable a task derived (switch = <branch>-sw01, vid = instance.vid, ...)."""
    tasks, inputs, out = _tasks(wf), set(wf["inputSchema"]["properties"]), set()
    for ref in _refs(val):
        parts = ref.split(".")
        if parts[1] == "job":
            var = parts[2]
            if var in seen:
                continue
            if var in inputs:
                out |= {var} if as_text else set()
            for t in tasks.values():  # a derived job variable: follow what the task that set it read
                if f"$var.job.{var}" in (t["variables"].get("outgoing") or {}).values():
                    out |= _spliced(wf, t["variables"]["incoming"], as_text, seen | {var})
            continue
        t = tasks.get(parts[1])
        if not t or t["name"] not in PURE:
            continue
        inc = t["variables"].get("incoming") or {}
        if t["name"] == "replace":
            out |= _spliced(wf, inc.get("str"), as_text, seen) | _spliced(wf, inc.get("newSubstr"), True, seen)
        else:
            out |= _spliced(wf, inc, as_text, seen)
    return out


def _templated_inputs(wf: dict) -> set[str]:
    """Workflow inputs spliced into text that reaches a sink (anything not pure and not display-only)."""
    found = set()
    for t in _tasks(wf).values():
        if t["name"] in PURE:
            continue
        for key, val in (t["variables"].get("incoming") or {}).items():
            if (t["name"], key) not in DISPLAY_ONLY:
                found |= _spliced(wf, val)
    return found


def _refuses_hostile(field: dict) -> list[str]:
    """What is wrong with a gate field that guards text: a string needs an anchored pattern (JSON Schema searches,
    it does not full-match) or an enum; an object needs that for each string property."""
    if "enum" in field:
        return []
    if "object" in (field.get("type") if isinstance(field.get("type"), list) else [field.get("type")]):
        return [p for sub in field.get("properties", {}).values() for p in _refuses_hostile(sub)]
    if field.get("type") in ("integer", "boolean"):
        return []
    pat = field.get("pattern")
    if not pat:
        return ["no pattern"]
    if not (pat.startswith("^") and pat.endswith("$")):
        return [f"pattern not anchored: {pat}"]
    # ECMA-262 (what JSON Schema uses): `$` is the very end of the input. Python's `$` also matches before a final
    # newline, so `\Z` stands in for it here to test what the Platform will do
    rx = re.compile(pat[:-1] + r"\Z")
    return [f"accepts {bad!r}" for bad in HOSTILE if rx.search(bad)]


def _check(wf: dict) -> list[str]:
    templated = _templated_inputs(wf)
    if not templated:
        return []
    gate = build.INPUT_GATES.get(wf["name"])
    if not gate:
        return [f"{wf['name']} splices {sorted(templated)} into text with no input gate (add it to INPUT_GATES)"]
    return [f"{wf['name']}: {name} {why}" for name in templated for why in (_refuses_hostile(gate.get(name) or {}) or [])
            ] + [f"{wf['name']}: {name} is not in its gate" for name in templated if name not in gate]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.stem)
def test_every_templated_input_is_gated_by_a_pattern_that_refuses_metacharacters(path: Path) -> None:
    assert _check(json.loads(path.read_text())) == []


def test_the_check_sees_an_input_reaching_a_command_through_a_query() -> None:
    """The review's miss: instance.vid -> query -> numberToString -> replace -> parse -> sendCommand, no gate."""
    wf = {"name": "Zz Synthetic", "inputSchema": {"properties": {"instance": {"type": "object"}}}, "tasks": {
        "1a": {"name": "query", "variables": {"incoming": {"obj": "$var.job.instance", "query": "vid"}, "outgoing": {}}},
        "1b": {"name": "numberToString", "variables": {"incoming": {"num": "$var.1a.return_data"}, "outgoing": {}}},
        "1c": {"name": "replace", "variables": {"incoming": {"str": '["show vlan __V__"]', "substr": "__V__",
                                                             "newSubstr": "$var.1b.numToString"}, "outgoing": {}}},
        "1d": {"name": "parse", "variables": {"incoming": {"text": "$var.1c.replacedString"}, "outgoing": {}}},
        "1e": {"name": "sendCommand", "variables": {"incoming": {"commands": "$var.1d.textObject"}, "outgoing": {}}},
    }}
    assert _check(wf) == ["Zz Synthetic splices ['instance'] into text with no input gate (add it to INPUT_GATES)"]


@pytest.mark.parametrize("name", sorted(build.INPUT_GATES))
def test_the_gate_is_the_only_way_in(name: str) -> None:
    wf = json.loads((ROOT / "itential" / "workflows" / f"{name.lower().replace(' ', '-')}.json").read_text())
    tr, tasks = wf["transitions"], _tasks(wf)
    assert list(tr["workflow_start"]) == ["9a01"]
    assert tasks["9a0a"]["name"] == "validateJsonSchema"
    schema = tasks["9a0a"]["variables"]["incoming"]["schema"]
    assert schema["properties"] == build.INPUT_GATES[name] and schema["additionalProperties"] is False
    # validateJsonSchema completes even for invalid data (measured): only the evaluate on `valid` can refuse
    assert tasks["9a0b"]["variables"]["incoming"]["evaluation_groups"][0]["evaluations"][0]["query"] == "valid"
    assert tr["9a0b"]["9a0c"]["state"] == "failure" and tr["9a0a"]["9a0c"]["state"] == "error"
    assert list(tr["9a0c"]) == ["workflow_end"]
    # the workflow's own first task(s) are reached only through the gate
    firsts = [dst for dst in tr["9a0b"] if dst != "9a0c"]
    assert firsts and all(tr["9a0b"][d]["state"] == "success" for d in firsts)
    for dst in firsts:
        assert [src for src, out in tr.items() if dst in out] == ["9a0b"], dst


SHOW = re.compile(build.SHOW_COMMAND["pattern"])


@pytest.mark.parametrize("command", [
    "show version", "show ip interface brief", "show running-config", "show running-config | include hostname",
    "show version | json", "show vlan | json", "show ip bgp summary | json", "show interfaces description",
    "show version | include Cisco IOS XE Software", "show bgp evpn summary", "show interfaces Ethernet1/1",
    "show running-config | section router bgp", "show ip route vrf MGMT",
])
def test_every_show_command_the_lab_sends_is_accepted(command: str) -> None:
    assert SHOW.fullmatch(command)


@pytest.mark.parametrize("command", [
    "reload", "configure terminal", "write erase", "sh ver", "SHOW version", "copy running-config tftp:",
    "show running-config | redirect flash:x", "show running-config | append flash:x", "show version | tee flash:x",
    "show version > flash:x", "show version\nreload", 'show version", "reload', "show version; reload",
    "show version | include x | redirect flash:y", "show " + "a" * 250,
])
def test_anything_but_a_read_is_refused(command: str) -> None:
    assert not SHOW.fullmatch(command)
