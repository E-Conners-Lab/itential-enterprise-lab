"""Describe Workflow (ADR 0080): the Platform describes one of its own workflows. The service's own behaviour is
tests/test_platform_describe.py; this module holds the workflow: reads only, the name as data, every failure a clean
end, the card as the Gateway renders it, and the service's place in versions.yaml."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("wf_build_describe", ROOT / "itential" / "workflows" / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)
build.LAYOUT_SEARCH = True  # this module compares its build against the committed JSON, canvas included
VERSIONS = build.VERSIONS
NAME = "Describe Workflow"
WF = build.describe_workflow()
TASKS = {k: t for k, t in WF["tasks"].items() if k not in ("workflow_start", "workflow_end")}
PD = VERSIONS["platform_describe"]


def test_the_committed_document_is_this_build() -> None:
    committed = json.loads((ROOT / "itential" / "workflows" / build.file_name(NAME)).read_text())
    assert committed == json.loads(json.dumps(WF))
    assert build.describe_workflow in build.BUILDERS and VERSIONS["workflows"]["describe_workflow"] == NAME


def test_it_only_reads() -> None:
    """One Gateway service (read-only by its own code), one runCode for the card, one Work Center card; nothing else
    leaves the engine."""
    outside = sorted({(t["app"], t["name"]) for t in TASKS.values() if t["app"] != "WorkFlowEngine"})
    assert outside == [("GatewayManager", "runCode"), ("GatewayManager", "runService"), ("WorkCenter", "InteractiveHTML")]
    services = {t["variables"]["incoming"]["serviceName"] for t in TASKS.values() if t["name"] == "runService"}
    assert services == {"platform-describe"}


def test_the_name_goes_in_as_data_behind_the_gate() -> None:
    gate = build.INPUT_GATES[NAME]["workflow"]
    assert gate["pattern"] == r"^[A-Za-z0-9 -]+$" and gate["maxLength"] == 80
    assert json.loads(build.DESCRIBE_PARAMS) == {"action": "workflow", "timeout": "60"}
    put = TASKS["1b"]["variables"]["incoming"]
    assert put["path"] == ["name"] and put["value"] == "$var.job.workflow"
    assert TASKS["1c"]["variables"]["incoming"]["params"] == "$var.1b.object"
    assert "9a0a" in TASKS and TASKS["9a0b"]["description"].startswith("the input gate")


def test_every_task_reaches_the_end() -> None:
    tr = WF["transitions"]
    seen, todo = set(), ["workflow_start"]
    while todo:
        n = todo.pop()
        if n in seen:
            continue
        seen.add(n)
        todo += list(tr.get(n, {}))
    assert seen == set(WF["tasks"]), "a task no path reaches"
    reach_end = {"workflow_end"}
    changed = True
    while changed:
        changed = False
        for a, tos in tr.items():
            if a not in reach_end and set(tos) & reach_end:
                reach_end.add(a)
                changed = True
    assert reach_end == set(WF["tasks"]), f"dead ends: {set(WF['tasks']) - reach_end}"
    assert tr["1c"]["8a"]["state"] == "error" and tr["3a"]["8c"]["state"] == "error"
    assert tr["1d"]["1e"]["state"] == "failure" and "workflow_end" in tr["1e"]


def test_the_card_is_the_guide_on_the_branded_page() -> None:
    """GUIDE_CARD_CODE is what the Gateway runs: the described object on stdin, the page out."""
    described = {"name": "Rotate AWS VPN Key", "summary": "Rotate AWS VPN Key: 106 tasks in 12 steps, 1 stop for a person",
                 "html": "<h1>Rotate AWS VPN Key</h1><h2>How it runs</h2><ul><li>By hand &amp; <code>x</code></li></ul>"}
    r = subprocess.run([sys.executable, "-I", "-c", build.GUIDE_CARD_CODE], input=json.dumps(described),
                       capture_output=True, text=True, check=True)
    page = json.loads(r.stdout)["html"]
    assert page.startswith("<!doctype html>") and "<h2>How it runs</h2>" in page and "For reading" in page
    assert "the workflow, described" in page and "Seen closes this card" in page
    assert html_escaped("Rotate AWS VPN Key: 106 tasks") in page
    assert TASKS["3c"]["variables"]["incoming"]["btn_success"] == "Seen" == TASKS["3c"]["variables"]["incoming"]["btn_failure"]
    assert TASKS["3c"]["variables"]["incoming"]["body"] == "$var.3b.return_data"


def html_escaped(text: str) -> str:
    import html

    return html.escape(text)


def test_the_other_cards_still_say_outage_response() -> None:
    """card_page gained two arguments; their defaults keep every existing card's band as it was."""
    page = build.card_page("t", "h", "l", "", "", "", "", "f")
    assert "outage response" in page and "Waiting for your approval" in page


