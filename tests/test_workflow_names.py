"""ADR 0067: a workflow's name says what it does, in the words an operator would use.

The rule, made mechanical so a new workflow cannot drift from it:
  - a verb then its object, in Title Case, words separated by single spaces ("Add Branch VLAN")
  - no `wf-` prefix and no version: git and the replay carry the version, not the name
  - at most 64 characters (it becomes an agent's tool name)
  - a description that says what the workflow does, not a restatement of the name
  - the file in itential/workflows/ is the name in lowercase with dashes
  - itential/versions.yaml is the one place a name is written; everything else reads it
  - a retired name is never reused and never referenced again outside the record
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / "itential" / "workflows"
VERSIONS = yaml.safe_load((ROOT / "itential" / "versions.yaml").read_text())
NAMES = VERSIONS["workflows"]
RETIRED = VERSIONS["retired_workflows"]

# The first word of every name. A new workflow whose verb is not here adds it on purpose, in review.
VERBS = {"Add", "Back", "Count", "Get", "List", "Push", "Remove", "Run", "Summarize"}
# Where old names may still appear: the history of what was decided and measured.
HISTORY = ("docs/adr/", "docs/handoffs/", "verify/results/")


def _file(name: str) -> str:
    return name.lower().replace(" ", "-") + ".json"


def _docs() -> dict[str, dict]:
    return {p.name: json.loads(p.read_text()) for p in sorted(WORKFLOWS.glob("*.json"))}


def test_every_workflow_file_is_a_named_workflow_and_every_name_has_its_file() -> None:
    docs = _docs()
    assert {d["name"] for d in docs.values()} == set(NAMES.values()), "itential/workflows/ and versions.yaml disagree"
    for file, doc in docs.items():
        assert file == _file(doc["name"]), f"{file}: the file for {doc['name']!r} is {_file(doc['name'])}"


@pytest.mark.parametrize("name", sorted(NAMES.values()))
def test_a_name_is_a_verb_and_an_object_in_title_case(name: str) -> None:
    assert re.fullmatch(r"[A-Z][a-z]+( [A-Za-z0-9]+)+", name), f"{name!r}: words of letters and digits, single spaces"
    assert name.split()[0] in VERBS, f"{name!r} must start with a verb; add a new one to VERBS on purpose"
    assert len(name) <= 64, f"{name!r} is over 64 characters"


@pytest.mark.parametrize("name", sorted(NAMES.values()))
def test_a_name_carries_no_prefix_and_no_version(name: str) -> None:
    assert not name.lower().startswith("wf"), f"{name!r}: no wf prefix"
    # a version number, not the word: "Get Device Software Version" is about the device's software
    assert not re.search(r"\bv\d+\b| \d+$", name, re.IGNORECASE), f"{name!r}: the version lives in git, not the name"


@pytest.mark.parametrize("file", sorted(_docs()))
def test_a_workflow_describes_what_it_does(file: str) -> None:
    doc = _docs()[file]
    description = (doc.get("description") or "").strip()
    assert len(description) >= 40, f"{file}: the description must say what the workflow does"
    assert description.lower() != doc["name"].lower(), f"{file}: the description repeats the name"


def test_a_retired_name_is_never_reused() -> None:
    assert not set(RETIRED) & set(NAMES.values())
    assert len(set(RETIRED)) == len(RETIRED)


def test_a_retired_name_appears_only_in_the_record() -> None:
    """The old names stay in history (ADRs, handoffs, verify logs) and in the retired list the play deletes."""
    pattern = "|".join(re.escape(n) for n in RETIRED)
    out = subprocess.run(["git", "grep", "-l", "-E", pattern], cwd=ROOT, capture_output=True, text=True, check=False).stdout.split()
    kept = ("itential/versions.yaml", "docs/PID.md", "tests/test_workflow_names.py")
    stray = [f for f in out if not f.startswith(HISTORY) and f not in kept]
    assert not stray, f"retired workflow names still referenced in {stray}"
    # the two files that hold both: the live part is above the record
    for rel, record in (("itential/versions.yaml", "retired_workflows:"), ("docs/PID.md", "## 5. Amendments")):
        text = (ROOT / rel).read_text()
        assert not re.search(pattern, text[: text.index(record)]), f"a retired name is still in use in {rel}"


def test_every_agent_workflow_tool_is_a_current_workflow() -> None:
    for path in sorted((ROOT / "itential" / "agents").glob("*.yaml")):
        agent = yaml.safe_load(path.read_text())
        for tool in agent["tools"]:
            if tool.get("kind") == "workflow":
                assert tool["reference"] in NAMES.values(), f"{path.name}: {tool['reference']!r} is not a workflow"


def test_the_lcm_actions_name_current_workflows() -> None:
    assert set(VERSIONS["lcm"]["actions"].values()) <= set(NAMES.values())
