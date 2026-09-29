#!/usr/bin/env python3
"""The Gateway's read-only SSH deploy key for the private repo terraform-run clones (ADR 0068 decision 9).

    .venv/bin/python scripts/deploy-key-to-vault.py dev|prod      (make deploy-key TIER=dev|prod)

Makes an ed25519 key pair in memory, writes both halves to the tier's Vault at vault.git.deploy_key_path, then adds the
public half to the GitHub repo as a read-only deploy key titled for the tier. The private key is never printed, never
written to a file and never in argv: it exists in this process's memory and in Vault. Vault is written first, so there
is never a GitHub key whose private half Vault does not hold; if GitHub refuses the key, the Vault entry is deleted
again. Refuses when the tier's Vault already holds a key or the repo already has a key with this tier's title.
Tokens: dev, the dev Vault's root token over SSH (vault-dev.yml); prod, VAULT_TOKEN or the make vault-login file.
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent.parent
V = yaml.safe_load((ROOT / "itential" / "versions.yaml").read_text())
VAULT, GIT = V["vault"], V["vault"]["git"]
CTX = ssl.create_default_context(cafile=str(ROOT / "docs" / "lab-root-ca.crt"))
ADMIN_FILE = Path(os.environ.get("VAULT_ADMIN_FILE", Path.home() / ".config/itential-enterprise-lab/vault-admin-token"))


def token(tier: str) -> str:
    if tier == "prod":
        if os.environ.get("VAULT_TOKEN"):
            return os.environ["VAULT_TOKEN"]
        if ADMIN_FILE.is_file() and ADMIN_FILE.stat().st_size:
            return ADMIN_FILE.read_text().strip()
        sys.exit("no administrator token: run make vault-login first")
    init = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", f"ubuntu@{V['vm']['ip']}",
                           f"sudo cat {VAULT['dev']['dir']}/init.json"], capture_output=True, text=True)
    if init.returncode != 0:
        sys.exit("could not read the dev Vault's root token over SSH (make vault-dev first)")
    return json.loads(init.stdout)["root_token"]


def vault(addr: str, tok: str, method: str, path: str, body: dict | None = None) -> int:
    """The HTTP status only: a response body (which could hold the key on a read) is never returned."""
    req = urllib.request.Request(f"{addr}/v1/{path}", method=method, headers={"X-Vault-Token": tok},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def gh(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args, "-R", GIT["repo"]], capture_output=True, text=True)


def main(tier: str) -> int:
    if tier not in ("dev", "prod"):
        print("usage: deploy-key-to-vault.py dev|prod")
        return 2
    addr = VAULT[tier]["url"]
    tok = token(tier)
    data_path = f"{VAULT['kv_mount']}/data/{GIT['deploy_key_path']}"
    meta_path = f"{VAULT['kv_mount']}/metadata/{GIT['deploy_key_path']}"
    title = f"itential-gateway-{tier} (Vault {GIT['deploy_key_path']})"

    have = vault(addr, tok, "GET", data_path)
    if have == 200:
        print(f"the {tier} Vault already holds a deploy key at {data_path} - nothing done")
        return 1
    if have != 404:
        print(f"the {tier} Vault answered {have} for {data_path} (token expired?) - nothing done")
        return 1
    listed = gh("repo", "deploy-key", "list", "--json", "title")
    if listed.returncode != 0:
        print(f"gh cannot list the deploy keys of {GIT['repo']}: {listed.stderr.strip()}")
        return 1
    if any(k["title"] == title for k in json.loads(listed.stdout or "[]")):
        print(f"{GIT['repo']} already has a deploy key titled {title!r} - nothing done")
        return 1

    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    public = f"{public} itential-gateway-{tier}"
    stored = vault(addr, tok, "POST", data_path, {"data": {"private_key": private, "public_key": public}})
    del key, private
    if stored != 200:
        print(f"the {tier} Vault answered {stored} to the write - nothing stored, nothing added to GitHub")
        return 1

    with tempfile.NamedTemporaryFile("w", suffix=".pub") as pub:  # the public half only
        pub.write(public + "\n")
        pub.flush()
        added = gh("repo", "deploy-key", "add", pub.name, "--title", title)
    if added.returncode != 0:
        removed = vault(addr, tok, "DELETE", meta_path)
        print(f"GitHub refused the key ({added.stderr.strip()}); the Vault entry was deleted again (HTTP {removed})")
        return 1
    fingerprint = subprocess.run(["ssh-keygen", "-lf", "-"], input=public, capture_output=True, text=True).stdout
    print(f"read-only deploy key {title!r} added to {GIT['repo']}; private half in the {tier} Vault at {data_path}")
    print(f"fingerprint: {fingerprint.split()[1] if fingerprint else 'n/a'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
