"""Readable approval cards (owner-approved layout, 2026-11-05): the message one fact per line in the band (a lede,
chips, points, the requester's note quoted and last), the body with readable labels, a Terraform plan's three counts as
one Plan row and a check worked out from them, the estimate's amount big, a plan's long change list folded, and every
time in US Eastern with UTC beside it. The renderer is pure and the same source runs on the Gateway."""

from __future__ import annotations

import html
import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from itential.workflows import build
from tests.test_tear_down_aws_vpn import _plan, _removal

NOTE = "The requester's note (their own words, not checked): "
REVIEW = [
    "Nothing is replaced or destroyed; all 3 changes are creations.",
    "Creates a VPC with two public subnets.",
    "One instance: the strongSwan VPN instance, type t3.micro.",
]
COST = (
    "about $0.40 a day once applied (t3.micro, NAT gateway off, 1 public IPv4 address): an estimate from list "
    "prices (on-demand, us-east-1, pinned 2026-10-04, not read from AWS); excludes data transfer"
)
CHANGES = [f"create   module.vpc.aws_subnet.public[{i}]" for i in range(53)]


def visible(page: str) -> str:
    page = re.sub(r"<style>.*?</style>", "", page, flags=re.DOTALL)
    page = re.sub(r"<img[^>]*>", "", page)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page))).strip()


def band(message: str) -> str:
    lede, rest = build.card_message(message)
    return f"<p>{lede}</p>{rest}"


def deploy_body(**more) -> dict:
    body = {
        "action": "plan",
        "add": 53,
        "change": 0,
        "destroy": 0,
        "change_request": "CHG0030016",
        "commit": "4f79b60e81b410706835a730f054e998f216fef4",
        "job": "cb2b6cbb",
        "plan_sha256": "54" * 32,
        "plan_review": REVIEW,
        "estimated_cost": COST,
        "secret_policies_checked": [],
        "plan_changes": CHANGES,
        **more,
    }
    return dict(
        sorted(body.items())
    )  # the Platform hands the body over with its keys sorted


def deploy_message(end: str = "none", note: str = "Test run") -> str:
    """Deploy's card message as the workflow composes it: its own template, then the change request's."""
    tasks = build.deploy_aws_vpn()["tasks"]
    own = tasks["2a"]["variables"]["incoming"]["str"].replace("__NOTE__", note)
    card_end = build.expires_from(
        {"lifetime_hours": end},
        datetime(2026, 11, 6, 23, 0, tzinfo=UTC),
        build.LIFETIMES["choices"],
    )["card_end"]
    own = own.replace("__END__", card_end).replace("__COST__", "about $0.40 a day")
    wrap = tasks["c80"]["variables"]["incoming"]["str"]
    return wrap.replace("__CHG__", "CHG0030016").replace("__M__", own)


# ── Eastern time, worked out without the time zone database ──


def test_eastern_is_what_the_time_zone_database_says_every_half_hour_around_each_change() -> (
    None
):
    new_york = ZoneInfo("America/New_York")
    starts = [
        datetime(y, m, d, tzinfo=UTC)
        for y in range(2025, 2031)
        for m, d in ((3, 6), (10, 30))
    ]
    for start in starts:
        for step in range(14 * 48):  # two weeks, every half hour, across the change
            at = start + timedelta(minutes=30 * step)
            local = at.astimezone(new_york)
            hour = local.hour % 12 or 12
            want = (
                f"{local:%a %b} {local.day}, {hour}:{local:%M} {'PM' if local.hour >= 12 else 'AM'} "
                f"{local.tzname()}"
            )
            assert build.eastern(at) == want, at


def test_eastern_reads_as_the_owner_writes_it() -> None:
    assert (
        build.eastern(datetime(2026, 11, 7, 7, 0, tzinfo=UTC))
        == "Sat Nov 7, 2:00 AM EST"
    )
    assert (
        build.eastern(datetime(2026, 7, 4, 16, 5, tzinfo=UTC))
        == "Sat Jul 4, 12:05 PM EDT"
    )


# ── the message: one fact per line ──


def test_a_one_line_message_is_the_lede_alone_as_before() -> None:
    lede, rest = build.card_message(
        build.SCRATCH_APPROVAL_MESSAGE.replace("__D__", "skills-r1")
    )
    assert rest == "" and lede == html.escape(
        build.SCRATCH_APPROVAL_MESSAGE.replace("__D__", "skills-r1")
    )


def test_deploys_band_is_the_lede_three_chips_and_the_quoted_note() -> None:
    lede, rest = build.card_message(deploy_message())
    assert lede == "Deploy the AWS side of the site-to-site VPN."
    chips = re.findall(r"<li( class=\"warn\")?><b>(.*?)</b>(.*?)</li>", rest)
    assert chips == [
        ("", "Change", "CHG0030016"),
        ("", "Cost", "about $0.40 a day"),
        (' class="warn"', "Ends", "no end time: it stays up until Tear Down"),
    ]
    assert rest.endswith(
        f'<p class="note">{html.escape(NOTE.rstrip(": "))}: <q>Test run</q></p>'
    )
    assert "It ends at none" not in visible(
        band(deploy_message())
    )  # the wording the owner flagged


