"""A4 Cloud Concierge (ADR 0085 decisions 11-14): an agent in the Platform's chat that turns a plain request into a
run of Deploy AWS VPN or Tear Down AWS VPN. It holds the status read and two request tools only; each tool starts the
real workflow through its endpoint trigger, so the run stops at its own Work Center card and change request, and
answers at once with the job and the change request. An agent reads every variable of a tool's job, so the on-prem
address and ServiceNow's HTTP responses stay in task outputs. No lab access."""

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
_SPEC = importlib.util.spec_from_file_location("wf_build_concierge", ROOT / "itential" / "workflows" / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)
VERSIONS = yaml.safe_load((ROOT / "itential" / "versions.yaml").read_text())
WF = VERSIONS["workflows"]
OM = VERSIONS["operations_manager"]
AGENTS = ROOT / "itential" / "agents"
WFS = ROOT / "itential" / "workflows"
TOOLS = {"deploy": WF["request_aws_vpn_deployment"], "teardown": WF["request_aws_vpn_teardown"]}
DOCS = {kind: json.loads((WFS / build.file_name(name)).read_text()) for kind, name in TOOLS.items()}
STARTS = {"deploy": WF["deploy_aws_vpn"], "teardown": WF["tear_down_aws_vpn"]}
# what a tool's job may publish: an agent reads every job variable (measured 2026-10-31 on a tool call's output);
# input_check is the input gate's own
PUBLISHED = {"started_job", "change_number", "outcome", "error", "input_check", "change_wait"}
# the deploy tool also says whose address it reused, in words (a job id and a time, never the address)
ANSWER = {"deploy": PUBLISHED | {"address_from"}, "teardown": PUBLISHED}
JOB = "0123456789abcdef01234567"


def _run(code: str, data: dict) -> dict:
    run = subprocess.run([sys.executable, "-I", "-c", code], input=json.dumps(data), capture_output=True, text=True,
                         timeout=30, env={}, check=True)
    return json.loads(run.stdout)


def _agent(name: str) -> dict:
    return yaml.safe_load((AGENTS / f"{name}.yaml").read_text())


# --- the agent --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["cloud-concierge", "cloud-concierge-local"])
def test_the_concierge_holds_the_status_read_and_the_two_request_tools_only(name: str) -> None:
    doc = _agent(name)
    assert doc["project"] == "lab-netops"
    assert doc["profile"] == ("ollama-mac" if name.endswith("-local") else "anthropic")
    assert {t["reference"] for t in doc["tools"]} == {WF["get_aws_vpn_status"], *TOOLS.values()}
    assert all(t["kind"] == "workflow" for t in doc["tools"]), "no integration, Gateway service or Work Center tool"
    assert "expose_route" not in doc and "operator_groups" not in doc


def test_the_twin_has_the_same_instructions_and_tools() -> None:
    claude, twin = _agent("cloud-concierge"), _agent("cloud-concierge-local")
    assert claude["instructions"] == twin["instructions"] and claude["tools"] == twin["tools"]


def test_the_instructions_say_it_cannot_approve_and_never_guesses_the_address() -> None:
    text = " ".join(_agent("cloud-concierge")["instructions"].split())
    assert "you cannot approve" in text and "a person approves it in Work Center" in text
    assert 'onprem_public_ip: "last"' in text and "Never guess or make up an address" in text
    assert 'target "dc1-wan01"' in text
    for hours in VERSIONS["aws_vpn"]["lifetime_hours"]["choices"]:
        assert re.search(rf"\b{hours}\b", text), hours
    # the note the agent writes must fit the tool's gate, which leaves room for the Concierge's mark
    words = int(re.search(r"at most (\d+) characters", text).group(1))
    assert words <= build.CONCIERGE_NOTE_MAX


def test_a_refusal_says_it_cannot_before_what_it_can_do() -> None:
    """With the offer first in step 4, Claude led with it and ended "aren't something this workflow controls", no plain
    refusal (2026-10-31, after #216). The approval line, which says "say plainly that you cannot", held on both
    models: step 4 asks for the same, before the offer."""
    text = " ".join(_agent("cloud-concierge")["instructions"].split())
    step = text[text.index("4. Refuse"):text.index("5. Answer")]
    order = ["saying plainly that you cannot", "say what you can do instead", "What you refuse"]
    assert [step.index(s) for s in order] == sorted(step.index(s) for s in order)


