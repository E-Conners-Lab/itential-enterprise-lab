#!/usr/bin/env python3
"""The tunnel key the clab AWS twin and clab-rtr1 share (AWS VPN step 6), straight into the dev Vault.

    .venv/bin/python scripts/twin-key-to-vault.py      (make twin-key)

Dev only: the twin is a dev-tier stand-in for the AWS box, and production's key is the real deployment's (Secrets
Manager, then vault.aws.psk_path). Makes a 40-character alphanumeric key and a version id in memory and writes both to
the dev Vault at aws_vpn.targets.<twin router>.psk_path, the entry the Gateway resolves for Hand Off
(vault.edge_gateway_aliases) and the clab play copies into the twin. The key is never printed, never written to a file
and never in argv. Refuses when the entry already exists: a new key is a rotation, and a rotation is Hand Off's to
carry to the router, not this script's. Token: the dev Vault's root token over SSH (vault-dev.yml).
"""

from __future__ import annotations

import importlib.util
import secrets
import string
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# the same token read and status-only Vault call as the deploy key's (ADR 0068 decision 9)
_SPEC = importlib.util.spec_from_file_location("deploy_key", ROOT / "scripts" / "deploy-key-to-vault.py")
dk = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dk)

CLAB = dk.yaml.safe_load((ROOT / "clab" / "versions.yaml").read_text())
ROUTER = CLAB["aws_twin"]["router"]
PSK_PATH = dk.V["aws_vpn"]["targets"][ROUTER]["psk_path"]
ALPHABET = string.ascii_letters + string.digits  # inside IOS XE's keyring line and strongSwan's secret = "..."
LENGTH = 40

token, vault = dk.token, dk.vault


def main() -> int:
    if not PSK_PATH.startswith("devices/"):
        print(f"{PSK_PATH} is outside devices/*, which the Gateway reads and the Platform cannot - nothing done")
        return 1
    addr, tok = dk.VAULT["dev"]["url"], token("dev")
    data_path = f"{dk.VAULT['kv_mount']}/data/{PSK_PATH}"

    have = vault(addr, tok, "GET", data_path)
    if have == 200:
        print(f"the dev Vault already holds the twin's key at {data_path} - nothing done")
        return 1
    if have != 404:
        print(f"the dev Vault answered {have} for {data_path} (token expired?) - nothing done")
        return 1

    key = "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))
    version = str(uuid.uuid4())
    # cas 0: Vault writes only if nothing is there, so a key written since the check above is never replaced
    stored = vault(addr, tok, "POST", data_path, {"options": {"cas": 0}, "data": {"psk": key, "version": version}})
    if stored != 200:
        print(f"the dev Vault answered {stored} to the write - nothing stored")
        return 1
    print(f"the twin's key for {ROUTER} is in the dev Vault at {data_path} (version {version})")
    print("next: playbooks/clab-dev.yml copies it into the twin; Hand Off carries it to the router")
    return 0


if __name__ == "__main__":
    sys.exit(main())
