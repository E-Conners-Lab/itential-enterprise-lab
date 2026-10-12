#!/usr/bin/env python3
"""ADR 0082 verify (S17): NetClaw's Platform contract, re-run without Slack and without NetClaw.

Every check acts as svc-netclaw, NetClaw's own service account, against the routes NetClaw's tools post to, and reads the
outcome independently (the administrator login, NetBox, the topology). The NetClaw side - the model, its tool list, the
refusal - is the delivery's eval (itential-deliveries/netclaw/eval), which needs the container.

  verify/netclawcheck.py <check> --tier prod|dev
    account   S17.1 svc-netclaw logs in and its session holds exactly the netclaw group
    gbac      S17.2 every automation but the department's carries the gbac versions.yaml names
    refusals  S17.3 another automation, a direct start, MOP, Configuration Manager, Studio and the card search refuse it;
              the show gate refuses a non-show command before a job exists
    guide     S17.4 (a) platform-guide's twin, started through its route, answers with the guide's counts
    show      S17.5 (b) one show job; on production the parsed BGP neighbors equal the topology's for dc1-spine01
    describe  S17.6 (c) the newest drill job described, its card found by svc-netclaw, then closed (Seen)
    vlan      S17.7 (d) one Add Branch VLAN job stops on its card; rejected; rolled back; NetBox holds no such VLAN

Reads .env (ITENTIAL_ADMIN_*, SVC_NETCLAW_<TIER>_PASSWORD, NETBOX_URL/NETBOX_TOKEN). Prints no secret.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "topology"))
VERSIONS = yaml.safe_load((ROOT / "itential" / "versions.yaml").read_text())
CTX = ssl.create_default_context(cafile=str(ROOT / "docs" / "lab-root-ca.crt"))
DEPARTMENT = set(VERSIONS["automation_access"]["unmanaged"])
OM = VERSIONS["operations_manager"]


def url_for(tier: str) -> str:
    return "https://itential.lab.internal" if tier == "prod" else f"https://{VERSIONS['vm']['name']}.lab.internal"


class Platform:
    def __init__(self, base: str, user: str, password: str) -> None:
        self.base = base
        code, tok = self.req("POST", "/login", {"username": user, "password": password})
        if code != 200:
            raise SystemExit(f"login {user}: HTTP {code}")
        self.token = tok

    def req(self, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        sep = "&" if "?" in path else "?"
        url = self.base + path + (f"{sep}token={self.token}" if getattr(self, "token", None) else "")
        r = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"Content-Type": "application/json", "User-Agent": "lab-verify/1.0"})
        try:
            with urllib.request.urlopen(r, timeout=120, context=CTX) as resp:
                raw = resp.read().decode()
                try:
                    return resp.status, json.loads(raw)
                except json.JSONDecodeError:
                    return resp.status, raw.strip().strip('"')
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode(errors="replace")[:300]

    def start(self, route: str, body: dict) -> tuple[int, str | None, object]:
        code, out = self.req("POST", f"/operations-manager/triggers/endpoint/{route}", body)
        data = (out.get("data") or {}) if isinstance(out, dict) else {}
        return code, data.get("_id") or data.get("sessionId"), out

    def job(self, job_id: str) -> dict:
        return self.req("GET", f"/operations-manager/jobs/{job_id}")[1]["data"]

    def wait(self, job_id: str, seconds: int = 300) -> dict:
        """Until the job ends or stops on a manual task; the doc gets `_waiting` = the running manual task ids."""
        deadline = time.time() + seconds
        while True:
            d = self.job(job_id)
            waiting = [k for k, t in (d.get("tasks") or {}).items() if t.get("type") == "manual" and t.get("status") == "running"]
            if d.get("status") in ("complete", "error", "canceled") or waiting or time.time() > deadline:
                d["_waiting"] = waiting
                return d
            time.sleep(4)

    def count(self, workflow: str) -> int:
        q = urllib.parse.urlencode({"equals[name]": workflow, "limit": 1})
        return int((self.req("GET", f"/operations-manager/jobs?{q}")[1].get("metadata") or {}).get("total") or 0)

    def newest(self, workflow: str) -> str | None:
        q = urllib.parse.urlencode({"equals[name]": workflow, "limit": 1, "sort": "metrics.start_time", "order": -1})
        rows = self.req("GET", f"/operations-manager/jobs?{q}")[1].get("data") or []
        return rows[0]["_id"] if rows else None


def logins(tier: str) -> tuple[Platform, Platform]:
    base = url_for(tier)
    ncl = Platform(base, VERSIONS["netclaw"]["user"], os.environ[VERSIONS["netclaw"]["password_env"][tier]])
    adm = Platform(base, os.environ["ITENTIAL_ADMIN_USER"], os.environ["ITENTIAL_ADMIN_PASSWORD"])
    return ncl, adm


def finish(adm: Platform, job: str, task: str, state: str, variables: dict | None = None) -> int:
    body = {"taskData": {"finish_state": state, "variables": variables or {}}}
    return adm.req("POST", f"/operations-manager/jobs/{job}/tasks/{task}/finish", body)[0]


def card_id(ncl: Platform, job: str) -> str | None:
    q = urllib.parse.urlencode({"rootExecutionId": job})
    code, items = ncl.req("GET", f"/work-center-service/work-items?{q}")
    pending = [i for i in (items.get("items") or []) if i.get("status") == "pending"] if isinstance(items, dict) else []
    return pending[0]["id"] if code == 200 and pending else None


# --- checks -------------------------------------------------------------------------------------------------------


def account(tier: str) -> None:
    ncl, _ = logins(tier)
    who = ncl.req("GET", "/whoami")[1]
    groups = sorted(g["name"] if isinstance(g, dict) else g for g in who.get("groups", []))
    assert groups == [VERSIONS["netclaw"]["group"]], f"groups {groups}"
    print(f"{VERSIONS['netclaw']['user']} logs in on {url_for(tier)}; its session holds exactly {groups}")


def gbac(tier: str) -> None:
    _, adm = logins(tier)
    names = {g["_id"]: g["name"] for g in adm.req("GET", "/authorization/groups?limit=100")[1]["results"]}
    access = VERSIONS["automation_access"]
    base, write = set(access["read"][tier]), set(access["write"])
    wrong, netclaw_seen = [], []
    autos = adm.req("GET", "/operations-manager/automations?limit=200")[1]["data"]
    for a in autos:
        if a["name"] in DEPARTMENT:
            continue
        g = a.get("gbac") or {}
        read = {names.get(i, i) for i in g.get("read", [])}
        want = base | ({VERSIONS["netclaw"]["group"]} if a["name"] in access["netclaw"] else set())
        if read != want or {names.get(i, i) for i in g.get("write", [])} != write:
            wrong.append((a["name"], sorted(read)))
        if VERSIONS["netclaw"]["group"] in read:
            netclaw_seen.append(a["name"])
    assert not wrong, f"gbac differs from versions.yaml on {wrong}"
    expected = set(access["netclaw"]) & {a["name"] for a in autos}
    assert set(netclaw_seen) == expected, f"netclaw reads {sorted(netclaw_seen)}"
    print(f"{len(autos) - len([a for a in autos if a['name'] in DEPARTMENT])} automations as versions.yaml says; "
          f"netclaw reads exactly {sorted(netclaw_seen)}")


def refusals(tier: str) -> None:
    ncl, _ = logins(tier)
    cases = [
        ("another automation's endpoint", "POST", f"/operations-manager/triggers/endpoint/{OM['ask_platform_guide']['endpoint']['route']}", {"request": "list"}),
        ("a direct workflow start", "POST", "/operations-manager/jobs/start",
         {"workflow": VERSIONS["workflows"]["show_command"], "options": {"type": "automation", "variables": {}}}),
        ("MOP run command", "POST", "/mop/RunCommandDevices", {"command": "show version", "devices": ["x"]}),
        ("Configuration Manager devices", "POST", "/configuration_manager/devices", {"options": {"limit": 1, "start": 0}}),
        ("Automation Studio workflows", "GET", "/automation-studio/workflows?limit=1", None),
        ("the Work Center search", "POST", "/work-center-service/work-items/search", {"filters": [], "limit": 1, "skip": 0}),
    ]
    for what, method, path, body in cases:
        code, _ = ncl.req(method, path, body)
        assert code in (401, 403), f"{what}: HTTP {code} (expected a refusal)"
        print(f"refused: {what} (HTTP {code})")
    before = ncl.count(VERSIONS["workflows"]["show_command"])
    code, job, _ = ncl.start(OM["show_command"]["endpoint"]["route"], {"device": "x", "command": "configure terminal"})
    assert code == 400 and not job, f"the show gate let 'configure terminal' through: HTTP {code}"
    assert ncl.count(VERSIONS["workflows"]["show_command"]) == before, "a job exists for a refused command"
    print("refused before any job: run-show-command 'configure terminal' (HTTP 400)")


def guide(tier: str) -> None:
    ncl, _ = logins(tier)
    row = next(line for line in (ROOT / "docs" / "workflows" / "README.md").read_text().splitlines()
               if line.startswith("| [Diagnose Fabric BGP Outage]"))
    steps, tasks, waits = [c.strip() for c in row.split("|")[2:5]]
    route = VERSIONS["netclaw"]["platform_guide_routes"]["twin"]
    question = ("How many steps and how many tasks does Diagnose Fabric BGP Outage have, and how many times does it "
                "stop for a person? Give the numbers.")
    code, sid, out = ncl.start(route, {"request": question})
    assert code == 200 and sid, f"{route}: HTTP {code} {str(out)[:200]}"
    deadline, status = time.time() + 600, ""
    while time.time() < deadline:
        doc = ncl.req("GET", f"/agent-session-manager/sessions/{sid}")[1]
        doc = (doc.get("data") or doc) if isinstance(doc, dict) else {}  # the session itself, or wrapped
        status = str(doc.get("status") or "")
        if status.upper() != "RUNNING":
            break
        time.sleep(5)
    assert status.upper().startswith("COMPLETE"), f"session {sid}: {status}"
    msgs = ncl.req("GET", f"/agent-session-manager/sessions/{sid}/messages?limit=500&sortBy=eventId&sortOrder=asc")[1]
    if isinstance(msgs, dict):
        msgs = msgs.get("data") or msgs.get("items") or []
    texts = sorted([m for m in msgs if m.get("type") == "inference-succeeded" and m.get("text")], key=lambda m: m.get("timestamp", 0))
    answer = (texts[-1]["text"] if texts else "").rsplit("</think>", 1)[-1]
    tools = {m["data"].get("toolName", "") for m in msgs if m.get("category") == "TOOL_CALLED"}
    assert tools <= {VERSIONS["workflows"]["describe_for_agent"]}, f"tools {tools}"
    missing = [n for n in (steps, tasks) if not re.search(rf"\b{n}\b", answer)]
    assert not missing, f"the answer lacks {missing}: {answer[:300]}"
    stops = "yes" if re.search(rf"\b{waits}\b|\btwice\b|\btwo\b", answer) else "not as a number"
    print(f"session {sid} ({route}): the answer carries {steps} steps and {tasks} tasks (the guide's {waits} stops "
          f"named: {stops}); tools {sorted(tools)}")


def show(tier: str) -> None:
    ncl, _ = logins(tier)
    wf = VERSIONS["workflows"]["show_command"]
    device, command = ("dc1-spine01", "show ip bgp summary") if tier == "prod" else ("clab-rtr1", "show version")
    before = ncl.count(wf)
    code, job, _ = ncl.start(OM["show_command"]["endpoint"]["route"], {"device": device, "command": command})
    assert code == 200 and job, f"HTTP {code}"
    d = ncl.wait(job)
    assert d.get("status") == "complete", f"job {job}: {d.get('status')}"
    assert ncl.count(wf) == before + 1, "not exactly one new job"
    v = d.get("variables") or {}
    if tier == "dev":
        raw = json.dumps(v.get("raw"))
        assert "Cisco IOS XE" in raw, "no version text from clab-rtr1"
        print(f"job {job}: {device} '{command}' answered (Cisco IOS XE); one job")
        return
    import derive  # topology/derive.py: the neighbors the device's own startup config carries

    want = {n["neighbor"] for n in derive.bgp_neighbors(derive.load_topology(), device)
            if n["afi"] == "ipv4" and not n.get("vrf")}
    parsed = json.dumps(v.get("parsed"))
    found = {ip for ip in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", parsed)
             if any(ipaddress.ip_address(ip) == ipaddress.ip_address(w) for w in want) or ip in want}
    assert v.get("parsed"), f"job {job}: nothing parsed ({v.get('parse_error')})"
    assert found == want, f"job {job}: neighbors {sorted(found)} vs the topology's {sorted(want)}"
    print(f"job {job}: {device} '{command}' parsed by {v.get('parser')}; neighbors {sorted(found)} = the topology's; one job")


def describe(tier: str) -> None:
    ncl, adm = logins(tier)
    target_wf = VERSIONS["workflows"]["diagnose_fabric_bgp_outage" if tier == "prod" else "show_command"]
    target = ncl.newest(target_wf)
    assert target, f"no {target_wf} job on this tier"
    code, job, _ = ncl.start(OM["describe_job"]["endpoint"]["route"], {"job": target})
    assert code == 200 and job, f"HTTP {code}"
    d = ncl.wait(job)
    assert d["_waiting"], f"job {job}: {d.get('status')}, no card"
    item = card_id(ncl, job)
    assert item, f"job {job}: svc-netclaw cannot find its card"
    print(f"newest {target_wf} job {target} described by job {job}; card /work-center/#/{item}; "
          f"outcome: {(d.get('variables') or {}).get('outcome', '')[:120]}")
    for t in d["_waiting"]:
        assert finish(adm, job, t, "success") == 200, "could not press Seen"
    assert adm.wait(job, 120).get("status") == "complete", "the job did not end after Seen"
    print(f"card closed (Seen); job {job} complete")


def vlan(tier: str) -> None:
    if tier != "prod":
        print("SKIP on dev: Add Branch VLAN needs the ServiceNow adapter, which dev does not run (the trigger answers 400)")
        return
    ncl, adm = logins(tier)
    wf = VERSIONS["workflows"]["branch_vlan"]
    name = f"netclaw-verify-{time.strftime('%H%M%S')}"
    before = ncl.count(wf)
    code, job, _ = ncl.start(OM["branch_vlan"]["endpoint"]["route"],
                             {"branch": "br1", "vlan_name": name, "switch_override": "", "change_request": False})
    assert code == 200 and job, f"HTTP {code}"
    d = ncl.wait(job, 300)
    assert d["_waiting"] == ["4a"], f"job {job}: {d.get('status')}, waiting {d['_waiting']} (expected the approval 4a)"
    assert ncl.count(wf) == before + 1, "not exactly one new job"
    assert card_id(ncl, job), "svc-netclaw cannot find the approval card"
    assert finish(adm, job, "4a", "failure", {"export": {"decision": "reject"}}) == 200, "could not reject"
    end = adm.wait(job, 300)
    rolled = (end.get("variables") or {}).get("rolled_back")
    assert end.get("status") in ("error", "complete") and rolled is True, f"job {job}: {end.get('status')}, rolled_back {rolled}"
    nb = urllib.request.Request(f"{os.environ['NETBOX_URL']}/api/ipam/vlans/?name={urllib.parse.quote(name)}",
                                headers={"Authorization": f"Token {os.environ['NETBOX_TOKEN']}", "Accept": "application/json",
                                         "User-Agent": "lab-verify/1.0"})
    count = json.load(urllib.request.urlopen(nb, timeout=30))["count"]
    assert count == 0, f"NetBox still holds {name}"
    print(f"job {job}: stopped on approval 4a, rejected, rolled back; NetBox holds no VLAN {name}; one job")


CHECKS = {"account": account, "gbac": gbac, "refusals": refusals, "guide": guide, "show": show, "describe": describe, "vlan": vlan}

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("check", choices=sorted(CHECKS))
    ap.add_argument("--tier", choices=["prod", "dev"], required=True)
    a = ap.parse_args()
    try:
        CHECKS[a.check](a.tier)
    except AssertionError as exc:
        sys.exit(f"{a.check}: {exc}")
