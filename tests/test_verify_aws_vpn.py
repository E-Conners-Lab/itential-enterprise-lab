"""Verify AWS VPN (PID S13 criterion 4, build step 7): the judge's rules, the code that runs on the Gateway is the code
tested here, the parameters are exactly what the services take, and the workflow only reads."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("wf_build_verify", ROOT / "itential" / "workflows" / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)
WF = json.loads((ROOT / "itential" / "workflows" / "verify-aws-vpn.json").read_text())
TARGETS = build.VERSIONS["aws_vpn"]["targets"]
CDP = Path.home() / "PycharmProjects" / "cloud-devops-pipeline" / "itential"


def envelope(rc: int | None, out: dict | None = None) -> dict:
    """What runService publishes: {id, jsonrpc, result: {return_code, stdout_json}}."""
    return {"id": 1, "jsonrpc": "2.0", "result": {"return_code": rc, "stdout_json": out or {}}}


def readings(router: str = "up", data_plane: str = "up", rc: int = 0, monitor: str | None = None, mrc: int = 0) -> dict:
    d = {
        "plan": {"stdout_json": {"monitor": "aws" if monitor else "none"}},
        "lab_edge": envelope(rc, {"router": router, "data_plane": data_plane, "readings": {"ike_sa_ready": True}}),
        "monitor": {},
    }
    if monitor:
        d["monitor"] = envelope(mrc, {"monitor": monitor, "readings": {"lambda_answered": True}})
    return d


# ── the judge (the spec's rules) ──


@pytest.mark.parametrize("d, passed, verdict", [
    # the twin: router and data plane, the AWS monitor not applicable
    (readings(), True, "tunnel up: router up, data plane up (AWS monitor not applicable)"),
    (readings("down", "down"), False, "tunnel down: router down, data plane down (AWS monitor not applicable)"),
    (readings("up", "down"), False, "disagreement: router up, data plane down (AWS monitor not applicable)"),
    (readings("could not check", "down"), False, "could not check router (data plane down) (AWS monitor not applicable)"),
    # lab-edge exited non-zero (refused, or could not log in): both of its signals could not be read
    (readings(rc=1), False, "could not check router and data plane (AWS monitor not applicable)"),
    # lab-edge never ran: the {} the workflow starts with
    ({**readings(), "lab_edge": {}}, False, "could not check router and data plane (AWS monitor not applicable)"),
    # a value the judge does not know is never taken for up
    (readings("UP", "up"), False, "could not check router (data plane up) (AWS monitor not applicable)"),
    # dc1-wan01: three signals
    (readings(monitor="up"), True, "tunnel up: router up, data plane up, AWS monitor up"),
    (readings("down", "down", monitor="down"), False, "tunnel down: router down, data plane down, AWS monitor down"),
    (readings(monitor="down"), False, "disagreement: router up, data plane up, AWS monitor down"),
    (readings(monitor="disagreement"), False, "disagreement: router up, data plane up, AWS monitor disagreement"),
    (readings(monitor="could not check"), False, "could not check AWS monitor (router up, data plane up)"),
    (readings(monitor="up", mrc=1), False, "could not check AWS monitor (router up, data plane up)"),
    ({**readings(monitor="up"), "monitor": {}}, False, "could not check AWS monitor (router up, data plane up)"),
    # could not check is named first, and the readable signals after it
    (readings("down", "up", monitor="could not check"), False, "could not check AWS monitor (router down, data plane up)"),
    (readings("could not check", "up", monitor="down"), False, "could not check router (data plane up, AWS monitor down)"),
])
def test_the_judge_follows_the_specs_rules(d: dict, passed: bool, verdict: str) -> None:
    got = build.judge(d)
    assert (got["passed"], got["verdict"]) == (passed, verdict), got


def test_a_monitor_reading_never_counts_where_the_monitor_does_not_apply() -> None:
    d = readings()
    d["monitor"] = envelope(0, {"monitor": "down"})  # stray: the twin has no AWS monitor
    got = build.judge(d)
    assert got["passed"] is True and got["signals"]["aws_monitor"] == "not applicable"
    assert got["readings"]["aws_monitor"] is None


def test_the_judge_passes_readings_and_service_errors_through_and_nothing_else() -> None:
    d = readings(rc=1)
    d["lab_edge"]["result"]["stdout_json"]["error"] = "NetmikoAuthenticationException"
    got = build.judge(d)
    assert got["errors"] == {"lab_edge": "NetmikoAuthenticationException"}
    assert set(got) == {"passed", "verdict", "signals", "readings", "errors"}


def _run(code: str, data: dict) -> dict:
    run = subprocess.run([sys.executable, "-I", "-c", code], input=json.dumps(data), capture_output=True, text=True,
                         timeout=30, env={}, check=True)
    return json.loads(run.stdout)


@pytest.mark.parametrize("d", [readings(), readings("down", "down"), readings(rc=1), readings(monitor="disagreement")])
def test_the_code_the_gateway_runs_is_the_judge_tested_here(d: dict) -> None:
    assert _run(build.JUDGE_CODE, d) == build.judge(d)
    assert WF["tasks"]["5d"]["variables"]["incoming"]["code"] == build.JUDGE_CODE


# ── what to read, per target ──


def test_the_twin_reads_its_pinned_outputs_and_needs_no_terraform() -> None:
    entry = TARGETS["clab-rtr1"]
    plan = _run(build.VERIFY_PLAN_CODE, {"targets": build.VERIFY_TARGETS, "target": "clab-rtr1"})
    assert plan["need_outputs"] is False and plan["monitor"] == "none" and plan["monitor_params"] is None
    assert plan["ready"] is True and plan["reason"] == ""
    edge = plan["lab_edge"]
    assert json.loads(edge["target_json"]) == entry["target"] and json.loads(edge["outputs_json"]) == entry["outputs"]
    assert (edge["action"], edge["username"], edge["timeout"]) == ("verify", entry["username"], build.LAB_EDGE_TIMEOUT)


def test_an_aws_target_reads_terraforms_outputs_first_then_checks_the_monitor() -> None:
    # dc1-wan01's entry as it will be once its window opens (its outputs come from the deployment)
    entry = {k: TARGETS["dc1-wan01"][k] for k in ("target", "username", "monitor")}
    targets = {"dc1-wan01": entry}
    first = _run(build.VERIFY_PLAN_CODE, {"targets": targets, "target": "dc1-wan01"})
    assert first["need_outputs"] is True and first["lab_edge"] is None and first["monitor_params"] is None
    assert first["ready"] is False
    deployed = {"strongswan_eip": "203.0.113.7", "strongswan_instance_id": "i-0123456789abcdef0"}
    second = _run(build.VERIFY_PLAN_CODE, {"targets": targets, "target": "dc1-wan01", "deployed": deployed})
    assert second["need_outputs"] is False and json.loads(second["lab_edge"]["outputs_json"]) == deployed
    assert second["monitor_params"] == {"action": "check", "instance_id": "i-0123456789abcdef0",
                                        "timeout": build.MONITOR_TIMEOUT}
    assert second["ready"] is True
    assert int(build.MONITOR_TIMEOUT) >= 200  # the Lambda's 75 s plus up to 90 s for its datapoint
    # nothing deployed: the state has no outputs, so there is nothing to read and the job says why
    empty = _run(build.VERIFY_PLAN_CODE, {"targets": targets, "target": "dc1-wan01", "deployed": {}})
    assert empty["ready"] is False and empty["need_outputs"] is False and "nothing is deployed" in empty["reason"]
    # a target that is not carried (closed, or unknown) is refused with a reason, never read
    gone = _run(build.VERIFY_PLAN_CODE, {"targets": targets, "target": "nope"})
    assert gone["ready"] is False and gone["need_outputs"] is False and gone["reason"].endswith("is not an open target")


PIN = build.VERSIONS["terraform_run"]["repository"]["reference"]


def _action_args(script: str, action: str) -> set[str]:
    """ACTION_ARGS[action] of a service AT THE GATEWAY'S PIN (git show), never whatever the clone has checked out."""
    import ast

    source = subprocess.run(["git", "-C", str(CDP), "show", f"{PIN}:itential/{script}"], capture_output=True,
                            text=True, check=True).stdout
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "ACTION_ARGS" for t in n.targets))
    value = next(v for k, v in zip(node.value.keys, node.value.values) if ast.literal_eval(k) == action)
    return ast.literal_eval(value)


