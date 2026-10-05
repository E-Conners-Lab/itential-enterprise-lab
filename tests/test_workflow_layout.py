"""The Studio canvas reads top to bottom and no task hides another (build.py layout(), owner preference 2026-09-24)."""

from __future__ import annotations

import itertools
import json
from collections import defaultdict
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[1] / "itential" / "workflows"
COL = 300  # build.py: tasks sharing a row are at least this far apart


def _docs() -> dict[str, dict]:
    return {p.name: json.loads(p.read_text()) for p in sorted(WORKFLOWS.glob("*.json"))}


@pytest.mark.parametrize("file", sorted(_docs()))
def test_start_is_alone_on_top_and_end_alone_at_the_bottom(file: str) -> None:
    loc = {k: t["nodeLocation"] for k, t in _docs()[file]["tasks"].items()}
    others = [v["y"] for k, v in loc.items() if k not in ("workflow_start", "workflow_end")]
    assert loc["workflow_start"]["y"] < min(others) and loc["workflow_end"]["y"] > max(others)


@pytest.mark.parametrize("file", sorted(_docs()))
def test_every_forward_arrow_points_down(file: str) -> None:
    doc = _docs()[file]
    loc = {k: t["nodeLocation"] for k, t in doc["tasks"].items()}
    flat_or_up = [(s, d) for s, ds in doc["transitions"].items() for d in ds if loc[d]["y"] <= loc[s]["y"]]
    # a loop back (retry, poll) is allowed to rise; none of today's workflows has one
    assert not flat_or_up, f"{file}: arrows that do not point down: {flat_or_up}"


@pytest.mark.parametrize("file", sorted(_docs()))
def test_no_task_sits_on_another(file: str) -> None:
    rows: dict[int, list[int]] = defaultdict(list)
    for t in _docs()[file]["tasks"].values():
        rows[t["nodeLocation"]["y"]].append(t["nodeLocation"]["x"])
    for y, xs in rows.items():
        xs.sort()
        assert all(b - a >= COL for a, b in itertools.pairwise(xs)), f"{file}: tasks crowd row y={y}: {xs}"


def _build():
    import importlib.util

    spec = importlib.util.spec_from_file_location("build", WORKFLOWS / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("file", sorted(_docs()))
def test_arrows_stay_clear_of_the_tasks(file: str) -> None:
    """Scored as Studio draws it (straight arrows): a small workflow is clean, a large one close to it."""
    doc = _docs()[file]
    where = {k: t["nodeLocation"] for k, t in doc["tasks"].items()}
    edges = [(s, d) for s, ds in doc["transitions"].items() for d in ds]
    crossing, through = _build().drawn_cost(where, edges)
    if len(doc["tasks"]) < 20:
        assert (crossing, through) <= (1, 1), f"{file}: {crossing} crossings, {through} arrows through a task"
    else:
        assert through <= THROUGH_ALLOWED.get(file, 2), f"{file}: {through} arrows pass through a task"


# Owner decision 2026-10-01 (option C): Hand Off's NetBox read-back added ~14 rows to its main path, and no placement
# of the notes gets back under 2 (best measured: 3, at more crossings). Canvas only - the job runs the same. The
# follow-up shortens the read-back (one merge task for its data, proved on dev) and removes this allowance.
# Owner decision 2026-10-03: arrow overlaps are fine as long as the workflow runs the same. Tear Down Expired AWS VPN
# (R2b) sends eight teardown failures to one Work Center task; a sweep of its placements (2026-10-04) found none under 3,
# and the state read's own check (ADR 0066) adds one more.
# Diagnose AWS VPN Outage (R6, 2026-10-05): five failure notes and the menu's three fixes meet at two tasks; same rule.
# Its HTML card (2026-10-05) adds the card's own chain and the engineer's note on both branches: 10, same rule.
THROUGH_ALLOWED = {"hand-off-aws-vpn.json": 5, "tear-down-expired-aws-vpn.json": 4, "diagnose-aws-vpn-outage.json": 10}
