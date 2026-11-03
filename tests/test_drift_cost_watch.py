"""A5 Drift and Cost Watch (ADR 0085, delivery aws-vpn-roadmap D6, D10, D11): the daily Check AWS Drift decides by itself
whether there is anything to propose (drift, or a deployment with no end time 24 h after its last apply), hands
drift-cost-watch the facts and a menu fixed by the situation, holds its one JSON line to that menu, and shows one card
whose approval starts Deploy (the same inputs) or Tear Down through its endpoint. Anything the watch cannot do falls
back to the R3 drift task, so the model can never hide drift. The functions are pure and runCode runs their own source
on the Gateway, so these tests test what runs."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "wf_build_watch", ROOT / "itential" / "workflows" / "build.py"
)
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)
RULES, GATE_RULES = build.WATCH_RULES, build.WATCH_GATE_RULES
NOW = datetime(2026, 11, 3, 7, 0, tzinfo=UTC)
INSTANCE = "module.vpn[0].aws_instance.strongswan"
EIP = "203.0.113.10"  # the outputs' strongSwan address: the watch never passes it on
NOTE_RULE = re.compile(build.CHANGE_NOTE_RULE["pattern"])


def drift(resources: list | None = None, checked: int = 53) -> dict:
    """drift_summary's runCode result, as Check AWS Drift publishes it."""
    rep = {
        "drifted": len(resources or []),
        "resources_checked": checked,
        "resources": resources or [],
    }
    return {
        "stdout_json": build.drift_summary(
            {"drift": {"result": {"return_code": 0, "stdout_json": {"drift": rep}}}}
        )
    }


TAGGED = [
    {"address": INSTANCE, "actions": ["update"], "attributes": ["tags", "tags_all"]}
]


def outputs(
    expires_at: str | None = "2026-11-03T10:12:00Z",
    last: str | None = "2026-11-02T22:00:00Z",
    nat: bool | None = False,
    rc: int = 0,
    deployed: bool = True,
) -> dict:
    out = (
        {
            "expires_at": expires_at,
            "strongswan_eip": EIP,
            "strongswan_instance_type": "t3.micro",
            "nat_gateway_enabled": nat,
        }
        if deployed
        else {}
    )
    return {
        "result": {
            "return_code": rc,
            "stdout_json": {
                "outputs": out,
                "state": {"read": last is not None, "last_modified": last},
            },
        }
    }


def gate(d: dict, now: datetime = NOW) -> dict:
    return build.watch_gate(d, now, GATE_RULES)


def answer(line: str, status: str = "COMPLETE") -> dict:
    return {"sessionId": "s1", "sessionStatus": status, "lastMessage": line}


def propose(g: dict, agent: dict) -> dict:
    return build.watch_proposal({"gate": {"stdout_json": g}, "agent": agent, **RULES})


DRIFT_DAY = gate({"drift": drift(TAGGED), "outputs": outputs()})
LONG_DAY = gate(
    {
        "drift": drift([]),
        "outputs": outputs(expires_at="none", last="2026-11-01T22:00:00Z"),
    }
)


# ── the gate: the workflow decides, never the model (D10) ──


@pytest.mark.parametrize(
    "d, says",
    [
        (
            {"drift": drift([], checked=0), "outputs": outputs(deployed=False)},
            "nothing is deployed",
        ),
        (
            {"drift": drift([]), "outputs": outputs()},
            "deployed until 2026-11-03T10:12:00Z",
        ),
        (
            {
                "drift": drift([]),
                "outputs": outputs(expires_at="none", last="2026-11-02T08:00:00Z"),
            },
            "last applied 23.0 h ago",
        ),
        (
            {"drift": drift([]), "outputs": outputs(expires_at="none", last=None)},
            "not known",
        ),
        ({"drift": drift([]), "outputs": outputs(rc=1)}, "could not be read"),
    ],
)
def test_nothing_to_propose_ends_quietly_with_no_agent_call(d: dict, says: str) -> None:
    g = gate(d)  # criterion 13: no card and no request for the model
    assert g["route"] == "quiet" and "request" not in g and says in g["summary"]


def test_an_old_deployment_with_an_end_time_is_tear_down_expired_s_not_the_watch_s() -> None:
    # applied 33 h ago but it has an end time: Tear Down Expired AWS VPN removes it then (R2b), so no proposal
    g = gate({"drift": drift([]), "outputs": outputs(expires_at="2026-11-04T07:00:00Z", last="2026-11-01T22:00:00Z")})
    assert g["route"] == "quiet" and "deployed until 2026-11-04T07:00:00Z" in g["summary"]