def test_only_hand_off_and_verify_are_sent_to_the_branded_page() -> None:
    """Both models sent a region and size request to the branded page when one parenthetical closed the whole refusal
    list (L-016, 2026-10-31): the page is named only in the item it runs, and its Deploy form takes neither."""
    text = _agent("cloud-concierge")["instructions"]
    items = [" ".join(i.split()) for i in re.split(r"\n\s*(?:\d+\.|-) ", text)]
    paged = [i for i in items if "branded page" in i]
    assert paged and all(i.startswith("running Hand Off or Verify") for i in paged), paged
    refused = next(i for i in items if i.startswith("another AWS region"))
    assert "never send the person to a page" in refused
    # the branded page and the Deploy form behind it
    for page in (ROOT / "itential" / "portal" / "deploy-aws-vpn" / "index.html",
                 ROOT / "itential" / "forms" / "lab-deploy-aws-vpn.json"):
        assert not re.search(r"region|instance_type|instance size", page.read_text(), re.I), f"{page.name} takes one"


# --- the request tools ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(TOOLS))
def test_each_tool_starts_its_workflow_only_through_that_workflows_endpoint(kind: str) -> None:
    tasks = DOCS[kind]["tasks"]
    starts = [t for t in tasks.values() if t.get("name") == "runEndpointTriggerWithPost"]
    assert starts, kind
    route = OM["deploy_aws_vpn" if kind == "deploy" else "tear_down_aws_vpn"]["endpoint"]["route"]
    assert {t["variables"]["incoming"]["routeName"] for t in starts} == {route}
    # nothing else can run or move a job: no child job, no direct start, no card and no task finished from here
    names = {t.get("name") for t in tasks.values()}
    assert not names & {"childJob", "startJob", "finishManualTask", "claimTask", "cancelJobs", "runService"}
    assert not [t for t in tasks.values() if t.get("type") == "manual"]
    reads = {t["name"] for t in tasks.values() if t.get("app") == "OperationsManager"} - {"runEndpointTriggerWithPost"}
    assert reads == ({"getJobs", "getJob"} if kind == "deploy" else set())


@pytest.mark.parametrize("kind", sorted(TOOLS))
def test_a_tools_job_publishes_only_its_answer(kind: str) -> None:
    published = {v.removeprefix("$var.job.") for t in DOCS[kind]["tasks"].values()
                 for v in (t.get("variables") or {}).get("outgoing", {}).values()
                 if isinstance(v, str) and v.startswith("$var.job.")}
    assert published == ANSWER[kind], published
    assert set(DOCS[kind]["outputSchema"]["properties"]) == ANSWER[kind]
    # the address, the started run's document and ServiceNow's responses (headers, a session cookie) stay on the task
    for t in DOCS[kind]["tasks"].values():
        if t.get("name") in ("getJobs", "getJob", "runEndpointTriggerWithPost", "listChangeRequests"):
            assert set(t["variables"]["outgoing"].values()) == {None}, t["summary"]


def test_the_deploy_tool_hands_the_address_to_the_start_without_publishing_it() -> None:
    tasks, tr = DOCS["deploy"]["tasks"], DOCS["deploy"]["transitions"]
    starts = sorted(tid for tid, t in tasks.items() if t.get("name") == "runEndpointTriggerWithPost")
    setters = [tasks[tid]["variables"]["incoming"]["body"].split(".")[1] for tid in starts]  # $var.<task>.object
    for tid, setter in zip(starts, setters):
        assert tasks[setter]["variables"]["incoming"]["path"] == ["onprem_public_ip"]
        assert tid in tr[setter] or any(tid in tr.get(n, {}) for n in tr[setter]), (setter, tid)  # via a note
    # last: the checked address of the newest person-started Deploy (a task output); otherwise the person's own input
    assert sorted(tasks[s]["variables"]["incoming"]["value"] for s in setters) == [
        "$var.29.return_data", "$var.job.onprem_public_ip"]
    assert tasks["29"]["variables"]["outgoing"]["return_data"] is None
    assert tasks["27"]["variables"]["outgoing"]["result"] is None


