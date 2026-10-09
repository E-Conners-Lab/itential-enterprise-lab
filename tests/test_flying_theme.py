"""The flying theme (ADR 0077): the mark and the palette changed, the words did not; every approval is a branded page.

The oracle for "the words did not change" is tests/fixtures/card-text-before-theme.json: the three cards' visible text,
rendered from the same fixtures on main before the theme and frozen here with the markup stripped."""

from __future__ import annotations

import html
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from itential.workflows import build
from tests import test_break_fabric_bgp as drill_t
from tests import test_diagnose_aws_vpn_outage as outage_t
from tests import test_diagnose_fabric_bgp_outage as fabric_t

ROOT = Path(__file__).resolve().parent.parent
BEFORE = json.loads((ROOT / "tests" / "fixtures" / "card-text-before-theme.json").read_text())
PORTAL = (ROOT / "itential" / "portal" / "deploy-aws-vpn" / "index.html").read_text()
ANCHOR_PATH = "M8 26c4 3 8 3 12 0s8-3 12 0"  # the old mark's waves
OLD_PALETTE = ("#1F4FD1", "#EEF3F7", "#0E2A4A", "#1E8C6A", "#D8433A", "#C7851A")
COCKPIT_JARGON = ("squawk", "go-around", "cleared for", "wheels up", "callsign", "mayday", "roger")

CARDS = {
    "outage": lambda: build.outage_card(outage_t.CARD_IN)["html"],
    "fabric": lambda: build.fabric_card(fabric_t.CARD_IN)["html"],
    "drill": lambda: build.drill_card(drill_t.CARD_IN)["html"],
}


def visible_text(page: str) -> str:
    page = re.sub(r"<style>.*?</style>", "", page, flags=re.S)
    page = re.sub(r"<img[^>]*>", "", page)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page))).strip()


@pytest.mark.parametrize("kind", sorted(CARDS))
def test_the_words_on_every_card_are_what_they_were_before_the_theme(kind: str) -> None:
    assert visible_text(CARDS[kind]()) == BEFORE[kind]


@pytest.mark.parametrize("kind", sorted(CARDS))
def test_the_mark_and_the_palette_changed_on_every_card(kind: str) -> None:
    page = CARDS[kind]()
    assert ANCHOR_PATH not in page and ANCHOR_PATH not in build.card_mark()
    assert all(c not in page for c in OLD_PALETTE)
    for colour in (build.CARD_NIGHT, build.CARD_PANEL, build.CARD_GREEN, build.CARD_AMBER, build.CARD_RED, build.CARD_SKY):
        assert colour in page, colour
    assert "Elliot's Itential Lab" in page and "outage response" in page
    assert not any(word in page.lower() for word in COCKPIT_JARGON)


def test_the_css_and_the_drawings_read_one_palette() -> None:
    for colour in build.CARD_PALETTE.values():
        assert colour in build.OUTAGE_CARD_CSS or colour in (build.CARD_GREY, build.CARD_TEXT, build.CARD_MUTED), colour
    for code in (build.OUTAGE_CARD_CODE, build.FABRIC_CARD_CODE, build.DRILL_CARD_CODE, build.APPROVAL_CARD_CODE):
        assert "CARD_GREEN = " in code and "CARD_RED = " in code  # the runner has the constants too
    # the meaning colours keep their meanings: green is Established, red is down, amber is waiting
    page = fabric_t._card()
    assert build.CARD_GREEN in page and build.CARD_RED in page


def test_the_mark_is_the_attitude_indicator_once_in_build_and_once_in_the_portal() -> None:
    mark = build.card_mark()
    assert mark.startswith('<img alt="" width="26" height="26" src="data:image/svg+xml;base64,')
    import base64
    svg = base64.b64decode(re.search(r"base64,([A-Za-z0-9+/=]+)", mark).group(1)).decode()
    assert '<clipPath id="c">' in svg and 'fill="#4FA3F7"' in svg and 'fill="#8A5A1E"' in svg and "M9 21h8l3 3 3-3h8" in svg
    assert 'id="mark-clip"' in PORTAL and "M9 21h8l3 3 3-3h8" in PORTAL and ANCHOR_PATH not in PORTAL
    assert "Elliot's Itential Lab" in PORTAL and "Network automation portal" in PORTAL
    assert not any(word in PORTAL.lower() for word in COCKPIT_JARGON)


def test_the_portal_keeps_its_route_ids_and_wears_the_palette() -> None:
    for hook in ('id="route-base"', 'id="route-progress"', 'id="waypoints"', 'id="legs"', 'id="aws-note"',
                 'id="chart-status"', 'class="wp-dot"', ".wp.active", ".wp.done", ".wp.failed", ".wp.rejected"):
        assert hook in PORTAL or hook.strip('"') in PORTAL, hook
    assert "--paper: #0B1322;" in PORTAL and "--kelp: #39C27A;" in PORTAL and "--buoy: #FF5A5F;" in PORTAL
    assert "--flare: #F2B134;" in PORTAL and "--cobalt: #4FA3F7;" in PORTAL
    assert "#D9C9A3" not in PORTAL and "waves" in PORTAL  # the shores are gone; the pattern id is kept for the JS-free SVG


# ── every approval is a branded page ──

APPROVALS = {
    "Deploy AWS VPN": ("2b", "approval", "Approve the AWS change", "$var.job.plan"),
    "Hand Off AWS VPN": ("6f", "approval", "Approve the router change", "$var.69.object"),  # NetBox, then Batfish (R7)
    "Push Configuration with Revert Timer": ("2c", "approval", "Approve the change under a revert timer", "$var.2a.return_data"),
    "Tear Down AWS VPN": ("2c", "approval", "Approve removing the router's AWS block", "$var.2a.return_data"),
}