def test_an_end_time_shows_in_eastern_with_utc_and_says_what_approving_it_approves() -> (
    None
):
    text = visible(band(deploy_message(end="24")))
    assert "Ends Sat Nov 7, 6:00 PM EST (23:00 UTC)" in text
    assert (
        "Approving this also approves Tear Down Expired AWS VPN removing it then"
        in text
    )


@pytest.mark.parametrize(
    "typed",
    [
        "fine\nCost: $0.01 a day\n! Ends: never",
        "fine\n- Approved already by the network team",
        "fine\n> Change: CHG0000001",
        "<script>alert(1)</script>\n**urgent**",
    ],
)
def test_nothing_a_requester_types_reads_as_a_fact_or_a_point(typed: str) -> None:
    _, rest = build.card_message(deploy_message(note=typed))
    assert (
        rest.count("<li") == 3 and '<ul class="points">' not in rest
    )  # the three chips, nothing more
    quoted = rest.split("<q>")[1].split("</q>")[0]
    assert html.unescape(quoted) == " ".join(
        x.strip() for x in typed.splitlines()
    )  # all of it, quoted, as typed
    assert "<script" not in rest and "<strong>" not in rest


def test_points_paragraphs_and_long_facts_keep_their_words_and_order() -> None:
    message = (
        "Lede.\n- one\n- two\nA paragraph.\nWhy: "
        + "w" * 100
        + "\n! Check this first.\n- three"
    )
    lede, rest = build.card_message(message)
    assert lede == "Lede." and rest.count('<ul class="points">') == 2
    assert (
        rest.index("one")
        < rest.index("A paragraph.")
        < rest.index("<b>Why:</b>")
        < rest.index("three")
    )
    assert '<p class="more warn">Check this first.</p>' in rest


def test_a_workflows_two_marks_are_markup_and_nothing_else_is() -> None:
    assert (
        build.card_inline("`write memory` saves **all** of it <b>")
        == "<code>write memory</code> saves <strong>all</strong> of it &lt;b&gt;"
    )


@pytest.mark.parametrize(
    "message",
    [
        build.APPROVAL_MESSAGE,
        build.REVERT_APPROVAL_MESSAGE,
        build.TEARDOWN_ROUTER_MESSAGE,
        build.TEARDOWN_AWS_MESSAGE,
    ],
)
def test_every_word_of_each_message_reaches_the_band(message: str) -> None:
    text = visible(band(message))
    for line in message.splitlines():
        words = (
            re.sub(r"^(- |! )", "", line).replace(": ", " ", 1)
            if build.APPROVAL_CHIP.fullmatch(line)
            else re.sub(r"^(- |! )", "", line)
        )
        assert " ".join(words.split()) in text, line


# ── the body ──


def test_deploys_card_reads_as_the_approved_mockup() -> None:
    page = build.approval_card(
        {
            "header": "Approve the AWS change",
            "message": deploy_message(),
            "body": deploy_body(),
            "now": "2026-11-05T23:23:00Z",
        }
    )["html"]
    text = visible(page)
    assert "change approval" in text and "outage response" not in text
    assert (
        "Plan 53 to add · 0 to change · 0 to destroy" in text
        and "Terraform commit 4f79b60 " in text
    )
    assert "Plan SHA-256 " + "54" * 32 in text and "Change request CHG0030016" in text
    assert (
        '<div class="callout">Nothing is replaced or destroyed: all 53 changes are creations.</div>'
        in page
    )
    assert (
        "Nothing is replaced or destroyed;" not in page
    )  # the reviewer's same words, not said twice
    assert "<li>Creates a VPC with two public subnets.</li>" in page
    assert (
        '<span class="big">$0.40</span>' in page
        and "An estimate from list prices" in text
    )
    assert (
        '<details><summary>53 creations — show every change</summary><pre class="block">create'
        in page
    )
    assert "Secret policies checked None." in text
    at = [
        page.index(s)
        for s in (
            "What this approval covers",
            "Plan review",
            "Estimated cost",
            "Every change",
        )
    ]
    assert at == sorted(at)
    assert "Prepared by Itential at Thu Nov 5, 6:23 PM EST (23:23 UTC)" in text