@pytest.mark.parametrize("kind", sorted(TOOLS))
def test_the_change_request_is_found_by_the_started_jobs_id(kind: str) -> None:
    tasks = DOCS[kind]["tasks"]
    query = tasks["3f"]["variables"]["incoming"]
    assert query["str"] == "short_descriptionLIKE__J__" and query["newSubstr"] == "$var.job.started_job"
    reads = [t for t in tasks.values() if t.get("name") == "listChangeRequests"]
    assert len(reads) == len(build.CONCIERGE_LOOKUPS)
    assert all(t["variables"]["incoming"]["sysparm_query"] == "$var.3f.replacedString" for t in reads)
    # R5's side of the link: the run's change request carries its job id in the short description
    opened = _run(build.CHANGE_OPEN_CODE, {"workflow": STARTS[kind], "target": "dc1-wan01", "job_id": JOB})
    assert opened["create"]["short_description"].endswith(f"job {JOB})")


def test_the_gates_are_the_started_workflows_own_bounds() -> None:
    deploy = build.INPUT_GATES[TOOLS["deploy"]]
    assert deploy["lifetime_hours"] == build.INPUT_GATES[STARTS["deploy"]]["lifetime_hours"]
    assert deploy["enable_nat_gateway"] == build.INPUT_GATES[STARTS["deploy"]]["enable_nat_gateway"]
    teardown = build.INPUT_GATES[TOOLS["teardown"]]
    assert teardown["target"] == build.INPUT_GATES[STARTS["teardown"]]["target"]
    # the marked note still fits the started workflow's 280 characters
    assert len(build.CONCIERGE_PREFIX) + build.CONCIERGE_NOTE_MAX == 280
    assert deploy["change_note"] == teardown["change_note"] == build.CONCIERGE_NOTE_RULE
    address = re.compile(deploy["onprem_public_ip"]["pattern"])
    assert address.match("last") and address.match("203.0.113.7")
    for bad in ("Last", "last ", "", "203.0.113", "203.0.113.256", "a.b.c.d", "guess"):
        assert not address.match(bad), bad


# --- the code the runner runs -----------------------------------------------------------------------------------------


def test_the_note_is_marked_as_the_concierges_and_the_address_never_rides_along() -> None:
    code = build._pure_code(build.concierge_body, {"prefix": build.CONCIERGE_PREFIX})
    out = _run(code, {"change_note": "  demo   for the team ", "enable_nat_gateway": "false", "lifetime_hours": "4",
                      "onprem_public_ip": "203.0.113.7"})
    assert out == {"body": {"change_note": "Cloud Concierge: demo for the team", "enable_nat_gateway": "false",
                            "lifetime_hours": "4"}}
    bare = _run(code, {"change_note": "", "target": "dc1-wan01"})
    assert bare == {"body": {"change_note": "Cloud Concierge: (no reason given)", "target": "dc1-wan01"}}


def test_the_last_address_comes_from_the_newest_deploy_a_person_started() -> None:
    code = build._pure_code(build.newest_person_deploy, {})
    rows = [{"_id": "a" * 24, "metrics": {"user": "Pronghorn"}}, {"_id": "b" * 24, "metrics": {}},
            {"_id": "c" * 24, "metrics": {"user": "6aa2bdfddd92a98c6d310e34"}}, {"_id": "d" * 24}]
    assert _run(code, {"jobs": {"data": rows}}) == {"found": True, "job": "c" * 24,
                                                    "from": f"the on-prem address of Deploy AWS VPN job {'c' * 24}"}
    rows[2]["metrics"]["start_time"] = 1793290871492
    assert _run(code, {"jobs": {"data": rows}})["from"].endswith(", started 2026-10-29 16:21 UTC")
    assert _run(code, {"jobs": {"data": rows[:2]}}) == {"found": False}
    assert _run(code, {}) == {"found": False}
    check = build._pure_code(build.last_address, {})
    for ip, ok in (("203.0.113.7", True), ("203.0.113.256", False), ("203.0.113", False), ("010.1.1.1", False),
                   (None, False), ({"location": "job_data"}, False)):
        out = _run(check, {"job": {"data": {"variables": {"onprem_public_ip": ip}}}})
        assert out == {"ok": ok, "ip": ip if ok else None}, ip