def _wf(name: str) -> dict:
    files = {w["name"]: w for w in (json.loads(p.read_text()) for p in (ROOT / "itential" / "workflows").glob("*.json")
                                   if p.name not in ("push-configuration-with-approval.json",))}
    return files[name]


@pytest.mark.parametrize("name", sorted(APPROVALS))
def test_each_approval_is_an_interactive_html_task_fed_by_the_branded_page(name: str) -> None:
    tid, var, header, body_ref = APPROVALS[name]
    wf = _wf(name)
    tasks, tr = wf["tasks"], wf["transitions"]
    card = tasks[tid]
    assert card["name"] == "InteractiveHTML" and card["type"] == "manual"
    inc = card["variables"]["incoming"]
    assert inc["header"] == header and inc["btn_success"] == "Approve" and inc["btn_failure"] == "Reject"
    assert inc["body"] == f"$var.{tid}5.return_data"
    assert tasks[f"{tid}1"]["variables"]["incoming"]["value"] == header
    assert tasks[f"{tid}3"]["variables"]["incoming"]["value"] == body_ref
    assert tasks[f"{tid}4"]["variables"]["incoming"]["code"] == build.APPROVAL_CARD_CODE
    assert tasks[f"{tid}4"]["variables"]["outgoing"]["result"] == f"$var.job.{var}_card"
    assert tasks[f"{tid}5"]["variables"]["incoming"]["query"] == "stdout_json.html"
    # the chain is linear, and the approval keeps both its edges
    for a, b in zip(build.approval_ids(tid), [*build.approval_ids(tid)[1:], tid]):
        assert tr[a] == {b: {"state": "success", "type": "standard"}}
    states = {e["state"] for e in tr[tid].values()}
    assert {"success", "failure"} <= states
    assert not any(t.get("name") == "ViewData" and t["variables"]["incoming"].get("btn_success") == "Approve"
                   for t in tasks.values()), "a form-based approval is left"


def test_tear_downs_second_card_is_a_branded_page_too() -> None:
    tasks = _wf("Tear Down AWS VPN")["tasks"]
    assert tasks["5f"]["name"] == "InteractiveHTML" and tasks["5f"]["variables"]["incoming"]["body"] == "$var.5f5.return_data"
    assert tasks["5f3"]["variables"]["incoming"]["value"] == "$var.job.destroy_plan"
    assert tasks["5f4"]["variables"]["outgoing"]["result"] == "$var.job.approval_2_card"


HAND_OFF_BODY = {"target": "dc1-wan01", "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                 "block": "crypto ikev2 keyring AWS\n peer aws\n  address 10.200.0.1\n  pre-shared-key ********\n",
                 "netbox": {"site": "dc1", "interface": "GigabitEthernet7"}}
REVERT_BODY = {"device": "dc1-wan01", "reason": "step 10 part B", "revert timer": "5 minutes",
               "kept only if": ["BGP neighbour 10.1.0.1 Established (VRF WAN)", "ping 10.1.0.1 from Loopback0 at least 80%"],
               "lines": ["ip access-list standard REVERT-PROBE", " remark probe"], "saving": "`write memory` saves the whole running configuration"}


def test_the_approval_page_lays_out_the_message_the_facts_and_the_blocks() -> None:
    page = build.approval_card({"header": "Approve the router change", "message": build.APPROVAL_MESSAGE.replace("__T__", "dc1-wan01"),
                                "body": HAND_OFF_BODY, "now": "2026-10-07T20:00:00Z"})["html"]
    text = visible_text(page)
    assert "Approve the router change" in text and "Hand Off the AWS VPN to dc1-wan01" in text
    assert "target dc1-wan01" in text and "sha256 e3b0c442" in text
    assert "<pre class=\"block\">crypto ikev2 keyring AWS" in page and "pre-shared-key ********" in page
    assert "&quot;site&quot;: &quot;dc1&quot;" in page  # nested objects as an escaped JSON block
    assert "Approve runs exactly what this card shows" in text and "Note for the record" in text
    assert "Elliot's Itential Lab" in text and "20:00 UTC" in text and "<script" not in page
    assert '<form name="decision">' in page and 'name="note"' in page


def test_the_approval_page_escapes_what_it_shows_and_caps_a_block() -> None:
    page = build.approval_card({"header": "<b>x</b>", "message": "<script>alert(1)</script>",
                                "body": {"lines": ["<img src=x onerror=alert(1)>"] + ["x" * 100] * 400}})["html"]
    assert "<script>alert" not in page and "<b>x</b>" not in page and "onerror=" not in page.split("<pre")[1].split("</pre>")[0].replace("&lt;img src=x onerror=alert(1)&gt;", "")
    assert "... (cut)" in page


def test_the_approval_page_runs_as_the_gateway_runs_it() -> None:
    run = subprocess.run([sys.executable, "-I", "-c", build.APPROVAL_CARD_CODE],
                         input=json.dumps({"header": "Approve the change under a revert timer",
                                           "message": build.REVERT_APPROVAL_MESSAGE.replace("__D__", "dc1-wan01"), "body": REVERT_BODY}),
                         capture_output=True, text=True, timeout=30, env={}, check=True)
    page = json.loads(run.stdout)["html"]
    assert "ip access-list standard REVERT-PROBE" in page and "BGP neighbour 10.1.0.1 Established" in page
    assert build.CARD_NIGHT in page and len(page) < 60_000