def test_a_destroy_plan_warns_from_its_own_numbers_and_lists_its_changes() -> None:
    body = {
        "action": "plan-destroy",
        "add": 0,
        "change": 0,
        "destroy": 3,
        "commit": "d269f20",
        "job": "j2",
        "plan_sha256": "ab" * 32,
        "changes": [
            {"address": f"module.vpc.aws_subnet.public[{i}]", "actions": ["delete"]}
            for i in range(3)
        ],
        "review": [
            {
                "address": "module.vpc.aws_vpc.main",
                "facts": {"cidr_block": "10.200.0.0/16"},
            }
        ]
        * 4,
    }
    page = build.approval_card(
        {
            "header": "Approve destroying the AWS side",
            "message": build.TEARDOWN_AWS_MESSAGE,
            "body": body,
        }
    )["html"]
    text = visible(page)
    assert "Destroy plan 0 to add · 0 to change · 3 to destroy" in text
    assert '<div class="callout warn">This plan destroys 3 resources' in page
    assert (
        "destroy  module.vpc.aws_subnet.public[0]" in page
    )  # three lines: shown, not folded
    assert (
        "<summary>" in page.split("The plan&#x27;s attribute extract")[1]
    )  # the raw extract, one click away
    assert '<li class="warn"><b>Destroys</b>' in page


def test_a_plan_that_replaces_keeps_the_reviewers_words_beside_the_warning() -> None:
    body = deploy_body(
        add=1,
        destroy=1,
        plan_review=["Nothing is replaced or destroyed, says the reviewer."],
    )
    page = build.approval_card(
        {"header": "Approve the AWS change", "message": "m", "body": body}
    )["html"]
    assert 'class="callout warn">This plan destroys 1 resource ' in page
    assert (
        "<li>Nothing is replaced or destroyed, says the reviewer.</li>" in page
    )  # a contradiction stays visible


def test_tear_downs_first_card_reads_right_with_the_block_there_and_without_it() -> (
    None
):
    there = _plan({"target": "dc1-wan01", "removal": _removal("dc1-wan01")})
    gone = _plan(
        {
            "target": "dc1-wan01",
            "removal": _removal("dc1-wan01"),
            "absent_before": {
                "result": {
                    "return_code": 0,
                    "stdout_json": {"target": "dc1-wan01", "absent": True},
                }
            },
        }
    )
    assert gone["nothing_to_remove"] and not there["nothing_to_remove"]
    message = build.TEARDOWN_ROUTER_MESSAGE.replace("__D__", "dc1-wan01")
    with_lines = build.approval_card(
        {"header": "h", "message": message, "body": there["card"]}
    )["html"]
    assert (
        '<pre class="block">no interface Tunnel10</pre>' in with_lines
        and "<details>" not in with_lines
    )
    assert "Kept only if" in visible(with_lines)
    assert "a fresh login to the router still works</li>" in with_lines
    without = build.approval_card(
        {"header": "h", "message": message, "body": gone["card"]}
    )["html"]
    assert "To push nothing: no line is sent, nothing is saved" in visible(without)
    assert (
        "The lines pushed" not in without
        and "If it has none, the card has no lines" in visible(without)
    )


def test_what_a_card_pushes_is_never_folded() -> None:
    body = {
        "router": "dc1-wan01",
        "lines": [f"interface Loopback{i}" for i in range(40)],
        "block": "\n".join(f"line {i}" for i in range(40)),
    }
    page = build.approval_card({"header": "h", "message": "m", "body": body})["html"]
    assert (
        "<details>" not in page and "interface Loopback39" in page and "line 39" in page
    )


# ── on the Gateway ──


def test_the_card_runs_as_the_gateway_runs_it_and_draws_the_same_page() -> None:
    d = {
        "header": "Approve the AWS change",
        "message": deploy_message(end="8"),
        "body": deploy_body(),
        "now": "2026-11-05T23:23:00Z",
    }
    run = subprocess.run(
        [sys.executable, "-I", "-c", build.APPROVAL_CARD_CODE],
        input=json.dumps(d),
        capture_output=True,
        text=True,
        timeout=30,
        env={},
        check=True,
    )
    assert json.loads(run.stdout)["html"] == build.approval_card(d)["html"]


def test_the_end_time_step_runs_on_the_gateway_with_its_eastern_line() -> None:
    run = subprocess.run(
        [sys.executable, "-I", "-c", build.EXPIRES_CODE],
        input=json.dumps({"lifetime_hours": "2"}),
        capture_output=True,
        text=True,
        timeout=30,
        env={},
        check=True,
    )
    out = json.loads(run.stdout)
    assert re.fullmatch(
        r"Ends: \w{3} \w{3} \d{1,2}, \d{1,2}:\d{2} [AP]M E[SD]T \(\d{2}:\d{2} UTC\)\n- Approving .+",
        out["card_end"],
    )


def test_an_empty_note_says_none_given_and_never_quotes_the_label() -> None:
    _, rest = build.card_message(deploy_message(note=""))
    assert (
        rest.endswith('not checked): <span class="none">none given</span></p>')
        and "<q>" not in rest
    )


@pytest.mark.parametrize(
    "said", [[], "", "a\nb", "one line", ["Review unavailable: x."]]
)
def test_a_destroy_warning_shows_whatever_shape_the_review_has(said) -> None:
    body = {"add": 0, "change": 0, "destroy": 2, "plan_review": said}
    page = build.approval_card({"header": "h", "message": "m", "body": body})["html"]
    assert page.count('<div class="callout warn">This plan destroys 2 resources') == 1