def _pin_present() -> bool:
    return CDP.exists() and subprocess.run(["git", "-C", str(CDP), "cat-file", "-e", f"{PIN}^{{commit}}"],
                                           capture_output=True).returncode == 0


@pytest.mark.skipif(not _pin_present(), reason="needs the cloud-devops-pipeline clone with the pinned commit")
def test_the_params_are_exactly_what_each_service_takes() -> None:
    plan = _run(build.VERIFY_PLAN_CODE, {"targets": build.VERIFY_TARGETS, "target": "clab-rtr1"})
    assert set(plan["lab_edge"]) == _action_args("lab-edge.py", "verify")
    aws = {"dc1-wan01": {k: TARGETS["dc1-wan01"][k] for k in ("target", "username", "monitor")}}
    plan = _run(build.VERIFY_PLAN_CODE, {"targets": aws, "target": "dc1-wan01",
                                         "deployed": {"strongswan_eip": "203.0.113.7", "strongswan_instance_id": "i-0123456789abcdef0"}})
    assert set(plan["monitor_params"]) == _action_args("aws-vpn-monitor.py", "check")
    assert set(json.loads(build.OUTPUTS_PARAMS)) == _action_args("terraform-run.py", "outputs")


# ── reads only, open targets only ──


def test_verify_only_reads() -> None:
    tasks = {tid: t for tid, t in WF["tasks"].items() if "variables" in t}
    names = {t["name"] for t in tasks.values()}
    assert not names & {"sendConfig", "sendCommand", "ViewData", "childJob"}, names
    services = {t["variables"]["incoming"]["serviceName"] for t in tasks.values() if t["name"] == "runService"}
    assert services == {"lab-edge", "aws-vpn-monitor", "terraform-run"}  # never lab-edge-push
    # every action the services are handed is a read
    assert json.loads(build.OUTPUTS_PARAMS)["action"] == "outputs"
    assert '"action": "verify"' in build.VERIFY_PLAN_CODE and '"action": "check"' in build.VERIFY_PLAN_CODE
    assert '"action": ' not in build.VERIFY_PLAN_CODE.replace('"action": "verify"', "").replace('"action": "check"', "")