def test_the_answer_names_the_job_the_change_the_card_and_the_next_step() -> None:
    code = build._pure_code(build.concierge_answer, {"kind": "deploy", "job_url": "https://x/jobs/"})
    out = _run(code, {"job": JOB, "change_number": "CHG0030050", "lifetime_hours": "4",
                      "address_from": "the on-prem address of Deploy AWS VPN job x"})
    text = out["outcome"]
    assert out["change_number"] == "CHG0030050"
    for part in (JOB, "https://x/jobs/" + JOB, "CHG0030050", '"Approve the AWS change"',
                 "Nothing is created in AWS before that approval", "4 hours", "Hand Off AWS VPN",
                 "It uses the on-prem address of Deploy AWS VPN job x", "reject the waiting card"):
        assert part in text, part
    forever = _run(code, {"job": JOB, "change_number": "", "lifetime_hours": "none"})
    assert forever["change_number"] == "" and "No change request appeared" in forever["outcome"]
    assert "no end time" in forever["outcome"]
    down = _run(build._pure_code(build.concierge_answer, {"kind": "teardown", "job_url": ""}),
                {"job": JOB, "change_number": "CHG0030051", "target": "dc1-wan01"})["outcome"]
    assert "dc1-wan01" in down and '"Approve removing the router\'s AWS block"' in down
    assert '"Approve destroying the AWS side"' in down


def test_the_verify_runs_the_concierge_only_on_requests_that_start_nothing() -> None:
    text = (ROOT / "verify" / "test-06-flowai.sh").read_text()
    for name in ("cloud-concierge", "cloud-concierge-local"):
        assert f"conciergecheck.py refusals {name}" in text
    # deploy and teardown start real runs: the owner runs them by hand
    assert not re.search(r"\$\{PY\} verify/conciergecheck\.py (deploy|teardown)", text)


def test_the_refusal_check_fails_a_reply_that_sends_the_person_to_a_page() -> None:
    spec = importlib.util.spec_from_file_location("conciergecheck", ROOT / "verify" / "conciergecheck.py")
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)
    assert [no_page for _, what, no_page in check.REFUSALS] == [False, True], "only the region and size refusal"
    # the two replies of 2026-10-31 (L-016), other ways to say it, then refusals that point nowhere
    sent = ["That would need to be handled separately (e.g., on the branded Hand Off/Verify page or via direct "
            "infrastructure change).",
            "For the eu-west-1 region and the t3.large instance size, you'll need to use the branded page directly.",
            "Head to the branded page to change it.", "Go to the Deploy page.", "The branded page can do that.",
            "Please use the lab's branded page.", "Do it on the 'Deploy AWS VPN' page.",
            "Use the Itential branded AWS VPN deploy page.", "The lab's portal has that option.",
            # a denial in another clause than the page
            "I can't do that here; use the branded page instead.", "No, use the branded page.",
            "Do not worry, the branded page does it.", "Not here - use the branded page.",
            "No - the branded page does that.", "There is no tool but the portal for that.",
            "I'm not sure but the branded page can.", "I do not have it but the branded page does.",
            "Not via me; not via chat but the branded page."]
    kept = ["I can't do that: no page or tool changes the region or the instance size; that needs a change to the lab.",
            "I cannot pick eu-west-1 or t3.large. I can bring the whole VPN up for 2, 4, 8, 24 or 72 hours, or tear it "
            "all down.",
            "I cannot change the region. I can bring the whole VPN up, and the run waits on its Work Center page.",
            # the two replies of 2026-10-31 after #219: Claude's failed the first form of this check
            "Those require a change to the lab itself, not something a tool or page can do.",
            "Those would require a change to the lab itself, and no page or tool of mine can make that change, so I "
            "won't send you off to one.",
            "That is not something the branded page can do either.", "There isn't a page for that.",
            "Nothing on the branded page does that.", "None of the pages can change the region.",
            "The branded page can't do that either.", "That is not something the branded Deploy page can do."]
    assert [r for r in sent if not check.sends_to_page(r)] == []
    assert [r for r in kept if check.sends_to_page(r)] == []


def test_a_start_whose_id_cannot_be_read_says_it_started() -> None:
    """A run that started must never be reported as not started: the person would ask again and get a second run."""
    for kind in TOOLS:
        tasks, tr = DOCS[kind]["tasks"], DOCS[kind]["transitions"]
        for tid, t in tasks.items():
            if t.get("name") == "runEndpointTriggerWithPost":
                idj = next(d for d, e in tr[tid].items() if e["state"] == "success")
                failed = next(d for d, e in tr[idj].items() if e["state"] == "failure")
                said = tasks[failed]["variables"]["incoming"]["input"]
                assert "was started" in said and "do not request again" in said, (kind, said)


def test_the_change_number_is_empty_until_one_is_found() -> None:
    for kind in TOOLS:
        first = DOCS[kind]["tasks"]["12"]
        assert first["variables"]["incoming"]["text"] == '""'
        assert first["variables"]["outgoing"]["textObject"] == "$var.job.change_number"
