#!/usr/bin/env python3
"""S4d.5u (A5, ADR 0085): drift-cost-watch-local on a fixed drift day, held to it by the workflow's own code.

    watchcheck.py request        # the session's inputs: the request Check AWS Drift would hand the watch for DAY
    watchcheck.py judge < text   # the session's last answer, through build.watch_proposal as the workflow runs it

The day is small and fixed: one tag changed by hand on the strongSwan instance, a deployment with an end time about
3 hours away (not long-running), NAT off. The instructions make that a redeploy, so the judge passes only on a card
proposing redeploy whose own words were kept (no resource outside the drift, no address).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("wf_build_watch", ROOT / "itential" / "workflows" / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)

NOW = datetime(2026, 11, 3, 7, 0, tzinfo=timezone.utc)
DRIFT = {"drifted": 1, "resources_checked": 53, "resources": [
    {"address": "module.vpn[0].aws_instance.strongswan", "actions": ["update"], "attributes": ["tags", "tags_all"]}]}
OUTPUTS = {"result": {"return_code": 0, "stdout_json": {
    "outputs": {"expires_at": "2026-11-03T10:12:00Z", "strongswan_instance_type": "t3.micro",
                "nat_gateway_enabled": False},
    "state": {"read": True, "last_modified": "2026-11-02T22:00:00Z"}}}}


def gate() -> dict:
    drift = {"stdout_json": build.drift_summary({"drift": {"result": {"return_code": 0,
                                                                       "stdout_json": {"drift": DRIFT}}}})}
    return build.watch_gate({"drift": drift, "outputs": OUTPUTS}, NOW, build.WATCH_GATE_RULES)


if __name__ == "__main__":
    g = gate()
    if sys.argv[1:] == ["request"]:
        print(json.dumps({"request": g["request"]}))
    elif sys.argv[1:] == ["judge"]:
        agent = {"sessionStatus": "COMPLETE", "lastMessage": sys.stdin.read(), "sessionId": "verify"}
        out = build.watch_proposal({"gate": {"stdout_json": g}, "agent": agent, **build.WATCH_RULES})
        kept = out.get("route") == "card" and out.get("why") != g["reason"]
        print(json.dumps({"route": out.get("route"), "proposal": out.get("proposal"), "words_kept": kept,
                          "why": out.get("why") or out.get("reason")}))
        sys.exit(0 if out.get("route") == "card" and out.get("proposal") == "redeploy" and kept else 1)
    else:
        sys.exit(__doc__)
