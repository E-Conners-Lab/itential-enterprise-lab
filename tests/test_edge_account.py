"""make edge-account / edge-account-copy (ADR 0068 step 10, the time-boxed account of dc1-wan01's window): the password
is made in memory, stored only under devices/* in the dev Vault and never over an existing entry, never printed, and
reaches the clipboard on stdin alone."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import re
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


LINE = re.compile(r"^username itential-aws-vpn privilege 15 secret 9 \$9\$[./0-9A-Za-z]{14}\$[./0-9A-Za-z]{43}$")


def test_the_router_gets_a_type9_line_never_the_password(vault, monkeypatch, capsys) -> None:
    seen = {}
    password = "TestOnly" * 4  # 32 characters, obviously not a real one
    monkeypatch.setattr(ea, "stored_value", lambda *a: (200, password))
    monkeypatch.setattr(ea.subprocess, "run", lambda argv, **kw: seen.update(argv=argv, input=kw.get("input")))
    assert ea.main(["--line"]) == 0
    assert seen["argv"] == ["pbcopy"] and LINE.match(seen["input"]) and password not in seen["input"]
    out = capsys.readouterr().out
    assert password not in out and "$9$" not in out  # neither the password nor the hash reaches the terminal


def test_type9_is_ciscos_scrypt_form() -> None:
    """Proved live against a hash clab-rtr1 stores (2026-10-01; the value is not committed): scrypt N=16384, r=1, p=1
    over the 14-character salt, the 32-byte key in Cisco's base64 alphabet without padding."""
    a, b = ea.type9("pw", "AAAAAAAAAAAAAA"), ea.type9("pw", "BBBBBBBBBBBBBB")
    assert a == ea.type9("pw", "AAAAAAAAAAAAAA") and a != b and a.startswith("$9$AAAAAAAAAAAAAA$") and len(a) == 61
    raw = hashlib.scrypt(b"pw", salt=b"AAAAAAAAAAAAAA", n=16384, r=1, p=1, dklen=32)
    assert a.split("$")[3] == base64.b64encode(raw).decode().rstrip("=").translate(
        str.maketrans(ea.STD_B64, ea.CISCO_B64))


@pytest.mark.parametrize("status, said", [(404, "make edge-account first"), (403, "token expired?")])
def test_line_refuses_without_a_readable_entry(vault, monkeypatch, capsys, status, said) -> None:
    monkeypatch.setattr(ea, "stored_value", lambda *a: (status, None))
    monkeypatch.setattr(ea.subprocess, "run", lambda *a, **k: pytest.fail("nothing may reach the clipboard"))
    assert ea.main(["--line"]) == 1 and said in capsys.readouterr().out


def test_a_refused_write_says_why(vault, capsys) -> None:
    _, state = vault
    state["post"] = 400
    assert ea.main([]) == 1 and "vault kv metadata delete" in capsys.readouterr().out


def test_an_unreadable_vault_refuses_before_writing(vault) -> None:
    calls, state = vault
    state["get"] = 403
    assert ea.main([]) == 1 and [c[0] for c in calls] == ["GET"]


def test_only_a_devices_path_is_ever_written(vault, monkeypatch) -> None:
    calls, _ = vault
    monkeypatch.setattr(ea, "ALIAS", {"path": "services/elsewhere", "key": "password"})
    assert ea.main([]) == 1 and calls == []


def test_stored_value_reads_kv_v2(monkeypatch) -> None:
    class Answer(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    body = json.dumps({"data": {"data": {"password": "pw", "username": "itential-aws-vpn"}, "metadata": {}}}).encode()
    monkeypatch.setattr(ea.urllib.request, "urlopen", lambda req, **kw: Answer(body))
    assert ea.stored_value("https://vault", "t", "lab/data/devices/x") == (200, "pw")


def test_the_entry_is_the_one_config_push_revert_binds() -> None:
    v = ea.dk.V
    entry = v["revert_push"]["targets"]["dc1-wan01"]
    alias = v["vault"]["dev_gateway_aliases"][entry["password_alias"]]
    assert ea.ALIAS == alias and alias["path"].startswith("devices/")
    svc = next(s for s in v["terraform_run"]["dev_services"] if s["name"] == "config-push-revert")
    assert {"name": entry["password_alias"], "type": "env", "target": "LAB_EDGE_PASSWORD_DC1_WAN01"} in svc["secrets"]


def test_the_entry_holds_the_keys_the_vault_tests_expect(vault) -> None:
    """tests/test_vault.py lists {password, username} for this path; this is where those keys come from."""
    calls, _ = vault
    ea.main([])
    assert set(calls[1][2]["data"]) == {ea.ALIAS["key"], "username"} == {"password", "username"}
