"""Describe Job (ADR 0080 decision 6): the Platform describes one of its own jobs. The run (each task's status, finish
state and times, as a job record carries them) is laid over the stored workflow's steps by describe.py; the service
reads the job and the workflow; the workflow shows the result on a card. These tests simulate runs on committed
documents, so no Platform and no real job record is needed; the service's own route is in test_platform_describe.py."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / "itential" / "workflows"
_D = importlib.util.spec_from_file_location("wf_describe_job", WORKFLOWS / "describe.py")
describe = importlib.util.module_from_spec(_D)
_D.loader.exec_module(describe)
_B = importlib.util.spec_from_file_location("wf_build_describe_job", WORKFLOWS / "build.py")
build = importlib.util.module_from_spec(_B)
_B.loader.exec_module(build)
build.LAYOUT_SEARCH = True
SVG_NS = "{http://www.w3.org/2000/svg}"
NAME = "Describe Job"
START, END = "workflow_start", "workflow_end"
T0 = 1791728820000  # 2026-10-11 14:27:00 UTC in epoch ms, like metrics.start_time


def document(name: str) -> dict:
    return json.loads((WORKFLOWS / describe.file_name(name)).read_text())


def simulate(doc: dict, answers: dict | None = None, stop_at: str | None = None, status: str = "complete",
             variables: dict | None = None) -> dict:
    """A job record as the Platform writes one, from a walk of the document: every task answers `success` unless
    `answers` says otherwise; the walk stops at `stop_at` (left running) or at the end. Tasks never reached are
    `incomplete` (or `canceled` when the job was), and a job's copy of a task keeps none of its incoming values."""
    answers = answers or {}
    job = json.loads(json.dumps({k: v for k, v in doc.items() if k not in ("inputSchema", "outputSchema")}))
    for tid, t in job["tasks"].items():
        t.update({"status": "canceled" if status == "canceled" else "incomplete", "finish_state": None,
                  "start_time": None, "end_time": None, "iterations": []})
        if "variables" in t:
            t["variables"]["incoming"] = {k: None for k in t["variables"].get("incoming", {})}
    clock, tid = T0, START
    job["tasks"][START].update({"status": "complete"})
    while True:
        fs = "success" if tid == START else answers.get(tid, "success")
        nxt = next((b for b, e in doc["transitions"].get(tid, {}).items() if e["state"] == fs), None)
        if tid != START:
            t = job["tasks"][tid]
            stamp = datetime.fromtimestamp(clock / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.")
            t["status"], t["start_time"] = "complete", stamp + "%03dZ" % (clock % 1000)  # ISO 8601 with milliseconds
            if tid == stop_at:
                t["status"] = "running"
                break
            clock += 1200
            t["end_time"], t["finish_state"] = clock, fs
        if nxt is None or nxt == END:
            if nxt == END and stop_at is None:
                job["tasks"][END]["status"] = "complete"
            break
        tid = nxt
    if status == "canceled":
        for t in job["tasks"].values():
            if t.get("status") == "running":
                t["status"] = "canceled"
    job.update({"_id": "6d43c80a53b14be58f8f8c8d", "status": "running" if stop_at and status == "complete" else status,
                "description": "a simulated run", "variables": variables or {}, "warnings": [], "errors": [],
                "last_updated": "2026-10-11T14:30:00.000Z",
                "metrics": {"start_time": T0, "end_time": None if stop_at else clock, "user": "x"}})
    return job


DW = document("Describe Workflow")  # 18 tasks in 5 steps: a small, well-known document to simulate on


def test_a_finished_run_reads_as_the_path_it_took() -> None:
    job = simulate(DW, variables={"outcome": "Rotate AWS VPN Key: 106 tasks in 12 steps", "guide": "# x"})
    out = describe.describe_job_document({"job": job, "document": DW, "read": "Read at T by platform-describe, as svc-copilot."})
    assert out["ok"] and out["workflow"] == "Describe Workflow" and out["job"] == job["_id"] and out["status"] == "complete"
    assert out["name"] == "Describe Workflow, job 6d43c80a"
    assert out["step_states"] == {"1": "ran", "2": "ran", "3": "ran", "4": "ran", "5": "skipped"}
    assert out["path"][0] == [0, 1, []] and out["path"][-1] == [4, 6, []], "start to step 1, the card to done"
    assert [a for a, _, _ in out["path"]] == [0, 1, 2, 3, 4]
    assert out["ran_tasks"] == 13 and out["ran_steps"] == 4 and out["tasks"] == 18 and out["steps"] == 5
    assert out["summary"].startswith("Describe Workflow, job 6d43c80a: complete in ") and "ended after step 4 (the card)" in out["summary"]
    md = out["markdown"]
    assert md.startswith("# Describe Workflow: job 6d43c80a\n\n**Complete.** Started 2026-10-11 14:27 UTC")
    assert "- step 1: the input gate — ran in" in md and "then inputs valid? yes → step 2." in md
    assert "- Not reached: step 5 (where it stops)." in md
    assert "## Where it stopped\n\n- Done at " in md and "after `3c` the workflow, described (step 4)." in md
    assert '- `outcome`: "Rotate AWS VPN Key: 106 tasks in 12 steps"' in md and "- `error`: nothing (not set)" in md
    assert "Not set by this run: `described`, `describe_result`, `guide_card`, `seen`, `input_check`." in md
    assert "## The tasks that ran" in md and "- `1d` platform-describe exited 0? — ran in 1.2 s, answered yes." in md
    assert "`_id`" not in md and "`initiator`" not in md, "the engine's own variables are not the workflow's words"
    assert "`1e`" not in md.split("## The tasks that ran")[1], "a task the run never reached is left out"
    assert "still matches this job's tasks and arrows" in md


def test_a_waiting_run_says_where_it_waits_and_for_how_long() -> None:
    job = simulate(DW, stop_at="3c")
    out = describe.describe_job_document({"job": job, "document": DW, "now": T0 + 15 * 60 * 1000})
    assert out["status"] == "running" and out["step_states"]["4"] == "waiting" and out["step_states"]["5"] == "skipped"
    assert out["manual"] == 1
    assert "running for 15 min" in out["summary"] and "waiting for a person at step 4 (the card)" in out["summary"]
    md = out["markdown"]
    assert "**Running.** Started 2026-10-11 14:27 UTC with the description \"a simulated run\". Running for 15 min." in md
    assert "- step 4: the card — waiting for a person since 2026-10-11 14:27 UTC (" in md
    assert "## Where it is now\n\n- `3c` the workflow, described (step 4): waits for a person since" in md
    assert "— waits for a person since 2026-10-11 14:27 UTC." in md


def test_a_run_that_turned_at_an_evaluation_shows_the_other_way() -> None:
    job = simulate(DW, answers={"1d": "failure"}, variables={"error": "no workflow named 'X' on this Platform"})
    out = describe.describe_job_document({"job": job, "document": DW})
    assert out["step_states"] == {"1": "ran", "2": "ran", "3": "skipped", "4": "skipped", "5": "skipped"}
    assert out["path"] == [[0, 1, []], [1, 2, ["inputs valid? yes"]], [2, 6, []]], "the read went straight to done"
    md = out["markdown"]
    assert "- step 2: the read — ran in" in md and "; then → done." in md
    assert "- `1d` platform-describe exited 0? — ran in 1.2 s, answered no." in md
    assert "- `1e` why it could not describe — ran in 1.2 s." in md
    assert "- `error`: \"no workflow named 'X' on this Platform\"" in md


def test_a_canceled_run_is_canceled_where_it_was_and_unreached_elsewhere() -> None:
    """The engine marks every unreached task canceled too; only the task that was in flight was canceled."""
    job = simulate(DW, stop_at="3c", status="canceled")
    out = describe.describe_job_document({"job": job, "document": DW})
    assert out["status"] == "canceled" and out["step_states"]["4"] == "canceled" and out["step_states"]["5"] == "skipped"
    assert out["ran_tasks"] == 13 and "canceled at step 4 (the card)" in out["summary"]
    assert "## Where it stopped\n\n- `3c` the workflow, described (step 4): canceled." in out["markdown"]


def test_the_chart_marks_the_run() -> None:
    job = simulate(DW, stop_at="3c")
    out = describe.describe_job_document({"job": job, "document": DW})
    root = ET.fromstring(out["svg"])
    texts = [("".join(t.itertext())).strip() for t in root.findall(f".//{SVG_NS}text")]
    assert texts.count("ran") == 3 and texts.count("waiting for a person now") == 1 and texts.count("not reached") == 1
    assert "waits for a person" not in texts, "the waiting step says so in the present tense instead"
    arrows = [p for p in root.findall(f".//{SVG_NS}path") if p.get("marker-end")]
    steps_ = describe.steps(DW)
    where = {tid: n for n, (_, ids) in enumerate(steps_, 1) for tid in ids}
    assert len(arrows) == len(describe.step_links(DW, where)), "every link is still drawn"
    thick = [p for p in arrows if p.get("stroke-width") == "2.6"]
    faded = [p for p in arrows if p.get("opacity") == "0.35"]
    assert len(thick) == len(out["path"]) == 4 and len(thick) + len(faded) == len(arrows)
    plain = describe.step_chart(DW, steps_, where)
    assert "opacity" not in plain and 'stroke-width="2.6"' not in plain, "without a run the chart is as before"
    assert out["svg"] == describe.describe_job_document({"job": job, "document": DW})["svg"], "deterministic"
    assert '<figure class="chart"><img class="chart"' in out["html"] and "<svg" not in out["html"]


def test_a_changed_or_missing_workflow_falls_back_to_the_jobs_own_copy() -> None:
    job = simulate(DW)
    older = json.loads(json.dumps(DW))
    del older["tasks"]["8c"]  # the stored workflow gained a task since the job ran: it no longer matches
    for stored in (older, None):
        out = describe.describe_job_document({"job": job, "document": stored})
        assert out["ok"] and "described from the job's own copy" in out["markdown"]
        assert out["tasks"] == 18 and out["ran_steps"] >= 1, "the job's copy still carries the steps build.py named"
    out = describe.describe_job_document({"job": job, "document": DW})
    assert "still matches" in out["markdown"]


def test_the_runner_takes_a_job_on_stdin_and_a_bad_record_is_an_answer() -> None:
    job = simulate(DW)
    r = subprocess.run([sys.executable, "-I", str(WORKFLOWS / "describe.py")], input=json.dumps({"job": job, "document": DW}),
                       capture_output=True, text=True, check=True)
    out = json.loads(r.stdout)
    assert out["ok"] and out["name"] == "Describe Workflow, job 6d43c80a" and out["brand"] == "the job, described"
    r = subprocess.run([sys.executable, "-I", str(WORKFLOWS / "describe.py")], input=json.dumps({"job": {"name": "x"}}),
                       capture_output=True, text=True, check=True)
    assert json.loads(r.stdout) == {"ok": False, "message": "no job record: expected {job: {_id, name, status, tasks, transitions, ...}}"}


# ── the workflow on the Platform: the same shape as Describe Workflow, the id as data, the job's card ──
WF = build.describe_job()
TASKS = {k: t for k, t in WF["tasks"].items() if k not in (START, END)}


def test_the_committed_document_is_this_build() -> None:
    committed = json.loads((WORKFLOWS / build.file_name(NAME)).read_text())
    assert committed == json.loads(json.dumps(WF))
    assert build.describe_job in build.BUILDERS and build.VERSIONS["workflows"]["describe_job"] == NAME


def test_it_only_reads_and_the_id_goes_in_as_data_behind_the_gate() -> None:
    outside = sorted({(t["app"], t["name"]) for t in TASKS.values() if t["app"] != "WorkFlowEngine"})
    assert outside == [("GatewayManager", "runCode"), ("GatewayManager", "runService"), ("WorkCenter", "InteractiveHTML")]
    assert {t["variables"]["incoming"]["serviceName"] for t in TASKS.values() if t["name"] == "runService"} == {"platform-describe"}
    gate = build.INPUT_GATES[NAME]["job"]
    assert gate["pattern"] == r"^[0-9a-f]{24}$"
    assert json.loads(build.DESCRIBE_JOB_PARAMS) == {"action": "job", "timeout": "60"}
    put = TASKS["1b"]["variables"]["incoming"]
    assert put["path"] == ["id"] and put["value"] == "$var.job.job"
    assert TASKS["1c"]["variables"]["incoming"]["params"] == "$var.1b.object"
    assert TASKS["3c"]["variables"]["incoming"]["header"] == "The job, described"


def test_every_task_reaches_the_end() -> None:
    tr = WF["transitions"]
    reach_end, changed = {END}, True
    while changed:
        changed = False
        for a, tos in tr.items():
            if a not in reach_end and set(tos) & reach_end:
                reach_end.add(a)
                changed = True
    assert reach_end == set(WF["tasks"]), f"dead ends: {set(WF['tasks']) - reach_end}"


def test_the_card_takes_the_jobs_words() -> None:
    job = simulate(DW, stop_at="3c")
    described = describe.describe_job_document({"job": job, "document": DW})
    r = subprocess.run([sys.executable, "-I", "-c", build.GUIDE_CARD_CODE], input=json.dumps(described),
                       capture_output=True, text=True, check=True)
    page = json.loads(r.stdout)["html"]
    assert "the job, described" in page and ">The run, start to end</h2>" in page and "For reading" in page
    assert page.count("What this run did") == 1, "the page's own heading, not repeated by the card's wrapper"
    assert "<strong>Running.</strong>" in page and "*Running" not in page, "the status line is bold, not starred"
    assert "only read this job&#x27;s record" in page or "only read this job's record" in page
    assert "from the job record and the stored workflow document" in page
    assert "stored copy of Describe Workflow and described the run" in page, "the job card's own decision line"
    workflow_page = json.loads(subprocess.run([sys.executable, "-I", "-c", build.GUIDE_CARD_CODE],
                                              input=json.dumps({"name": "X", "summary": "X: 1 task", "html": "<p>x</p>"}),
                                              capture_output=True, text=True, check=True).stdout)["html"]
    assert "the workflow, described" in workflow_page and "What it does, start to end" in workflow_page


@pytest.mark.parametrize("name", ["Describe Workflow", "Describe Job"])
def test_both_describers_share_one_shape(name: str) -> None:
    doc = document(name)
    titles = [t for t, _ in describe.steps(doc)]
    assert [t.split(":")[0] for t in titles] == ["the input gate", "the read", "the guide", "the card", "where it stops"]
