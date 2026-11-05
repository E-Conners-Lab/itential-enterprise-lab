"""Operations Manager queries the Platform accepts. Runs over EVERY generated workflow: the Platform answers a query that
sorts on a field its `include` leaves out with a 400 ("Parameter 'include' must specify the value of 'sort'"), and the
task takes its error edge - Check AWS Drift's open-card read did exactly that on production 2026-11-05, so the watch
never ran and every drift went to the R3 task. Measured: the same query with the sort field included answers 200."""

from __future__ import annotations

import json
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / "itential" / "workflows"


def _queries() -> list[tuple[str, str, dict]]:
    found = []
    for path in sorted(WORKFLOWS.glob("*.json")):
        for tid, task in json.loads(path.read_text())["tasks"].items():
            if not isinstance(task, dict) or task.get("app") != "OperationsManager":
                continue
            query = (task.get("variables", {}).get("incoming") or {}).get("queryParameters")
            if isinstance(query, dict):
                found.append((path.name, tid, query))
    return found


def test_every_sorted_query_includes_its_sort_field() -> None:
    sorted_queries = [(name, tid, q) for name, tid, q in _queries() if q.get("sort") and q.get("include")]
    assert len(sorted_queries) >= 4  # Check AWS Drift's two reads and both Request AWS VPN Deployment reads
    for name, tid, query in sorted_queries:
        assert query["sort"] in query["include"].split(","), f"{name}: {tid} sorts on {query['sort']} but leaves it out"


def test_the_drift_watch_open_card_read_includes_its_sort_field() -> None:
    (query,) = [q for name, tid, q in _queries() if name == "check-aws-drift.json" and tid == "ab"]
    assert query["sort"] == "metrics.start_time" and "metrics.start_time" in query["include"].split(",")