def test_a_day_is_long_running_from_24_hours_after_the_last_apply() -> None:
    d = {
        "drift": drift([]),
        "outputs": outputs(expires_at="none", last="2026-11-02T07:00:00Z"),
    }
    assert gate(d)["route"] == "watch" and gate(d)["situation"] == "long-running"
    assert gate(d, now=datetime(2026, 11, 3, 6, 54, tzinfo=UTC))["route"] == "quiet"


@pytest.mark.parametrize(
    "d, why",
    [
        ({"drift": drift(TAGGED), "outputs": outputs(rc=1)}, "could not be read"),
        (
            {
                "drift": drift(TAGGED),
                "outputs": outputs(expires_at="2026-11-03T06:00:00Z"),
            },
            "Tear Down Expired",
        ),
        (
            {"drift": drift(TAGGED), "outputs": outputs(expires_at="soon")},
            "Tear Down Expired",
        ),
    ],
)
def test_drift_the_watch_does_not_take_opens_the_drift_task(d: dict, why: str) -> None:
    g = gate(
        d
    )  # an expired deployment is Tear Down Expired AWS VPN's (R2b): the watch never duplicates it
    assert g["route"] == "fallback" and why in g["reason"] and "request" not in g


def test_a_drift_day_offers_redeploy_or_tear_down_and_names_attributes_only() -> None:
    g = DRIFT_DAY
    assert (
        g["route"] == "watch"
        and g["situation"] == "drift"
        and g["menu"] == ["redeploy", "tear-down"]
    )
    assert f"{INSTANCE}: tags, tags_all" in g["request"] and g["drifted"] == [INSTANCE]
    assert (
        "values are never given" in g["request"] and "about $0.40 a day" in g["request"]
    )
    assert EIP not in json.dumps(
        g
    )  # the deployment's public address never reaches the model or the card


def test_a_long_running_day_offers_only_tear_down_with_the_cost_so_far() -> None:
    g = LONG_DAY  # owner, 2026-11-02: no "nothing", so the model can never hold back the cost card
    assert g["situation"] == "long-running" and g["menu"] == ["tear-down"]
    assert "- nothing" not in g["request"]
    assert (
        "no end time for 33.0 h" in g["reason"]
        and "so far" in g["reason"]
        and g["lifetime"] == "none"
    )


def test_a_drift_day_that_is_also_long_running_says_so() -> None:
    g = gate(
        {
            "drift": drift(TAGGED),
            "outputs": outputs(expires_at="none", last="2026-11-01T22:00:00Z"),
        }
    )
    assert (
        g["situation"] == "drift"
        and "also long-running" in g["request"]
        and "no end time for 33.0 h" in g["reason"]
    )


@pytest.mark.parametrize(
    "ends, lifetime",
    [
        (
            "2026-11-03T10:12:00Z",
            "4",
        ),  # 3.2 h left: the allowed lifetime nearest it (owner, 2026-11-02)
        ("2026-11-03T08:00:00Z", "2"),
        ("2026-11-03T15:00:00Z", "8"),
        ("2026-11-03T12:54:00Z", "4"),  # 5.9 h left: nearer 4 than 8, even below it
        ("2026-11-04T13:00:00Z", "24"),  # 30 h left: 24, not 72
        ("2026-11-05T07:00:00Z", "24"),  # 48 h left, as near 24 as 72: the shorter
        ("2026-11-06T07:00:00Z", "72"),
        ("2026-11-09T07:00:00Z", "72"),  # more than any choice: the longest
        ("none", "none"),
    ],
)
def test_a_redeploy_keeps_the_end_as_close_as_deploy_allows(
    ends: str, lifetime: str
) -> None:
    g = gate({"drift": drift(TAGGED), "outputs": outputs(expires_at=ends)})
    assert g["lifetime"] == lifetime and g["lifetime"] in build.DEPLOY_HOURS


@pytest.mark.parametrize(
    "nat, text", [(True, "true"), (False, "false"), (None, "false")]
)
def test_a_redeploy_keeps_the_nat_gateway_the_state_records(nat, text: str) -> None:
    g = gate({"drift": drift(TAGGED), "outputs": outputs(nat=nat)})
    assert g["nat"] == text
    if nat:
        assert (
            "about $1.60 a day" in g["cost"]
        )  # the NAT gateway and its address are billed too


