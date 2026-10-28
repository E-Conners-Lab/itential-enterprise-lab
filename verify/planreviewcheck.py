#!/usr/bin/env python3
"""S4d.5r (A2, ADR 0085): plan-reviewer-local on a fixed plan, held to it by the workflow's own code.

    planreviewcheck.py request        # the session's inputs: the request Deploy would hand the reviewer for PLAN
    planreviewcheck.py judge < text   # the session's last answer, through build.plan_review as Deploy runs it

The plan is small and fixed: one replacement, one destroy pair, one create and one IAM update, NAT off. The judge passes
only when the review is available, no line was left out, and the computed estimate is repeated.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("wf_build_review", ROOT / "itential" / "workflows" / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)

PLAN = {"action": "plan", "job": "verify", "add": 2, "change": 1, "destroy": 3, "review_withheld": "", "review": [
    {"address": "module.vpn[0].aws_instance.strongswan", "type": "aws_instance", "actions": ["delete", "create"],
     "replace": True, "replace_attributes": ["ami"], "changed_attributes": [],
     "facts": {"instance_type": "t3.micro", "associate_public_ip_address": True}},
    {"address": "module.vpc.aws_nat_gateway.main[0]", "type": "aws_nat_gateway", "actions": ["delete"], "replace": False,
     "replace_attributes": [], "changed_attributes": [], "facts": {"connectivity_type": "public"}},
    {"address": "module.vpc.aws_eip.nat[0]", "type": "aws_eip", "actions": ["delete"], "replace": False,
     "replace_attributes": [], "changed_attributes": [], "facts": {"domain": "vpc"}},
    {"address": "module.vpn[0].aws_vpc_security_group_ingress_rule.natt", "type": "aws_vpc_security_group_ingress_rule",
     "actions": ["create"], "replace": False, "replace_attributes": [], "changed_attributes": [],
     "facts": {"ip_protocol": "udp", "from_port": 4500, "to_port": 4500, "cidr_ipv4": "the on-prem public address"}},
    {"address": "module.vpn_monitor[0].aws_iam_role_policy.lambda_inline", "type": "aws_iam_role_policy",
     "actions": ["update"], "replace": False, "replace_attributes": [], "changed_attributes": ["policy"],
     "facts": {"name": "cloud-devops-vpn-monitor-inline"}},
]}


def facts() -> dict:
    return build.plan_facts({"plan": PLAN, "nat": "false", "prices": build.PLAN_PRICES})


if __name__ == "__main__":
    f = facts()
    if sys.argv[1:] == ["request"]:
        print(json.dumps({"request": f["request"]}))
    elif sys.argv[1:] == ["judge"]:
        agent = {"sessionStatus": "COMPLETE", "lastMessage": sys.stdin.read(), "sessionId": "verify"}
        out = build.plan_review({"facts": {"stdout_json": f}, "agent": agent})
        cost = f["cost"].split("$")[1].split()[0]  # "0.40" of "about $0.40 a day"
        said = " ".join(out["review"])
        print(json.dumps({"available": out["available"], "why": out["why"], "dropped": out["dropped"],
                          "lines": len(out["review"]), "cost_repeated": cost in said, "first": out["review"][:1]}))
        sys.exit(0 if out["available"] and not out["dropped"] and cost in said else 1)
    else:
        sys.exit(__doc__)
