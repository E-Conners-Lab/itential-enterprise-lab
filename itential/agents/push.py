#!/usr/bin/env python3
"""Push the prompt of each itential/agents/*.yaml document to the live Platform, and nothing else.

`flowai.yml` is the full path: it resolves every tool reference to a Tool Registry referenceId,
creates the project, re-syncs roles and then creates or updates the agent. That is what to run
after a workflow import, because a re-imported workflow gets a new uuid and the agent's tool
references have to be resolved again (ADR 0038).

This script is the narrow path for the common case: the prompt text changed and nothing else did.
It PATCHes `prompt.instructions` and `prompt.inputSchema` on an agent that already exists - the same
call the play's "Agent update in place" task makes - and deliberately touches no tool, no provider
and no operator, so it cannot break a tool binding. If an agent in the documents does not exist on
the Platform yet, it is reported and skipped: creating one needs the resolved tool ids, which is the
play's job.

    .venv/bin/python itential/agents/push.py --check            # diff every document, write nothing
    .venv/bin/python itential/agents/push.py netbox-sot-local   # push one
    .venv/bin/python itential/agents/push.py                    # push every document that differs

Reads .env the way the verify scripts do (ITENTIAL_ADMIN_USER, ITENTIAL_ADMIN_PASSWORD); the
Platform is https://itential.lab.internal with docs/lab-root-ca.crt, overridable with PLATFORM/CA.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
AGENTS = ROOT / "itential" / "agents"
AGENT_SERVICE = "agent-project-service"


def load_env() -> None:
    """`set -a; . ./.env; set +a`, for the keys we need and without overriding a real environment."""
    env = ROOT / ".env"
    if not env.exists():
        sys.exit("missing .env")
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class Platform:
    """Cookie-session client: POST /login once, then carry the cookie."""

    def __init__(self, base: str, ca: str | None) -> None:
        self.base = base.rstrip("/")
        ctx = ssl.create_default_context(cafile=ca) if ca else None
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()),
            urllib.request.HTTPSHandler(context=ctx) if ctx else urllib.request.HTTPSHandler(),
        )

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with self.opener.open(req, timeout=60) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:  # the Platform puts the reason in the body
            sys.exit(f"{method} {path} -> {e.code}: {e.read().decode()[:400]}")
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {"raw": raw.decode(errors="replace")}  # /login answers a bare token

    def login(self, user: str, password: str) -> None:
        self.call("POST", "/login", {"username": user, "password": password})

    def agents(self) -> dict[str, dict]:
        items = self.call("GET", f"/{AGENT_SERVICE}/operable-agents")["data"]["items"]
        return {a["name"]: a for a in items}


def diff(name: str, field: str, live: str, want: str) -> bool:
    """Print a unified diff of one field; True when they differ."""
    if live == want:
        return False
    print(f"--- {name}.{field} (live)\n+++ {name}.{field} (itential/agents/{name}.yaml)")
    for line in difflib.unified_diff(
        live.splitlines(), want.splitlines(), lineterm="", n=1
    ):
        if not line.startswith(("---", "+++")):
            print(f"  {line}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*", help="agent names; default every document")
    ap.add_argument("--check", action="store_true", help="show the diff, write nothing")
    args = ap.parse_args()

    load_env()
    platform = Platform(
        os.environ.get("PLATFORM", "https://itential.lab.internal"),
        os.environ.get("CA", str(ROOT / "docs" / "lab-root-ca.crt")),
    )
    platform.login(
        os.environ.get("ITENTIAL_ADMIN_USER", "admin@itential"),
        os.environ["ITENTIAL_ADMIN_PASSWORD"],
    )
    live = platform.agents()

    docs = {p.stem: yaml.safe_load(p.read_text()) for p in sorted(AGENTS.glob("*.yaml"))}
    wanted = args.names or sorted(docs)
    changed, skipped = [], []

    for name in wanted:
        if name not in docs:
            sys.exit(f"no document itential/agents/{name}.yaml")
        doc = docs[name]
        if name not in live:
            skipped.append(name)
            continue
        now = live[name]
        differs = diff(name, "instructions", now.get("instructions", ""), doc["instructions"])
        differs |= diff(
            name, "inputSchema",
            json.dumps(now.get("inputSchema", {}), indent=2, sort_keys=True),
            json.dumps(doc["input_schema"], indent=2, sort_keys=True),
        )
        differs |= diff(name, "description", now.get("description", ""), doc["description"])
        if not differs:
            continue
        changed.append(name)
        if args.check:
            continue
        platform.call(
            "PATCH", f"/{AGENT_SERVICE}/agents/{now['_id']}",
            {
                "description": doc["description"],
                "prompt": {"instructions": doc["instructions"], "inputSchema": doc["input_schema"]},
            },
        )

    if not args.check and changed:
        # Read back: the PATCH answering 200 is not proof the prompt stored.
        after = platform.agents()
        for name in changed:
            stored = after[name].get("instructions", "")
            if stored != docs[name]["instructions"]:
                sys.exit(f"{name}: PATCH returned 200 but the stored prompt still differs")
        print(f"\npushed and verified: {', '.join(changed)}")
    elif args.check:
        print(f"\nwould push: {', '.join(changed) if changed else 'nothing'}")
    else:
        print("\nnothing to push: every document matches the Platform")

    if skipped:
        print(f"not on the Platform, run the flowai play to create: {', '.join(skipped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
