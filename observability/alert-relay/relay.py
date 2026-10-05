#!/usr/bin/env python3
"""Alertmanager to Operations Manager relay (R6, ADR 0072).

Alertmanager cannot call an endpoint trigger itself: the Platform takes the session token only as a `?token=` URL
parameter (a Bearer header is refused, Basic auth needs TLS at the Platform, and the load balancer terminates it;
probes 2026-10-05), and tokens expire. This service takes Alertmanager's v4 webhook on POST /alert, keeps only firing
alerts named in its route table, checks every label value it forwards against a pattern, logs in to the Platform and
starts the route's endpoint trigger. A Platform refusal answers 502, so Alertmanager retries; the token is never
logged. Stdlib only, like the Platform exporter it sits beside.

Environment: ITENTIAL_URL, ITENTIAL_USER, ITENTIAL_PASSWORD, ITENTIAL_CA (PEM file), RELAY_ROUTES (JSON
{"<alertname>": "<endpoint route>"}), PORT (9121).
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer

MAX_BODY = 64 * 1024
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
DEVICE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
INTERFACE = re.compile(r"[A-Za-z][A-Za-z0-9/.:-]{0,63}")
FINGERPRINT = re.compile(r"[0-9a-f]{1,32}")
STARTS = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?Z")


@dataclass(frozen=True)
class Config:
    url: str
    user: str
    password: str
    ca: str | None
    routes: dict[str, str]


def _alert_payload(alert: dict) -> dict | None:
    """The fields a workflow gets: each checked, or the alert is dropped."""
    labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
    starts = STARTS.fullmatch(str(alert.get("startsAt", "")))
    fields = {
        "alertname": (NAME, labels.get("alertname")),
        "device": (DEVICE, labels.get("device")),
        "interface": (INTERFACE, labels.get("ifDescr")),
        "fingerprint": (FINGERPRINT, alert.get("fingerprint")),
    }
    out = {}
    for key, (pattern, value) in fields.items():
        if not isinstance(value, str) or not pattern.fullmatch(value):
            return None
        out[key] = value
    if not starts:
        return None
    out["starts_at"] = starts.group(1) + "Z"
    return out


def trigger_calls(doc: dict, routes: dict[str, str]) -> list[tuple[str, dict]]:
    """(endpoint route, body) for every firing, allow-listed, well-formed alert in an Alertmanager v4 document."""
    if not isinstance(doc, dict) or doc.get("version") != "4" or not isinstance(doc.get("alerts"), list):
        return []
    calls = []
    for alert in doc["alerts"]:
        if not isinstance(alert, dict) or alert.get("status") != "firing":
            continue
        payload = _alert_payload(alert)
        if payload and payload["alertname"] in routes:
            calls.append((routes[payload["alertname"]], payload))
    return calls


def _post(cfg: Config, path: str, body: dict) -> bytes:
    ctx = ssl.create_default_context(cafile=cfg.ca) if cfg.ca else None
    req = urllib.request.Request(cfg.url + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, context=ctx, timeout=60) as r:
        return r.read()


def deliver(cfg: Config, calls: list[tuple[str, dict]]) -> bool:
    """Logs in once per delivery (alerts are rare) and starts each route's trigger; False on any refusal."""
    if not calls:
        return True
    try:
        token = _post(cfg, "/login", {"username": cfg.user, "password": cfg.password}).decode().strip().strip('"')
        for route, body in calls:
            _post(cfg, f"/operations-manager/triggers/endpoint/{route}?token={token}", body)
            print(f"relayed {body['alertname']} {body['device']} {body['interface']} -> {route}", flush=True)
    except (urllib.error.URLError, OSError, ValueError) as e:
        # the class only: a URL in the message would carry the token
        print(f"relay failed: {type(e).__name__} {getattr(e, 'code', '')}".strip(), flush=True)
        return False
    return True


def handler(cfg: Config) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):  # no request lines in the log
            pass

        def _answer(self, code: int) -> None:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            self._answer(200 if self.path == "/-/healthy" else 404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            if self.path != "/alert" or not 0 < length <= MAX_BODY:
                return self._answer(400)
            try:
                doc = json.loads(self.rfile.read(length))
            except ValueError:
                return self._answer(400)
            self._answer(200 if deliver(cfg, trigger_calls(doc, cfg.routes)) else 502)

    return Handler


def main() -> None:
    cfg = Config(os.environ["ITENTIAL_URL"].rstrip("/"), os.environ["ITENTIAL_USER"], os.environ["ITENTIAL_PASSWORD"],
                 os.environ.get("ITENTIAL_CA"), json.loads(os.environ["RELAY_ROUTES"]))
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "9121"))), handler(cfg)).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
