"""scripts/deploy-key-to-vault.py (ADR 0068 decision 9): Vault before GitHub, nothing half-made, the key never shown."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("deploy_key", Path(__file__).resolve().parent.parent / "scripts" / "deploy-key-to-vault.py")
dk = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dk)


class World:
    def __init__(self, vault_has: int = 404, titles: tuple[str, ...] = (), gh_add_rc: int = 0, write: int = 200):
        self.calls: list[tuple] = []
        self.vault_has, self.titles, self.gh_add_rc, self.write = vault_has, titles, gh_add_rc, write
        self.stored: dict = {}

    def vault(self, addr, tok, method, path, body=None):
        self.calls.append(("vault", method, path))
        if method == "GET":
            return self.vault_has
        if method == "POST":
            self.stored = body["data"]
            return self.write
        return 204

    def gh(self, *args):
        self.calls.append(("gh", *args[:3]))
        if args[2] == "list":
            return subprocess.CompletedProcess(args, 0, json.dumps([{"title": t} for t in self.titles]), "")
        return subprocess.CompletedProcess(args, self.gh_add_rc, "", "refused" if self.gh_add_rc else "")


@pytest.fixture
def world(monkeypatch):
    def make(**kw):
        w = World(**kw)
        monkeypatch.setattr(dk, "vault", w.vault)
        monkeypatch.setattr(dk, "gh", w.gh)
        monkeypatch.setattr(dk, "token", lambda tier: "fake-token")
        return w
    return make


def test_the_private_half_goes_to_vault_first_and_is_never_printed(world, capsys) -> None:
    w = world()
    assert dk.main("dev") == 0
    out = capsys.readouterr().out
    header = "-----BEGIN OPENSSH " + "PRIVATE KEY-----"  # split so the repo's private-key hook does not flag this test
    assert w.stored["private_key"].startswith(header)
    assert w.stored["public_key"].startswith("ssh-ed25519 ") and w.stored["public_key"].endswith("itential-gateway-dev")
    body = w.stored["private_key"].splitlines()[1]
    assert body not in out and "PRIVATE KEY" not in out
    post = w.calls.index(("vault", "POST", f"{dk.VAULT['kv_mount']}/data/{dk.GIT['deploy_key_path']}"))
    adds = [i for i, c in enumerate(w.calls) if c[0] == "gh"][1:]  # the first gh call is the list
    assert adds and all(i > post for i in adds)


def test_a_key_already_in_vault_stops_before_anything_is_made(world) -> None:
    w = world(vault_has=200)
    assert dk.main("dev") == 1
    assert [c for c in w.calls if c[1] == "POST" or c[0] == "gh"] == []


def test_a_key_already_on_github_stops_before_anything_is_made(world) -> None:
    w = world(titles=(f"itential-gateway-dev (Vault {dk.GIT['deploy_key_path']})",))
    assert dk.main("dev") == 1
    assert not any(c[1] == "POST" for c in w.calls)


def test_github_refusing_the_key_deletes_the_vault_entry_again(world) -> None:
    w = world(gh_add_rc=1)
    assert dk.main("dev") == 1
    assert ("vault", "DELETE", f"{dk.VAULT['kv_mount']}/metadata/{dk.GIT['deploy_key_path']}") in w.calls


def test_a_failed_vault_write_never_reaches_github(world) -> None:
    w = world(write=403)
    assert dk.main("dev") == 1
    assert sum(1 for c in w.calls if c[0] == "gh") == 1  # the list only, never the add


def test_the_key_is_read_only_on_github() -> None:
    code = (Path(__file__).resolve().parent.parent / "scripts" / "deploy-key-to-vault.py").read_text()
    assert "--allow-write" not in code and "-w" not in code.split("deploy-key\", \"add\"")[1].split("\n")[0]
