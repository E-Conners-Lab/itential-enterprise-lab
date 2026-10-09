"""R7, the Batfish pre-change proof (ADR 0076, PID 1.47): Batfish on tools-01 pinned by digest and admitted from the Gateway
VM only, batfish-check pinned with the cloud-devops-pipeline commit and installed with hashes, Hand Off's section between
the render and the card (a failed proof ends Hand Off with nothing sent; an unavailable Batfish says "not proven" on the
card), and Drill Batfish Gate holding an identical copy of that section so each check is proven able to fail."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("wf_build_batfish", ROOT / "itential" / "workflows" / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)
VERSIONS = build.VERSIONS
HA2 = yaml.safe_load((ROOT / "itential" / "ha2" / "versions.yaml").read_text())
WFS = ROOT / "itential" / "workflows"
HAND = json.loads((WFS / "hand-off-aws-vpn.json").read_text())
DRILL = json.loads((WFS / "drill-batfish-gate.json").read_text())
RUNNER = ROOT / "itential" / "gateway5-runner"
TOOLS_PLAY = (ROOT / "ansible" / "playbooks" / "platform-ha2-tools.yml").read_text()
COMPOSE = (ROOT / "itential" / "ha2" / "tools.compose.yml.j2").read_text()
SECTION = ("f8", "f0", "f1", "fb", "f2", "f3", "f4", "f5", "f6", "f7")
OPEN = sorted(n for n, t in VERSIONS["aws_vpn"]["targets"].items() if t["window"] == "open")


def _run(code: str, data: dict) -> dict:
    out = subprocess.run([sys.executable, "-c", code], input=json.dumps(data), capture_output=True, text=True,
                         check=True)
    return json.loads(out.stdout)


def _tasks(wf: dict) -> dict:
    return {tid: t for tid, t in wf["tasks"].items() if "variables" in t}


def _without_location(t: dict) -> dict:
    return {k: v for k, v in t.items() if k != "nodeLocation"}


def _reach(wf: dict, start: str) -> set[str]:
    seen, todo = set(), [start]
    while todo:
        node = todo.pop()
        if node not in seen:
            seen.add(node)
            todo.extend(wf["transitions"].get(node, {}))
    return seen


# ── the pins: Batfish by digest, pybatfish by hash, the service by commit (ADR 0076 decisions 1 and 7) ──


def test_batfish_is_pinned_by_digest_and_runs_on_the_tools_vm() -> None:
    img = VERSIONS["images"]["batfish"]
    assert img["repository"] == "docker.io/batfish/batfish" and img["tag"] != "latest"
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", img["digest"])
    assert "image: {{ images.batfish.repository }}@{{ images.batfish.digest }}" in COMPOSE  # the digest, never the tag
    tools = next(v for v in HA2["vms"] if v["name"] == "tools-01")
    assert VERSIONS["aws_vpn"]["batfish"] == {"host": tools["ip"], "port": HA2["tools"]["batfish_port"],
                                              "drills": ["none", "aws-open", "acl-any", "inet-open"]}
    assert '"{{ ip }}:{{ tools.batfish_port }}:9996"' in COMPOSE  # the OOB address only, never 0.0.0.0
    assert 'JAVA_TOOL_OPTIONS: "-Xmx{{ tools.batfish_heap }}"' in COMPOSE
    assert "mem_limit: {{ tools.batfish_memory_limit }}" in COMPOSE
    assert HA2["tools"]["batfish_heap"] == "2g" and HA2["tools"]["batfish_memory_limit"] == "3g"
    batfish = COMPOSE.split("  batfish:", 1)[1].split("\n\n", 1)[0]
    assert "volumes:" not in batfish  # a snapshot lives only for the answer


def test_only_the_gateway_vm_may_reach_batfish() -> None:
    script = (ROOT / "itential" / "ha2" / "batfish-access.sh.j2").read_text()
    assert "PORT=9996" in script and "{% for src in batfish_allow %}" in script
    assert 'iptables -w -A "$CHAIN" -s {{ src }} -j ACCEPT' in script and 'iptables -w -A "$CHAIN" -j DROP' in script
    assert 'iptables -w -I DOCKER-USER 1 -p tcp --dport "$PORT" -j "$CHAIN"' in script
    assert "batfish_allow: \"{{ vms | selectattr('role', 'equalto', 'gateway') | map(attribute='ip') | list }}\"" in TOOLS_PLAY
    assert [v["ip"] for v in HA2["vms"] if v["role"] == "gateway"] == ["10.100.0.80"]  # iag-01, the runner's host
    for task in ("DOCKER-USER allowlist for Batfish", "Unit that re-applies the Batfish allowlist",
                 "Batfish allowlist unit enabled and applied now", "The Batfish allowlist is live", "Batfish answers"):
        assert task in TOOLS_PLAY, task
    assert "PartOf=docker.service" in TOOLS_PLAY.split("batfish-access.service", 1)[1].split("register: batfish_unit")[0]
    assert "/v2/networks" in TOOLS_PLAY


def test_pybatfish_is_installed_only_with_hashes_next_to_netmiko() -> None:
    req = (RUNNER / "requirements-lab-edge.txt").read_text()
    pins = dict(re.findall(r"^([a-z0-9_.-]+)==([^ \\]+)", req, re.M))
    assert pins["pybatfish"] == "2025.7.7.2423" and {"pandas", "numpy", "requests", "netmiko"} <= set(pins)
    tf = dict(re.findall(r"^([a-z0-9_.-]+)==([^ \\]+)", (RUNNER / "requirements-terraform-run.txt").read_text(), re.M))
    assert not set(pins) & set(tf)  # the three both need are listed once, in terraform-run's file
    assert {"urllib3", "python-dateutil", "six"} <= set(tf)
    assert "-c requirements-terraform-run.txt" in req  # compiled under its constraints, so the two files agree
    assert VERSIONS["stack"]["runner_image"].endswith("-pb2025.7.7.2423")


def test_the_service_is_pinned_with_the_edge_services_and_holds_only_the_password_alias() -> None:
    svc = next(s for s in VERSIONS["terraform_run"]["edge_services"] if s["name"] == "batfish-check")
    assert svc["filename"] == "itential/batfish-check.py" and svc["working-directory"] == "."
    assert svc["secrets"] == [{"name": "dc1-wan01-aws-vpn-password", "type": "env", "target": "LAB_EDGE_PASSWORD_DC1_WAN01"}]
    # cfa2bea (cdp #42) brought batfish-check; the pin moved on to its descendant 71d5223 (cdp #43) on the rebase
    assert VERSIONS["terraform_run"]["repository"]["reference"] == "71d522382fca7d09d63d3579500a9bc43fc31da3"


# ── Hand Off: the section between the render and the card (ADR 0076 decision 4) ──


def test_the_proof_runs_after_the_render_and_before_the_card() -> None:
    tasks, tr = _tasks(HAND), HAND["transitions"]
    assert tr["5e"]["f8"]["state"] == "success"
    assert tasks["f8"]["variables"]["incoming"] == {"pass_on_null": False, "query": "stdout_json.batfish",
                                                    "obj": "$var.job.handoff_plan"}
    assert tasks["f0"]["variables"]["incoming"] == {"obj": "$var.f8.return_data", "path": ["sha256"],
                                                    "value": "$var.job.sha256"}  # the approved one, as data
    assert tasks["f1"]["name"] == "runService" and tasks["f1"]["variables"]["incoming"]["serviceName"] == "batfish-check"
    assert tasks["f1"]["variables"]["incoming"]["params"] == "$var.f0.object"
    assert tasks["f1"]["variables"]["outgoing"]["result"] == "$var.job.batfish_result"
    # the exit code is evaluated (ADR 0066's rule for every service run): non-zero is not proven, on to the card
    assert tr["f1"]["fb"]["state"] == "success" and tasks["fb"]["name"] == "evaluation"
    assert tasks["fb"]["variables"]["incoming"]["evaluation_groups"][0]["evaluations"][0]["query"] == "result.return_code"
    assert tr["fb"]["f2"]["state"] == "success" and tr["fb"]["f7"]["state"] == "failure"
    assert tasks["f3"]["name"] == "runCode" and tasks["f3"]["variables"]["incoming"]["code"] == build.BATFISH_SUMMARY_CODE
    assert tasks["f4"]["variables"]["incoming"]["evaluation_groups"][0]["evaluations"][0]["query"] == "stdout_json.verdict"
    # pass and not proven reach the card (through the NetBox read-back); the card carries the entry
    for tid in ("f5", "f7"):
        assert tr[tid]["d0"]["state"] == "success" and "6f" in _reach(HAND, tid)
    assert tasks["f5"]["variables"]["outgoing"]["return_data"] == "$var.job.batfish"
    assert tasks["f7"]["variables"]["outgoing"]["output"] == "$var.job.batfish" and "not proven" in tasks["f7"]["variables"]["incoming"]["input"]
    assert tasks["69"]["variables"]["incoming"] == {"obj": "$var.6d.object", "path": ["batfish"], "value": "$var.job.batfish"}
    assert tr["6d"]["69"]["state"] == "success" and tr["69"]["6e"]["state"] == "success"
    # the branded approval (ADR 0077) renders 69's object: NetBox, then Batfish, as rows on the card
    assert tasks["6f3"]["variables"]["incoming"]["value"] == "$var.69.object"
    assert tasks["6f"]["variables"]["incoming"]["body"] == "$var.6f5.return_data"


def test_a_failed_proof_ends_hand_off_with_nothing_sent() -> None:
    tasks, tr = _tasks(HAND), HAND["transitions"]
    assert tr["f4"]["f6"]["state"] == "success" and tr["f4"]["f5"]["state"] == "failure"  # verdict == fail -> g6
    assert tr["f6"] == {"f9": {"state": "success", "type": "standard"}}
    assert tasks["f9"]["variables"]["incoming"]["newSubstr"] == "$var.job.batfish_message"
    assert tasks["f9"]["variables"]["outgoing"]["replacedString"] == "$var.job.error"
    assert tr["f9"] == {"workflow_end": {"state": "success", "type": "standard"}}
    reached = _reach(HAND, "f6")
    assert "6f" not in reached and "7c" not in reached  # no card, no push
    # the error names the failed checks and says nothing was sent (batfish_summary's message)
    out = _run(build.BATFISH_SUMMARY_CODE, {"batfish": {"result": {"return_code": 0, "stdout_json": {
        "verdict": "fail", "checks": {"parses": {"status": "pass", "detail": "ok"},
                                      "inet_in_admits_the_peer_only": {"status": "fail", "detail": "1 flow(s)"}}}}}})
    assert out["verdict"] == "fail" and out["failed"] == ["inet_in_admits_the_peer_only"]
    assert out["message"] == "the Batfish proof failed (inet_in_admits_the_peer_only): nothing was sent to the router"
    assert "inet_in_admits_the_peer_only: fail - 1 flow(s)" in out["card"] and "parses: pass" in out["card"]


def test_an_unavailable_batfish_says_not_proven_and_the_card_is_still_shown() -> None:
    tr = HAND["transitions"]
    for tid in ("f8", "f1", "f3", "f5"):  # a Gateway error anywhere in the section: not proven, on to the card
        assert tr[tid]["f7"]["state"] == "error", tid
    assert "6f" in _reach(HAND, "f7")
    for d, why in (({}, "gave no answer"),
                   ({"batfish": {"result": {"return_code": 1, "stdout_json": {"action": "check", "error": "ConnectionError"}}}},
                    "ConnectionError"),
                   ({"batfish": {"result": {"return_code": 0, "stdout_json": {"verdict": "pass"}}}}, "answered without checks")):
        out = _run(build.BATFISH_SUMMARY_CODE, d)
        assert out["verdict"] == "not proven" and why in out["card"] and "approval stays possible" in out["card"], d
        assert out["message"] == "" and out["failed"] == []


def test_a_passing_proof_is_written_on_the_card_check_by_check() -> None:
    checks = {name: {"status": "pass", "detail": "x"} for name in build.BATFISH_CHECKS}
    out = _run(build.BATFISH_SUMMARY_CODE, {"batfish": {"result": {"return_code": 0, "stdout_json": {
        "verdict": "pass", "checks": checks, "seconds": 4.2, "batfish_host": "10.100.0.81"}}}})
    assert out["verdict"] == "pass" and out["message"] == ""
    assert out["card"].startswith("Batfish: PASS (7 checks, 4.2 s on 10.100.0.81): parses: pass; ")
    assert all(f"{name}: pass" in out["card"] for name in build.BATFISH_CHECKS)
    assert build.BATFISH_CHECKS[-2:] == ("self_zone_drops_aws", "vpc_prefix_list_unchanged")  # the two text checks


def test_the_summary_runs_as_the_gateway_runs_it() -> None:
    tasks = _tasks(HAND)
    assert tasks["f3"]["variables"]["incoming"]["code"] == build.BATFISH_SUMMARY_CODE
    assert tasks["f3"]["variables"]["incoming"]["data"] == "$var.f2.object"
    assert tasks["f2"]["variables"]["incoming"] == {"obj": {}, "path": ["batfish"], "value": "$var.job.batfish_result"}


# ── Drill Batfish Gate: the same section, a broken candidate, no card (ADR 0076 decision 6) ──


def test_the_drill_is_registered_gated_and_takes_a_target_and_a_mode() -> None:
    name = VERSIONS["workflows"]["drill_batfish_gate"]
    assert DRILL["name"] == name == "Drill Batfish Gate" and build.drill_batfish_gate in build.BUILDERS
    props = DRILL["inputSchema"]["properties"]
    assert props["target"]["enum"] == OPEN and props["drill"]["enum"] == ["none", "aws-open", "acl-any", "inet-open"]
    assert build.INPUT_GATES[name] == {"target": {"enum": OPEN},
                                       "drill": {"type": "string", "enum": ["none", "aws-open", "acl-any", "inet-open"]}}
    assert name not in VERSIONS["operations_manager"]  # started by the verify (jobs/start), like Break Fabric BGP


def test_the_drills_section_is_hand_offs_own_copy() -> None:
    hand, drill = _tasks(HAND), _tasks(DRILL)
    for tid in SECTION:
        assert _without_location(hand[tid]) == _without_location(drill[tid]), tid
    # and the render before it is the same render (same service, same params path, same published SHA-256)
    for tid in ("5a", "5b", "5c", "5d", "5e"):
        assert _without_location(hand[tid]) == _without_location(drill[tid]), tid
    # the section's own edges are the same; only where they lead afterwards differs
    for tid in ("f8", "f0", "f1", "fb", "f2", "f3", "f4"):
        assert HAND["transitions"][tid] == DRILL["transitions"][tid], tid
    assert DRILL["transitions"]["f5"]["a0"]["state"] == "success" and DRILL["transitions"]["f7"]["a0"]["state"] == "success"
    assert DRILL["transitions"]["f6"] == {"a0": {"state": "success", "type": "standard"}}


def test_the_drill_sends_nothing_and_shows_no_card() -> None:
    tasks = _tasks(DRILL)
    services = {t["variables"]["incoming"]["serviceName"] for t in tasks.values() if t["name"] == "runService"}
    assert services == {"terraform-run", "lab-edge", "batfish-check"}  # reads, a render and the proof: never the push
    assert not [tid for tid, t in DRILL["tasks"].items() if t.get("type") == "manual"]
    assert "lab-edge-push" not in json.dumps(DRILL) and "config-push-revert" not in json.dumps(DRILL)


def test_the_drill_mode_reaches_the_service_through_the_plan() -> None:
    tasks = _tasks(DRILL)
    assert tasks["1b"]["variables"]["incoming"] == {"obj": "$var.1a.object", "path": ["drill"], "value": "$var.job.drill"}
    assert tasks["1b"]["variables"]["outgoing"]["object"] == "$var.job.handoff_in"
    assert tasks["1c"]["variables"]["incoming"]["code"] == build.LAB_EDGE_PLAN_CODE
    base = {"targets": build.VERIFY_TARGETS, "target": "dc1-wan01", "deployed": {"strongswan_eip": "203.0.113.7"}}
    for mode in ("aws-open", "acl-any", "inet-open"):
        plan = _run(build.LAB_EDGE_PLAN_CODE, {**base, "drill": mode})
        assert plan["batfish"]["drill"] == mode and plan["batfish"]["action"] == "check"
    assert "drill" not in _run(build.LAB_EDGE_PLAN_CODE, {**base, "drill": "none"})["batfish"]  # the healthy one
    assert "drill" not in _run(build.LAB_EDGE_PLAN_CODE, base)["batfish"]  # Hand Off never passes one
    assert _run(build.LAB_EDGE_PLAN_CODE, base)["batfish"]["batfish_host"] == VERSIONS["aws_vpn"]["batfish"]["host"]
    assert _run(build.LAB_EDGE_PLAN_CODE, base)["batfish"]["timeout"] == build.BATFISH_TIMEOUT


@pytest.mark.parametrize("mode, verdict, failed, passed", [
    ("none", "pass", [], True),
    ("none", "fail", ["parses"], False),
    ("none", "not proven", [], False),
    ("aws-open", "fail", ["aws_reaches_no_lab_address", "self_zone_drops_aws"], True),
    ("aws-open", "fail", ["aws_reaches_no_lab_address", "self_zone_drops_aws", "parses"], True),  # more may fail
    ("aws-open", "fail", ["self_zone_drops_aws"], False),  # the simulated check did not catch it
    ("acl-any", "fail", ["lab_reaches_aws_only_from_pinned_prefixes"], True),
    ("acl-any", "pass", [], False),
    ("inet-open", "fail", ["inet_in_admits_the_peer_only", "nothing_else_gained_or_lost"], True),
    ("inet-open", "not proven", [], False),
    ("everything", "fail", ["parses"], False),
])
def test_the_judge_expects_what_each_mode_breaks(mode: str, verdict: str, failed: list, passed: bool) -> None:
    out = _run(build.DRILL_JUDGE_CODE, {"drill": mode, "summary": {"stdout_json": {"verdict": verdict, "failed": failed}}})
    assert out["drill_passed"] is passed, out
    assert out["outcome"].startswith(f"drill {mode} {'passed' if passed else 'FAILED'}") or mode == "everything"
    assert build.DRILL_EXPECTS == {"none": [], "aws-open": ["aws_reaches_no_lab_address", "self_zone_drops_aws"],
                                   "acl-any": ["lab_reaches_aws_only_from_pinned_prefixes"],
                                   "inet-open": ["inet_in_admits_the_peer_only", "nothing_else_gained_or_lost"]}
    assert set(build.DRILL_EXPECTS) == set(VERSIONS["aws_vpn"]["batfish"]["drills"])


def test_the_drill_publishes_its_verdict() -> None:
    tasks, tr = _tasks(DRILL), DRILL["transitions"]
    assert tasks["a2"]["variables"]["incoming"]["code"] == build.DRILL_JUDGE_CODE
    assert tasks["a1"]["variables"]["incoming"] == {"obj": "$var.a0.object", "path": ["summary"],
                                                    "value": "$var.job.batfish_summary"}
    assert tasks["a3"]["variables"]["outgoing"]["return_data"] == "$var.job.outcome"
    assert tr["a4"]["a5"]["state"] == "success" and tr["a4"]["a6"]["state"] == "failure"
    assert tasks["a5"]["variables"]["incoming"]["input"] == "true" and tasks["a6"]["variables"]["incoming"]["input"] == "false"
    assert tasks["a5"]["variables"]["outgoing"]["output"] == "$var.job.drill_passed"
    for b in ("b0", "b1", "b6", "b8", "c3"):
        assert tr[b] == {"workflow_end": {"state": "success", "type": "standard"}}
        assert tasks[b]["variables"]["outgoing"]["output"] == "$var.job.error"


def test_the_verify_runs_every_mode_and_sweeps_the_drill() -> None:
    script = (ROOT / "verify" / "test-13a-aws-vpn.sh").read_text()
    assert 'check "S13.R7a Drill Batfish Gate' in script and "vpn drill" in script
    helper = (ROOT / "verify" / "awsvpncheck.py").read_text()
    assert '"drill": c_drill' in helper and 'V["aws_vpn"]["batfish"]["drills"]' in helper
    assert 'variables.get("drill_passed") is True' in helper


def test_the_tools_compose_file_has_no_empty_top_level_section() -> None:
    # Compose refuses "volumes must be a mapping": the Ollama removal (#36) took the last named volume and left the
    # key behind, and the first tools play after it (R7, 2026-10-09) failed on tools-01 before Batfish came up.
    # Rendered with the pinned values the play renders with: each top-level key must be a mapping.
    import jinja2

    ha2 = yaml.safe_load((ROOT / "itential" / "ha2" / "versions.yaml").read_text())
    tools_vm = next(vm for vm in ha2["vms"] if vm["role"] == "tools")
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    rendered = env.from_string(COMPOSE).render(**{**VERSIONS, **ha2}, ip=tools_vm["ip"])  # the play's two vars files
    doc = yaml.safe_load(rendered)
    for key, value in doc.items():
        assert isinstance(value, dict), f"top-level `{key}:` is {value!r}, not a mapping - Compose refuses the file"
    assert "batfish" in doc["services"]
