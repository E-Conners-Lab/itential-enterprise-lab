"""ADR 0049: the Anthropic key budget is declared, metered from the platform and guarded in the agent verifies."""

from __future__ import annotations

import os
from pathlib import Path

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