def test_only_open_targets_get_in_and_are_carried() -> None:
    open_targets = sorted(n for n, e in TARGETS.items() if e["window"] == "open")
    assert build.INPUT_GATES["Verify AWS VPN"] == {"target": {"enum": open_targets}}
    assert sorted(build.VERIFY_TARGETS) == open_targets
    assert WF["inputSchema"]["properties"]["target"]["enum"] == open_targets
    assert "psk" not in json.dumps(build.VERIFY_TARGETS)  # no Vault path or key name in a job document


def test_every_path_reaches_the_judge_or_ends_with_a_reason() -> None:
    tr, tasks = WF["transitions"], WF["tasks"]
    # the twin skips Terraform; a lab-edge or a monitor that fails is still judged
    assert tr["1c"]["3e"]["state"] == "failure" and tr["1c"]["2a"]["state"] == "success"
    # nothing to read (nothing deployed): the plan's own reason ends the job
    assert tr["3e"]["3f"]["state"] == "failure" and tasks["3f"]["variables"]["outgoing"]["return_data"] == "$var.job.error"
    assert tr["3b"]["4a"]["state"] == "error" and tr["3c"]["3d"]["state"] == "failure" and "4a" in tr["3d"]
    assert tr["4a"]["5a"]["state"] == "failure" and tr["4c"]["5a"]["state"] == "error" and "5a" in tr["4e"]
    for end in ("8a", "8b", "8c"):
        assert list(tr[end]) == ["workflow_end"], end
    # the verdict reads: a judge that printed none still ends with a reason (8c), never a dead end
    for read in ("5f", "50", "3f"):
        assert tr[read]["workflow_end"]["state"] == "success" and tr[read]["8c" if read != "3f" else "8b"]["state"] == "error"
    assert tasks["5f"]["variables"]["outgoing"]["return_data"] == "$var.job.outcome"
    assert tasks["50"]["variables"]["outgoing"]["return_data"] == "$var.job.error"


# ── every committed workflow is what build.py makes ──


@pytest.mark.parametrize("builder", build.BUILDERS, ids=lambda b: b.__name__)
def test_every_committed_workflow_is_what_the_generator_makes(builder) -> None:
    wf = builder()
    committed = json.loads((ROOT / "itential" / "workflows" / build.file_name(wf["name"])).read_text())
    assert committed == json.loads(json.dumps(wf)), f"run .venv/bin/python itential/workflows/build.py ({wf['name']})"
