#!/usr/bin/env python3
"""The time-boxed account dc1-wan01's window uses (ADR 0068 steps 10-11; owner, 2026-10-01): its password, made in
memory, straight into the tier's Vault - the entry config-push-revert, lab-edge and lab-edge-push bind through a
Gateway alias. Each tier's Vault holds its own (ADR 0070: production runs the AWS VPN since 2026-10-04).

    .venv/bin/python scripts/edge-account-to-vault.py dev|prod          (make edge-account TIER=...)       store it
    .venv/bin/python scripts/edge-account-to-vault.py dev|prod --line   (make edge-account-line TIER=...)  its line

The password is never printed, never written to a file, never in argv - and never leaves this script and Vault: the
router gets only its Cisco type-9 (scrypt) hash. `--line` salts and hashes it here (N=16384, r=1, p=1, 14-character
salt, Cisco's base64 alphabet: proved against a hash clab-rtr1 stores, 2026-10-01) and puts
`username ... privilege 15 secret 9 $9$...` on the macOS clipboard, for the change set's by-hand step. So the plain
password is in no clipboard history, no terminal and no CLI history (step 10 review). `make edge-account` refuses when
the entry exists (cas 0).
Under devices/*, so the Gateway reads it and the Platform cannot. The entry, the alias and the account go together
when R1 is signed off. Token: the dev Vault's root token over SSH (vault-dev.yml), production's administrator token
(make vault-login).
"""

from __future__ import annotations

import base64
import hashlib
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
ALIAS = dk.V["vault"]["edge_gateway_aliases"][ENTRY["password_alias"]]
ALPHABET = string.ascii_letters + string.digits  # nothing IOS-XE's `username ... secret` line or a shell could misread
LENGTH = 32
# Cisco type 9: scrypt with these costs, a 14-character salt and the hash in Cisco's base64 alphabet, no padding
STD_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
CISCO_B64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def type9(password: str, salt: str) -> str:
    key = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=1, p=1, dklen=32)
    return "$9$" + salt + "$" + base64.b64encode(key).decode().rstrip("=").translate(str.maketrans(STD_B64, CISCO_B64))


def account_line(password: str) -> str:
    salt = "".join(secrets.choice(CISCO_B64) for _ in range(14))
    return f"username {ENTRY['username']} privilege 15 secret 9 {type9(password, salt)}"


def stored_value(addr: str, tok: str, data_path: str) -> tuple[int, str | None]:
    """(HTTP status, the password) - read only to hash it here; dk.vault returns statuses alone, by design."""
    req = urllib.request.Request(f"{addr}/v1/{data_path}", headers={"X-Vault-Token": tok})
    try:
        with urllib.request.urlopen(req, context=dk.CTX, timeout=30) as r:
            return r.status, ((json.load(r).get("data") or {}).get("data") or {}).get(ALIAS["key"])
    except urllib.error.HTTPError as e:
        return e.code, None


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("dev", "prod") or argv[1:] not in ([], ["--line"]):
        print("usage: edge-account-to-vault.py dev|prod [--line]")
        return 2
    tier, argv = argv[0], argv[1:]
    if not ALIAS["path"].startswith("devices/"):
        print(f"{ALIAS['path']} is outside devices/*, which the Gateway reads and the Platform cannot - nothing done")
        return 1
    addr, tok = dk.VAULT[tier]["url"], dk.token(tier)
    data_path = f"{dk.VAULT['kv_mount']}/data/{ALIAS['path']}"

    if argv == ["--line"]:
        status, value = stored_value(addr, tok, data_path)
        if status != 200:
            print(f"the {tier} Vault answered {status} for {data_path} "
                  f"({'make edge-account first' if status == 404 else 'token expired?'}) - nothing copied")
            return 1
        if not value:
            print(f"{data_path} has no {ALIAS['key']} - nothing copied")
            return 1
        subprocess.run(["pbcopy"], input=account_line(value), text=True, check=True)
        print(f"the {ENTRY['username']} account line (type-9 hash only) is on the clipboard: paste it on {ROUTER} in "
              "configuration mode")
        return 0

    have = dk.vault(addr, tok, "GET", data_path)
    if have == 200:
        print(f"the {tier} Vault already holds {ROUTER}'s account at {data_path} - nothing done")
        return 1
    if have != 404:
        print(f"the {tier} Vault answered {have} for {data_path} (token expired?) - nothing done")
        return 1
    password = "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))
    # cas 0: Vault writes only if nothing is there, so an entry written since the check above is never replaced
    stored = dk.vault(addr, tok, "POST", data_path,
                      {"options": {"cas": 0}, "data": {ALIAS["key"]: password, "username": ENTRY["username"]}})
    if stored != 200:
        hint = " (a soft-deleted entry still blocks it: vault kv metadata delete removes it for good)" if stored == 400 else ""
        print(f"the {tier} Vault answered {stored} to the write - nothing stored{hint}")
        return 1
    print(f"{ROUTER}'s {ENTRY['username']} password is in the {tier} Vault at {data_path}")
    print(f"next: the {tier} converge binds it; then make edge-account-line TIER={tier} and paste it on the router")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