# ── at most one open card ──


def _open(names: list, day: dict) -> dict:
    return build.watch_open({"jobs": {"data": [{"_id": str(i), "name": n} for i, n in enumerate(names)]},
                             "self": build.WF["check_aws_drift"], "changes": build.WATCH_CHANGES,
                             "gate": {"stdout_json": day}})


@pytest.mark.parametrize("names", [["Check AWS Drift"], ["Check AWS Drift", "Show Version", "Describe Job"]])
def test_nothing_open_lets_the_watch_run(names: list) -> None:
    assert _open(names, DRIFT_DAY) == {"ok": True, "route": "watch"}


@pytest.mark.parametrize("day", [DRIFT_DAY, LONG_DAY])
def test_an_earlier_card_still_open_ends_today_s_run(day: dict) -> None:
    out = _open(["Check AWS Drift", "Check AWS Drift"], day)  # D10: at most one open card, the drift task included
    assert out["route"] == "end" and "an earlier Check AWS Drift run is still waiting" in out["summary"]
    assert out["summary"].startswith(day["summary"])


@pytest.mark.parametrize("change", ["Deploy AWS VPN", "Tear Down Expired AWS VPN", "Diagnose AWS VPN Outage"])
def test_a_change_in_flight_never_hides_drift(change: str) -> None:
    # Tear Down Expired AWS VPN runs at the top of every hour, 07:00 included: drift still opens the R3 drift task
    out = _open(["Check AWS Drift", change], DRIFT_DAY)
    assert out["route"] == "fallback" and change in out["reason"]
    long = _open(["Check AWS Drift", change], LONG_DAY)  # no drift: only the cost proposal waits a day
    assert long["route"] == "end" and change in long["summary"]


def test_running_jobs_that_cannot_be_read_hand_back_to_r3() -> None:
    assert build.watch_open({"jobs": {}, "self": "x", "changes": []})["ok"] is False


# ── the proposal, held to the menu and the facts ──


def test_a_redeploy_proposal_makes_the_card_and_the_deploy_inputs() -> None:
    p = propose(
        DRIFT_DAY,
        answer(
            '{"proposal": "redeploy", "why": "A tag on '
            + INSTANCE
            + ' was changed by hand."}'
        ),
    )
    assert p["route"] == "card" and p["proposal"] == "redeploy" and not p["unavailable"]
    assert p["body"] == {
        "change_note": "Drift and Cost Watch: A tag on "
        + INSTANCE
        + " was changed by hand.",
        "enable_nat_gateway": "false",
        "lifetime_hours": "4",
    }
    assert "__FROM__" in p["message"] and "Reject starts nothing" in p["message"]
    assert p["card"]["what_changed_outside_terraform"] == [
        f"{INSTANCE}: tags, tags_all"
    ]
    assert "__J__" in p["started"] and p["started"].count("__J__") == 1


def test_a_tear_down_proposal_targets_the_one_router_in_aws() -> None:
    p = propose(
        LONG_DAY,
        answer(
            '{"proposal": "tear-down", "why": "It has run with no end time for over a day."}'
        ),
    )
    assert p["route"] == "card" and p["body"] == {
        "change_note": "Drift and Cost Watch: It has run with no end time "
        "for over a day.",
        "target": "dc1-wan01",
    }
    assert "__FROM__" not in p["message"] and "two cards" in " ".join(
        p["card"]["the_run_it_starts"]
    )


@pytest.mark.parametrize(
    "agent",
    [
        {},  # the session errored: runAgent's answer stays {}
        answer("", status="CANCELED"),
        answer("I think you should redeploy."),
        answer(
            '{"proposal": "nothing", "why": "Tags do not matter."}'
        ),  # nothing is on no menu
        answer('{"proposal": "rebuild", "why": "x"}'),
    ],
)
def test_on_a_drift_day_anything_but_a_menu_answer_opens_the_drift_task(
    agent: dict,
) -> None:
    p = propose(
        DRIFT_DAY, agent
    )  # criterion 16, and D10: the agent can never hide drift
    assert p["route"] == "fallback" and p["reason"] and "body" not in p


def test_on_a_long_running_day_a_failed_watch_still_proposes_tear_down() -> None:
    p = propose(LONG_DAY, {})
    assert p["route"] == "card" and p["proposal"] == "tear-down" and p["unavailable"]
    assert (
        p["why"] == LONG_DAY["reason"]
        and "the workflow's own reason" in p["card"]["why"]
    )


