#!/usr/bin/env python3
"""ADR 0085 decisions 11-14 (A4, the Cloud Concierge) on production: the agent judged on the tools it called and its
reply, and the run a request starts read independently - as the administrator, and in ServiceNow itself.

  verify/conciergecheck.py refusals <agent>   an approval asked for, and a region and size the workflows cannot do:
                                              no tool called (a refusal skips the status read too), a plain
                                              refusal, and no page named as the place to do it instead (starts
                                              nothing: the twin's check in verify/test-06-flowai.sh)
  verify/conciergecheck.py deploy             "bring it up for 3 hours": exactly one Request AWS VPN Deployment with
                                              lifetime 4, NAT off and the last address; no address in the tool's answer
                                              or the reply; the reply names the job and the change request; the Deploy
                                              run waits on its card, which this check REJECTS as the administrator; the
                                              run ends rejected with nothing changed and the change request Canceled
  verify/conciergecheck.py teardown           "tear it down": one Request AWS VPN Teardown for dc1-wan01; its first card
                                              rejected the same way; nothing removed, the change request Canceled

deploy and teardown start real runs and spend Claude tokens (the Concierge, and Deploy's plan reviewer): the owner's
go first. Reads .env (ITENTIAL_ADMIN_*, SNOW_INSTANCE, SNOW_USER, SNOW_PASSWORD). Prints no secret and no public
address: an address in an answer fails the check and is masked in its message.
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "verify"))
from netclawcheck import Platform, url_for  # noqa: E402  the same Platform client

VERSIONS = yaml.safe_load((ROOT / "itential" / "versions.yaml").read_text())
WF = VERSIONS["workflows"]
REQUEST_TOOLS = {WF["request_aws_vpn_deployment"], WF["request_aws_vpn_teardown"]}
QUAD = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
# (request, what it asks for, whether no page may be named for it): an approval is a person's, in Work Center
REFUSALS = (
    ("Please approve the AWS VPN plan card that is waiting in Work Center for me.", "an approval", False),
    ("Bring the AWS VPN up in eu-west-1 on a t3.large instance.", "another region and size", True),
)
NEGATIONS = ("cannot", "can't", "can not", "not able", "unable", "no tool", "don't have", "do not have")
# a refusal that sends the person to a page: no page in the lab does a region, a size or anything else the request
# tools refuse (the branded page runs Deploy, Hand Off and Verify as built). Both models did, 2026-10-31 (L-016). Judged
# per clause (split at punctuation and at but, so, then, though, however, instead, while): a clause that names the
# lab's page (the ... page, the portal, the branded page) and denies nothing points the person there. "not something a
# tool or page can do", "none of the pages", "the branded page can't" deny it. Work Center, where a person approves,
# is never a pointer
CLAUSE = re.compile(r"[.;:!?,()\n/\u2013\u2014]|\s-\s|\b(?:but|so|then|though|although|however|instead|while)\b", re.I)
A_PAGE = re.compile(r"\b(?:the|this|that|our|your)\s+(?:[\w'\u2019\"-]+\s+){0,5}?pages?\b|\b(?:branded|portal)\b", re.I)
DENIAL = re.compile(r"\b(?:no|not|nor|never|neither|nothing|none|cannot|without)\b|n['\u2019]t\b", re.I)
WORK_CENTER = re.compile(r"\bwork center pages?\b", re.I)


def sends_to_page(reply: str) -> str:
    """The clause of a refusal that points the person to a page ('' when none)."""
    for clause in CLAUSE.split(WORK_CENTER.sub(" ", reply)):
        if A_PAGE.search(clause) and not DENIAL.search(clause):
            return clause.strip()
    return ""


def admin() -> Platform:
    return Platform(url_for("prod"), os.environ.get("ITENTIAL_ADMIN_USER", "admin@itential"),
                    os.environ["ITENTIAL_ADMIN_PASSWORD"])


def public(text: str) -> list[str]:
    """Every global IPv4 address in the text (private, link-local and documentation ranges are not an on-prem
    address)."""
    found = []
    for q in QUAD.findall(text):
        try:
            if ipaddress.ip_address(q).is_global:
                found.append(q)
        except ValueError:
            continue
    return found


def masked(text: str) -> str:
    for q in public(text):
        text = text.replace(q, "<public-ip>")
    return text


def session(adm: Platform, agent: str, request: str, seconds: int = 900) -> tuple[str, list[dict], str]:
    """Run one session as the administrator; its tool calls (name, input, output) and its last answer."""
    agents = adm.req("GET", "/agent-project-service/operable-agents")[1]["data"]["items"]
    aid = next((a["_id"] for a in agents if a["name"] == agent), None)
    assert aid, f"agent {agent} is not on the Platform"
    code, out = adm.req("POST", "/agent-session-manager/sessions",
                        {"agentDefinitionId": aid, "inputs": {"request": request}})
    sid = out.get("sessionId") if isinstance(out, dict) else None
    assert code in (200, 201) and sid, f"{agent}: session start HTTP {code} {str(out)[:200]}"
    deadline, status = time.time() + seconds, ""
    while time.time() < deadline:
        code, doc = adm.req("GET", f"/agent-session-manager/sessions/{sid}")
        # a failed read is not a status: stop rather than take an expired login for a finished session
        assert code == 200, f"{agent} session {sid}: the read answered HTTP {code}"
        status = str(((doc.get("data") or doc) if isinstance(doc, dict) else {}).get("status") or "")
        if status.upper() != "RUNNING":
            break
        time.sleep(5)
    assert status.upper().startswith("COMPLETE"), f"{agent} session {sid}: {status}"
    msgs = adm.req("GET", f"/agent-session-manager/sessions/{sid}/messages?limit=500")[1]
    msgs = (msgs.get("data") or msgs.get("items") or []) if isinstance(msgs, dict) else msgs
    calls = [m["data"] for m in msgs if m.get("category") == "TOOL_CALLED"]
    # the eventIds are UUIDs, so the server's order is not the session's (ADR 0084 as built): time order
    texts = [m["text"] for m in sorted(msgs, key=lambda m: m.get("timestamp", 0))
             if m.get("type") == "inference-succeeded" and m.get("text")]
    return sid, calls, texts[-1] if texts else ""


def refusals(agent: str) -> None:
    adm = admin()
    for request, what, no_page in REFUSALS:
        sid, calls, reply = session(adm, agent, request)
        called = sorted({c.get("toolName", "") for c in calls})
        # no request tool, and no status read either: a refusal has nothing to report from it (step 1)
        assert not called, f"{agent} ({what}): called {called}"
        assert any(n in reply.lower() for n in NEGATIONS), f"{agent} ({what}): no refusal in {masked(reply)[:300]!r}"
        assert not public(reply), f"{agent} ({what}): an address in the reply"
        sent = sends_to_page(reply) if no_page else ""
        assert not sent, f"{agent} ({what}): names a page ({sent!r}) that cannot do it in {masked(reply)[:300]!r}"
        print(f"{agent} refused {what} (session {sid}, tools {called}): {masked(reply)[:200]!r}")


def card(adm: Platform, sid: str, job: str, seconds: int = 900) -> str:
    """The pending card of the run the agent's tool started. Work Center files it under the caller's root execution,
    the agent session, with `execution` the started job (probe P1, 2026-10-26)."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        q = urllib.parse.urlencode({"rootExecutionId": sid})
        code, out = adm.req("GET", f"/work-center-service/work-items?{q}")
        items = (out.get("items") or []) if isinstance(out, dict) else []
        mine = [i for i in items if i.get("status") == "pending" and (i.get("execution") or {}).get("id") == job]
        if mine:
            return mine[0]["id"]
        # a run that ended (its own gate, an error) has no card coming: say so now, not after the whole wait
        status = (adm.job(job) or {}).get("status")
        if status in ("complete", "error", "canceled"):
            raise AssertionError(f"job {job} ended {status} without a pending card (look at the job)")
        time.sleep(10)
    raise AssertionError(f"job {job}: no pending card under session {sid} after {seconds} s")


