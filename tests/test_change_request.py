"""R5 (ADR 0085): every person-started Deploy, Hand Off and Tear Down opens one ServiceNow standard change before it
plans or reads anything, shows its number on every card, and walks it with the cards - approved -> Scheduled ->
Implement, rejected -> Canceled, ended before any approval -> Canceled, after one -> Review -> Closed. ServiceNow
refusing the create stops the run, refusing Implement applies nothing. The same tasks go into all three (no child jobs);
the scheduled and read-only workflows open none."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("wf_build_change", ROOT / "itential" / "workflows" / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)
WFS = ROOT / "itential" / "workflows"
CHANGED = {name: json.loads((WFS / name).read_text())
           for name in ("deploy-aws-vpn.json", "hand-off-aws-vpn.json", "tear-down-aws-vpn.json")}
# per workflow: its cards, and what an approval may never reach without the change in Implement (or a rejection at all)
GUARDED = {"deploy-aws-vpn.json": {"2b": ("3e", "4d")}, "hand-off-aws-vpn.json": {"6f": ("7c",)},
           "tear-down-aws-vpn.json": {"2c": ("3b", "5b", "6e"), "5f": ("6e",)}}
NOT_CHANGED = ("tear-down-expired-aws-vpn.json", "verify-aws-vpn.json", "get-aws-vpn-status.json",
               "check-aws-drift.json", "rotate-aws-vpn-key.json", "rotate-aws-vpn-key-monthly.json")
JOB = "0123456789abcdef01234567"


def _reach(tr: dict, start: str, stop: tuple = ()) -> set[str]:
    seen, todo = set(), [start]
    while todo:
        node = todo.pop()
        if node in seen or node in stop:
            continue
        seen.add(node)
        todo.extend(tr.get(node, {}))
    return seen


def _reads(tasks: dict) -> set[str]:
    """What plans or reads: a Gateway service (Terraform, the router, the AWS monitor) or a NetBox call."""
    return {tid for tid, t in tasks.items()
            if t.get("name") == "runService" or str(t.get("name", "")).startswith(("dcim_", "ipam_"))}


def _run(code: str, data: dict) -> dict:
    run = subprocess.run([sys.executable, "-I", "-c", code], input=json.dumps(data), capture_output=True, text=True,
                         timeout=30, env={}, check=True)
    return json.loads(run.stdout)


@pytest.mark.parametrize("name", sorted(CHANGED))
def test_one_change_opens_from_the_template_before_anything_is_planned_or_read(name: str) -> None:
    wf = CHANGED[name]
    tasks, tr = wf["tasks"], wf["transitions"]
    creates = [tid for tid, t in tasks.items() if t.get("name") == "createStandardChange"]
    assert creates == ["cec"]
    assert tasks["cec"]["variables"]["incoming"]["template_sys_id"] == build.VERSIONS["aws_vpn"]["change_request"]["template"]
    assert tasks["cec"]["variables"]["incoming"]["requestBodyPayload"] == "$var.ceb.return_data"  # data, never templated
    # no plan, output read or device read is reachable from the start without passing the opened change
    before = _reach(tr, "workflow_start", stop=("cec",))
    assert not before & _reads(tasks), sorted(before & _reads(tasks))
    # and the change's number is read back before the run goes on
    assert tasks["cee"]["variables"]["outgoing"]["return_data"] == "$var.job.change_number"


@pytest.mark.parametrize("name", NOT_CHANGED)
def test_the_scheduled_and_read_only_workflows_open_no_change(name: str) -> None:
    text = (WFS / name).read_text()
    assert "StandardChange" not in text and "change_number" not in text, name


@pytest.mark.parametrize("name", sorted(CHANGED))
def test_servicenow_refusing_the_create_stops_the_run_with_nothing_done(name: str) -> None:
    wf = CHANGED[name]
    tasks, tr = wf["tasks"], wf["transitions"]
    for src in ("cea", "ceb", "cec", "ced", "cee"):  # every failure of the open goes to the one refusal
        assert "cef" in tr[src], src
    assert tasks["cef"]["variables"]["outgoing"]["output"] == "$var.job.error"
    assert not _reach(tr, "cef") & _reads(tasks)
    assert not _reach(tr, "cef") & {"cd0", "cd1", "cd2", "cd3"}  # nothing to close: no change was opened


@pytest.mark.parametrize("name", sorted(CHANGED))
def test_every_card_carries_the_change_number(name: str) -> None:
    tasks = CHANGED[name]["tasks"]
    for card in GUARDED[name]:
        message = tasks[f"{card}2"]["variables"]["incoming"]["value"]
        prep = message.split(".")[1][:2]  # the card's c8* / c9* tasks
        assert tasks[f"{prep}0"]["variables"]["incoming"]["str"].startswith("Change request __CHG__.")
        assert tasks[f"{prep}0"]["variables"]["incoming"]["newSubstr"] == "$var.job.change_number"
        body = tasks[f"{prep}2"]["variables"]["incoming"]
        assert body["path"] == ["change_request"] and body["value"] == "$var.job.change_number"
        assert tasks[f"{card}3"]["variables"]["incoming"]["value"] == f"$var.{prep}2.object"


@pytest.mark.parametrize("name", sorted(CHANGED))
def test_no_rejection_can_reach_an_apply_or_a_push(name: str) -> None:
    """Approve and Reject each have their own decision chain: a rejected card cannot reach the guarded tasks at all,
    not even through a runtime check (the safety is in the graph)."""
    tr = CHANGED[name]["transitions"]
    for card, guarded in GUARDED[name].items():
        side = {e["state"]: d for d, e in tr[card].items()}
        assert not _reach(tr, side["failure"]) & set(guarded), card
        assert set(guarded) <= _reach(tr, side["success"]), card


@pytest.mark.parametrize("name", sorted(CHANGED))
def test_an_approval_reaches_the_change_only_through_scheduled_then_implement(name: str) -> None:
    wf = CHANGED[name]
    tasks, tr = wf["tasks"], wf["transitions"]
    card = next(iter(GUARDED[name]))  # the first card moves the change; Tear Down's second only notes it
    states = [json.dumps(tasks[t]["variables"]["incoming"].get("requestBodyPayload")) for t in ("cad", "cae")]
    assert tasks["cad"]["name"] == "updateStandardChange" and '"-2"' in states[0]
    assert tasks["cae"]["variables"]["incoming"]["obj"] == {"state": "-1"}  # Implement, with the approval's note
    assert tasks["caf"]["variables"]["incoming"]["requestBodyPayload"] == "$var.cae.object"
    # the guarded tasks are reachable from the approval only through Implement
    side = {e["state"]: d for d, e in tr[card].items()}
    assert not _reach(tr, side["success"], stop=("caf",)) & set(GUARDED[name][card])
    # refused (Scheduled or Implement), nothing guarded is reachable and the run ends canceled with why
    for src in ("cad", "caf"):
        assert tr[src]["c83"]["state"] == "error"
    assert not _reach(tr, "c83") & set(GUARDED[name][card]) and "cd0" in _reach(tr, "c83")


def test_deploy_discards_the_approved_plan_when_servicenow_refuses_implement() -> None:
    tasks, tr = CHANGED["deploy-aws-vpn.json"]["tasks"], CHANGED["deploy-aws-vpn.json"]["transitions"]
    assert list(tr["c83"]) == ["72"] and tasks["74"]["variables"]["incoming"]["serviceName"] == "terraform-run"
    assert tasks["72"]["variables"]["incoming"]["str"] == build.DISCARD_TPL
    assert "8c" in _reach(tr, "74") and not _reach(tr, "c83") & {"3e", "4d"}


def test_tear_downs_second_card_notes_the_change_either_way() -> None:
    """The AWS destroy card comes after the router change, so neither decision ends the change: an approval and a
    rejection each add a work note (the decision, the card's note), and neither moves the state."""
    tasks, tr = CHANGED["tear-down-aws-vpn.json"]["tasks"], CHANGED["tear-down-aws-vpn.json"]["transitions"]
    updates = {tid: t["variables"]["incoming"]["requestBodyPayload"] for tid, t in tasks.items()
               if tid[:2] in ("cb", "c9") and t["name"] == "updateStandardChange"}
    assert updates == {"cbe": "$var.cbd.object", "c9d": "$var.c9c.object"}
    for body in ("cbd", "c9c"):
        assert tasks[body]["variables"]["incoming"] == {"obj": {}, "path": ["work_notes"],
                                                         "value": "$var.job.change_approval_note"}
    approve, reject = (next(d for d, e in tr["5f"].items() if e["state"] == s) for s in ("success", "failure"))
    assert "cbe" in _reach(tr, approve) and "c9d" in _reach(tr, reject) and "cbe" not in _reach(tr, reject)
    assert build.card_sides(tr, "5f") == ("6a", "a2")


def test_tear_downs_cards_show_the_requesters_note() -> None:
    tasks = CHANGED["tear-down-aws-vpn.json"]["tasks"]
    for card, prep in (("2c", "c8"), ("5f", "c9")):
        assert tasks[f"{card}2"]["variables"]["incoming"]["value"] == f"$var.{prep}5.replacedString"
        note = tasks[f"{prep}5"]["variables"]["incoming"]
        assert note["substr"] == "__N__" and note["newSubstr"] == "$var.job.change_note"
        assert "The requester's note (their own words, not checked): __N__" in tasks[f"{prep}0"]["variables"]["incoming"]["str"]
    # Deploy's card already carries its note in its own message, and Hand Off has none: no second copy
    for name, card in (("deploy-aws-vpn.json", "2b"), ("hand-off-aws-vpn.json", "6f")):
        assert "__N__" not in json.dumps(CHANGED[name]["tasks"]), name


@pytest.mark.parametrize("name", sorted(CHANGED))
def test_every_end_after_the_open_goes_through_the_close(name: str) -> None:
    tr = CHANGED[name]["transitions"]
    after_open = next(d for d, e in tr["cee"].items() if e["state"] == "success")
    entries = tuple(build.CHANGE_ENTRIES.values())
    assert "workflow_end" not in _reach(tr, after_open, stop=entries)
    # and the close's own ways out: done, or the task to finish it by hand
    for kind, entry in build.CHANGE_ENTRIES.items():
        assert {"cdc", "cc4", "workflow_end"} <= _reach(tr, entry), kind


@pytest.mark.parametrize("name", sorted(CHANGED))
def test_once_the_change_is_in_implement_no_ending_cancels_it(name: str) -> None:
    """After the first card's approval moved the change to Implement, every ending is a Review -> Closed (failed or
    successful), never a cancel or a rejection; and both closes are still possible from there."""
    tr = CHANGED[name]["transitions"]
    approved = build.card_sides(tr, next(iter(GUARDED[name])))[0]
    after = _reach(tr, approved)
    assert not after & {build.CHANGE_ENTRIES["canceled"], build.CHANGE_ENTRIES["rejected"]}
    assert {build.CHANGE_ENTRIES["failed"], build.CHANGE_ENTRIES["successful"]} <= after


@pytest.mark.parametrize("name", sorted(CHANGED))
def test_a_refused_update_after_the_run_leaves_a_card_a_person_can_open(name: str) -> None:
    """The fix-by-hand task is an InteractiveHTML card (ADR 0085), not ViewData (whose 6.6.0 failure was a stale
    browser cache, ADR 0078 amendment 2026-10-29)."""
    tasks, tr = CHANGED[name]["tasks"], CHANGED[name]["transitions"]
    card = tasks["cc4"]
    assert card["name"] == "InteractiveHTML" and card["type"] == "manual"
    assert card["variables"]["incoming"]["body"] == "$var.cc3.return_data"
    assert not any(t.get("name") == "ViewData" for tid, t in tasks.items() if tid.startswith("c"))
    for src, state in (("cda", "error"), ("cdb", "failure"), ("cdc", "error"), ("cde", "failure"), ("cdf", "error")):
        assert tr[src]["cc0"]["state"] == state, src
    assert {d: e["state"] for d, e in tr["cc4"].items()} == {"workflow_end": "success", "cc5": "failure"}


def test_the_fix_card_says_what_to_write_and_escapes_every_value() -> None:
    page = _run(build.CHANGE_FIX_CARD_CODE, {"kind": "failed", "reason": "<script>x</script>", "number": "CHG0031234",
                                             "job_id": JOB})["html"]
    assert "Fix CHG0031234 by hand" in page and "<script>x</script>" not in page and "&lt;script&gt;" in page
    assert page.index("<dd>Review</dd>") < page.index("<dd>Closed</dd>") and "unsuccessful" in page


def test_the_change_tasks_are_the_same_in_all_three() -> None:
    """D2c: one generator writes them; only the workflow's own words differ (name, target, note, window)."""
    def strip(t: dict) -> dict:
        return {k: v for k, v in json.loads(json.dumps(t)).items() if k != "nodeLocation"}
    own = {"ce4", "ce5", "ce6", "ce7"}
    first, *rest = CHANGED.values()
    shared = [tid for tid in first["tasks"] if tid[:2] in ("ce", "cd", "cc") and len(tid) == 3]
    assert len(shared) == 13 + 16 + 6
    for wf in rest:
        for tid in shared:
            a, b = strip(first["tasks"][tid]), strip(wf["tasks"][tid])
            if tid in own:
                a["variables"]["incoming"].pop("value"), b["variables"]["incoming"].pop("value")
            assert a == b, (wf["name"], tid)
            if not tid.startswith("cd") or tid in ("cd0", "cd1", "cd2", "cd3"):
                continue
            assert first["transitions"][tid] == wf["transitions"][tid], (wf["name"], tid)


def test_the_open_texts_name_the_job_and_the_ways_in_and_no_one() -> None:
    page = _run(build.CHANGE_OPEN_CODE, {"workflow": "Deploy AWS VPN", "target": "the AWS side (lab VPC)",
                                         "note": "  window\nfor  tests ", "window": "2026-10-28T02:00:00Z",
                                         "job_id": JOB})["create"]
    assert page["short_description"] == f"Deploy AWS VPN: the AWS side (lab VPC) (itential-enterprise-lab, job {JOB})"
    assert page["assignment_group"] == build.SNOW_GROUP
    notes = page["work_notes"]
    assert "their own words, not checked): window for tests\n" in notes
    assert "until 2026-10-28T02:00:00Z (Tear Down Expired AWS VPN removes it then)" in notes
    # a trigger-started run cannot read itself, so the note names the ways in and the job, which records who
    assert "Started in Itential (the job records who and how)" in notes
    assert notes.endswith(f"Job: {build.CHANGE['job_url']}{JOB}")
    bare = _run(build.CHANGE_OPEN_CODE, {"workflow": "Tear Down AWS VPN", "target": "dc1-wan01", "note": "",
                                         "window": "none", "job_id": JOB})["create"]["work_notes"]
    assert "(none)" in bare and "with no end time" in bare


@pytest.mark.parametrize("name", sorted(CHANGED))
def test_no_change_request_task_reads_the_job_back(name: str) -> None:
    """A trigger-started run (the page's, the form's) answers 401 to getJob on itself (measured 2026-10-27): the reads
    are gone rather than left failing on every job."""
    tasks = CHANGED[name]["tasks"]
    assert not [t for t, v in tasks.items() if v.get("name") in ("getJob", "getTask")]
    assert "approver_roles" not in build.CHANGE


def test_the_decision_note_says_what_was_decided_and_where_the_card_records_who() -> None:
    note = _run(build.CHANGE_DECISION_CODE, {"decision": "approved", "label": "the AWS change", "job_id": JOB,
                                             "export": {"decision": {"note": "looks right"}}})["work_note"]
    assert note.startswith(f"Approved in Itential Work Center (the card on job {build.CHANGE['job_url']}{JOB} records "
                           "who and when; noted ")
    assert note.endswith("UTC): the AWS change. Their note: looks right.")
    asked = _run(build.CHANGE_DECISION_CODE, {"decision": "rejected", "label": "the router change", "job_id": JOB,
                                              "export": {"decision": {"note": "Is this right?"}}})["work_note"]
    assert asked.startswith("Rejected in Itential Work Center") and asked.endswith("Their note: Is this right?")
    empty = _run(build.CHANGE_DECISION_CODE, {"decision": "rejected", "label": "x", "job_id": JOB, "export": None})
    assert empty["work_note"].endswith("Their note: (none).")


@pytest.mark.parametrize("kind,states,code", [("canceled", ["4"], None), ("rejected", ["4"], None),
                                              ("failed", ["0", "3"], "unsuccessful"),
                                              ("successful", ["0", "3"], "successful")])
def test_the_close_follows_how_the_run_ended(kind: str, states: list, code: str | None) -> None:
    out = _run(build.CHANGE_CLOSE_CODE, {"kind": kind, "reason": "why it ended", "note": "Rejected by x.", "job_id": JOB})
    got = [out["first"]["state"]] + ([out["second"]["state"]] if out["two_steps"] else [])
    assert got == states
    link = f"Job: {build.CHANGE['job_url']}{JOB}"
    if code:
        assert out["second"]["close_code"] == code and out["second"]["close_notes"] == f"why it ended. {link}"
    else:
        want = "Canceled: why it ended." if kind == "canceled" else "Rejected by x. Outcome: why it ended."
    if kind == "rejected":  # with no decision note the work note still says it was rejected
        bare = _run(build.CHANGE_CLOSE_CODE, {"kind": "rejected", "reason": "done!", "note": "", "job_id": JOB})
        assert bare["first"]["work_notes"].startswith("Rejected in Itential Work Center. Outcome: done! Job: ")
        assert out["first"]["work_notes"] == f"{want} {link}" and out["second"] is None
