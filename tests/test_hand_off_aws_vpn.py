"""Hand Off AWS VPN (PID S13 criterion 3, build step 8): nothing reaches the router before a person approves the exact
block, only lab-edge-push carries the key, every failure says whether anything was sent, and the verify steps after
the push are Verify AWS VPN's own - written into the workflow, not called as a child job (owner decision 2026-10-01)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("wf_build_handoff", ROOT / "itential" / "workflows" / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)
WFS = ROOT / "itential" / "workflows"
HAND = json.loads((WFS / "hand-off-aws-vpn.json").read_text())
VERIFY = json.loads((WFS / "verify-aws-vpn.json").read_text())
TARGETS = build.VERSIONS["aws_vpn"]["targets"]
CDP = Path.home() / "PycharmProjects" / "cloud-devops-pipeline"
PIN = build.VERSIONS["terraform_run"]["repository"]["reference"]


def _tasks(wf: dict) -> dict:
    return {tid: t for tid, t in wf["tasks"].items() if "variables" in t}


def _reach(wf: dict, start: str) -> set[str]:
    seen, todo = set(), [start]
    while todo:
        node = todo.pop()
        if node in seen:
            continue
        seen.add(node)
        todo.extend(wf["transitions"].get(node, {}))
    return seen


def _run(code: str, data: dict) -> dict:
    run = subprocess.run([sys.executable, "-I", "-c", code], input=json.dumps(data), capture_output=True, text=True,
                         timeout=30, env={}, check=True)
    return json.loads(run.stdout)


# ── no child jobs; the verify steps are Verify's own ──


def test_no_workflow_calls_a_child_job_for_the_vpn() -> None:
    for wf in (HAND, VERIFY):
        assert not [t for t in _tasks(wf).values() if t["name"] == "childJob"], wf["name"]


def test_hand_offs_verify_steps_are_verify_aws_vpns_own() -> None:
    section = [f"e{c}" for c in "0123456789abcdef"]

    def strip(t: dict, plan: str) -> str:
        t = {k: v for k, v in t.items() if k != "nodeLocation"}
        return json.dumps(t, sort_keys=True).replace(plan, "PLAN")

    for tid in section:
        assert strip(HAND["tasks"][tid], "handoff_plan") == strip(VERIFY["tasks"][tid], "verify_plan"), tid
    # inside the section the arrows are the same; only where it leaves differs (each workflow's own outcome)
    inside = set(section)
    for tid in section:
        h = {d: e for d, e in HAND["transitions"][tid].items() if d in inside}
        v = {d: e for d, e in VERIFY["transitions"][tid].items() if d in inside}
        assert h == v, tid
    assert HAND["tasks"]["ee"]["variables"]["incoming"]["code"] == build.JUDGE_CODE


# ── nothing reaches the router before the approval, and only the approved block does ──


def test_the_push_is_reached_only_through_an_approval() -> None:
    tasks, tr = _tasks(HAND), HAND["transitions"]
    push = [tid for tid, t in tasks.items() if t["name"] == "runService"
            and t["variables"]["incoming"]["serviceName"] == "lab-edge-push"]
    assert push == ["7c"]
    assert tasks["6f"]["name"] == "ViewData" and tr["6f"]["7a"]["state"] == "success"
    # every way into the push chain comes from the approval's success edge
    into = {src for src, out in tr.items() if "7a" in out}
    assert into == {"6f"}
    # reject: ends with rejected = true and nothing sent; the push is not reachable from there
    assert tr["6f"]["a0"]["state"] == "failure" and "7c" not in _reach(HAND, "a0")
    assert tasks["a2"]["variables"]["incoming"]["input"].endswith("nothing was sent to the router")


def test_the_approved_sha_is_the_rendered_one_and_the_push_gets_exactly_it() -> None:
    tasks = _tasks(HAND)
    assert tasks["5d"]["variables"]["incoming"]["query"] == "result.stdout_json.sha256"
    assert tasks["5d"]["variables"]["outgoing"]["return_data"] == "$var.job.sha256"
    assert tasks["6b"]["variables"]["incoming"]["value"] == "$var.job.sha256"  # on the card
    assert tasks["7b"]["variables"]["incoming"] == {"obj": "$var.7a.return_data", "path": ["sha256"],
                                                    "value": "$var.job.sha256"}  # to the push, as data
    assert tasks["7c"]["variables"]["incoming"]["params"] == "$var.7b.object"


def test_the_card_shows_the_masked_block_and_never_a_key() -> None:
    tasks = _tasks(HAND)
    assert tasks["5e"]["variables"]["incoming"]["query"] == "result.stdout_json.block_masked"
    assert tasks["6c"]["variables"]["incoming"]["value"] == "$var.job.block_masked"
    assert tasks["6f"]["variables"]["incoming"]["body"] == "$var.6d.object"
    assert "never shown" in build.APPROVAL_MESSAGE
    # only lab-edge-push is bound to the key aliases (versions.yaml), and no task names a Vault path or a key
    text = json.dumps(HAND)
    assert "aws-vpn-psk" not in text and "devices/aws-vpn" not in text and "LAB_EDGE_PSK" not in text


def test_every_failure_before_the_push_ends_saying_nothing_was_sent() -> None:
    tasks, tr = _tasks(HAND), HAND["transitions"]
    before = ("b0", "b1", "b2", "b3", "b4", "b5", "b6", "b7", "b8", "b9")
    for b in before:
        assert list(tr[b]) == ["workflow_end"], b
        assert tasks[b]["variables"]["incoming"]["input"].endswith("nothing was sent to the router"), b
        assert tasks[b]["variables"]["outgoing"]["output"] == "$var.job.error"
    # changed is false from the start; only the push's own answer changes it
    # (behind the input gate, whose refusal ends before anything runs)
    assert tr["9a0b"]["10"]["state"] == "success" and [s for s, out in tr.items() if "10" in out] == ["9a0b"]
    assert tasks["10"]["variables"]["outgoing"]["output"] == "$var.job.changed"
    setters = [tid for tid, t in tasks.items() if "$var.job.changed" in json.dumps(t["variables"]["outgoing"])]
    assert sorted(setters) == ["10", "70", "c5"] and tasks["70"]["variables"]["incoming"]["input"] == "true"
    assert tasks["c5"]["variables"]["incoming"]["input"] == "true" and tr["c5"] == {"c4": {"state": "success", "type": "standard"}}
    # set only when the push summary says the router may have changed (an evaluation, not a query of a false value)
    assert tr["7f"] == {"70": {"state": "success", "type": "standard"}, "71": {"state": "failure", "type": "standard"}}
    # a precheck that refuses or finds something missing ends before the render; a render that refuses before the card
    assert "5b" not in _reach(HAND, "4e") and "6f" not in _reach(HAND, "b6")


def test_after_the_push_every_outcome_says_what_the_router_went_through() -> None:
    tasks, tr = _tasks(HAND), HAND["transitions"]
    assert tr["7c"]["c0"]["state"] == "error" and "check the router" in tasks["c0"]["variables"]["incoming"]["input"]
    assert tr["7c"]["74"]["state"] == "success" and tr["74"]["7d"]["state"] == "success" and tr["75"] == {"7d": {"state": "success", "type": "standard"}}
    # the push's answer is summarised on the Gateway, and only a saved push goes on to the verify steps
    assert tasks["7e"]["variables"]["incoming"]["code"] == build.PUSH_SUMMARY_CODE
    assert tasks["7d"]["variables"]["incoming"]["value"] == "$var.job.push_result"
    assert tasks["71"]["variables"]["outgoing"]["return_data"] == "$var.job.router_state"
    assert tr["72"] == {"8a": {"state": "success", "type": "standard"}, "73": {"state": "failure", "type": "standard"}}
    assert tasks["72"]["variables"]["incoming"]["evaluation_groups"][0]["evaluations"][0]["query"] == "stdout_json.state"
    assert tasks["73"]["variables"]["outgoing"]["return_data"] == "$var.job.error" and "workflow_end" in tr["73"]
    # a push answer that cannot be read: changed = true (the router may differ), then "check the router"
    assert tr["7e"]["c5"]["state"] == "error" and tr["71"]["c5"]["state"] == "error" and tr["73"]["c5"]["state"] == "error"
    assert tr["8a"] == {"e0": {"state": "success", "type": "standard"}}  # the verify steps follow the push
    assert tr["ef"] == {"90": {"state": "success", "type": "standard"}, "94": {"state": "failure", "type": "standard"}}
    assert tasks["92"]["variables"]["outgoing"]["replacedString"] == "$var.job.outcome"
    assert tasks["96"]["variables"]["outgoing"]["replacedString"] == "$var.job.error"
    assert tasks["90"]["variables"]["incoming"]["newSubstr"] == "$var.job.router_state"
    assert tasks["8a"]["variables"]["incoming"]["time"] >= 30


# ── the plan: exactly what each service takes, for the open targets only ──


def test_the_twins_plan_carries_every_param_and_no_aws_steps() -> None:
    entry = TARGETS["clab-rtr1"]
    plan = _run(build.LAB_EDGE_PLAN_CODE, {"targets": build.VERIFY_TARGETS, "target": "clab-rtr1"})
    assert plan["ready"] is True and plan["monitor_ready"] is None and plan["monitor_params"] is None
    for key, action in (("precheck", "precheck"), ("render", "render"), ("push", "push"), ("lab_edge", "verify")):
        assert plan[key]["action"] == action
        assert json.loads(plan[key]["target_json"]) == entry["target"]
    assert json.loads(plan["push"]["outputs_json"]) == entry["outputs"] == json.loads(plan["render"]["outputs_json"])
    assert plan["push"]["username"] == plan["precheck"]["username"] == entry["username"]
    assert "sha256" not in plan["push"]  # only the approved one, added after the card


def _action_args(script: str, action: str) -> set[str]:
    import ast

    source = subprocess.run(["git", "-C", str(CDP), "show", f"{PIN}:itential/{script}"], capture_output=True,
                            text=True, check=True).stdout
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "ACTION_ARGS" for t in n.targets))
    return ast.literal_eval(next(v for k, v in zip(node.value.keys, node.value.values) if ast.literal_eval(k) == action))


def _pin_present() -> bool:
    return CDP.exists() and subprocess.run(["git", "-C", str(CDP), "cat-file", "-e", f"{PIN}^{{commit}}"],
                                           capture_output=True).returncode == 0


@pytest.mark.skipif(not _pin_present(), reason="needs the cloud-devops-pipeline clone with the pinned commit")
def test_hand_offs_params_are_exactly_what_each_service_takes_at_the_pin() -> None:
    plan = _run(build.LAB_EDGE_PLAN_CODE, {"targets": build.VERIFY_TARGETS, "target": "clab-rtr1"})
    assert set(plan["precheck"]) == _action_args("lab-edge.py", "precheck")
    assert set(plan["render"]) == _action_args("lab-edge.py", "render")
    assert set(plan["push"]) | {"sha256"} == _action_args("lab-edge-push.py", "push")
    aws = {"dc1-wan01": {k: TARGETS["dc1-wan01"][k] for k in ("target", "username", "monitor")}}
    plan = _run(build.LAB_EDGE_PLAN_CODE, {"targets": aws, "target": "dc1-wan01",
                                           "deployed": {"strongswan_eip": "203.0.113.7",
                                                        "strongswan_instance_id": "i-0123456789abcdef0"}})
    assert set(plan["monitor_ready"]) == _action_args("aws-vpn-monitor.py", "ready")
    assert plan["monitor_ready"]["instance_id"] == "i-0123456789abcdef0"


def test_no_target_monitored_by_aws_is_open_until_its_netbox_read_back_exists() -> None:
    # Hand Off reads NetBox back for dc1-wan01 only, and that is built with its records (build step 10): until then a
    # target monitored by AWS must stay closed, or its Hand Off would skip a read-back the spec requires
    assert not [n for n, e in TARGETS.items() if e["window"] == "open" and e["monitor"] == "aws"]
    assert "not applicable" in _tasks(HAND)["6d"]["variables"]["incoming"]["value"]


def test_only_open_targets_get_in() -> None:
    open_targets = sorted(n for n, e in TARGETS.items() if e["window"] == "open")
    assert build.INPUT_GATES["Hand Off AWS VPN"] == {"target": {"enum": open_targets}}
    assert HAND["tasks"]["1a"]["variables"]["incoming"]["obj"] == {"targets": build.VERIFY_TARGETS}


# ── the push's answer, as lab-edge-push gives it at the pin ──


def _push_at_pin():
    """lab-edge-push's `result` dict (main), `router_state` and REVERT_MINUTES at the Gateway's pin."""
    import ast

    source = subprocess.run(["git", "-C", str(CDP), "show", f"{PIN}:itential/lab-edge-push.py"], capture_output=True,
                            text=True, check=True).stdout
    tree = ast.parse(source)
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    result = next(ast.literal_eval(n.value) for n in ast.walk(main) if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "result" for t in n.targets))
    scope: dict = {}
    for n in tree.body:
        if (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "REVERT_MINUTES" for t in n.targets)) or (
                isinstance(n, ast.FunctionDef) and n.name == "router_state"):
            exec(compile(ast.Module([n], []), "lab-edge-push.py", "exec"), scope)  # noqa: S102 - the pinned source
    return result, scope["router_state"]


def _envelope(rc, out):
    return {"push": {"id": 1, "jsonrpc": "2.0", "result": {"return_code": rc, "stdout_json": out}}}


@pytest.mark.skipif(not _pin_present(), reason="needs the cloud-devops-pipeline clone with the pinned commit")
def test_the_push_summary_reads_every_answer_lab_edge_push_gives() -> None:
    base, router_state = _push_at_pin()
    assert "already_in_place" in base  # the pin's lab-edge-push answers a second Hand Off without sending
    sent = {**base, "sent": True}
    cases = [
        # (exit code, stdout as main emits it, state, changed)
        (0, {**sent, "key_sent": True, "proved": True, "confirmed": True, "saved": True, "changed": True}, "saved", True),
        # a new key version, or a block that drifted: lines sent under the revert timer, saved
        (0, {**sent, "proved": True, "confirmed": True, "saved": True, "changed": False}, "saved", False),
        # a second Hand Off with the same values: the router already holds it, nothing is sent, only saved
        (0, {**base, "already_in_place": True, "proved": True, "saved": True}, "saved", False),
        # in place, but the save did not answer [OK]: nothing was sent, not saved
        (1, {**base, "already_in_place": True, "proved": True, "error": "Refused",
             "router": router_state(base)}, "failed", False),
        # a rejected line or a post-read that did not prove: rolled back, and it exits 0
        (0, {**sent, "rejected": ["% Invalid input"], "rolled_back": True}, "rolled back", False),
        (0, {**sent, "proved": False, "rolled_back": True}, "rolled back", False),
        # refused before anything was sent (SHA mismatch, prerequisites changed)
        (1, {**base, "error": "the block rendered now is not the approved one", "router": router_state(base)}, "failed", False),
        # confirmed but write memory did not answer [OK]: live and NOT saved
        (1, {**sent, "proved": True, "confirmed": True, "changed": True, "error": "Refused",
             "router": router_state({**sent, "confirmed": True})}, "failed", True),
        # the session dropped after the lines: a change may be pending its revert timer
        (1, {**sent, "error": "ReadTimeout", "router": router_state(sent)}, "failed", True),
    ]
    for rc, out, state, changed in cases:
        got = build.push_summary(_envelope(rc, out))
        assert (got["state"], got["changed"]) == (state, changed), (out, got)
        # the router line: the service's own when it gave one, else what router_state would say (in place: said so)
        want = out.get("router") or ("already in place: nothing sent, saved" if out.get("already_in_place")
                                     else router_state(out))
        assert got["router"] == want, got
        assert _run(build.PUSH_SUMMARY_CODE, _envelope(rc, out)) == got  # what the Gateway runs
        assert (got["message"] == "") == (state == "saved")
    # no answer at all: unknown, may have changed, check the router
    for empty in ({}, {"push": {}}, _envelope(None, None), _envelope(1, "not an object"), _envelope(0, ["x"])):
        got = build.push_summary(empty)
        assert got["state"] == "failed" and got["changed"] is True and got["router"].startswith("unknown")