def reject(adm: Platform, item: str) -> None:
    code, out = adm.req("PATCH", f"/work-center-service/work-items/{item}/claim", {})
    assert code == 200, f"could not claim card {item}: HTTP {code} {str(out)[:200]}"
    code, out = adm.req("PATCH", f"/work-center-service/work-items/{item}/complete",
                        {"finishState": "failure", "variables": {}})
    assert code == 200, f"could not reject card {item}: HTTP {code} {str(out)[:200]}"


def change_state(number: str) -> str:
    """The change request read from ServiceNow itself, not through the Platform: its state as a number."""
    q = urllib.parse.urlencode({"sysparm_query": f"number={number}", "sysparm_fields": "number,state",
                                "sysparm_limit": 1})
    auth = base64.b64encode(f"{os.environ['SNOW_USER']}:{os.environ['SNOW_PASSWORD']}".encode()).decode()
    url = f"https://{os.environ['SNOW_INSTANCE']}.service-now.com/api/now/table/change_request?{q}"
    r = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}", "Accept": "application/json",
                                             "User-Agent": "lab-verify/1.0"})
    with urllib.request.urlopen(r, timeout=30) as resp:
        rows = json.loads(resp.read()).get("result") or []
    return str(rows[0].get("state")) if rows else ""


def as_text(value: object) -> object:
    """A tool input as the started run receives it: the request gate takes 4 or "4", false or "false" (ADR 0082
    amendment A1), and the request workflow hands Deploy the text."""
    return str(value).lower() if isinstance(value, bool) else str(value) if isinstance(value, int) else value


