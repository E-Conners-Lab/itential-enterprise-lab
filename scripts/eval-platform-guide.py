#!/usr/bin/env python3
"""The eval set for platform-guide (ADR 0081): the Platform explains its own workflows in conversation, through one
read-only tool (Describe for an Agent). Every case is a request an engineer would type, with the facts the answer must
carry taken from the committed documents (the same documents the Platform holds) and the tool call it must make; the
no-such-workflow case must be answered as such, with nothing invented. Each agent (platform-guide on Claude, the local
twin) runs every case; the score is facts carried, tool discipline and no refusal.

Run from the lab repo root with .env present (ITENTIAL_ADMIN_USER / ITENTIAL_ADMIN_PASSWORD). Reads the Platform and
starts agent sessions; writes verify/results/<UTC>-platform-guide-eval.json and nothing else.

usage:
  scripts/eval-platform-guide.py [--platform https://itential.lab.internal] [--agents platform-guide,platform-guide-local]
                                 [--cases 1,3,6] [--timeout 600]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / "itential" / "workflows"
_SPEC = importlib.util.spec_from_file_location("wf_describe_eval", WORKFLOWS / "describe.py")
describe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(describe)
TOOL = "Describe for an Agent"
BGP = "Diagnose Fabric BGP Outage"


def env() -> dict:
    out = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            out[k.strip()] = v.split(" #", 1)[0].strip().strip('"').strip("'")
    return out


def document(name: str) -> dict:
    return json.loads((WORKFLOWS / describe.file_name(name)).read_text())


def manual_steps(doc: dict) -> list:
    """The short names of the steps that hold a manual task: where the workflow waits for a person."""
    return [describe.short(title) for title, ids in describe.steps(doc)
            if any(doc["tasks"][i].get("type") == "manual" for i in ids)]


class Platform:
    def __init__(self, url: str, user: str, password: str) -> None:
        self.url, self.cookie = url.rstrip("/"), ""
        self.ctx = ssl.create_default_context(cafile=str(ROOT / "docs" / "lab-root-ca.crt"))
        status, _ = self.call("POST", "/login", {"username": user, "password": password})
        if status != 200:
            raise SystemExit(f"login answered {status}")

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        data = json.dumps(body).encode() if body is not None else None
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.cookie:
            h["Cookie"] = self.cookie
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=120, context=self.ctx) as r:
                cs = r.headers.get_all("Set-Cookie") or []
                if cs:
                    self.cookie = "; ".join(c.split(";", 1)[0] for c in cs)
                text = r.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode(errors="replace")[:400]
        try:
            return 200, json.loads(text)
        except ValueError:
            return 200, text

    def get(self, path: str) -> object:
        return self.call("GET", path)[1]

    def agent_id(self, name: str) -> str:
        for a in self.get("/agent-project-service/operable-agents")["data"]["items"]:
            if a.get("name") == name:
                return a["_id"]
        raise SystemExit(f"agent {name} not found")

    def run_agent(self, name: str, request: str, timeout_s: int) -> tuple[str, str, float]:
        """(session id, terminal state, seconds)."""
        status, r = self.call("POST", "/agent-session-manager/sessions",
                              {"agentDefinitionId": self.agent_id(name), "inputs": {"request": request}})
        sid = r.get("sessionId") if isinstance(r, dict) else ""
        if status != 200 or not sid:
            raise SystemExit(f"session start failed for {name}: {status} {r}")
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            state = (self.get(f"/agent-session-manager/sessions/{sid}").get("status") or "").lower()
            if state in ("complete", "completed", "failed", "error", "canceled", "cancelled"):
                return sid, state, time.time() - t0
            time.sleep(4)
        return sid, "timeout", time.time() - t0

    def session(self, sid: str) -> tuple[str, list, dict]:
        """(the final text, the tool calls [(name, inputs)], token usage)."""
        msgs = self.get(f"/agent-session-manager/sessions/{sid}/messages?limit=500&sortBy=eventId&sortOrder=asc")
        if isinstance(msgs, dict):
            msgs = msgs.get("data") or msgs.get("items") or []
        texts = sorted([m for m in msgs if m.get("type") == "inference-succeeded" and m.get("text")],
                       key=lambda m: m.get("timestamp", 0))
        tools = [(m["data"].get("toolName", ""), m["data"].get("toolInput") or m["data"].get("input") or m["data"].get("arguments") or {})
                 for m in msgs if m.get("category") == "TOOL_CALLED"]
        usage = [m["data"]["tokenUsage"] for m in msgs if m.get("type") == "inference-succeeded"
                 and (m.get("data") or {}).get("tokenUsage")]
        return (texts[-1]["text"] if texts else ""), tools, {
            "in": sum(u.get("inputTokens", 0) for u in usage), "out": sum(u.get("outputTokens", 0) for u in usage)}

    def latest_job(self, name: str) -> dict | None:
        q = urllib.parse.quote(name)
        d = self.get(f"/operations-manager/jobs?limit=200&skip=0")
        items = (d.get("data") if isinstance(d, dict) else d) or []
        mine = [j for j in items if j.get("name") == name and j.get("status") == "complete"]
        mine.sort(key=lambda j: (j.get("metrics") or {}).get("start_time") or 0, reverse=True)
        return mine[0] if mine else None

    def job(self, job_id: str) -> dict:
        return self.get(f"/operations-manager/jobs/{job_id}")["data"]


def cases(p: Platform) -> list:
    """Each case: request, the tool's what the agent must use (any-of), facts (every one a substring of the answer,
    case-insensitive; a list inside = any-of), none (substrings that must not appear)."""
    bgp, rotate, vlan = document(BGP), document("Rotate AWS VPN Key"), document("Add Branch VLAN")
    docs = [document(n) for n in describe.guided(describe.VERSIONS if hasattr(describe, "VERSIONS") else _versions())]
    listing = describe.describe_list({"documents": docs})
    stoppers = sorted(r["name"] for r in listing["workflows"] if r["manual"])
    out = [
        {"n": 1, "request": f"What does {BGP} do?", "what": ["workflow"],
         "facts": [str(len(bgp["tasks"]) - 2), str(len(describe.steps(bgp))), ["person", "approv", "Work Center"]], "none": []},
        {"n": 2, "request": "Which workflows on this Platform stop for a person, and how many workflows are there in all?",
         "what": ["list"], "facts": stoppers[:3], "none": []},
        {"n": 3, "request": "What does the BGP outage workflow do?", "what": ["workflow"],
         "facts": [str(len(bgp["tasks"]) - 2), BGP], "none": []},
        {"n": 4, "request": "Where does Rotate AWS VPN Key wait for a person?", "what": ["workflow"],
         "facts": [[s.split(". ", 1)[-1] for s in manual_steps(rotate)][0] if manual_steps(rotate) else "person"], "none": []},
        {"n": 5, "request": "What inputs does Add Branch VLAN take?", "what": ["workflow"],
         "facts": sorted(vlan["inputSchema"]["properties"])[:3], "none": []},
        {"n": 6, "request": "What does Launch Rockets do?", "what": ["workflow", "list"],
         # every honest way of saying it is not there (the twin said "does not include ... isn't there", 2026-10-11)
         "facts": [["no workflow", "not on this Platform", "no such", "does not exist", "doesn't exist", "could not find",
                    "couldn't find", "not find", "isn't a workflow", "is not a workflow", "not listed", "no match",
                    "does not include", "doesn't include", "not include", "isn't there", "is not there", "not there",
                    "cannot describe", "can't describe", "nothing called", "no entry", "not among"]],
         "none": ["step 1", "Step 1"]},
        {"n": 8, "request": "Does Push Configuration with Approval change devices, and what stops it?", "what": ["workflow"],
         "facts": [["Work Center", "approv"], ["push", "send", "change"]], "none": []},
    ]
    job = p.latest_job(BGP) or p.latest_job("Describe Workflow")
    if job:
        record = p.job(job["_id"])
        name = record["name"]
        stored = document(name) if (WORKFLOWS / describe.file_name(name)).exists() else None
        d = describe.describe_job_document({"job": record, "document": stored, "now": time.time() * 1000})
        out.append({"n": 7, "request": f"What did job {job['_id']} do?", "what": ["job"],
                    "facts": [name, d["status"], str(d["ran_steps"])], "none": [], "job": job["_id"]})
    return sorted(out, key=lambda c: c["n"])


def _versions() -> dict:
    import yaml

    return yaml.safe_load((WORKFLOWS.parent / "versions.yaml").read_text())


def has(text: str, fact) -> bool:
    low = text.lower()
    if isinstance(fact, list):
        return any(str(f).lower() in low for f in fact)
    return str(fact).lower() in low


def score(case: dict, text: str, tools: list, state: str) -> dict:
    names = [n for n, _ in tools]
    whats = [str((i or {}).get("what", "")).lower() for n, i in tools if n == TOOL]
    tool_ok = bool(names) and all(n == TOOL for n in names) and any(w in case["what"] for w in whats) and len(names) <= 3
    missing = [f for f in case["facts"] if not has(text, f)]
    forbidden = [f for f in case["none"] if has(text, f)]
    answered = state in ("complete", "completed") and len(text.strip()) > 20
    return {"tool_ok": tool_ok, "tools": [(n, i) for n, i in tools], "missing": missing, "forbidden": forbidden,
            "answered": answered, "pass": tool_ok and not missing and not forbidden and answered}


def rescore(path: Path, all_cases: list) -> int:
    """The stored answers judged again with the current case definitions (a judge made stricter or wider); a job case
    whose id has moved on keeps its stored verdict. Writes <file>-rescored.json beside the original."""
    stored = json.loads(path.read_text())
    by_n = {c["n"]: c for c in all_cases}
    for agent, block in stored["agents"].items():
        print(f"\n== {agent} (rescored)")
        for row in block["cases"]:
            case = by_n.get(row["n"])
            if case is None or (case.get("job") and case["job"] not in row["request"]):
                print(f"{'PASS' if row['pass'] else 'FAIL'}  case {row['n']}: kept (no current definition for it)")
                continue
            s = score(case, row["answer"], [tuple(t) for t in row["tools"]], row["state"])
            row.update(s)
            print(f"{'PASS' if s['pass'] else 'FAIL'}  case {row['n']}: {row['request'][:60]!r}"
                  + ("" if s["pass"] else f" (missing {s['missing']} forbidden {s['forbidden']})"))
        block["passed"] = sum(1 for r in block["cases"] if r["pass"])
        print(f"{agent}: {block['passed']}/{block['of']}")
    out = path.with_name(path.stem + "-rescored.json")
    out.write_text(json.dumps(stored, indent=1))
    print(f"written {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")
    return 0 if all(a["passed"] == a["of"] for a in stored["agents"].values()) else 1


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--platform", default="https://itential.lab.internal")
    ap.add_argument("--agents", default="platform-guide,platform-guide-local")
    ap.add_argument("--cases", default="", help="comma-separated case numbers (default: all)")
    ap.add_argument("--timeout", type=int, default=600, help="seconds per session")
    ap.add_argument("--rescore", default="", help="re-judge the answers stored in this results file (no new sessions)")
    args = ap.parse_args(argv)
    e = env()
    p = Platform(args.platform, e.get("ITENTIAL_ADMIN_USER", "admin@itential"), e["ITENTIAL_ADMIN_PASSWORD"])
    wanted = {int(x) for x in args.cases.split(",") if x.strip()} if args.cases else None
    all_cases = [c for c in cases(p) if wanted is None or c["n"] in wanted]
    if args.rescore:
        return rescore(Path(args.rescore), all_cases)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    results = {"platform": args.platform, "when": stamp, "tool": TOOL, "agents": {}}
    for agent in [a.strip() for a in args.agents.split(",") if a.strip()]:
        rows = []
        print(f"\n== {agent}")
        for c in all_cases:
            sid, state, secs = p.run_agent(agent, c["request"], args.timeout)
            text, tools, usage = p.session(sid)
            s = score(c, text, tools, state)
            rows.append({"n": c["n"], "request": c["request"], "session": sid, "state": state, "seconds": round(secs, 1),
                         "usage": usage, "answer": text, **s})
            mark = "PASS" if s["pass"] else "FAIL"
            why = "" if s["pass"] else " (" + "; ".join(x for x in [
                "" if s["tool_ok"] else "tools " + str([n for n, _ in tools]),
                ("missing " + str(s["missing"])) if s["missing"] else "",
                ("forbidden " + str(s["forbidden"])) if s["forbidden"] else "",
                "" if s["answered"] else f"state {state}"] if x) + ")"
            print(f"{mark}  case {c['n']}: {c['request'][:60]!r} {secs:.0f}s tokens in={usage['in']} out={usage['out']}{why}")
        passed = sum(1 for r in rows if r["pass"])
        results["agents"][agent] = {"passed": passed, "of": len(rows), "cases": rows}
        print(f"{agent}: {passed}/{len(rows)}")
    out = ROOT / "verify" / "results" / f"{stamp}-platform-guide-eval.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, indent=1))
    print(f"\nwritten {out.relative_to(ROOT)}")
    return 0 if all(a["passed"] == a["of"] for a in results["agents"].values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
