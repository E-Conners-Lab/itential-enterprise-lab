#!/usr/bin/env python3
"""netops-knowledge's two secrets, straight into production's Vault (ADR 0074).

    .venv/bin/python scripts/knowledge-secrets-to-vault.py token            (make knowledge-token)
    .venv/bin/python scripts/knowledge-secrets-to-vault.py token --rotate   (make knowledge-token ROTATE=1)
    .venv/bin/python scripts/knowledge-secrets-to-vault.py pull-token       (make knowledge-pull-token)

token: the bearer token FlowMCP sends, made in memory (secrets.token_urlsafe(32)) and written to
vault.knowledge.token_path. It is never printed, never in a file and never in argv. Without --rotate it refuses when
an entry exists (check-and-set 0); --rotate replaces it, after which `make netops-knowledge` gives the pod the new
hash and the Gateway play re-registers the server (ADR 0074, the 90-day rotation).

pull-token: a GitHub token with read:packages only, which k3s pulls the private image with. The owner creates it on
GitHub and pastes it here (no echo). It is checked before it is written (ADR 0074 lab PR B): a classic token (`ghp_`,
the kind GHCR accepts), and GHCR must serve the PINNED image's manifest with it (a real pull's first step). Only then
does it go, with the GitHub user name, to vault.knowledge.pull_path.

Token: VAULT_TOKEN or the make vault-login file (production only: the service runs on production, ADR 0070).
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import secrets
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
V = yaml.safe_load((ROOT / "itential" / "versions.yaml").read_text())
VAULT, KNOWLEDGE = V["vault"], V["vault"]["knowledge"]
CTX = ssl.create_default_context(cafile=str(ROOT / "docs" / "lab-root-ca.crt"))
ADMIN_FILE = Path(os.environ.get("VAULT_ADMIN_FILE", Path.home() / ".config/itential-enterprise-lab/vault-admin-token"))


def admin_token() -> str:
    if os.environ.get("VAULT_TOKEN"):
        return os.environ["VAULT_TOKEN"]
    if ADMIN_FILE.is_file() and ADMIN_FILE.stat().st_size:
        return ADMIN_FILE.read_text().strip()
    sys.exit("no administrator token: run make vault-login first")


def vault(tok: str, method: str, path: str, body: dict | None = None) -> int:
    """The HTTP status only: a response body (which could hold a secret on a read) is never returned."""
    req = urllib.request.Request(f"{VAULT['prod']['url']}/v1/{path}", method=method, headers={"X-Vault-Token": tok},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def write(tok: str, path: str, data: dict[str, str], *, replace: bool) -> int:
    data_path = f"{VAULT['kv_mount']}/data/{path}"
    have = vault(tok, "GET", data_path)
    if have not in (200, 404):
        print(f"production Vault answered {have} for {data_path} (token expired?) - nothing done")
        return 1
    if have == 200 and not replace:
        print(f"production Vault already holds {data_path} - nothing done (ROTATE=1 replaces the token)")
        return 1
    # cas 0 when creating: Vault writes only if nothing is there, so an entry written since the check is never replaced
    body: dict = {"data": data} | ({} if replace else {"options": {"cas": 0}})
    stored = vault(tok, "POST", data_path, body)
    if stored != 200:
        print(f"production Vault answered {stored} to the write - nothing stored")
        return 1
    print(f"{'replaced' if have == 200 else 'stored'} {data_path} ({', '.join(sorted(data))}); the value was not shown")
    return 0


def pull_problem(username: str, pull: str) -> str | None:
    """Why GHCR would not let k3s pull the pinned image with this token, or None. Never shows the token."""
    if not pull.startswith("ghp_"):
        return "not a classic token (ghp_...): GHCR pulls need a classic token with read:packages"
    image = V["netops_knowledge"]["image"]
    host, _, repo = image["repository"].partition("/")
    public = ssl.create_default_context()  # ghcr.io: the public CAs, not the lab's
    basic = base64.b64encode(f"{username}:{pull}".encode()).decode()
    try:
        req = urllib.request.Request(f"https://{host}/token?scope=repository:{repo}:pull&service={host}",
                                     headers={"Authorization": f"Basic {basic}"})
        with urllib.request.urlopen(req, context=public, timeout=30) as r:
            bearer = json.load(r).get("token", "")
        req = urllib.request.Request(f"https://{host}/v2/{repo}/manifests/{image['digest']}", method="HEAD",
                                     headers={"Authorization": f"Bearer {bearer}", "Accept": ", ".join((
                                         "application/vnd.oci.image.index.v1+json",
                                         "application/vnd.oci.image.manifest.v1+json",
                                         "application/vnd.docker.distribution.manifest.v2+json"))})
        with urllib.request.urlopen(req, context=public, timeout=30) as r:
            return None if r.status == 200 else f"GHCR answered {r.status} for the pinned image"
    except urllib.error.HTTPError as e:
        return f"GHCR answered {e.code}: the token cannot pull {image['repository']} (read:packages, the right user?)"
    except (urllib.error.URLError, OSError) as e:
        return f"GHCR could not be reached ({type(e).__name__}): nothing checked"


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("token", "pull-token"):
        print("usage: knowledge-secrets-to-vault.py token [--rotate] | pull-token")
        return 2
    tok = admin_token()
    if argv[0] == "token":
        return write(tok, KNOWLEDGE["token_path"], {"token": secrets.token_urlsafe(32)}, replace="--rotate" in argv)
    username = input("GitHub user name the read:packages token belongs to: ").strip()
    pull = getpass.getpass("read:packages token (not echoed): ").strip()
    if not username or not pull:
        print("both are needed - nothing done")
        return 1
    problem = pull_problem(username, pull)
    if problem:
        print(f"{problem} - nothing stored")
        return 1
    print("GHCR serves the pinned image with this token")
    return write(tok, KNOWLEDGE["pull_path"], {"username": username, "token": pull}, replace=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
