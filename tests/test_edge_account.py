"""make edge-account / edge-account-copy (ADR 0068 step 10, the time-boxed account of dc1-wan01's window): the password
is made in memory, stored only under devices/* in the dev Vault and never over an existing entry, never printed, and
reaches the clipboard on stdin alone."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("edge_account", ROOT / "scripts" / "edge-account-to-vault.py")
ea = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ea)


@pytest.fixture
def vault(monkeypatch):
    calls: list[tuple[str, str, dict | None]] = []
    state = {"get": 404, "post": 200}

    def fake(addr, tok, method, path, body=None):
        calls.append((method, path, body))
        return state["get"] if method == "GET" else state["post"]

    monkeypatch.setattr(ea.dk, "token", lambda tier: "dev-root-token")
    monkeypatch.setattr(ea.dk, "vault", fake)
    return calls, state


def test_the_password_goes_to_the_dev_vault_once_and_is_never_printed(vault, capsys) -> None:
    calls, _ = vault
    assert ea.main([]) == 0
    method, path, body = calls[1]
    assert method == "POST" and path == "lab/data/devices/dc1-wan01-aws-vpn" and body["options"] == {"cas": 0}
    password = body["data"]["password"]
    assert len(password) == 32 and password.isalnum() and body["data"]["username"] == "itential-aws-vpn"
    assert password not in capsys.readouterr().out


def test_an_existing_entry_is_never_replaced(vault) -> None:
    calls, state = vault
    state["get"] = 200
    assert ea.main([]) == 1 and [c[0] for c in calls] == ["GET"]


def test_copy_hands_the_password_to_pbcopy_on_stdin_only(vault, monkeypatch, capsys) -> None:
    seen = {}
    monkeypatch.setattr(ea, "stored_value", lambda *a: "Secret0123456789abcdefABCDEF0123")

    def fake_run(argv, **kw):
        seen.update(argv=argv, input=kw.get("input"))

    monkeypatch.setattr(ea.subprocess, "run", fake_run)
    assert ea.main(["--copy"]) == 0
    assert seen == {"argv": ["pbcopy"], "input": "Secret0123456789abcdefABCDEF0123"}
    assert "Secret0123" not in capsys.readouterr().out


def test_the_entry_is_the_one_config_push_revert_binds() -> None:
    v = ea.dk.V
    entry = v["revert_push"]["targets"]["dc1-wan01"]
    alias = v["vault"]["dev_gateway_aliases"][entry["password_alias"]]
    assert ea.ALIAS == alias and alias["path"].startswith("devices/")
    svc = next(s for s in v["terraform_run"]["dev_services"] if s["name"] == "config-push-revert")
    assert {"name": entry["password_alias"], "type": "env", "target": "LAB_EDGE_PASSWORD_DC1_WAN01"} in svc["secrets"]
