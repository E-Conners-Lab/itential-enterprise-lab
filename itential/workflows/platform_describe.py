#!/usr/bin/env python3
"""platform-describe: the Platform describes its own workflows (ADR 0080).

A Gateway 5 executable service on this repository. It logs in to the Platform as a read-only account, reads ONE
workflow by name or ONE job by id (and, for a job, the stored workflow it ran), or the list of every workflow, runs the
guide's walker (describe.py, the code the pages in docs/workflows/ are made with) and prints one JSON object: ok, the
name, the counts, the guide as Markdown and as HTML; for a job, the path it took, where it is or stopped and what it
said; for the list, one line per workflow (ADR 0081: the agent's view).

  platform_describe.py --action workflow --name "Rotate AWS VPN Key" [--timeout 30]
  platform_describe.py --action job --id 6d43c80a53b14be58f8f8c8d [--timeout 30]     # ADR 0080 decision 6
  platform_describe.py --action list --which all                                   # ADR 0081: every workflow, one line each
  platform_describe.py --action workflow|job --which <name or id>                  # the agent's one argument (ADR 0081)

Environment (the Gateway resolves the secret per run; the rest is the runner's, as VAULT_ADDR is):
  PLATFORM_URL        https://itential.lab.internal (required)
  PLATFORM_USER       the read-only account (default svc-copilot)
  PLATFORM_PASSWORD   its password, from the Vault alias (required; never in argv, never printed)
  PLATFORM_CACERT     the CA file for the Platform's TLS (default: VAULT_CACERT; empty: the system's)
  PLATFORM_CLIENT_ID, PLATFORM_CLIENT_SECRET
                      Itential Cloud: a service account's client credentials, traded at POST /oauth/token for a
                      bearer token. When either is set both are required and the password is not used (the cloud has
                      no /login for a service account). From Vault aliases, like the password.

Read-only by construction: GET, plus the one login POST (/login, or /oauth/token for client credentials), anywhere else
refused before a socket opens. Exit 0 with ok true, 1 otherwise; the workflow evaluates both.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import describe  # noqa: E402 - the walker beside this file

NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -")
JOB_ID = re.compile(r"^[0-9a-f]{24}$")  # a Mongo object id, as the Platform shows one
LOGIN_POSTS = {"/login", "/oauth/token"}  # the only POSTs: a password login, or a client-credentials token


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib copies every header but the content ones onto it (the session cookie and the
    bearer token included), to whatever host it names. A 3xx comes back as its own answer instead."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


class Platform:
    """The Platform API, GET only (plus the login POST). The same guard verify/prod-snapshot.py has."""

    def __init__(self, url: str, user: str, password: str, cacert: str, timeout: float, client_id: str = "",
                 client_secret: str = "") -> None:
        self.url = url.rstrip("/")
        self.user, self._password, self.timeout = user, password, timeout
        self._client = (client_id, client_secret)
        self._tls = ssl.create_default_context(cafile=cacert) if cacert else ssl.create_default_context()
        self._open = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=self._tls)).open
        self._cookie = ""
        self._bearer = ""

    def _request(self, method: str, path: str, body: dict | None = None,
                 form: dict | None = None) -> tuple[int, str, list]:
        url = self.url + path
        if not (method == "GET" or (method == "POST" and path in LOGIN_POSTS)):
            raise RuntimeError(f"refused: {method} {path} (only GET and the login POST are allowed)")
        headers = {"Accept": "application/json"}
        if form is not None:  # OAuth's token endpoint takes a form, not JSON
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode() if body is not None else None
            if data is not None:
                headers["Content-Type"] = "application/json"
        if self._cookie:
            headers["Cookie"] = self._cookie
        if self._bearer:
            headers["Authorization"] = f"Bearer {self._bearer}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._open(req, timeout=self.timeout) as resp:
                return resp.status, resp.read().decode(), resp.headers.get_all("Set-Cookie") or []
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode(errors="replace"), []

    def login(self) -> str:
        """An empty string when logged in, else why not (never the password or the client secret)."""
        if self._client[0]:
            return self._token()
        status, text, cookies = self._request("POST", "/login", {"username": self.user, "password": self._password})
        if status != 200:
            return f"login as {self.user} answered HTTP {status}"
        if cookies:
            self._cookie = "; ".join(c.split(";", 1)[0] for c in cookies)
        else:  # the Platform answers the token itself when no cookie is set
            token = text.strip().strip('"')
            self._cookie = f"token={token}"
        return ""

    def _token(self) -> str:
        """Itential Cloud: the service account's client credentials for a bearer token, sent on every GET after."""
        client_id, client_secret = self._client
        status, text, _ = self._request("POST", "/oauth/token", form={
            "grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret})
        if status != 200:
            return f"the token request answered HTTP {status}"
        try:
            token = json.loads(text).get("access_token")
        except (ValueError, AttributeError):
            token = None
        if not isinstance(token, str) or not token:
            return "the token request answered no access_token"
        self._bearer = token
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


def fetch_job(api: Platform, job_id: str) -> tuple[dict | None, str]:
    """One job record by id, or why there is none."""
    status, data = api.get("/operations-manager/jobs/" + job_id)
    if status == 404:
        return None, f"no job {job_id} on this Platform"
    if status != 200 or not isinstance(data, dict) or not isinstance(data.get("data"), dict):
        return None, f"the job answered HTTP {status}"
    return data["data"], ""


def describe_job(api: Platform, job_id: str, now: datetime) -> dict:
    """The run laid over the steps (ADR 0080 decision 6): the job record, plus the stored workflow for the words when
    it can be read; a stored workflow that is gone or changed is a note in the answer, not a failure."""
    if not JOB_ID.match(job_id):
        return {"ok": False, "message": "the job id must be 24 hexadecimal characters, as the Platform shows it"}
    job, why = fetch_job(api, job_id)
    if job is None:
        return {"ok": False, "message": why}
    name = str(job.get("name") or "")
    doc = fetch_workflow(api, name)[0] if name and len(name) <= 80 and not set(name) - NAME_CHARS else None
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    return describe.describe_job_document({
        "job": job, "document": doc, "now": now.timestamp() * 1000,
        "read": f"Read from the Platform's job record at {stamp} by platform-describe, as {api.user}.",
    })


def fetch_workflows(api: Platform) -> tuple[list, str]:
    """Every stored document, or why the list could not be read."""
    status, data = api.get("/automation-studio/workflows?limit=200")
    if status != 200 or not isinstance(data, dict):
        return [], f"the workflow list answered HTTP {status}"
    return [i for i in data.get("items") or [] if isinstance(i, dict)], ""


def describe_list(api: Platform, now: datetime) -> dict:
    """Every workflow on one line (ADR 0081): the agent's first call when a request names a workflow loosely."""
    docs, why = fetch_workflows(api)
    if why:
        return {"ok": False, "message": why}
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    return describe.describe_list({
        "documents": docs,
        "read": f"Read from the Platform's stored documents at {stamp} by platform-describe, as {api.user}.",
    })


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="the Platform describes its own workflows (ADR 0080)")
    parser.add_argument("--action", required=True, choices=["workflow", "job", "list"])
    parser.add_argument("--name", default="", help="the workflow's name (action workflow)")
    parser.add_argument("--id", default="", help="the job's id (action job)")
    parser.add_argument("--which", default="", help="the name or the id, whichever the action takes (ADR 0081); "
                                                    "ignored by the list")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds per Platform call")
    args = parser.parse_args(argv)

    url, user = os.environ.get("PLATFORM_URL", ""), os.environ.get("PLATFORM_USER", "svc-copilot")
    password = os.environ.get("PLATFORM_PASSWORD", "")
    client_id, client_secret = os.environ.get("PLATFORM_CLIENT_ID", ""), os.environ.get("PLATFORM_CLIENT_SECRET", "")
    cacert = os.environ.get("PLATFORM_CACERT", os.environ.get("VAULT_CACERT", ""))
    # client credentials when either half is set (Itential Cloud), the password otherwise
    credential = ((("PLATFORM_CLIENT_ID", client_id), ("PLATFORM_CLIENT_SECRET", client_secret))
                  if client_id or client_secret else (("PLATFORM_PASSWORD", password),))
    missing = [n for n, v in (("PLATFORM_URL", url), *credential) if not v]
    if missing:
        out = {"ok": False, "message": "the runner has no " + " and ".join(missing) + ": nothing was read"}
    else:
        api = Platform(url, user, password, cacert, args.timeout, client_id, client_secret)
        try:
            why = api.login()
            if why:
                out = {"ok": False, "message": why}
            elif args.action == "list":
                out = describe_list(api, datetime.now(timezone.utc))
            elif args.action == "job":
                out = describe_job(api, args.which or args.id, datetime.now(timezone.utc))
            else:
                out = describe_workflow(api, args.which or args.name, datetime.now(timezone.utc))
        except (urllib.error.URLError, OSError, RuntimeError) as exc:
            out = {"ok": False, "message": f"the Platform could not be reached or refused: {type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(out) + "\n")
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
