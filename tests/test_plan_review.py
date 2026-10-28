"""A2 Plan Reviewer (ADR 0085, delivery aws-vpn-roadmap E.3): Deploy AWS VPN hands plan-reviewer the plan's changes and a
computed estimate, holds the one JSON line it answers to the plan, and puts the review on the card; a review that cannot
be made says "Review unavailable" and the card works as before. The functions are pure and runCode runs their own
source on the Gateway, so these tests test what runs."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("wf_build_review", ROOT / "itential" / "workflows" / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)
PRICES = build.PLAN_PRICES
AGENTS = ROOT / "itential" / "agents"
REVIEW_TASKS = ("60", "61", "62", "63", "64", "65", "66", "67", "68", "6d", "69", "6a", "6b", "6c", "6e")


def entry(address: str, rtype: str, actions: list, facts: dict | None = None, **more) -> dict:
    return {"address": address, "type": rtype, "actions": actions, "replace": "delete" in actions and "create" in actions,
            "replace_attributes": more.get("replace_attributes", []),
            "changed_attributes": more.get("changed_attributes", []), "facts": facts or {}}


INSTANCE = "module.vpn[0].aws_instance.strongswan"
NAT = "module.vpc.aws_nat_gateway.main[0]"
NAT_EIP = "module.vpc.aws_eip.nat[0]"
RULE = "module.vpn[0].aws_vpc_security_group_ingress_rule.ike"
ROLE = "module.vpn[0].aws_iam_role.strongswan"
FRESH = [
    entry(INSTANCE, "aws_instance", ["create"], {"instance_type": "t3.small", "associate_public_ip_address": True}),
    entry("module.vpn[0].aws_eip.strongswan", "aws_eip", ["create"], {"domain": "vpc"}),
    entry(RULE, "aws_vpc_security_group_ingress_rule", ["create"],
          {"ip_protocol": "udp", "from_port": 500, "to_port": 500, "cidr_ipv4": "the on-prem public address"}),
    entry(ROLE, "aws_iam_role", ["create"], {"name": "cloud-devops-vpn-strongswan"}),
]
CHURN = [
    entry(RULE, "aws_vpc_security_group_ingress_rule", ["create"], {"ip_protocol": "udp"}),
    entry(INSTANCE, "aws_instance", ["delete", "create"], {"instance_type": "t3.micro"}, replace_attributes=["ami"]),
    entry(NAT, "aws_nat_gateway", ["delete"], {"connectivity_type": "public"}),
    entry(ROLE, "aws_iam_role", ["update"], {"name": "x"}, changed_attributes=["tags"]),
]


def plan(review: list, **more) -> dict:
    return {"action": "plan", "job": "j1", "plan_sha256": "ab" * 32, "commit": "d269f20", "add": 1, "change": 0,
            "destroy": 0, "changes": [{"address": e["address"], "actions": e["actions"]} for e in review],
            "secret_policies_checked": 1, "review": review, "review_withheld": "", **more}


def facts(review: list, nat: str = "false", **more) -> dict:
    return build.plan_facts({"plan": plan(review, **more), "nat": nat, "prices": PRICES})


def answer(*lines: dict, status: str = "COMPLETE", before: str = "") -> dict:
    return {"sessionId": "s1", "sessionStatus": status, "lastMessage": before + json.dumps({"review": list(lines)})}


def review(f: dict, agent: dict) -> dict:
    return build.plan_review({"facts": {"stdout_json": f}, "agent": agent})


# ── the estimate: one function, Status and Deploy agree (D5) ──


def test_the_estimate_is_the_pinned_prices_and_counts_each_public_address() -> None:
    off = build.estimate_per_day({"instance_type": "t3.micro", "nat": False, "public_ipv4": 1}, PRICES)
    on = build.estimate_per_day({"instance_type": "t3.micro", "nat": True, "public_ipv4": 2}, PRICES)
    hourly = (PRICES["instance_hour"]["t3.micro"] + PRICES["public_ipv4_hour"]
              + (PRICES["secret_month"] + PRICES["root_volume_month"]) / 730)
    assert off["per_day_usd"] == round(hourly * 24, 2) and not off["assumed"]
    assert on["per_day_usd"] == round((hourly + PRICES["nat_gateway_hour"] + PRICES["public_ipv4_hour"]) * 24, 2)
    assumed = build.estimate_per_day({"nat": False}, PRICES)
    assert assumed["assumed"] and assumed["instance_type"] == PRICES["assumed_instance_type"]
    assert build.estimate_per_day({"instance_type": "m7i.48xlarge"}, PRICES)["per_day_usd"] is None


def test_get_aws_vpn_status_and_the_deploy_card_give_the_same_estimate() -> None:
    for nat in (False, True):
        outputs = {"strongswan_eip": "198.51.100.20", "expires_at": "none", "nat_gateway_enabled": nat,
                   "strongswan_instance_type": "t3.small"}
        status = build.aws_vpn_status({"outputs": {"result": {"return_code": 0, "stdout_json": {"outputs": outputs}}}},
                                      build.datetime.now(build.timezone.utc), PRICES)
        assert status["estimate"]["per_day_usd"] == facts(FRESH, nat=str(nat).lower())["per_day_usd"]


# ── the facts plan-reviewer is handed ──


def test_the_facts_count_each_kind_and_carry_every_change_with_its_facts() -> None:
    f = facts(CHURN)
    assert f["counts"] == {"create": 1, "update": 1, "replace": 1, "destroy": 1}
    assert "1 to create, 1 to change, 1 to replace, 1 to destroy" in f["request"]
    for e in CHURN:
        assert e["address"] in f["request"]
    assert "(forced by: ami)" in f["request"] and "(changing: tags)" in f["request"]
    assert f["changes"] == [{"address": e["address"], "kind": k} for e, k in
                            zip(CHURN, ("create", "replace", "destroy", "update"))]


def test_the_estimate_reads_the_planned_instance_type_and_the_nat_input() -> None:
    assert "t3.small" in facts(FRESH)["estimate"] and facts(FRESH)["per_day_usd"] > facts(CHURN)["per_day_usd"]
    nat_on = facts(FRESH, nat="true")
    assert "NAT gateway on, 2 public IPv4 addresses" in nat_on["request"] and nat_on["cost"].startswith("about $")
    # nothing in the plan sets the type (a no-op, or Terraform cannot know it yet): the assumed type, said so
    unknown = [entry(INSTANCE, "aws_instance", ["create"], {"instance_type": "known after apply"})]
    assert "(assumed: the plan does not set it)" in facts(unknown)["estimate"]
    assert facts([entry(INSTANCE, "aws_instance", ["delete"], {"instance_type": "t3.small"})])["per_day_usd"] == \
        facts([])["per_day_usd"]  # a destroyed instance is not what runs afterwards
    assert PRICES["basis"] in facts(FRESH)["estimate"] and "estimate" in facts(FRESH)["estimate"]


def test_a_withheld_extract_hands_over_addresses_and_actions_only_and_says_so() -> None:
    f = build.plan_facts({"plan": plan([], review_withheld="it would show a public IP address",
                                       changes=[{"address": INSTANCE, "actions": ["create"]}]),
                          "nat": "false", "prices": PRICES})
    assert "withheld (it would show a public IP address)" in f["request"] and f"create {INSTANCE}" in f["request"]
    assert f["plan"]["review_withheld"] == "it would show a public IP address"


def test_a_plan_that_changes_nothing_says_so() -> None:
    f = facts([])
    assert "The plan changes nothing." in f["request"] and f["changes"] == []
    assert "review_withheld" not in f["plan"] and "review" not in f["plan"] and "changes" not in f["plan"]


# ── the answer, held to the plan (criteria 6-8) ──


def test_a_good_review_is_kept_with_replacements_and_deletions_first() -> None:
    out = review(facts(CHURN), answer({"text": "Adds one IKE rule from the on-prem public address.", "addresses": [RULE]},
                                      {"text": "Updates the strongSwan role's tags.", "addresses": [ROLE]},
                                      {"text": "Replaces the strongSwan instance (a new AMI forces it).",
                                       "addresses": [INSTANCE]},
                                      {"text": "Removes the NAT gateway.", "addresses": [NAT]},
                                      {"text": "Estimated cost: about $0.40 a day.", "addresses": []}))
    assert out["available"] and out["dropped"] == 0 and out["why"] == ""
    assert out["review"][:2] == ["Replaces the strongSwan instance (a new AMI forces it).", "Removes the NAT gateway."]
    assert out["card"]["plan_review"] == out["review"]


def test_a_line_naming_a_resource_not_in_the_plan_is_left_out() -> None:
    out = review(facts(CHURN), answer({"text": "Replaces the instance.", "addresses": [INSTANCE]},
                                      {"text": "Creates a load balancer.", "addresses": ["module.vpn[0].aws_lb.front"]},
                                      {"text": "Opens module.vpc.aws_security_group.web to the world.", "addresses": []},
                                      {"text": "aws_nat_gateway.main[0] goes away.", "addresses": []}))
    assert out["review"][0] == "Replaces the instance." and out["dropped"] == 2
    assert "aws_nat_gateway.main[0] goes away." in out["review"]  # the end of a plan address names it
    assert out["review"][-1].startswith("(2 lines left out")



def test_one_mistyped_address_among_good_ones_keeps_the_line_and_its_place() -> None:
    """Measured 2026-10-28 on qwen3.8:27b: the twin wrote module.vpc.aws_nat_gateway.main[0 (no closing bracket) next
    to two good addresses, and the strict check dropped the plan's most important line. The card shows only the text."""
    out = review(facts(CHURN), answer({"text": "Adds one IKE rule.", "addresses": [RULE]},
                                      {"text": "Removes the NAT gateway.", "addresses": [NAT[:-1], INSTANCE]}))
    assert out["review"] == ["Removes the NAT gateway.", "Adds one IKE rule."] and out["dropped"] == 0
    # and the twin listed prefixes and labels as addresses (same run): they are not resource names, so they do not count
    out = review(facts(CHURN), answer({"text": "IKE from the on-prem public address; routes to 10.101.0.0/16.",
                                       "addresses": ["the on-prem public address", "10.101.0.0/16", "anywhere"]}))
    assert out["available"] and out["dropped"] == 0


def test_a_line_with_a_public_address_is_left_out_and_a_private_prefix_kept() -> None:
    # global, as cloud-devops-pipeline's own guard judges it (a documentation range such as 203.0.113.0/24 is not)
    out = review(facts(FRESH), answer({"text": "IKE from 8.8.8.8 is allowed.", "addresses": [RULE]},
                                      {"text": "The VPC is 10.0.0.0/16.", "addresses": []}))
    assert out["review"] == ["The VPC is 10.0.0.0/16.", "(1 line left out: named a resource not in the plan or a public address)"]


def test_at_most_eight_lines_each_at_most_300_characters() -> None:
    lines = [{"text": f"Line {i} " + "x" * 400, "addresses": []} for i in range(12)]
    out = review(facts(FRESH), answer(*lines))
    assert len(out["review"]) == build.PLAN_REVIEW_LINES and all(len(x) <= 300 for x in out["review"])
    assert out["review"][0].endswith("...")


def test_the_json_line_is_the_last_line_of_the_answer() -> None:
    out = review(facts(FRESH), answer({"text": "Creates the strongSwan instance (t3.small).", "addresses": [INSTANCE]},
                                      before="Here is the review:\n"))
    assert out["available"] and out["review"] == ["Creates the strongSwan instance (t3.small)."]


def test_a_fenced_json_line_is_read_and_an_unreadable_dotted_quad_is_no_address() -> None:
    """Code review 2026-10-28: local models fence their JSON even when told not to, and ipaddress refuses a leading zero
    (10.01.0.0) - an exception there lost a review that had run, and the card blamed the step."""
    good = {"text": "Creates the strongSwan instance (t3.small).", "addresses": [INSTANCE]}
    fenced = {"sessionStatus": "COMPLETE", "lastMessage": "```json\n" + json.dumps({"review": [good]}) + "\n```"}
    assert review(facts(FRESH), fenced)["review"] == [good["text"]]
    odd = review(facts(FRESH), answer({"text": "Routes 10.01.0.0/16 to the tunnel.", "addresses": []}, good))
    assert odd["available"] and odd["dropped"] == 0 and len(odd["review"]) == 2


def test_a_review_that_cannot_be_used_says_review_unavailable_and_the_card_still_carries_the_plan() -> None:
    f = facts(CHURN)
    cases = {
        "the reviewer session ended without an answer (canceled, failed or not started)": {},
        "the reviewer did not finish (CANCELED)": answer({"text": "x"}, status="CANCELED"),
        "the reviewer did not finish (FAILED)": {"sessionId": "s", "sessionStatus": "FAILED"},
        "the reviewer's answer was not the JSON line": {"sessionStatus": "COMPLETE", "lastMessage": "Looks fine to me."},
        "the reviewer returned no lines": answer(),
        "every line named a resource that is not in the plan": answer({"text": "x", "addresses": ["aws_s3_bucket.b"]}),
    }
    for why, agent in cases.items():
        out = review(f, agent)
        assert not out["available"] and out["why"] == why, (why, out["why"])
        assert out["card"]["plan_review"][0].startswith(f"Review unavailable: {why}.")
        assert out["card"]["estimated_cost"] == f["estimate"] and out["card"]["plan_sha256"] == "ab" * 32
        assert out["card"]["plan_changes"] == [f"{c['kind']:<8} {c['address']}" for c in f["changes"]]


def test_the_card_renders_the_review_first_then_the_estimate_then_every_change() -> None:
    out = review(facts(CHURN), answer({"text": "Replaces the <b>instance</b>.", "addresses": [INSTANCE]}))
    # the Platform hands the body over with its keys sorted (measured on Deploy 1a8c577d): the order holds anyway
    body = dict(sorted(out["card"].items()))
    page = build.approval_card({"header": "Approve the AWS change", "message": "m", "body": body})["html"]
    at = [page.index(label) for label in ("Plan review (advisory", "Estimated cost", "Every change in the plan")]
    assert at == sorted(at) and "Replaces the &lt;b&gt;instance&lt;/b&gt;." in page and "<b>instance" not in page
    assert '"review"' not in page  # the raw extract stays in the job's plan variable, off the card (E.1 finding)


def test_the_runcode_scripts_are_the_functions() -> None:
    f = facts(CHURN)
    run = lambda code, data: json.loads(subprocess.run([sys.executable, "-I", "-c", code], input=json.dumps(data),
                                                       capture_output=True, text=True, check=True).stdout)
    assert run(build.PLAN_FACTS_CODE, {"plan": plan(CHURN), "nat": "false"}) == json.loads(json.dumps(f))
    agent = answer({"text": "Replaces the instance.", "addresses": [INSTANCE]})
    assert run(build.PLAN_REVIEW_CODE, {"facts": {"stdout_json": f}, "agent": agent}) == \
        json.loads(json.dumps(review(f, agent)))


# ── Deploy's wiring: between the plan and the card, never in the way ──


def _deploy() -> tuple[dict, dict, dict]:
    wf = build.deploy_aws_vpn()
    return wf, wf["tasks"], wf["transitions"]


def test_the_review_runs_between_the_plan_and_the_card() -> None:
    _, tasks, tr = _deploy()
    assert list(tr["10"]) == ["60"] and tr["6c"]["2a"]["state"] == "success"
    agent = tasks["68"]
    assert agent["name"] == "runAgent" and agent["variables"]["incoming"] == {
        "agent": build.PLAN_REVIEWER_MARKER, "inputs": {"request": "$var.67.return_data"}}
    assert tasks["65"]["variables"]["incoming"]["code"] == build.PLAN_FACTS_CODE
    assert tasks["6b"]["variables"]["incoming"]["code"] == build.PLAN_REVIEW_CODE
    assert tasks["64"]["variables"]["incoming"]["value"] == "$var.job.enable_nat_gateway"


def test_every_review_step_that_cannot_run_goes_on_to_the_card() -> None:
    _, _, tr = _deploy()
    for tid in REVIEW_TASKS:
        assert set(tr[tid]) <= set(REVIEW_TASKS) | {"2a"}, (tid, tr[tid])
    for tid in ("65", "66", "67", "6b"):
        assert tr[tid]["2a"]["state"] == "error"
    assert tr["68"]["6d"]["state"] == "error" and list(tr["6d"]) == ["69"]  # the check then says why
    assert tr["6c"]["6e"]["state"] == "error" and list(tr["6e"]) == ["2a"]


def test_the_card_starts_from_review_unavailable_and_the_session_from_no_answer() -> None:
    _, tasks, _ = _deploy()
    fallback = tasks["61"]["variables"]
    assert fallback["outgoing"]["object"] == "$var.job.plan_card"
    assert fallback["incoming"]["obj"]["plan_review"][0].startswith("Review unavailable")
    assert fallback["incoming"]["value"] == "$var.job.plan"
    assert tasks["60"]["variables"]["outgoing"]["output"] == "$var.job.plan_cost"
    assert tasks["62"]["variables"]["outgoing"]["textObject"] == "$var.job.review_result"
    assert tasks["6c"]["variables"]["outgoing"]["return_data"] == "$var.job.plan_card"
    assert tasks["66"]["variables"]["outgoing"]["return_data"] == "$var.job.plan_cost"


def test_the_card_message_carries_the_computed_estimate_not_a_written_one() -> None:
    _, tasks, _ = _deploy()
    message = tasks["2a"]["variables"]["incoming"]["str"]
    assert "__COST__" in message and "$1 a day" not in message
    cost = tasks["2d"]["variables"]["incoming"]
    assert cost["str"] == "$var.2c.replacedString" and cost["newSubstr"] == "$var.job.plan_cost"
    assert build.card_input(tasks, "2b", 3) == "$var.job.plan_card"


def test_the_reviewer_and_its_twin_share_instructions_that_ask_for_the_line_the_check_reads() -> None:
    claude, twin = (yaml.safe_load((AGENTS / f"{n}.yaml").read_text()) for n in ("plan-reviewer", "plan-reviewer-local"))
    assert claude["instructions"] == twin["instructions"]
    assert (claude["profile"], twin["profile"]) == ("anthropic", "ollama-mac")
    text = " ".join(claude["instructions"].split())
    assert '{"review": [{"text": ' in text and "ONE line of JSON" in text and "never approve" in text
    assert "Never work out a cost of your own" in text and "{{request}}" in text
