"""platform-guide (ADR 0081): the Platform explains its own workflows in conversation. The agent's one tool, Describe
for an Agent, runs platform-describe without a card and publishes only the guide and a summary; the agent and its
local twin hold the same rules (answer only from the tool, never invent a step or a count); the verify runs the twin;
the eval set is in scripts/. These run in CI with no lab access."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("wf_build_guide", ROOT / "itential" / "workflows" / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)
build.LAYOUT_SEARCH = True
_DSPEC = importlib.util.spec_from_file_location("wf_describe_guide", ROOT / "itential" / "workflows" / "describe.py")
describe = importlib.util.module_from_spec(_DSPEC)
_DSPEC.loader.exec_module(describe)
VERSIONS = build.VERSIONS
NAME = "Describe for an Agent"
WF = build.describe_for_agent()
TASKS = {k: t for k, t in WF["tasks"].items() if k not in ("workflow_start", "workflow_end")}
AGENTS = ROOT / "itential" / "agents"
AGENT = yaml.safe_load((AGENTS / "platform-guide.yaml").read_text())
TWIN = yaml.safe_load((AGENTS / "platform-guide-local.yaml").read_text())
VERIFY = ROOT / "verify" / "test-06-flowai.sh"
EVAL = ROOT / "scripts" / "eval-platform-guide.py"
ADR = ROOT / "docs" / "adr" / "0081-the-platform-explains-its-workflows-in-conversation.md"


# --- the tool ---------------------------------------------------------------------------------------------------------
def test_the_committed_document_is_this_build() -> None:
    committed = json.loads((ROOT / "itential" / "workflows" / build.file_name(NAME)).read_text())
    assert committed == json.loads(json.dumps(WF))
    assert build.describe_for_agent in build.BUILDERS and VERSIONS["workflows"]["describe_for_agent"] == NAME


def test_it_only_reads_and_opens_no_card() -> None:
    """One Gateway service (read-only by its own code) and nothing else leaves the engine: no card, no runCode."""
    outside = sorted({(t["app"], t["name"]) for t in TASKS.values() if t["app"] != "WorkFlowEngine"})
    assert outside == [("GatewayManager", "runService")]
    services = {t["variables"]["incoming"]["serviceName"] for t in TASKS.values() if t["name"] == "runService"}
    assert services == {"platform-describe"}
    assert not [t for t in TASKS.values() if t.get("type") == "manual"]


def test_what_and_which_go_in_as_data_behind_the_gate() -> None:
    gate = build.INPUT_GATES[NAME]
    assert gate["what"]["enum"] == ["list", "workflow", "job"]
    assert gate["which"]["pattern"] == r"^[A-Za-z0-9 -]+$" and gate["which"]["maxLength"] == 80 and gate["which"]["minLength"] == 1
    assert json.loads(build.DESCRIBE_AGENT_PARAMS) == {"timeout": "60"}
    assert TASKS["1b"]["variables"]["incoming"]["path"] == ["action"] and TASKS["1b"]["variables"]["incoming"]["value"] == "$var.job.what"
    assert TASKS["1c"]["variables"]["incoming"]["path"] == ["which"] and TASKS["1c"]["variables"]["incoming"]["value"] == "$var.job.which"
    assert TASKS["1d"]["variables"]["incoming"]["params"] == "$var.1c.object"
    assert "9a0a" in TASKS and TASKS["9a0b"]["description"].startswith("the input gate")


def test_the_agent_reads_only_the_guide_and_the_summary() -> None:
    """The envelope (the HTML, the chart, the whole answer) stays on the task: the twin has 32k of context."""
    assert set(WF["outputSchema"]["properties"]) == {"outcome", "guide", "error", "input_check"}
    assert TASKS["1d"]["variables"]["outgoing"] == {"result": None}
    published = {t["variables"]["outgoing"].get("return_data") for t in TASKS.values() if t["name"] == "query"}
    assert published == {"$var.job.outcome", "$var.job.guide", "$var.job.error"}
    assert not any(v for t in TASKS.values() for v in t["variables"]["outgoing"].values()
                   if isinstance(v, str) and v in ("$var.job.described", "$var.job.describe_result"))


def test_every_task_reaches_the_end() -> None:
    tr = WF["transitions"]
    seen, todo = set(), ["workflow_start"]
    while todo:
        n = todo.pop()
        if n in seen:
            continue
        seen.add(n)
        todo.extend(tr.get(n, {}))
    assert "workflow_end" in seen and seen >= set(WF["tasks"])


# --- the list, on the committed documents -----------------------------------------------------------------------------
def test_the_list_is_every_workflow_on_one_line() -> None:
    docs = [json.loads((ROOT / "itential" / "workflows" / describe.file_name(n)).read_text())
            for n in describe.guided(VERSIONS)]
    out = describe.describe_list({"documents": docs, "read": "Read for the test."})
    assert out["ok"] and out["count"] == len(docs) and len(out["workflows"]) == len(docs)
    rows = {r["name"]: r for r in out["workflows"]}
    bgp = rows["Diagnose Fabric BGP Outage"]
    assert (bgp["tasks"], bgp["steps"], bgp["manual"]) == (116, 12, 2)
    assert rows[NAME]["manual"] == 0 and rows["Push Configuration with Approval"]["manual"] >= 1
    assert all(r["what"].endswith(".") and "\n" not in r["what"] for r in out["workflows"])
    assert out["markdown"].count("\n| ") == len(docs) + 1  # the header row and one per workflow
    assert re.match(r"^\d+ workflows on this Platform, \d+ of them stop for a person$", out["summary"])
    names = [r["name"] for r in out["workflows"]]
    assert names == sorted(names, key=str.lower)


def test_a_document_without_steps_lists_with_no_steps() -> None:
    bare = {"name": "Vendor Pack Flow", "description": "Does a thing; then another. More words.",
            "tasks": {"workflow_start": {}, "a": {"name": "query", "summary": "q"}, "workflow_end": {}},
            "transitions": {"workflow_start": {"a": {"state": "success", "type": "standard"}},
                            "a": {"workflow_end": {"state": "success", "type": "standard"}}}}
    out = describe.describe_list({"documents": [bare, {"name": "", "tasks": {}}, "junk"]})
    assert out["count"] == 1 and out["workflows"][0] == {"name": "Vendor Pack Flow", "tasks": 1, "steps": 0,
                                                         "manual": 0, "what": "Does a thing."}
    assert out["summary"] == "1 workflow on this Platform, 0 of them stop for a person"


# --- the agent and its twin -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("doc", [AGENT, TWIN], ids=["platform-guide", "platform-guide-local"])
def test_the_agent_has_the_one_tool_and_nothing_wider(doc: dict) -> None:
    assert doc["tools"] == [{"reference": NAME, "kind": "workflow"}]
    text = " ".join(doc["instructions"].split())  # the prompt wraps at 120 columns; the rules are read unwrapped
    assert "{{request}}" in text and doc["input_schema"]["required"] == ["request"]
    for rule in ('what "list", which "all"', 'what "workflow"', 'what "job"', "Only from what the tool returned",
                 "Never name a workflow, a step, a task, a count or a time the guide does not give",
                 "never change anything", "never ask for or repeat a password"):
        assert rule in text, f"{doc['name']} lacks the rule {rule!r}"
    # the card workflows wait on a person's Seen: an agent must never be handed them
    assert not {"Describe Workflow", "Describe Job"} & {t["reference"] for t in doc["tools"]}


def test_the_twin_is_the_same_agent_on_the_local_model() -> None:
    assert AGENT["profile"] == "anthropic" and TWIN["profile"] == "ollama-mac"
    assert AGENT["project"] == TWIN["project"] == "lab-netops"
    assert TWIN["instructions"].splitlines()[0].strip() != "/no_think"  # ADR 0079: the profile switches thinking off
    assert "at most eight sentences" in TWIN["instructions"]


def test_the_verify_runs_both_and_the_eval_set_exists() -> None:
    text = VERIFY.read_text()
    assert re.search(r'check "S16\.6 platform-guide ', text) and "run_agent platform-guide " in text
    assert re.search(r'check "S16\.7 platform-guide-local ', text) and "run_agent platform-guide-local " in text
    assert EVAL.exists()
    e = EVAL.read_text()
    assert "platform-guide-local" in e and NAME in e and "Launch Rockets" in e, "the eval set names the twin, the tool and the no-such-workflow case"


def test_the_adr_and_the_pid_record_it() -> None:
    assert ADR.exists()
    pid = (ROOT / "docs" / "PID.md").read_text()
    assert "| 1.54 |" in pid and "platform-guide" in pid and "ADR 0081" in pid
