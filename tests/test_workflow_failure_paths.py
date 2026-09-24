"""Failure paths of the lab's workflows (ADR 0066, amending ADR 0059): every Gateway device task has its result
checked, a device's refusal is read from its own reply, and every failure ends the job with a reason instead of
dead-ending it - a dead-ended job is retryable, never finished, and leaves an agent or a Lifecycle Manager action
waiting. Measured on the dev tier 2026-09-23/24; production's job history showed both faults had already fired."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_every_gateway_device_task_has_its_result_checked_and_every_failure_reaches_the_end() -> None:
    """ADR 0066: a Gateway JSON-RPC error - a sealed Vault, for one - finishes sendCommand/sendConfig `success`, so only
    an evaluation of the result can see it, and that evaluation's failure must reach workflow_end (ADR 0059: a job
    that dead-ends hangs the calling agent). Measured on dev 2026-09-23."""
    import json
    for path in sorted((ROOT / "itential" / "workflows").glob("wf-*.json")):
        wf = json.loads(path.read_text())
        tasks, tr = wf["tasks"], wf["transitions"]
        for tid, task in tasks.items():
            if not isinstance(task, dict) or task.get("name") not in ("sendCommand", "sendConfig"):
                continue
            nxt = [b for b, e in tr.get(tid, {}).items() if e["state"] == "success"]
            checks = [b for b in nxt if tasks.get(b, {}).get("name") == "evaluation"]
            assert checks, f"{path.name}: {tid} {task['name']} result is never checked"
            for c in checks:
                fail = [b for b, e in tr.get(c, {}).items() if e["state"] == "failure"]
                assert fail, f"{path.name}: evaluation {c} after {tid} has no failure edge (dead-end)"


def test_branch_vlan_rolls_back_a_failure_between_the_reservation_and_the_push_and_only_there() -> None:
    """Owner decision 2026-09-23: keep branch-vlan's designed error-end, but an external call that fails after the NetBox
    reservation and before the device push goes through the rollback instead of leaving the VLAN reserved."""
    import json
    wf = json.loads((ROOT / "itential" / "workflows" / "wf-branch-vlan-v1.json").read_text())
    tasks, tr = wf["tasks"], wf["transitions"]
    assert tasks["3d"]["name"] == "ipam_vlans_create" and tasks["5c"]["name"] == "sendConfig"
    assert tasks["8a"]["name"] == "ipam_vlans_destroy"
    assert tr["e6"].get("8a") == {"state": "error", "type": "standard"}
    # nothing after the push rolls the reservation back: the VLAN is live on the switch by then
    for tid in ("6a", "ea1", "ea9", "f1", "f3"):
        assert "8a" not in tr.get(tid, {}), tid


def test_compliance_report_never_reads_compliant_without_a_checked_device_and_every_call_can_end() -> None:
    import contextlib
    import importlib.util
    import io
    import json
    spec = importlib.util.spec_from_file_location("b", ROOT / "itential" / "workflows" / "build.py")
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)

    def summary(reports: list) -> dict:
        out = io.StringIO()
        stdin, sys_stdin = io.StringIO(json.dumps({"reports": reports})), __import__("sys")
        old, sys_stdin.stdin = sys_stdin.stdin, stdin
        try:
            with contextlib.redirect_stdout(out):
                exec(b.SUMMARY_CODE, {})
        finally:
            sys_stdin.stdin = old
        return json.loads(out.getvalue())

    checked = {"deviceName": "r1", "totals": {"passes": 3, "errors": 0, "warnings": 0}, "issues": []}
    blank = {"deviceName": "r2", "totals": {}, "issues": []}
    assert summary([])["compliant"] is False and "error" in summary([])
    assert summary([checked])["compliant"] is True
    got = summary([checked, blank])
    assert got["compliant"] is False and got["devices_not_checked"] == ["r2"]

    wf = json.loads((ROOT / "itential" / "workflows" / "wf-compliance-report-v1.json").read_text())
    tasks, tr = wf["tasks"], wf["transitions"]
    for tid, task in tasks.items():
        if isinstance(task, dict) and task.get("app") in ("ConfigurationManager", "GatewayManager"):
            assert any(e["state"] == "error" for e in tr.get(tid, {}).values()), f"{tid} {task.get('name')} can dead-end"
    assert tr["d4"].get("e2", {}).get("state") == "failure", "the last attempt must end cleanly, not dead-end"
    for tid in ("6c", "6d", "6e"):  # the steps after the runner call can throw on an unexpected reply
        assert tr[tid].get("e1", {}).get("state") == "error", tid


def test_show_command_keeps_the_raw_answer_when_the_parser_cannot_be_chosen() -> None:
    """Measured on dev 2026-09-23: a node NetBox does not have dead-ended the job at 3c after the device had answered.
    Every step after the command that can throw now ends through 5c with the raw output and parse_error."""
    import json
    wf = json.loads((ROOT / "itential" / "workflows" / "wf-show-command-v1.json").read_text())
    tasks, tr = wf["tasks"], wf["transitions"]
    assert tasks["2a"]["variables"]["outgoing"]["result"] == "$var.job.raw", "raw is published before any lookup"
    for tid in ("3a", "3b", "3c", "4a"):
        assert tr[tid].get("5c") == {"state": "error", "type": "standard"}, tid
    assert tr["5c"] == {"workflow_end": {"state": "success", "type": "standard"}}
    assert tasks["5c"]["variables"]["outgoing"] == {"output": "$var.job.parse_error"}


def test_config_push_reads_the_device_reply_and_never_saves_a_refused_push() -> None:
    """Measured 2026-09-24: send-config reports success: true while IOS-XE answers "% Invalid input detected". The push
    reads the reply on the runner; a refused line skips `write memory` and ends with the refusal (owner decision)."""
    import contextlib
    import importlib.util
    import io
    import json
    import sys
    spec = importlib.util.spec_from_file_location("b", ROOT / "itential" / "workflows" / "build.py")
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)

    def scan(output: str) -> dict:
        old, sys.stdin = sys.stdin, io.StringIO(json.dumps({"output": output}))
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                exec(b.REPLY_CODE, {})
        finally:
            sys.stdin = old
        return json.loads(out.getvalue())

    refused = "r2(config)#sername admin\n                     ^\n% Invalid input detected at '^' marker.\nr2(config)#end"
    assert scan(refused)["rejected"] is True and "sername admin" in scan(refused)["message"]
    assert scan("r1(config)#ip domain name lab.internal\nr1(config)#end")["rejected"] is False
    assert scan("sw1(config)#interface Ethernet1\n% Warning: in use\nsw1(config-if-Et1)#end")["rejected"] is False
    assert scan("sw1(config)#vlan 5000\n% Incomplete command\nsw1(config)#end")["rejected"] is True

    wf = json.loads((ROOT / "itential" / "workflows" / "wf-config-push-v1.json").read_text())
    tr = wf["transitions"]
    assert tr["3b"]["3c"]["state"] == "success", "the reply is read only after the Gateway ran the lines"
    assert tr["3f"] == {"4a": {"state": "success", "type": "standard"}, "8c": {"state": "failure", "type": "standard"}}
    for tid in ("3c", "3d", "3e"):
        assert tr[tid]["8e"]["state"] == "error", f"{tid}: an unreadable reply must not fall through to the save"
    # the save (4a) is reachable only through "every line accepted?"
    assert [a for a, m in tr.items() if "4a" in m] == ["3f"]


def test_branch_vlan_reads_the_switch_reply_before_netbox_marks_the_vlan_active() -> None:
    """Owner decision 2026-09-24: as in wf-config-push-v1, send-config's success flag is not trusted for refused lines.
    A refusal rolls the NetBox reservation back (8a); an unreadable reply ends in error WITHOUT a rollback, because
    the push most likely worked and deleting the reservation would orphan a live VLAN."""
    import json
    wf = json.loads((ROOT / "itential" / "workflows" / "wf-branch-vlan-v1.json").read_text())
    tasks, tr = wf["tasks"], wf["transitions"]
    assert tasks["50"]["name"] == "runCode" and "refusal = re.compile" in tasks["50"]["variables"]["incoming"]["code"]
    assert [a for a, m in tr.items() if "6a" in m] == ["51"], "NetBox 'VLAN active' only after every line was accepted"
    assert tr["51"]["52"]["state"] == "failure" and tr["52"] == {"8a": {"state": "success", "type": "standard"}}
    assert tasks["52"]["variables"]["outgoing"] == {"return_data": "$var.job.device_error"}
    for tid in ("5e", "5f", "50"):
        assert tr[tid]["8c"]["state"] == "error", tid
    assert tr["8c"] == {}, "an unreadable reply keeps the designed error-end and does not roll back"
