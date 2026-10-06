"""Rotate AWS VPN Key (R4, owner decisions 2026-10-04): the pre-shared key between AWS and dc1-wan01 is changed in place.
Only with the tunnel up: a new key in Vault and Secrets Manager (aws-vpn-psk write), strongSwan reloads it (aws-vpn-monitor
reload: the monitor Lambda's fixed SSM document), the router takes it (lab-edge-push: revert timer, a fresh SA, save),
the two newest Vault versions are kept (prune), then Verify's own checks. When the router does not take it, or the AWS
side fails part-way, the AWS side goes back to the previous key by itself (restore-previous, reload) and a Work Center
task says what happened. Operations Manager has no monthly repeat (minute/hour/day/week), so Rotate AWS VPN Key Monthly
runs every day and rotates only on the 1st (UTC); Rotate AWS VPN Key runs on demand. One section, two copies."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("wf_build_rotate", ROOT / "itential" / "workflows" / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)
VERSIONS = build.VERSIONS
NAME, MONTHLY = "Rotate AWS VPN Key", "Rotate AWS VPN Key Monthly"
ARN = "arn:aws:secretsmanager:us-east-1:<account>:secret:vpn/onprem-psk-AbCdEf"
OUTPUTS = {"strongswan_eip": "198.51.100.20", "strongswan_instance_id": "i-0abc", "psk_secret_arn": ARN,
           "strongswan_private_ip": "10.0.1.10", "vpc_private_prefixes": ["10.0.64.0/20"]}


def _svc(out: dict | None, rc: int = 0) -> dict:
    return {"result": {"return_code": rc, "stdout_json": out}}


def _plan(outputs, rc: int = 0) -> dict:
    return build.rotation_plan({"outputs": _svc({"action": "outputs", "outputs": outputs}, rc)}, build.VERIFY_TARGETS)


def _step(stage: str, answer: dict) -> dict:
    return build.rotation_step({"stage": stage, "answer": answer})


# ── the plan (pure; the same source runs on the Gateway) ──


def test_a_deployment_gives_every_services_params_for_the_one_aws_target() -> None:
    p = _plan(OUTPUTS)
    assert p["ok"] and p["deployed"] and p["target"] == "dc1-wan01"
    assert p["write"] == {"action": "write", "secret_arn": ARN, "timeout": build.PSK_TIMEOUT}
    assert p["restore"] == {"action": "restore-previous", "secret_arn": ARN, "timeout": build.PSK_TIMEOUT}
    assert p["prune"] == {"action": "prune", "timeout": build.PSK_TIMEOUT}
    assert p["reload"] == {"action": "reload", "instance_id": "i-0abc", "timeout": build.RELOAD_TIMEOUT}
    assert p["edge_in"] == {"target": "dc1-wan01", "targets": build.VERIFY_TARGETS, "deployed": OUTPUTS}


def test_nothing_deployed_is_nothing_to_rotate() -> None:
    p = _plan({})
    assert p["ok"] and p["deployed"] is False and "nothing is deployed" in p["message"]


@pytest.mark.parametrize("outputs, rc", [({}, 1), (None, 0)])
def test_outputs_that_cannot_be_read_change_nothing(outputs, rc: int) -> None:
    p = _plan(outputs, rc)
    assert p["ok"] is False and "nothing was changed" in p["message"]


@pytest.mark.parametrize("missing", ["psk_secret_arn", "strongswan_instance_id"])
def test_a_deployment_without_its_secret_or_instance_is_refused(missing: str) -> None:
    p = _plan({k: v for k, v in OUTPUTS.items() if k != missing})
    assert p["ok"] is False and "nothing was changed" in p["message"]


def test_the_edge_plan_reads_the_plans_edge_input() -> None:
    """LAB_EDGE_PLAN_CODE (Hand Off's own) gives the precheck, render and push params from edge_in."""
    run = subprocess.run([sys.executable, "-I", "-c", build.LAB_EDGE_PLAN_CODE],
                         input=json.dumps(_plan(OUTPUTS)["edge_in"]), capture_output=True, text=True, timeout=30,
                         env={}, check=True)
    edge = json.loads(run.stdout)
    assert edge["ready"] and edge["push"]["action"] == "push" and edge["lab_edge"]["action"] == "verify"


# ── each step's verdict: go on, roll back, or stop ──


def test_a_written_key_goes_on() -> None:
    s = _step("write", _svc({"written": True, "vault_version": 7, "version": "u"}))
    assert s["ok"] and not s["rollback"] and "version 7" in s["message"]


def test_a_key_only_vault_holds_is_rolled_back() -> None:
    s = _step("write", _svc({"written": False, "vault_version": 7, "error": "the PSK is in Vault ... AccessDenied"}, 1))
    assert s["ok"] is False and s["rollback"] and "AccessDenied" in s["message"]


@pytest.mark.parametrize("answer", [_svc({"written": False, "error": "Refused"}, 1), {}, _svc(None, 1)])
def test_a_key_never_written_stops_with_nothing_changed(answer: dict) -> None:
    s = _step("write", answer)
    assert s["ok"] is False and s["rollback"] is False and "nothing was changed" in s["message"]


@pytest.mark.parametrize("answer", [_svc({"reloaded": False, "error": "timed out"}, 1), {},
                                    _svc({"reloaded": True, "instance_matches": False}, 1)])
def test_a_reload_that_did_not_happen_is_rolled_back(answer: dict) -> None:
    s = _step("reload", answer)
    assert s["ok"] is False and s["rollback"]
    assert _step("reload", _svc({"reloaded": True, "instance_matches": True}))["ok"]


def test_a_saved_push_goes_on() -> None:
    s = _step("push", _svc({"target": "dc1-wan01", "sent": True, "key_sent": True, "saved": True, "changed": True}))
    assert s["ok"] and not s["rollback"] and "dc1-wan01" in s["message"]


@pytest.mark.parametrize("out", [
    {"sent": True, "key_sent": True, "rolled_back": True, "rejected": ["% Invalid"]},  # rejected, rolled back
    {"sent": True, "key_sent": True, "error": "ReadTimeout"},  # dropped before confirm: the revert timer restores it
    {"sent": False, "error": "the router's prerequisites changed"},  # refused before sending
])
def test_a_router_that_did_not_take_the_key_rolls_aws_back(out: dict) -> None:
    s = _step("push", _svc(out, 0 if out.get("rolled_back") else 1))
    assert s["ok"] is False and s["rollback"]


def test_a_push_that_sent_no_key_did_not_rotate_the_router() -> None:
    """After a new write the router's recorded version is always older: a push that finds the block and the version
    already in place was handed the old key (a stale Vault read on the Gateway), so AWS goes back to it."""
    s = _step("push", _svc({"target": "dc1-wan01", "sent": False, "key_sent": False, "already_in_place": True,
                            "saved": True, "changed": False}))
    assert s["ok"] is False and s["rollback"] and "no key" in s["message"]


def test_no_answer_from_the_push_rolls_aws_back() -> None:
    """The router never confirms without lab-edge-push: its revert timer restores the old key."""
    assert _step("push", {})["rollback"]


def test_a_router_that_confirmed_but_did_not_save_keeps_the_new_key_everywhere() -> None:
    s = _step("push", _svc({"sent": True, "key_sent": True, "confirmed": True,
                             "error": "`write memory` did not report [OK]"}, 1))
    assert s["ok"] is False and s["rollback"] is False and "write memory" in s["message"]


def test_the_rollback_says_what_it_restored_or_that_it_could_not() -> None:
    ok = _step("restore", _svc({"restored": True, "version": "u-old"}))
    assert ok["ok"] and "previous key" in ok["message"]
    bad = _step("restore", _svc({"restored": False, "error": "Secrets Manager refused"}, 1))
    assert bad["ok"] is False and "by hand" in bad["message"] and "Secrets Manager refused" in bad["message"]
    back = _step("reload_back", _svc({"reloaded": True, "instance_matches": True}))
    assert back["ok"] and "previous key" in back["message"]
    assert _step("reload_back", {})["ok"] is False


def test_an_unknown_stage_never_goes_on() -> None:
    s = _step("other", _svc({"written": True}))
    assert s["ok"] is False and s["rollback"] is False


@pytest.mark.parametrize("code, data", [
    ("ROTATE_PLAN_CODE", {"outputs": _svc({"outputs": OUTPUTS})}),
    ("ROTATE_STEP_CODE", {"stage": "push", "answer": _svc({"saved": True, "key_sent": True, "target": "dc1-wan01"})}),
])
def test_the_pure_functions_run_as_the_gateway_runs_them(code: str, data: dict) -> None:
    run = subprocess.run([sys.executable, "-I", "-c", getattr(build, code)], input=json.dumps(data),
                         capture_output=True, text=True, timeout=30, env={}, check=True)
    assert json.loads(run.stdout)["ok"] is True


@pytest.mark.parametrize("day, due", [(1, True), (2, False), (28, False)])
def test_the_monthly_run_rotates_on_the_first_only(day: int, due: bool) -> None:
    d = build.rotation_due(datetime(2026, 11, day, 8, 30, tzinfo=timezone.utc))
    assert d["due"] is due and (due or "1st" in d["message"])


def test_the_due_check_runs_as_the_gateway_runs_it() -> None:
    run = subprocess.run([sys.executable, "-I", "-c", build.ROTATE_DUE_CODE], input="{}", capture_output=True,
                         text=True, timeout=30, env={}, check=True)
    assert json.loads(run.stdout)["due"] in (True, False)


# ── the workflows ──

WF, WM = build.rotate_aws_vpn_key(), build.rotate_aws_vpn_key_monthly()
TASKS, TR = WF["tasks"], WF["transitions"]


def _reach(start: str, tr: dict = TR) -> set[str]:
    seen, todo = set(), [start]
    while todo:
        node = todo.pop()
        if node not in seen:
            seen.add(node)
            todo.extend(tr.get(node, {}))
    return seen


def _service(tid: str, tasks: dict = TASKS) -> tuple[str, str]:
    t = tasks[tid]["variables"]["incoming"]
    return t["serviceName"], t["params"]


def _services(tasks: dict = TASKS) -> dict[str, str]:
    """Each runService task's id and the service it runs."""
    return {tid: t["variables"]["incoming"]["serviceName"] for tid, t in tasks.items() if t.get("name") == "runService"}


def test_both_are_registered_named_and_take_no_input() -> None:
    assert WF["name"] == NAME == VERSIONS["workflows"]["rotate_aws_vpn_key"]
    assert WM["name"] == MONTHLY == VERSIONS["workflows"]["rotate_aws_vpn_key_monthly"]
    for wf, builder in ((WF, build.rotate_aws_vpn_key), (WM, build.rotate_aws_vpn_key_monthly)):
        assert builder in build.BUILDERS and not (wf.get("inputSchema") or {}).get("properties")


def test_the_rotation_tasks_are_the_same_in_both() -> None:
    section, _, _ = build.rotation_section()
    for tid in section:
        strip = lambda t: {k: v for k, v in t.items() if k != "nodeLocation"}  # noqa: E731
        assert strip(TASKS[tid]) == strip(WM["tasks"][tid]), tid


def test_the_services_are_the_five_rotation_needs() -> None:
    assert sorted(set(_services().values())) == [
        "aws-vpn-monitor", "aws-vpn-psk", "lab-edge", "lab-edge-push", "terraform-run"]


def test_nothing_is_written_before_the_tunnel_is_proved_up() -> None:
    write = "4b"
    assert _service(write)[0] == "aws-vpn-psk" and "stdout_json.write" in json.dumps(TASKS["4a"])
    assert [src for src, d in TR.items() if "4a" in d] == ["3d"] and "4a" in _reach("2e")
    # every path to the write passes the router-up and data-plane-up evaluations of the precheck
    assert TR["2d"]["2e"]["state"] == "success" and TR["2e"]["3a"]["state"] == "success"
    assert TASKS["2d"]["name"] == TASKS["2e"]["name"] == "evaluation"
    assert "router" in json.dumps(TASKS["2d"]) and "data_plane" in json.dumps(TASKS["2e"])
    assert write not in _reach("8d") and write not in _reach("11")


def test_the_order_is_write_reload_push_prune_verify() -> None:
    assert "5a" in _reach("4e") and "6a" in _reach("5e") and "7a" in _reach("6f") and "e0" in _reach("7a")
    assert TR["4e"]["5a"]["state"] == TR["5e"]["6a"]["state"] == TR["6f"]["7a"]["state"] == "success"
    assert "4e" not in _reach("5a") and "6a" not in _reach("7a")  # no loop back


def test_only_a_step_that_says_rollback_reaches_the_rollback() -> None:
    into_r0 = sorted(src for src, d in TR.items() if "b0" in d)
    assert into_r0 == ["4f", "5f", "60"]
    for src in into_r0:
        assert TR[src]["b0"]["state"] == "success" and "rollback" in json.dumps(TASKS[src])
    assert "b2" in _reach("b0") and "b7" in _reach("b0") and "f0" in _reach("b0")
    assert "b0" not in _reach("7a")  # a saved push is never rolled back


def test_the_rollback_restores_then_reloads() -> None:
    assert _service("b2")[0] == "aws-vpn-psk" and "stdout_json.restore" in json.dumps(TASKS["b1"])
    assert _service("b7")[0] == "aws-vpn-monitor" and "stdout_json.reload" in json.dumps(TASKS["b6"])
    assert TR["b5"]["b6"]["state"] == "success"  # a reload only after the key is restored


def test_the_one_manual_task_is_the_work_center_task() -> None:
    assert [tid for tid, t in TASKS.items() if t.get("type") == "manual"] == ["f0"]
    assert TASKS["f0"]["name"] == "ViewData" and "error" in TASKS["f0"]["variables"]["incoming"]["message"]


def test_every_task_reaches_the_end() -> None:
    for wf in (WF, WM):
        for tid in wf["tasks"]:
            assert "workflow_end" in _reach(tid, wf["transitions"]) or tid == "workflow_end", (wf["name"], tid)
    assert {"outcome", "error", "step", "rollback_step", "push_result"} <= set(WF["outputSchema"]["properties"])


def test_the_monthly_run_reaches_the_rotation_only_when_due() -> None:
    tr = WM["transitions"]
    into = sorted(src for src, d in tr.items() if "01" in d)
    assert into == ["d2"] and tr["d2"]["01"]["state"] == "success"
    assert "01" not in _reach("d3", tr) and "f0" not in _reach("d4", tr)  # a broken clock opens no task


# ── the schedule, the retired tier and the pin ──


def test_the_schedule_runs_the_monthly_one_every_day_off_the_hour() -> None:
    om = VERSIONS["operations_manager"]["rotate_aws_vpn_key_monthly"]
    assert om["automation"] == MONTHLY
    assert om["schedule"]["repeat_unit"] == "day" and om["schedule"]["repeat_frequency"] == 1
    # off the hour: Tear Down Expired AWS VPN runs at the top of every hour, Check AWS Drift at 07:00
    assert om["schedule"]["at_utc"] == "08:30"


def test_platform_yml_loops_the_schedule_task_over_all_three() -> None:
    play = yaml.safe_load((ROOT / "ansible" / "playbooks" / "platform.yml").read_text())[0]
    task = next(t for t in play["tasks"] if t.get("ansible.builtin.include_tasks") == "tasks/aws-vpn-schedule.yml")
    assert task["loop"] == ["tear_down_expired_aws_vpn", "check_aws_drift", "rotate_aws_vpn_key_monthly"]


def test_the_retired_tier_also_loses_the_rotation() -> None:
    text = (ROOT / "ansible" / "playbooks" / "tasks" / "aws-vpn-retire.yml").read_text()
    assert "'rotate_aws_vpn_key_monthly'" in text


def test_the_gateway_runs_a_commit_with_reload_restore_and_prune() -> None:
    # 3e3f032 (cdp #33) brought them; 691531d (cdp #34), 9ab2290 (cdp #35, R6), e7a6f67 (cdp #36) and 60839ab
    # (cdp #37, R10's fabric-bgp), 02b62f1 (cdp #38, R10 PR C: plan and the drill) and e7a8648 (cdp #39) are its
    # descendants
    assert VERSIONS["terraform_run"]["repository"]["reference"] == "e7a8648ab3718020269f35659f5f3f10f211a98e"
