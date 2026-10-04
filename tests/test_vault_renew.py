"""The administrator token renews itself (owner decision 2026-10-04, option 3): every make target and verify that uses
the make vault-login token renews it first (auth/token/renew-self), so one login lasts until token_max_ttl instead of
token_ttl. The token never reaches argv or the output."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (ROOT / "scripts" / "vault-prod.sh").read_text()
MAKEFILE = (ROOT / "Makefile").read_text()


def _action(name: str) -> str:
    return re.search(rf"\n  {name}\)\n(.*?)\n    ;;", SCRIPT, re.S).group(1)


def test_renew_asks_vault_with_the_token_from_a_file_descriptor_and_prints_only_the_time_left() -> None:
    renew = _action("renew")
    assert "vcall POST auth/token/renew-self at" in renew and "unset at" in renew and "unset out" in renew
    assert '["auth"]["lease_duration"]' in renew  # the only field read out of the answer (which holds the token)
    for line in renew.splitlines():
        if "echo" in line:
            assert "$at" not in line and "$out" not in line and "${out" not in line, line


def test_renew_fails_when_the_token_is_gone() -> None:
    renew = _action("renew")
    assert renew.count("exit 1") == 2 and "make vault-login" in renew


def _target(name: str) -> str:
    return re.search(rf"\n{name}:.*?\n((?:\t.*\n)+)", MAKEFILE).group(1)


def test_the_make_targets_that_use_the_token_renew_it_first() -> None:
    for name in ("vault-config", "vault-cutover"):
        recipe = _target(name)
        assert recipe.index("scripts/vault-prod.sh renew") < recipe.index("VAULT_TOKEN="), name
    assert _target("vault-renew").strip() == "scripts/vault-prod.sh renew"


def test_the_verifies_renew_before_reading_the_token() -> None:
    for name in ("test-13a-aws-vpn.sh", "test-09a-vault.sh"):
        text = (ROOT / "verify" / name).read_text()
        assert text.index("scripts/vault-prod.sh renew") < text.index('VAULT_ADMIN_TOKEN=$(tr -d'), name