def test_the_service_definition_names_this_repository_and_the_alias() -> None:
    assert PD["repository"]["url"] == "https://github.com/E-Conners-Lab/itential-enterprise-lab.git"
    assert re.fullmatch(r"[0-9a-f]{40}", PD["repository"]["reference"]), "a full commit SHA (ADR 0068)"
    (svc,) = PD["services"]
    assert svc["name"] == "platform-describe" and (ROOT / svc["filename"]).exists()
    aliases = VERSIONS["vault"]["gateway_aliases"]
    for secret in svc["secrets"]:
        assert secret["name"] in aliases and secret["type"] == "env"
    assert {s["target"] for s in svc["secrets"]} == {"PLATFORM_PASSWORD"}


def test_the_pin_is_a_commit_on_main() -> None:
    """The Gateway checks the pinned commit out; one that is not on main runs code no review covered."""
    try:
        subprocess.run(["git", "cat-file", "-e", PD["repository"]["reference"] + "^{commit}"], cwd=ROOT, check=True,
                       capture_output=True)
        base = subprocess.run(["git", "merge-base", "--is-ancestor", PD["repository"]["reference"], "origin/main"],
                              cwd=ROOT, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip("no git history for the pin here")
    assert base.returncode == 0, "the pin is not an ancestor of origin/main"


# ── the deploy wiring: both tiers import the service, both runners know the Platform, the alias has its script ──


def test_both_gateway_plays_import_the_service_after_vault() -> None:
    for play in ("ansible/playbooks/itential.yml", "ansible/playbooks/platform-ha2-gateway.yml"):
        text = (ROOT / play).read_text()
        assert "tasks/gateway-describe.yml" in text, play
        assert text.index("tasks/gateway-vault.yml") < text.index("tasks/gateway-describe.yml"), f"{play}: the aliases first"
    assert (ROOT / "ansible" / "playbooks" / "tasks" / "gateway-describe.yml").exists()


def test_both_runners_carry_the_platform_address_and_the_account() -> None:
    prod = (ROOT / "itential" / "ha2" / "gateway.compose.yml.j2").read_text()
    dev = (ROOT / "itential" / "compose.override.yml").read_text()
    for text in (prod, dev):
        assert "PLATFORM_URL:" in text and "PLATFORM_USER:" in text
    assert "RUNNER_PLATFORM_URL=" in (ROOT / "ansible" / "playbooks" / "itential.yml").read_text()
    urls = PD["platform_url"]
    assert urls["prod"].startswith("https://") and urls["dev"].startswith("http://platform:")
    assert PD["user"] == "svc-copilot"


def test_the_password_is_seeded_from_env_and_only_the_gateway_reads_it() -> None:
    v = VERSIONS["vault"]
    seed = v["secrets"]["platform/svc-copilot"]["password"]
    assert seed == {"env": "SVC_COPILOT_PROD_PASSWORD", "dev_env": "SVC_COPILOT_DEV_PASSWORD"}
    assert "platform/svc-copilot" in v["approles"]["itential-gateway"]["policy_paths"]
    assert not any(p.startswith("platform") for p in v["approles"]["itential-platform"]["policy_paths"])
