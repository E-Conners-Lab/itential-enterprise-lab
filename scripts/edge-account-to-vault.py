#!/usr/bin/env python3
"""The time-boxed account dc1-wan01's window uses (ADR 0068 steps 10-11; owner, 2026-10-01): its password, made in
memory, straight into the dev Vault - the entry config-push-revert (and, in step 11, lab-edge and lab-edge-push) bind
through a Gateway alias.

    .venv/bin/python scripts/edge-account-to-vault.py          (make edge-account)       make and store it
    .venv/bin/python scripts/edge-account-to-vault.py --copy   (make edge-account-copy)  put it on the clipboard

The password is never printed, never written to a file and never in argv. `make edge-account` refuses when the entry
exists (cas 0). `--copy` hands it to macOS pbcopy on stdin for the one time the owner types it on the router (the
change set's by-hand step, after the archive so hidekeys masks it); clear the clipboard right after (pbcopy </dev/null).
Under devices/*, so the Gateway reads it and the Platform cannot. The entry, the alias and the account go together
when R1 is signed off. Token: the dev Vault's root token over SSH (vault-dev.yml).
"""

from __future__ import annotations

import importlib.util
import json
import secrets
import string
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("deploy_key", ROOT / "scripts" / "deploy-key-to-vault.py")
dk = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dk)

ROUTER = "dc1-wan01"
ENTRY = dk.V["revert_push"]["targets"][ROUTER]
ALIAS = dk.V["vault"]["dev_gateway_aliases"][ENTRY["password_alias"]]
ALPHABET = string.ascii_letters + string.digits  # nothing IOS-XE's `username ... secret` line or a shell could misread
LENGTH = 32


def stored_value(addr: str, tok: str, data_path: str) -> str | None:
    """The password itself, for the clipboard only (dk.vault returns statuses alone, by design)."""
    req = urllib.request.Request(f"{addr}/v1/{data_path}", headers={"X-Vault-Token": tok})
    try:
        with urllib.request.urlopen(req, context=dk.CTX, timeout=30) as r:
            return (json.load(r).get("data") or {}).get("data", {}).get(ALIAS["key"])
    except urllib.error.HTTPError:
        return None


def main(argv: list[str]) -> int:
    if not ALIAS["path"].startswith("devices/"):
        print(f"{ALIAS['path']} is outside devices/*, which the Gateway reads and the Platform cannot - nothing done")
        return 1
    addr, tok = dk.VAULT["dev"]["url"], dk.token("dev")
    data_path = f"{dk.VAULT['kv_mount']}/data/{ALIAS['path']}"

    if argv == ["--copy"]:
        value = stored_value(addr, tok, data_path)
        if not value:
            print(f"no password at {data_path} (make edge-account first) - nothing copied")
            return 1
        subprocess.run(["pbcopy"], input=value, text=True, check=True)
        print(f"the {ENTRY['username']} password is on the clipboard: paste it into the router's username line, then "
              "clear the clipboard (pbcopy </dev/null)")
        return 0
    if argv:
        print("usage: edge-account-to-vault.py [--copy]")
        return 2

    have = dk.vault(addr, tok, "GET", data_path)
    if have == 200:
        print(f"the dev Vault already holds {ROUTER}'s account at {data_path} - nothing done")
        return 1
    if have != 404:
        print(f"the dev Vault answered {have} for {data_path} (token expired?) - nothing done")
        return 1
    password = "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))
    # cas 0: Vault writes only if nothing is there, so an entry written since the check above is never replaced
    stored = dk.vault(addr, tok, "POST", data_path,
                      {"options": {"cas": 0}, "data": {ALIAS["key"]: password, "username": ENTRY["username"]}})
    if stored != 200:
        print(f"the dev Vault answered {stored} to the write - nothing stored")
        return 1
    print(f"{ROUTER}'s {ENTRY['username']} password is in the dev Vault at {data_path}")
    print("next: the dev converge binds it; in the window, make edge-account-copy, then type the account on the router")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
