"""Ask Platform Guide (ADR 0081 decision 6): the Slack front door's workflow. The question goes in as data behind the
gate, platform-guide answers it in the Platform's own session, the answer goes on a small Work Center card the bot
reads back into Slack, and is published as text. These tests hold the committed document to the build, the workflow
to its three outside tasks, the card to what the Gateway renders (and to what the bot's reader expects), and the
automation and its endpoint to versions.yaml and the plays."""

from __future__ import annotations

import html
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("wf_build_ask_guide", ROOT / "itential" / "workflows" / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)
build.LAYOUT_SEARCH = True  # this module compares its build against the committed JSON, canvas included
VERSIONS = build.VERSIONS
NAME = "Ask Platform Guide"
WF = build.ask_platform_guide()
TASKS = {k: t for k, t in WF["tasks"].items() if k not in ("workflow_start", "workflow_end")}
OM = VERSIONS["operations_manager"]["ask_platform_guide"]


def render(data: dict) -> dict:
    """ANSWER_CARD_CODE as the Gateway runs it: the object on stdin, {html, text, outcome} out."""
    r = subprocess.run([sys.executable, "-I", "-c", build.ANSWER_CARD_CODE], input=json.dumps(data),
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def slack_lines(page: str) -> list:
    """The card as the Slack bot reads it back (department-agents slack-bot/bot.py card_to_slack, written here
    independently): every <p> and <li> in order, tags stripped, entities unescaped."""
    out = []
    for tag, inner in re.findall(r"<(p|li)[^>]*>(.*?)</\1>", page, flags=re.S):
        text = html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
        if text:
            out.append((tag, text))
    return out


def test_the_committed_document_is_this_build() -> None:
    committed = json.loads((ROOT / "itential" / "workflows" / build.file_name(NAME)).read_text())
    assert committed == json.loads(json.dumps(WF))
    assert build.ask_platform_guide in build.BUILDERS and VERSIONS["workflows"]["ask_platform_guide"] == NAME


def test_three_things_leave_the_engine_the_agent_the_card_code_and_the_card() -> None:
    outside = sorted({(t["app"], t["name"]) for t in TASKS.values() if t["app"] != "WorkFlowEngine"})
    assert outside == [("AgentSessionManager", "runAgent"), ("GatewayManager", "runCode"), ("WorkCenter", "InteractiveHTML")]
    agent = TASKS["1a"]["variables"]["incoming"]
    assert agent["agent"] == build.ASK_GUIDE_AGENT_MARKER == "__AGENT_ID:platform-guide__"
    assert agent["inputs"] == {"request": "$var.job.request"}
    assert TASKS["1a"]["actor"] == "Pronghorn", "the agent runs as the Platform; the automation's group access gates the asker"


def test_the_question_goes_in_as_data_behind_the_gate() -> None:
    gate = build.INPUT_GATES[NAME]
    assert gate == {"request": {"type": "string", "minLength": 1, "maxLength": 2000}}
    assert "9a0a" in TASKS and TASKS["9a0b"]["description"].startswith("the input gate")
    assert WF["inputSchema"]["required"] == ["request"]
    put = TASKS["2a"]["variables"]["incoming"]
    assert put["path"] == ["request"] and put["value"] == "$var.job.request"
    assert TASKS["2b"]["variables"]["incoming"]["value"] == "$var.job.agent_result"
    assert TASKS["2c"]["variables"]["incoming"]["data"] == "$var.2b.object"


def test_the_answer_is_published_and_the_card_has_one_button() -> None:
    assert TASKS["2d"]["variables"]["outgoing"]["return_data"] == "$var.job.answer"
    assert TASKS["2e"]["variables"]["outgoing"]["return_data"] == "$var.job.outcome"
    card = TASKS["3a"]["variables"]["incoming"]
    assert card["body"] == "$var.2f.return_data" and card["btn_success"] == "Seen" == card["btn_failure"]
    assert TASKS["3a"]["groups"] == [], "the automation's group access decides who asks; the card itself stays open"
    assert sorted(WF["outputSchema"]["properties"]) == ["agent_result", "answer", "error", "input_check", "outcome"]


def test_an_agent_that_did_not_run_still_gets_a_card_with_the_reason() -> None:
    tr = WF["transitions"]
    assert tr["1a"]["1b"]["state"] == "error" and "2a" in tr["1c"]
    assert TASKS["1b"]["variables"]["outgoing"]["output"] == "$var.job.agent_result"
    assert TASKS["1c"]["variables"]["outgoing"]["output"] == "$var.job.error"
    for tid in ("2c", "2d", "2e", "2f"):
        assert tr[tid]["8b"]["state"] == "error"
    assert TASKS["8b"]["variables"]["outgoing"]["output"] == "$var.job.error"


def test_every_task_reaches_the_end() -> None:
    tr = WF["transitions"]
    seen, todo = set(), ["workflow_start"]
    while todo:
        n = todo.pop()
        if n in seen:
            continue
        seen.add(n)
        todo += list(tr.get(n, {}))
    assert seen == set(WF["tasks"]), "a task no path reaches"
    reach_end = {"workflow_end"}
    changed = True
    while changed:
        changed = False
        for a, tos in tr.items():
            if a not in reach_end and set(tos) & reach_end:
                reach_end.add(a)
                changed = True
    assert reach_end == set(WF["tasks"]), f"dead ends: {set(WF['tasks']) - reach_end}"


def test_the_card_carries_the_question_and_the_answer_as_the_gateway_renders_it() -> None:
    out = render({"request": "What does Describe Job do?",
                  "agent": {"sessionId": "abc123", "sessionStatus": "COMPLETE",
                            "lastMessage": "Describe Job: 18 tasks in 4 steps.\n\n- It reads **one job** by id.\n"
                                           "- It `publishes` the guide.\n1. first\n2. second\n## Where it waits\nAt the card."}})
    assert out["outcome"] == "answered" and out["text"].startswith("Describe Job: 18 tasks")
    page = out["html"]
    assert page.startswith("<!doctype html>") and "the Platform, asked" in page
    assert '<blockquote class="ask">What does Describe Job do?</blockquote>' in page
    assert "<strong>one job</strong>" in page and "<code>publishes</code>" in page
    assert "<ul><li>" in page and "<ol><li>first</li>" in page and '<p class="h">Where it waits</p>' in page
    assert "(session abc123)" in page and "Seen closes this card" in page


def test_the_card_reads_back_into_slack_as_the_answer_and_one_foot_line() -> None:
    """The bot drops the first <p> when it ends with "agent", posts every other <p>/<li>, and sets the last one in
    italics when it starts with "Answered by" or "Nothing was changed"; the question itself is not a <p>."""
    out = render({"request": "the question", "agent": {"sessionId": "s1", "sessionStatus": "COMPLETE",
                                                        "lastMessage": "Line one.\n- a bullet\nLine two."}})
    lines = slack_lines(out["html"])
    assert lines[0] == ("p", "Platform guide agent")
    assert lines[1:-1] == [("p", "Line one."), ("li", "a bullet"), ("p", "Line two.")]
    assert lines[-1][0] == "p" and lines[-1][1].startswith("Answered by the Platform guide agent")
    assert all(text != "the question" for _, text in lines)


@pytest.mark.parametrize("agent, reason", [
    ("The Platform guide agent could not run this request.", "could not run this request"),
    ({"sessionId": "s3", "sessionStatus": "FAILED", "lastMessage": ""}, "did not finish (session status FAILED)"),
    ({"sessionId": "s4", "sessionStatus": "COMPLETE", "lastMessage": "   "}, "did not finish (session status COMPLETE)"),
    (None, "could not run this request"),
])
def test_no_answer_is_a_card_that_says_why(agent, reason: str) -> None:
    out = render({"request": "q", "agent": agent})
    assert out["outcome"] == "no answer" and reason in out["text"]
    lines = slack_lines(out["html"])
    assert reason in lines[1][1] and lines[-1][1].startswith("Nothing was changed.")


def test_the_local_twins_leaked_reasoning_is_cut_at_the_last_think_marker() -> None:
    out = render({"request": "q", "agent": {"sessionId": "s2", "sessionStatus": "COMPLETE",
                                             "lastMessage": "let me think\n</think>\nfirst try\n</think>\n\nThe answer."}})
    assert out["text"] == "The answer." and "let me think" not in out["html"] and "first try" not in out["html"]


def test_nothing_the_asker_or_the_model_writes_can_inject_markup() -> None:
    out = render({"request": "<script>alert(1)</script> & co",
                  "agent": {"sessionId": "<i>", "sessionStatus": "COMPLETE",
                            "lastMessage": "<img src=x onerror=alert(1)> **<b>bold</b>** `<u>`"}})
    page = out["html"]
    assert "<script>" not in page and "<img src=x" not in page and "<b>bold" not in page and "<u>" not in page and "<i>" not in page
    assert "&lt;script&gt;" in page and "<strong>&lt;b&gt;bold&lt;/b&gt;</strong>" in page


def test_the_automation_and_the_slack_endpoint_are_declared_and_wired() -> None:
    assert OM["automation"] == NAME and OM["endpoint"]["route"] == "ask-platform-guide"
    assert OM["endpoint"]["name"] == "Ask Platform Guide from Slack" and "Slack bot" in OM["endpoint"]["description"]
    play = (ROOT / "ansible" / "playbooks" / "platform.yml").read_text()
    wired = re.search(r"include_tasks: tasks/outage-trigger\.yml\n\s+vars: \{otr_key: ask_platform_guide\}\n(.*)", play)
    assert wired and not wired.group(1).strip().startswith("when:"), "both tiers: dev runs the twin"
    trigger = (ROOT / "ansible" / "playbooks" / "tasks" / "outage-trigger.yml").read_text()
    assert "otr.endpoint.description" in trigger
    ids = (ROOT / "ansible" / "playbooks" / "tasks" / "workflow-agent-ids.yml").read_text()
    assert "regex_replace', '-local$'" in ids and "dev_overlay" in ids, "dev fills the marker with the local twin"


def test_the_guide_page_names_the_slack_endpoint() -> None:
    page = (ROOT / "docs" / "workflows" / "ask-platform-guide.md").read_text()
    assert 'From the endpoint trigger "Ask Platform Guide from Slack" (route `ask-platform-guide`)' in page
