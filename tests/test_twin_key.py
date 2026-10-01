"""scripts/twin-key-to-vault.py (AWS VPN step 6): dev only, never replaces a key, never shows one, and writes what
lab-edge-push and the twin both accept."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("twin_key", ROOT / "scripts" / "twin-key-to-vault.py")
tk = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tk)

# cloud-devops-pipeline itential/lab-edge-push.py PSK and VERSION (the twin's twin.sh takes the same key set)
PUSH_PSK = re.compile(r"[A-Za-z0-9._+/=-]{32,}")
PUSH_VERSION = re.compile(r"[A-Za-z0-9-]{1,64}")


class World:
    def __init__(self, vault_has: int = 404, write: int = 200):
        self.calls: list[tuple] = []
        self.vault_has, self.write, self.body, self.tiers = vault_has, write, {}, []

    def token(self, tier):
        self.tiers.append(tier)
        return "fake-token"

    def vault(self, addr, tok, method, path, body=None):
        self.calls.append((addr, method, path))
        if method == "GET":
            return self.vault_has
        self.body = body
        return self.write


@pytest.fixture
def world(monkeypatch):
    def make(**kw):
        w = World(**kw)
        monkeypatch.setattr(tk, "vault", w.vault)
        monkeypatch.setattr(tk, "token", w.token)
        return w
    return make


def test_the_key_goes_to_the_dev_vault_at_the_twin_routers_path_and_is_never_printed(world, capsys) -> None:
    w = world()
    assert tk.main() == 0
    path = f"{tk.dk.VAULT['kv_mount']}/data/{tk.dk.V['aws_vpn']['targets']['clab-rtr1']['psk_path']}"
    assert w.tiers == ["dev"] and all(c[0] == tk.dk.VAULT["dev"]["url"] for c in w.calls)
    assert w.calls == [(tk.dk.VAULT["dev"]["url"], "GET", path), (tk.dk.VAULT["dev"]["url"], "POST", path)]
    data = w.body["data"]
    assert set(data) == {"psk", "version"}
    assert data["psk"] not in capsys.readouterr().out


def test_what_is_written_is_what_lab_edge_push_and_the_twin_accept(world) -> None:
    keys = set()
    for _ in range(20):
        w = world()
        assert tk.main() == 0
        psk, version = w.body["data"]["psk"], w.body["data"]["version"]
        assert PUSH_PSK.fullmatch(psk) and re.fullmatch(r"[A-Za-z0-9]{40}", psk)
        assert PUSH_VERSION.fullmatch(version)
        keys.add(psk)
    assert len(keys) == 20  # a fresh key every time


def test_a_key_already_in_vault_is_never_replaced(world) -> None:
    w = world(vault_has=200)
    assert tk.main() == 1
    assert [c[1] for c in w.calls] == ["GET"]
    w = world()
    assert tk.main() == 0
    assert w.body["options"] == {"cas": 0}  # nor one written since the check


def test_an_unreadable_vault_writes_nothing(world) -> None:
    w = world(vault_has=403)
    assert tk.main() == 1
    assert [c[1] for c in w.calls] == ["GET"]


def test_a_refused_write_reports_failure(world) -> None:
    world(write=400)
    assert tk.main() == 1


def test_the_script_never_touches_production() -> None:
    code = (ROOT / "scripts" / "twin-key-to-vault.py").read_text()
    assert '"prod"' not in code and "VAULT_TOKEN" not in code and 'token("dev")' in code
    assert "secrets.choice" in code and "random." not in code  # the CSPRNG, not random