def test_nothing_on_a_long_running_day_still_gets_the_tear_down_card() -> None:
    p = propose(
        LONG_DAY,
        answer('{"proposal": "nothing", "why": "The request says it must stay up."}'),
    )
    assert p["route"] == "card" and p["proposal"] == "tear-down" and p["unavailable"]
    assert "must stay up" not in json.dumps(p)  # off-menu words never reach the card


def test_the_answer_may_sit_in_a_fence_after_a_word() -> None:
    p = propose(
        DRIFT_DAY,
        answer(
            'Here it is:\n```json\n{"proposal": "tear-down", "why": "Remove it."}\n```'
        ),
    )
    assert p["route"] == "card" and p["proposal"] == "tear-down"


@pytest.mark.parametrize("words", [
    "The tunnel peer 8.8.8.8 changed.",                         # a global address
    "The instance at 10.0.0.5 was retagged.",                   # a private one: no address of any kind
    "It costs about $90.00 a day.",                             # a cost: the card shows the computed one (D5)
    "It costs about $0.40 a day, as computed.",                 # even the right one
    "About 90 dollars so far.",
    "module.vpc.aws_nat_gateway.main[0] was deleted by hand.",  # a resource that did not drift
])
def test_words_that_leave_the_facts_are_replaced_by_the_workflow_s_reason(words: str) -> None:
    p = propose(DRIFT_DAY, answer(json.dumps({"proposal": "redeploy", "why": words})))
    assert p["route"] == "card" and p["why"] == DRIFT_DAY["reason"] and words not in json.dumps(p)
    assert "the workflow's own reason" in p["card"]["why"]


def test_the_change_note_passes_deploy_s_own_gate() -> None:
    long = 'A "quoted" <b>tag</b> \\ changed\non ' + INSTANCE + ". " + "x" * 400
    p = propose(DRIFT_DAY, answer(json.dumps({"proposal": "redeploy", "why": long})))
    note = p["body"]["change_note"]
    assert (
        note.startswith(RULES["prefix"])
        and len(note) <= 280
        and NOTE_RULE.fullmatch(note)
    )


# ── what runs on the Gateway is this source ──


def _run(code: str, data: dict) -> dict:
    run = subprocess.run(
        [sys.executable, "-I", "-c", code],
        input=json.dumps(data),
        capture_output=True,
        text=True,
        timeout=30,
        env={},
        check=True,
    )
    return json.loads(run.stdout)


def test_the_gate_runs_as_the_gateway_runs_it() -> None:
    assert (
        _run(
            build.WATCH_GATE_CODE,
            {"drift": drift(TAGGED), "outputs": outputs(expires_at="none")},
        )["route"]
        == "watch"
    )


def test_the_open_check_runs_as_the_gateway_runs_it() -> None:
    jobs = {"data": [{"name": "Check AWS Drift"}, {"name": "Deploy AWS VPN"}]}
    assert _run(build.WATCH_OPEN_CODE, {"jobs": jobs, "gate": {"stdout_json": DRIFT_DAY}})["route"] == "fallback"


def test_the_proposal_check_runs_as_the_gateway_runs_it() -> None:
    out = _run(
        build.WATCH_PROPOSAL_CODE,
        {
            "gate": {"stdout_json": DRIFT_DAY},
            "agent": answer('{"proposal": "redeploy", "why": "Put the tag back."}'),
        },
    )
    assert out["route"] == "card" and out["body"]["lifetime_hours"] == "4"


# ── the workflow ──

WF = build.check_aws_drift()
TASKS, TR = WF["tasks"], WF["transitions"]


def _into(tid: str) -> set[str]:
    return {src for src, dst in TR.items() if tid in dst}


def _named(name: str) -> list[str]:
    return [tid for tid, t in TASKS.items() if t.get("name") == name]


def _reach(start: str, cut: tuple[str, str] | None = None) -> set[str]:
    seen, todo = set(), [start]
    while todo:
        node = todo.pop()
        for dst in TR.get(node, {}):
            if (node, dst) != cut and dst not in seen:
                seen.add(dst)
                todo.append(dst)
    return seen


def _code(fn: str) -> str:
    """The runCode task that runs `fn`'s source."""
    (tid,) = [t for t in _named("runCode") if f"def {fn}(" in TASKS[t]["variables"]["incoming"]["code"]]
    return tid