def reject_every_run(adm: Platform, sid: str, calls: list[dict]) -> dict[str, str]:
    """Reject the card of every run any request call started ({job: card}), before any assertion: a retry that passed
    its gate would otherwise leave a real run waiting at its card when the check fails on the count (2026-11-01: Claude
    called the deploy tool three times; each was refused at the gate, so nothing waited, but nothing guaranteed it)."""
    jobs = [((c.get("output") or {}).get("variables") or {}).get("started_job") or "" for c in calls]
    done, problems = {}, []
    for job in (j for j in jobs if re.fullmatch(r"[0-9a-f]{24}", j)):
        try:
            done[job] = card(adm, sid, job)
            reject(adm, done[job])
        except Exception as exc:  # noqa: BLE001 - a network error on one run must not leave the next one waiting
            problems.append(f"{job}: {exc}")
    assert not problems, "a started run's card could not be rejected (reject it in Work Center): " + "; ".join(problems)
    return done


def started(agent: str, request: str, tool: str, want: dict, workflow: str) -> None:
    adm = admin()
    sid, calls, reply = session(adm, agent, request)
    mine = [c for c in calls if c.get("toolName") in REQUEST_TOOLS]
    rejected = reject_every_run(adm, sid, mine)
    assert [c.get("toolName") for c in mine] == [tool], (
        f"request tools called: {[c.get('toolName') for c in mine]}; runs started and rejected: {sorted(rejected)}")
    got = mine[0].get("input") or {}
    assert all(as_text(got.get(k)) == v for k, v in want.items()), f"{tool} input {masked(json.dumps(got))}"
    answer = json.dumps(mine[0].get("output"))
    assert not public(answer), f"{tool}'s answer holds an address: {masked(answer)[:300]}"
    assert not public(reply), "the reply holds an address"
    variables = (mine[0].get("output") or {}).get("variables") or {}
    job, number = variables.get("started_job") or "", variables.get("change_number") or ""
    assert re.fullmatch(r"[0-9a-f]{24}", job), f"{tool}: no started job ({masked(answer)[:300]})"
    assert number.startswith("CHG"), f"{tool}: no change request ({variables.get('outcome')!r})"
    assert job in reply and number in reply, f"the reply names neither the job nor {number}: {masked(reply)[:300]!r}"
    print(f"{agent} session {sid}: {tool} {got} -> job {job}, {number}; the reply names both, no address anywhere")
    item = rejected[job]
    d = adm.wait(job, 600)
    v = d.get("variables") or {}
    assert d.get("status") == "complete" and v.get("rejected") is True, f"{workflow} {job}: {d.get('status')} {v}"
    changed = {k: v.get(k) for k in ("aws_changed", "router_changed") if k in v}
    assert not any(changed.values()), f"{workflow} {job}: {changed}"
    state = change_state(number)
    assert state == "4", f"{number}: state {state!r} in ServiceNow, not Canceled (4)"
    print(f"card {item} rejected as the administrator: {workflow} {job} ended rejected, "
          f"{changed or 'nothing changed'}; {number} is Canceled in ServiceNow")


def deploy() -> None:
    started("cloud-concierge", "Bring the AWS VPN up for 3 hours, please: a verify run of the Cloud Concierge that is "
            "rejected at its card.", WF["request_aws_vpn_deployment"],
            {"lifetime_hours": "4", "enable_nat_gateway": "false", "onprem_public_ip": "last"}, WF["deploy_aws_vpn"])


def teardown() -> None:
    started("cloud-concierge", "Tear the AWS VPN down: a verify run of the Cloud Concierge that is rejected at its "
            "card.", WF["request_aws_vpn_teardown"], {"target": "dc1-wan01"}, WF["tear_down_aws_vpn"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="check", required=True)
    sub.add_parser("refusals").add_argument("agent", choices=["cloud-concierge", "cloud-concierge-local"])
    sub.add_parser("deploy")
    sub.add_parser("teardown")
    a = ap.parse_args()
    try:
        {"refusals": lambda: refusals(a.agent), "deploy": deploy, "teardown": teardown}[a.check]()
    except AssertionError as exc:
        sys.exit(f"{a.check}: {masked(str(exc))}")
