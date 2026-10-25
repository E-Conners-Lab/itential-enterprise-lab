#!/usr/bin/env python3
"""ADR 0084 verify: the skills eval suite's governed path on production, one probe per acceptance criterion.

Every check acts as the account the eval runner uses - svc-eval for the agent tools, svc-eval-approver for the card and
the restore - against the routes the runner posts to, and reads the outcome independently: the administrator login for
gbac, inventories and Work Center, and the router itself over RESTCONF as `judge` with its certificate pin (never through
the Platform it is checking).

  verify/skillslabcheck.py <check> --tier prod
    accounts   both accounts log in and each session holds exactly its group
    inventory  1  skills-lab holds exactly skl-r1..3 with its groups; none in lab, Configuration Manager or NetBox
    gbac       2  the agent-tool routes answer svc-eval; the restore, a production route and a direct start refuse it;
                  the approver may start none of the agent tools; each scratch automation's gbac as versions.yaml says
    forbidden  4  `write memory`, `do copy running-config startup-config` and `int gi1` (the management path)
                  refused before any card
    restconf   5  an allowlisted path returns JSON; the username path is refused by the gate; no password in the job
    hold       3  a push waits on its card, svc-eval cannot claim it, the router is unchanged; the approver finds the
                  card itself and releases it, and the change is on the router; a second push rejected leaves it off;
                  then the baseline is restored
    restore    6  after a probe change, the restore reports restored with equal hashes and the change is gone
    agents     7  one skill-eval-local session (--claude: skill-eval too) follows a probe procedure with the scratch
                  tools only; no agent document holds the procedure; svc-eval deletes each session once scored and
                  the administrator reads it gone; then how far the delete reaches, on a throwaway session the
                  administrator starts (skill-eval-local)
    profile    8  anthropic-sonnet-5-5 serves skill-eval; every agent document is operable
    project       the pair sits in skills_lab.project alone, which has no group among its members
    all           every check but agents, in the order above

Production only: the scratch lab is production's. Reads .env (ITENTIAL_ADMIN_*, SVC_EVAL_PROD_PASSWORD,
SVC_EVAL_APPROVER_PROD_PASSWORD, SKILLS_LAB_JUDGE_PASSWORD, SKILLS_LAB_READER_PASSWORD, NETBOX_URL, NETBOX_TOKEN).
Prints no secret. The hold and restore probes change skl-r1 and put its baseline back.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "verify"))
from netclawcheck import Platform, card_id, url_for  # noqa: E402  the same Platform client and card lookup

VERSIONS = yaml.safe_load((ROOT / "itential" / "versions.yaml").read_text())
SKILLS = VERSIONS["skills_lab"]
ACCESS = VERSIONS["eval_access"]
OM = VERSIONS["operations_manager"]
WF = VERSIONS["workflows"]
ROUTE = {k: OM[k]["endpoint"]["route"] for k in SKILLS["workflows"]}
PROBE_DEVICE = "skl-r1"
INTERFACES = "/restconf/data/Cisco-IOS-XE-interfaces-oper:interfaces"
PROBE_PROCEDURE = ("Procedure (a probe of the lab's governed path, ADR 0084): 1. Read the operational interface list of "
                   f"{PROBE_DEVICE} with Get RESTCONF Data from a Scratch Device, path {INTERFACES}. 2. Count the "
                   "interfaces whose name starts with GigabitEthernet. Task: report that count. Finish with a final "
                   "line DIAGNOSIS: <the count>.")


def runner() -> Platform:
    return Platform(url_for("prod"), ACCESS["run"]["user"], os.environ[ACCESS["run"]["password_env"]])


def approver() -> Platform:
    return Platform(url_for("prod"), ACCESS["approve"]["user"], os.environ[ACCESS["approve"]["password_env"]])


def admin() -> Platform:
    return Platform(url_for("prod"), os.environ["ITENTIAL_ADMIN_USER"], os.environ["ITENTIAL_ADMIN_PASSWORD"])


def judge(device: str, path: str) -> int:
    """The router's own answer to one RESTCONF GET as `judge`, on a connection whose certificate matched the pin before
    any credential was sent: the HTTP status only (200 present, 404 absent)."""
    node = SKILLS["nodes"][device]
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # verification is the pin below, not a CA
    conn = http.client.HTTPSConnection(node["mgmt_host"], 443, timeout=60, context=context)
    try:
        conn.connect()
        presented = hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
        assert presented == node["cert_sha256"], f"{device}: the certificate is not the pinned one; nothing was sent"
        token = base64.b64encode(f"judge:{os.environ['SKILLS_LAB_JUDGE_PASSWORD']}".encode()).decode()
        conn.request("GET", path, headers={"Authorization": f"Basic {token}", "Accept": "application/yang-data+json"})
        response = conn.getresponse()
        response.read()
        return response.status
    finally:
        conn.close()


def loopback(n: int) -> str:
    return f"/restconf/data/Cisco-IOS-XE-native:native/interface/Loopback={n}"


def run(p: Platform, key: str, body: dict, seconds: int = 300) -> dict:
    code, job, out = p.start(ROUTE[key], body)
    assert code == 200 and job, f"{ROUTE[key]} as {key}: HTTP {code} {str(out)[:200]}"
    d = p.wait(job, seconds)
    d["_id"] = job
    return d


def decide(adm: Platform, appr: Platform, job: str, approve: bool) -> None:
    """Release or reject the job's card as svc-eval-approver: it finds the card itself, then Work Center claim and
    complete (the runner's path). On 6.6.0 the card is visible only to its job's groups, which the push opens to
    eval-approvers just before the card."""
    item = card_id(appr, job)
    assert item, f"job {job}: the approver sees no pending card (the administrator sees {card_id(adm, job)})"
    code, out = appr.req("PATCH", f"/work-center-service/work-items/{item}/claim", {})
    assert code == 200, f"the approver could not claim card {item}: HTTP {code} {str(out)[:200]}"
    code, out = appr.req("PATCH", f"/work-center-service/work-items/{item}/complete",
                         {"finishState": "success" if approve else "failure", "variables": {}})
    assert code == 200, f"the approver could not complete card {item}: HTTP {code} {str(out)[:200]}"


def propose(evl: Platform, lines: list[str], reason: str) -> dict:
    return run(evl, "scratch_push", {"device": PROBE_DEVICE, "lines": lines, "reason": reason})


def restore(appr: Platform, device: str = PROBE_DEVICE) -> dict:
    d = run(appr, "scratch_restore", {"device": device}, 600)
    v = d.get("variables") or {}
    assert d.get("status") == "complete" and v.get("restored") is True, (
        f"restore job {d['_id']}: {d.get('status')}, restored {v.get('restored')}, {v.get('error')}")
    assert v["baseline_sha256"] == v["running_sha256"]
    return d


# --- checks -------------------------------------------------------------------------------------------------------


def accounts() -> None:
    for p, account in ((runner(), ACCESS["run"]), (approver(), ACCESS["approve"])):
        who = p.req("GET", "/whoami")[1]
        groups = sorted(g["name"] if isinstance(g, dict) else g for g in who.get("groups", []))
        assert groups == [account["group"]], f"{account['user']}: groups {groups}"
        print(f"{account['user']} logs in; its session holds exactly {groups}")


def inventory() -> None:
    adm = admin()
    ids = {g["name"]: g["_id"] for g in adm.req("GET", "/authorization/groups?limit=100")[1]["results"]}
    inv = adm.req("GET", f"/inventory_manager/v1/inventories/{SKILLS['inventory']}")[1]["result"]
    assert sorted(inv["groups"]) == sorted(ids[g] for g in SKILLS["inventory_groups"]), f"groups {inv['groups']}"
    nodes = adm.req("GET", f"/inventory_manager/v1/inventories/{SKILLS['inventory']}/nodes")[1]["result"]["data"]
    have = {n["name"]: n["attributes"]["itential_host"] for n in nodes}
    assert have == {name: node["mgmt_host"] for name, node in SKILLS["nodes"].items()}, f"nodes {have}"
    lab = adm.req("GET", "/inventory_manager/v1/inventories/lab/nodes")[1]["result"]["data"]
    assert not [n["name"] for n in lab if n["name"] in SKILLS["nodes"]], "a scratch router is in the lab inventory"
    cm = adm.req("POST", "/configuration_manager/devices", {"options": {"limit": 500, "start": 0}})[1]
    assert not any(name in json.dumps(cm) for name in SKILLS["nodes"]), "Configuration Manager lists a scratch router"
    q = urllib.request.Request(f"{os.environ['NETBOX_URL']}/api/dcim/devices/?name__isw=skl-",
                               headers={"Authorization": f"Token {os.environ['NETBOX_TOKEN']}",
                                        "Accept": "application/json", "User-Agent": "lab-verify/1.0"})
    assert json.load(urllib.request.urlopen(q, timeout=30))["count"] == 0, "NetBox holds a scratch router as a device"
    print(f"{SKILLS['inventory']}: exactly {sorted(have)} with groups {SKILLS['inventory_groups']}; none in lab, "
          "Configuration Manager or NetBox's devices")


def gbac() -> None:
    evl, appr, adm = runner(), approver(), admin()
    show = run(evl, "scratch_show", {"device": PROBE_DEVICE, "command": "show clock"})
    assert show.get("status") == "complete" and (show.get("variables") or {}).get("raw"), f"show job {show['_id']}"
    code, job, _ = evl.start(ROUTE["scratch_restconf"], {"device": PROBE_DEVICE, "path": INTERFACES})
    assert code == 200 and job, f"svc-eval: {ROUTE['scratch_restconf']} HTTP {code}"
    print(f"svc-eval: {ROUTE['scratch_show']} answered (job {show['_id']}), {ROUTE['scratch_restconf']} started (job {job})")
    for who, p, route, body in (
            ("svc-eval", evl, ROUTE["scratch_restore"], {"device": PROBE_DEVICE}),
            ("svc-eval", evl, OM["show_command"]["endpoint"]["route"], {"device": "dc1-spine01", "command": "show clock"}),
            ("svc-eval-approver", appr, ROUTE["scratch_show"], {"device": PROBE_DEVICE, "command": "show clock"}),
            ("svc-eval-approver", appr, ROUTE["scratch_push"], {"device": PROBE_DEVICE, "lines": ["interface Loopback97"],
                                                                "reason": "gbac probe"})):
        code, job, _ = p.start(route, body)
        assert code in (401, 403) and not job, f"{who}: {route} HTTP {code} (expected a refusal)"
        print(f"refused: {who} on {route} (HTTP {code})")
    code, _ = evl.req("POST", "/operations-manager/jobs/start",
                      {"workflow": WF["scratch_show"], "options": {"type": "automation", "variables": {}}})
    assert code in (401, 403), f"svc-eval started a workflow directly: HTTP {code}"
    print(f"refused: svc-eval starting {WF['scratch_show']} directly (HTTP {code})")
    names = {g["_id"]: g["name"] for g in adm.req("GET", "/authorization/groups?limit=100")[1]["results"]}
    autos = {a["name"]: a for a in adm.req("GET", "/operations-manager/automations?limit=200")[1]["data"]}
    for key in SKILLS["workflows"]:
        a = autos[OM[key]["automation"]]
        read = sorted(names.get(i, i) for i in (a.get("gbac") or {}).get("read", []))
        group = ACCESS["approve" if key == "scratch_restore" else "run"]["group"]
        assert read == sorted(["admin_group", group]), f"{a['name']}: read {read}"
    print("each scratch automation reads admin_group and its one eval group, nothing else")


def forbidden() -> None:
    evl, adm = runner(), admin()
    for lines in (["write memory"], ["do copy running-config startup-config"], ["int gi1", " shutdown"]):
        d = propose(evl, lines, "forbidden probe")
        v = d.get("variables") or {}
        assert d.get("status") == "complete" and not d["_waiting"], f"job {d['_id']}: {d.get('status')} {d['_waiting']}"
        assert str(v.get("error", "")).startswith("refused before any approval") and v.get("changed") is False, v
        assert card_id(adm, d["_id"]) is None, f"job {d['_id']}: a card exists"
        print(f"job {d['_id']}: {lines} refused before any card ({v['error'][:60]}...)")


def restconf() -> None:
    evl, adm = runner(), admin()
    d = run(evl, "scratch_restconf", {"device": PROBE_DEVICE, "path": INTERFACES})
    data = (d.get("variables") or {}).get("data") or {}
    names = [i.get("name") for i in (data.get("Cisco-IOS-XE-interfaces-oper:interfaces") or {}).get("interface", [])]
    assert d.get("status") == "complete" and "GigabitEthernet1" in names, f"job {d['_id']}: {names}"
    print(f"job {d['_id']}: {INTERFACES} on {PROBE_DEVICE} returned JSON ({len(names)} interfaces)")
    before = evl.count(WF["scratch_restconf"])
    code, job, _ = evl.start(ROUTE["scratch_restconf"],
                             {"device": PROBE_DEVICE, "path": "/restconf/data/Cisco-IOS-XE-native:native/username"})
    assert code == 400 and not job and evl.count(WF["scratch_restconf"]) == before, f"the username path: HTTP {code}"
    print("refused before any job: the username path (HTTP 400)")
    record = json.dumps(adm.req("GET", f"/operations-manager/jobs/{d['_id']}")[1])
    assert os.environ["SKILLS_LAB_READER_PASSWORD"] not in record, f"job {d['_id']} holds the reader's password"
    print(f"job {d['_id']}: the record holds no password")


def hold() -> None:
    evl, appr, adm = runner(), approver(), admin()
    assert judge(PROBE_DEVICE, loopback(99)) == 404, "Loopback99 is already on skl-r1: run the restore check first"
    d = propose(evl, ["interface Loopback99", " description skills-lab-verify"], "hold probe")
    assert d["_waiting"] == ["2c"], f"job {d['_id']}: {d.get('status')}, waiting {d['_waiting']}"
    item = card_id(adm, d["_id"])
    code, _ = evl.req("PATCH", f"/work-center-service/work-items/{item}/claim", {})
    assert code in (401, 403), f"svc-eval claimed the card: HTTP {code}"
    assert judge(PROBE_DEVICE, loopback(99)) == 404, "the router changed while the job was held"
    print(f"job {d['_id']}: held on its card; svc-eval's claim refused (HTTP {code}); the router unchanged")
    decide(adm, appr, d["_id"], approve=True)
    end = evl.wait(d["_id"], 300)
    assert end.get("status") == "complete" and (end.get("variables") or {}).get("changed") is True, end.get("status")
    assert judge(PROBE_DEVICE, loopback(99)) == 200, "released, but Loopback99 is not on the router"
    print(f"job {d['_id']}: released by svc-eval-approver; Loopback99 on the router (read as judge)")
    d = propose(evl, ["interface Loopback98", " description skills-lab-verify"], "hold probe, rejected")
    assert d["_waiting"] == ["2c"], f"job {d['_id']}: waiting {d['_waiting']}"
    decide(adm, appr, d["_id"], approve=False)
    end = evl.wait(d["_id"], 300)
    v = end.get("variables") or {}
    assert end.get("status") == "complete" and v.get("changed") is False and "rejected" in str(v.get("outcome")), v
    assert judge(PROBE_DEVICE, loopback(98)) == 404, "rejected, but Loopback98 is on the router"
    print(f"job {d['_id']}: rejected; Loopback98 never reached the router")
    r = restore(appr)
    assert judge(PROBE_DEVICE, loopback(99)) == 404, "restored, but Loopback99 is still on the router"
    print(f"restore job {r['_id']}: skl-r1 back to its baseline")


def restore_check() -> None:
    evl, appr, adm = runner(), approver(), admin()
    d = propose(evl, ["interface Loopback96", " description skills-lab-verify"], "restore probe")
    decide(adm, appr, d["_id"], approve=True)
    assert evl.wait(d["_id"], 300).get("status") == "complete" and judge(PROBE_DEVICE, loopback(96)) == 200
    r = restore(appr)
    v = r["variables"]
    assert judge(PROBE_DEVICE, loopback(96)) == 404, "restored, but the probe change is still on the router"
    print(f"restore job {r['_id']}: restored, baseline {v['baseline_sha256'][:12]} = running {v['running_sha256'][:12]}; "
          "the probe change is gone (read as judge)")


def agents(claude: bool) -> None:
    evl, adm = runner(), admin()
    tools = {WF["scratch_show"], WF["scratch_restconf"], WF["scratch_push"]}
    operable = adm.req("GET", "/agent-project-service/operable-agents")[1]["data"]["items"]
    for item in operable:
        assert "a probe of the lab's governed path" not in json.dumps(item), f"{item['name']} holds the probe text"
    # the runner looks the eval agents up itself, as the workbench does
    code, mine = evl.req("GET", "/agent-project-service/operable-agents")
    assert code == 200, f"svc-eval cannot list the agents it operates: HTTP {code}"
    ids = {i["name"]: i["_id"] for i in mine["data"]["items"]}
    # scoped by the agents' operators: exactly the eval agents (measured on production, 2026-10-25)
    assert set(ids) == set(SKILLS["agents"]), f"svc-eval operates {sorted(ids)}, not exactly the eval agents"
    print(f"svc-eval operates exactly the eval agents: {', '.join(sorted(ids))}")
    for name in SKILLS["agents"] if claude else [n for n in SKILLS["agents"] if n.endswith("-local")]:
        code, out = evl.req("POST", "/agent-session-manager/sessions",
                            {"agentDefinitionId": ids[name], "inputs": {"request": PROBE_PROCEDURE}})
        sid = out.get("sessionId") if isinstance(out, dict) else None
        assert code in (200, 201) and sid, f"{name}: HTTP {code} {str(out)[:200]}"
        status = session_end(evl, sid)
        assert status.upper().startswith("COMPLETE"), f"{name} session {sid}: {status}"
        msgs = evl.req("GET", f"/agent-session-manager/sessions/{sid}/messages?limit=500&sortBy=eventId&sortOrder=asc")[1]
        msgs = (msgs.get("data") or msgs.get("items") or []) if isinstance(msgs, dict) else msgs
        called = {m["data"].get("toolName", "") for m in msgs if m.get("category") == "TOOL_CALLED"}
        # the eventIds are UUIDs, so the server's eventId order is not the session's (measured 2026-10-23): time order
        texts = [m["text"] for m in sorted(msgs, key=lambda m: m.get("timestamp", 0))
                 if m.get("type") == "inference-succeeded" and m.get("text")]
        assert called and called <= tools, f"{name}: tools {called}"
        assert texts and "DIAGNOSIS: 4" in texts[-1], f"{name}: {texts[-1][-200:] if texts else 'no answer'}"
        print(f"{name} session {sid}: tools {sorted(called)}; DIAGNOSIS: 4")
        # the runner's last step: the record keeps the request, so the scored session is deleted as svc-eval
        code, out = evl.req("DELETE", f"/agent-session-manager/sessions/{sid}")
        assert code in (200, 204), f"svc-eval could not delete session {sid}: HTTP {code} {str(out)[:200]}"
        assert gone(adm, sid), f"session {sid}: deleted, but the administrator still reads it"
        print(f"{name} session {sid}: deleted by svc-eval; the administrator reads neither it nor its messages")
    reach(evl, adm, ids["skill-eval-local"])


def session_end(p: Platform, sid: str, seconds: int = 900) -> str:
    deadline, status = time.time() + seconds, ""
    while time.time() < deadline:
        doc = p.req("GET", f"/agent-session-manager/sessions/{sid}")[1]
        status = str(((doc.get("data") or doc) if isinstance(doc, dict) else {}).get("status") or "")
        if status.upper() != "RUNNING":
            break
        time.sleep(5)
    return status


def gone(adm: Platform, sid: str) -> bool:
    """The session is gone for the administrator: its record answers 404 or empty, and it has no messages. Any other
    answer (an expired login, a server error) is not an absence: the probe stops rather than record a wrong reach."""
    code, doc = adm.req("GET", f"/agent-session-manager/sessions/{sid}")
    assert code in (200, 404), f"session {sid}: the administrator's read answered HTTP {code}"
    record = (doc["data"] if isinstance(doc, dict) and "data" in doc else doc) if code == 200 else None
    code, msgs = adm.req("GET", f"/agent-session-manager/sessions/{sid}/messages?limit=5")
    assert code in (200, 404), f"session {sid} messages: the administrator's read answered HTTP {code}"
    msgs = ((msgs.get("data") or msgs.get("items") or []) if isinstance(msgs, dict) else msgs) if code == 200 else []
    return not record and not msgs


def reach(evl: Platform, adm: Platform, agent_id: str) -> None:
    """How far svc-eval's deleteSession reaches: a measurement, recorded in ADR 0084 decision 6. The session is a
    throwaway the administrator starts on the local twin; whichever way it goes, the probe removes it."""
    code, out = adm.req("POST", "/agent-session-manager/sessions",
                        {"agentDefinitionId": agent_id, "inputs": {"request": "Reply with the single word OK. Call no tool."}})
    sid = out.get("sessionId") if isinstance(out, dict) else None
    assert code in (200, 201) and sid, f"the administrator's throwaway session: HTTP {code} {str(out)[:200]}"
    session_end(adm, sid)
    code, _ = evl.req("DELETE", f"/agent-session-manager/sessions/{sid}")
    if gone(adm, sid):
        print(f"measured: svc-eval deleted a session the administrator started ({sid}, HTTP {code}): the delete "
              "reaches every session")
        return
    adm.req("DELETE", f"/agent-session-manager/sessions/{sid}")
    assert gone(adm, sid), f"throwaway session {sid} is still there: delete it by hand"
    print(f"measured: svc-eval could not delete a session the administrator started ({sid}, HTTP {code}); "
          "the administrator removed it")


def project() -> None:
    adm = admin()
    projects = adm.req("GET", "/agent-project-service/projects")[1]["data"]["items"]
    ours = [pj for pj in projects if pj["name"] == SKILLS["project"]]
    assert len(ours) == 1, f"{SKILLS['project']}: {len(ours)} project(s)"
    groups = [m for m in ours[0].get("members", []) if m.get("type") == "group"]
    assert not groups, f"{SKILLS['project']}: group members {groups}"
    ids = {i["name"]: i["_id"] for i in adm.req("GET", "/agent-project-service/operable-agents")[1]["data"]["items"]}
    pair = {ids[name] for name in SKILLS["agents"]}
    held = {c["reference"] for c in ours[0].get("components", []) if c.get("type") == "agent"}
    assert held == pair, f"{SKILLS['project']} holds {held}, not exactly {sorted(SKILLS['agents'])}"
    elsewhere = [pj["name"] for pj in projects if pj is not ours[0]
                 and pair & {c["reference"] for c in pj.get("components", [])}]
    assert not elsewhere, f"the pair is also in {elsewhere}"
    print(f"{SKILLS['project']}: exactly {SKILLS['agents']}, no group among its members, the pair in no other project")


def profile() -> None:
    adm = admin()
    want = next(p for p in VERSIONS["llm"]["profiles"] if p["name"] == "anthropic-sonnet-5-5")
    profiles = adm.req("GET", "/model-registry-service/profiles")[1]
    found = [p for p in (profiles.get("profiles") or profiles.get("data") or []) if p.get("name") == want["name"]]
    assert len(found) == 1, f"{want['name']}: {len(found)} profile(s)"
    detail = adm.req("GET", f"/model-registry-service/profiles/{found[0]['id']}")[1]
    model = next((m for m in detail.get("models") or [] if m.get("name") == want["model"]), None)
    assert model, f"{want['name']} does not list {want['model']}"
    # without them the Platform sends thinking disabled, which claude-sonnet-5-5 refuses (measured 2026-10-24)
    assert model.get("modelVariables") == want["model_variables"], f"{want['name']}: {model.get('modelVariables')}"
    operable = {i["name"]: i for i in adm.req("GET", "/agent-project-service/operable-agents")[1]["data"]["items"]}
    documents = {yaml.safe_load(p.read_text())["name"] for p in (ROOT / "itential" / "agents").glob("*.yaml")}
    assert documents <= set(operable), f"not operable: {sorted(documents - set(operable))}"
    assert found[0]["id"] in json.dumps(operable["skill-eval"]), "skill-eval is not bound to the new profile"
    print(f"{want['name']} lists {want['model']} with {want['model_variables']} and serves skill-eval; "
          f"all {len(documents)} agent documents operable")


CHECKS = {"accounts": accounts, "inventory": inventory, "gbac": gbac, "forbidden": forbidden, "restconf": restconf,
          "hold": hold, "restore": restore_check, "profile": profile, "project": project}

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("check", choices=sorted([*CHECKS, "agents", "all"]))
    ap.add_argument("--tier", choices=["prod"], required=True, help="production only: the scratch lab is production's")
    ap.add_argument("--claude", action="store_true", help="agents: skill-eval too (Anthropic tokens), not only the twin")
    a = ap.parse_args()
    try:
        if a.check == "agents":
            agents(a.claude)
        else:
            for name in (CHECKS if a.check == "all" else [a.check]):
                print(f"--- {name}")
                CHECKS[name]()
    except AssertionError as exc:
        sys.exit(f"{a.check}: {exc}")