def _evaluation(query: str, value, of: str | None = None) -> str:
    """The one evaluation of `query` == `value` (on the task `of`, when given)."""
    def first(t: dict) -> dict:
        return t["variables"]["incoming"]["evaluation_groups"][0]["evaluations"][0]

    (tid,) = [tid for tid, t in TASKS.items() if t.get("name") == "evaluation"
              and first(t)["query"] == query and first(t)["operand_2"]["variable"] == value
              and (of is None or first(t)["operand_1"]["task"] == of)]
    return tid


def test_the_agent_runs_only_after_the_gate_and_the_open_check() -> None:
    (agent,) = _named("runAgent")
    assert TASKS[agent]["variables"]["incoming"]["agent"] == build.WATCH_MARKER
    assert agent in _reach("workflow_start")
    # every way to the agent passes the gate's "something to propose?" answered yes, then the open check's "an earlier
    # card open?" and "a change in flight, with drift?" both answered no
    gate, opened = _code("watch_gate"), _code("watch_open")
    for tid, state in ((_evaluation("stdout_json.route", "watch", of=gate), "success"),
                       (_evaluation("stdout_json.route", "end", of=opened), "failure"),
                       (_evaluation("stdout_json.route", "fallback", of=opened), "failure")):
        nxt = next(d for d, e in TR[tid].items() if e["state"] == state)
        assert agent not in _reach("workflow_start", cut=(tid, nxt)), tid


def test_the_approved_run_starts_only_through_deploy_s_or_tear_down_s_endpoint() -> (
    None
):
    starts = _named("runEndpointTriggerWithPost")
    routes = sorted(TASKS[s]["variables"]["incoming"]["routeName"] for s in starts)
    assert routes == sorted([RULES["routes"]["deploy"], RULES["routes"]["teardown"]])
    (card,) = _named("InteractiveHTML")
    for (
        s
    ) in starts:  # every start sits after the card's approval, never on its reject path
        seen, todo = set(), [card]
        while todo:
            n = todo.pop()
            for dst, edge in TR.get(n, {}).items():
                if n == card and edge["state"] != "success" or dst in seen:
                    continue
                seen.add(dst)
                todo.append(dst)
        assert s in seen
    assert not _named("childJob") and not _named("startJob")


def test_the_on_prem_address_never_becomes_a_job_variable() -> None:
    # it is read into task outputs (the Concierge's way) and handed to Deploy's start only
    for tid, t in TASKS.items():
        out = json.dumps(t.get("variables", {}).get("outgoing") or {})
        assert "onprem_public_ip" not in out and "$var.job.ip" not in out, tid
    address = [tid for tid, t in TASKS.items() if "stdout_json.ip" in json.dumps(t)]
    assert len(address) == 1 and not any(
        str(v).startswith("$var.job")
        for v in TASKS[address[0]]["variables"]["outgoing"].values()
    )


def test_every_way_back_to_r3_keeps_the_drift_task_reachable() -> None:
    (task,) = _named("ViewData")
    (words,) = _into(task)
    # the gate's fallback, a change in flight, the proposal's fallback, a redeploy with no address, and R3's `found?`
    assert len(_into(words)) == 5
    # a watch step that errors goes back to R3's own routing: the gate, the running-jobs read, the open check and the
    # proposal check
    (found,) = [src for src in _into(words) if TASKS[src]["name"] == "evaluation"]
    back = {src for src, dst in TR.items() if dst.get(found, {}).get("state") == "error"}
    code = {src: TASKS[src]["variables"]["incoming"].get("code", "") for src in back}
    assert sorted(TASKS[src]["name"] for src in back) == ["getJobs", "runCode", "runCode", "runCode"]
    for fn in ("def watch_gate", "def watch_open", "def watch_proposal"):
        assert sum(fn in c for c in code.values()) == 1, fn


def test_the_watch_agents_hold_no_tools_and_the_twin_has_the_same_words() -> None:
    claude = yaml.safe_load((ROOT / "itential" / "agents" / "drift-cost-watch.yaml").read_text())
    twin = yaml.safe_load((ROOT / "itential" / "agents" / "drift-cost-watch-local.yaml").read_text())
    assert claude["tools"] == [] == twin["tools"] and claude["instructions"] == twin["instructions"]
    assert (claude["profile"], twin["profile"]) == ("anthropic", "ollama-mac")
    for word in ("redeploy", "tear-down alone", "a person decides", "{{request}}"):
        assert word in claude["instructions"]
