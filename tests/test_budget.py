"""ADR 0049: the Anthropic key budget is declared, metered from the platform and guarded in the agent verifies."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = yaml.safe_load((ROOT / "itential" / "versions.yaml").read_text())


def test_budget_declared_with_prices() -> None:
    b = VERSIONS["llm"]["budget"]
    assert b["provider"] == "anthropic" and b["weekly_usd"] == 15
    assert (
        b["input_usd_per_mtok"] > 0
        and b["output_usd_per_mtok"] > b["input_usd_per_mtok"]
    )


def test_meter_exists_and_is_wired() -> None:
    meter = ROOT / "verify" / "tokens.sh"
    assert meter.exists() and os.access(meter, os.X_OK)
    text = meter.read_text()
    assert (
        "agent-session-manager/sessions" in text
        and "totalInputTokens" in text
        and "--check" in text
    )
    assert "tokens:" in (ROOT / "Makefile").read_text()


def test_agent_verifies_guard_the_budget() -> None:
    for name in ("test-06-flowai.sh", "test-06c-netbox.sh"):
        text = (ROOT / "verify" / name).read_text()
        assert "verify/tokens.sh --check" in text and "ANTHROPIC_VERIFY" in text, (
            f"{name} lacks the budget guard"
        )
        assert text.index("iap_login ||") < text.index("verify/tokens.sh --check"), (
            f"{name}: the guard runs after the login"
        )


def test_verifies_that_spend_nothing_have_no_guard() -> None:
    assert "tokens.sh" not in (ROOT / "verify" / "test-06b-platform.sh").read_text()


def _netclawusage():
    spec = importlib.util.spec_from_file_location(
        "netclawusage", ROOT / "verify" / "netclawusage.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# a model.completed event as OpenClaw 2026.8.35 writes it (measured in netclaw-cloud, 2026-11-04)
_EVENT = {
    "type": "model.completed",
    "runId": "run-1",
    "provider": "anthropic",
    "modelId": "claude-sonnet-5",
    "data": {
        "usage": {
            "input": 691,
            "output": 237,
            "cacheRead": 9414,
            "cacheWrite": 9979,
            "total": 20321,
        }
    },
}


def test_budget_prices_cache_tokens() -> None:
    b = VERSIONS["llm"]["budget"]
    assert b["cache_write_multiplier"] == 1.25 and b["cache_read_multiplier"] == 0.1


def test_meter_counts_netclaw() -> None:
    text = (ROOT / "verify" / "tokens.sh").read_text()
    assert "verify/netclawusage.py harvest" in text
    assert "netclawusage.totals(" in text and "netclawusage.cost(" in text
    assert text.index("netclawusage.py harvest") < text.index("<<'PY'"), (
        "the harvest must run before the report reads the ledger"
    )
    # a failed harvest fails the --check guard closed (proven live 2026-11-04 with docker off the PATH: exit 1)
    assert 'os.environ["NC_OK"] != "1"' in text and "NC_OK=0" in text


def test_netclaw_ledger_lives_outside_the_repo() -> None:
    nu = _netclawusage()
    assert not nu.LEDGER.resolve().is_relative_to(ROOT)


def test_netclaw_query_reads_openclaw_store(tmp_path: Path) -> None:
    nu = _netclawusage()
    db = sqlite3.connect(tmp_path / "openclaw-agent.sqlite")
    db.execute(
        "create table trajectory_runtime_events (event_json text, created_at integer)"
    )
    db.execute(
        "insert into trajectory_runtime_events values (?, ?)",
        (json.dumps(_EVENT), 1_793_758_458_662),
    )
    db.execute(
        "insert into trajectory_runtime_events values (?, ?)",
        (json.dumps({"type": "context.compiled", "runId": "run-1"}), 1),
    )
    rows = db.execute(nu.QUERY).fetchall()
    assert rows == [
        (
            "run-1",
            1_793_758_458_662,
            "anthropic",
            "claude-sonnet-5",
            691,
            237,
            9414,
            9979,
        )
    ]


def test_netclaw_ledger_keeps_each_run_once(tmp_path: Path) -> None:
    nu = _netclawusage()
    ledger = tmp_path / "usage.jsonl"
    rows = {"netclaw": [["run-1", 1000, "anthropic", "claude-sonnet-5", 1, 2, 3, 4]]}
    assert nu.record(rows, ledger) == 1
    assert nu.record(rows, ledger) == 0, "a second harvest must not count a run twice"
    assert ledger.stat().st_mode & 0o777 == 0o600
    (rec,) = nu.load(ledger)
    assert rec["container"] == "netclaw" and rec["cache_write"] == 4


def test_netclaw_totals_window_and_cost(tmp_path: Path) -> None:
    nu = _netclawusage()
    ledger = tmp_path / "usage.jsonl"
    now = 1_800_000_000.0
    day_ms = 86_400_000
    million = 1_000_000
    rows = {
        "netclaw": [
            [
                "new",
                now * 1000 - day_ms,
                "anthropic",
                "claude-sonnet-5",
                million,
                million,
                million,
                million,
            ],
            [
                "old",
                now * 1000 - 8 * day_ms,
                "anthropic",
                "claude-sonnet-5",
                5,
                5,
                5,
                5,
            ],
        ]
    }
    nu.record(rows, ledger)
    per = nu.totals(7, now=now, ledger=ledger)
    p = per["anthropic|claude-sonnet-5"]
    assert p["runs"] == 1, "a run older than the window is left out"
    # a million of each at $2 in / $10 out, writes 1.25x and reads 0.1x the input price
    assert abs(nu.cost(p, VERSIONS["llm"]["budget"]) - (2 + 10 + 2.5 + 0.2)) < 1e-9


def _fake_docker(returncode: int, stdout: str = "", stderr: str = ""):
    def fake(*args: str, stdin: str | None = None):
        return subprocess.CompletedProcess(
            ["docker", *args], returncode, stdout, stderr
        )

    return fake


def test_netclaw_harvest_of_a_missing_container_is_empty(monkeypatch) -> None:
    nu = _netclawusage()
    # what docker inspect answers for a name it does not know (measured 2026-11-04)
    monkeypatch.setattr(
        nu, "docker", _fake_docker(1, "\n", "error: no such object: no-such-container")
    )
    assert nu.netclaw_containers(["no-such-container"]) == {}


def test_netclaw_docker_down_is_an_error_not_an_empty_list(monkeypatch) -> None:
    nu = _netclawusage()
    down = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
    monkeypatch.setattr(nu, "docker", _fake_docker(1, "", down))
    with pytest.raises(RuntimeError, match="docker ps failed"):
        nu.netclaw_containers()
    with pytest.raises(RuntimeError, match="docker inspect failed"):
        nu.netclaw_containers(["netclaw"])


def test_netclaw_stopped_container_without_a_store_has_no_usage(monkeypatch) -> None:
    nu = _netclawusage()
    # docker cp's answer when the path is missing (measured 2026-11-04)
    missing = "Error response from daemon: Could not find the file /home/node/.openclaw/agents in container x"
    monkeypatch.setattr(nu, "docker", _fake_docker(1, "", missing))
    assert nu.read_rows("x", running=False) == []


def test_netclaw_one_unreadable_container_does_not_stop_the_others(
    monkeypatch, tmp_path: Path
) -> None:
    nu = _netclawusage()
    ledger = tmp_path / "usage.jsonl"
    monkeypatch.setattr(nu, "LEDGER", ledger)
    monkeypatch.setattr(
        nu, "netclaw_containers", lambda names: {"bad": True, "good": True}
    )

    def read_rows(name: str, running: bool):
        if name == "bad":
            raise RuntimeError("bad: reading its store failed")
        return [["run-good", 1000, "anthropic", "claude-sonnet-5", 1, 2, 3, 4]]

    monkeypatch.setattr(nu, "read_rows", read_rows)
    assert nu.main(["harvest"]) == 1, (
        "a container that could not be read must fail the harvest"
    )
    assert [r["run"] for r in nu.load(ledger)] == ["run-good"], (
        "the readable one is still recorded"
    )
