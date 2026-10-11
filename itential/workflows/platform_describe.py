#!/usr/bin/env python3
"""platform-describe: the Platform describes its own workflows (ADR 0080).

A Gateway 5 executable service on this repository. It logs in to the Platform as a read-only account, reads ONE
workflow by name (or, next, one job by id), runs the guide's walker (describe.py, the code the pages in
docs/workflows/ are made with) and prints one JSON object: ok, the name, the counts, the guide as Markdown and as HTML.

  platform_describe.py --action workflow --name "Rotate AWS VPN Key" [--timeout 30]
  platform_describe.py --action job --id <job id>          # ADR 0080 decision 6: not yet

Environment (the Gateway resolves the secret per run; the rest is the runner's, as VAULT_ADDR is):
  PLATFORM_URL        https://itential.lab.internal (required)
  PLATFORM_USER       the read-only account (default svc-copilot)
  PLATFORM_PASSWORD   its password, from the Vault alias (required; never in argv, never printed)
  PLATFORM_CACERT     the CA file for the Platform's TLS (default: VAULT_CACERT; empty: the system's)

Read-only by construction: POST /login and GET only, anywhere else refused before a socket opens. Exit 0 with ok true,
1 otherwise; the workflow evaluates both.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import describe  # noqa: E402 - the walker beside this file

NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -")


class Platform:
    """The Platform API, GET only (plus the login POST). The same guard verify/prod-snapshot.py has."""

    def __init__(self, url: str, user: str, password: str, cacert: str, timeout: float) -> None:
        self.url = url.rstrip("/")
        self.user, self._password, self.timeout = user, password, timeout
        self._tls = ssl.create_default_context(cafile=cacert) if cacert else ssl.create_default_context()
        self._cookie = ""

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, str, list]:
        url = self.url + path
        if not (method == "GET" or (method == "POST" and path == "/login")):
            raise RuntimeError(f"refused: {method} {path} (only GET and POST /login are allowed)")
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self._cookie:
            headers["Cookie"] = self._cookie
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        ctx = self._tls if url.startswith("https://") else None
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                return resp.status, resp.read().decode(), resp.headers.get_all("Set-Cookie") or []
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode(errors="replace"), []

    def login(self) -> str:
        """An empty string when logged in, else why not (never the password)."""
        status, text, cookies = self._request("POST", "/login", {"username": self.user, "password": self._password})
        if status != 200:
            return f"login as {self.user} answered HTTP {status}"
        if cookies:
            self._cookie = "; ".join(c.split(";", 1)[0] for c in cookies)
        else:  # the Platform answers the token itself when no cookie is set
            token = text.strip().strip('"')
            self._cookie = f"token={token}"
        return ""

    def get(self, path: str) -> tuple[int, object]:
        status, text, _ = self._request("GET", path)
        try:
            return status, json.loads(text)
        except ValueError:
            return status, text


def fetch_workflow(api: Platform, name: str) -> tuple[dict | None, str]:
    """The one stored document with this name, or why there is none."""
    status, data = api.get("/automation-studio/workflows?equals[name]=" + urllib.parse.quote(name))
    if status != 200 or not isinstance(data, dict):
        return None, f"the workflow list answered HTTP {status}"
    items = [i for i in data.get("items") or [] if isinstance(i, dict) and i.get("name") == name]
    if not items:
        return None, f"no workflow named {name!r} on this Platform"
    if len(items) > 1:
        return None, f"{len(items)} workflows named {name!r} on this Platform: which one is not decidable"
    return items[0], ""


def describe_workflow(api: Platform, name: str, now: datetime) -> dict:
    if not name or len(name) > 80 or set(name) - NAME_CHARS:
        return {"ok": False, "message": "the name must be 1-80 characters of letters, digits, spaces and dashes"}
    doc, why = fetch_workflow(api, name)
    if doc is None:
        return {"ok": False, "message": why}
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    return describe.describe_document({
        "document": doc,
        "runs": [f"Started by hand or by an automation on this Platform; this description was read from the stored "
                 f"document at {stamp} by platform-describe, as {api.user}."],
        "source": f"Described from the document as the Platform stores it, read at {stamp}.",
    })


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="the Platform describes its own workflows (ADR 0080)")
    parser.add_argument("--action", required=True, choices=["workflow", "job"])
    parser.add_argument("--name", default="", help="the workflow's name (action workflow)")
    parser.add_argument("--id", default="", help="the job's id (action job)")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds per Platform call")
    args = parser.parse_args(argv)

    url, user = os.environ.get("PLATFORM_URL", ""), os.environ.get("PLATFORM_USER", "svc-copilot")
    password = os.environ.get("PLATFORM_PASSWORD", "")
    cacert = os.environ.get("PLATFORM_CACERT", os.environ.get("VAULT_CACERT", ""))
    missing = [n for n, v in (("PLATFORM_URL", url), ("PLATFORM_PASSWORD", password)) if not v]
    if missing:
        out = {"ok": False, "message": "the runner has no " + " and ".join(missing) + ": nothing was read"}
    elif args.action == "job":
        out = {"ok": False, "message": "action job is not built yet (ADR 0080 decision 6)"}
    else:
        api = Platform(url, user, password, cacert, args.timeout)
        try:
            why = api.login()
            out = {"ok": False, "message": why} if why else describe_workflow(api, args.name, datetime.now(timezone.utc))
        except (urllib.error.URLError, OSError, RuntimeError) as exc:
            out = {"ok": False, "message": f"the Platform could not be reached or refused: {type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(out) + "\n")
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
