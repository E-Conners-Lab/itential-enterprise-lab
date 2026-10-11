"""The workflow guides (docs/workflows/): one page per workflow, rendered by itential/workflows/describe.py from
the committed document and nothing else. These tests hold the pages to the documents (a page is never edited by
hand), the documents to the steps build.py names (every task of a guided workflow is in a step, and a step is written
`short: what it does`), and the pages to the runbooks' publishing rule (no environment-specific value transcribed)."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml
from test_runbooks import FORBIDDEN

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / "itential" / "workflows"
DOCS = ROOT / "docs" / "workflows"
_SPEC = importlib.util.spec_from_file_location("wf_describe", WORKFLOWS / "describe.py")
describe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(describe)
VERSIONS = yaml.safe_load((WORKFLOWS.parent / "versions.yaml").read_text())
PAGES = describe.pages(VERSIONS)
GUIDED = describe.guided(VERSIONS)


def document(name: str) -> dict:
    return json.loads((WORKFLOWS / describe.file_name(name)).read_text())


@pytest.mark.parametrize("path", sorted(PAGES), ids=lambda p: p.name)
def test_every_page_is_what_the_committed_document_renders(path: Path) -> None:
    """`python itential/workflows/describe.py --write` after a build is the only way a page changes."""
    assert path.exists(), f"{path.relative_to(ROOT)} missing: run describe.py --write"
    assert path.read_text() == PAGES[path], f"{path.relative_to(ROOT)} is stale: run describe.py --write"


def test_no_page_exists_without_a_guided_workflow() -> None:
    stray = sorted(p.name for p in DOCS.glob("*.md")) if DOCS.exists() else []
    assert stray == sorted(p.name for p in PAGES), "a page in docs/workflows/ has no workflow in versions.yaml"


@pytest.mark.parametrize("name", sorted(GUIDED))
def test_every_task_of_a_guided_workflow_is_in_a_step(name: str) -> None:
    """A step is the task's description (build.py `step`); a task left with its summary there has none."""
    doc = document(name)
    loose = [tid for tid, t in doc["tasks"].items() if tid not in (describe.START, describe.END)
             and (not t.get("description") or t["description"] == t.get("summary"))]
    assert not loose, f"{name}: tasks without a step: {loose}"


@pytest.mark.parametrize("name", sorted(GUIDED))
def test_every_step_is_written_short_colon_what_it_does(name: str) -> None:
    """The diagram shows the short part, the headings the whole; the short part must stay short."""
    for title, _ in describe.steps(document(name)):
        assert re.match(r"^[^:]{3,40}: \S", title), f"{name}: step title not `short: what it does`: {title!r}"


@pytest.mark.parametrize("name", sorted(GUIDED))
def test_the_walk_through_names_every_task_once(name: str) -> None:
    doc = document(name)
    page = PAGES[describe.page_path(name)]
    walk = page.split("## The walk-through")[1].split("## Where it waits")[0]
    for tid in doc["tasks"]:
        if tid in (describe.START, describe.END):
            continue
        assert walk.count(f"- `{tid}` ") == 1, f"{name}: task {tid} is not exactly once in the walk-through"


@pytest.mark.parametrize("name", sorted(GUIDED))
def test_the_steps_read_in_canvas_order(name: str) -> None:
    """The steps come in the order they end on the canvas (ADR 0067: top to bottom), so a step that catches failures
    from everywhere, drawn beside the first one it catches, is read after the steps it catches."""
    doc = document(name)
    ends = [max(doc["tasks"][tid]["nodeLocation"]["y"] for tid in ids) for _, ids in describe.steps(doc)]
    assert ends == sorted(ends), f"{name}: steps out of canvas order: {ends}"


@pytest.mark.parametrize("path", sorted(PAGES), ids=lambda p: p.name)
def test_no_page_transcribes_an_environment_specific_value(path: Path) -> None:
    """The runbooks' rule (ADR 0056 decision 4) holds for the guides: they are published with them."""
    for pattern, what in FORBIDDEN:
        hits = re.findall(pattern, PAGES[path])
        assert not hits, f"{path.name} transcribes {what}: {hits[:3]}"


def test_the_index_lists_every_page() -> None:
    index = PAGES[DOCS / "README.md"]
    for path in PAGES:
        if path.name != "README.md":
            assert f"]({path.name})" in index, f"README.md does not link {path.name}"
