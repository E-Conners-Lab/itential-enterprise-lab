#!/usr/bin/env python3
"""Minimal MCP streamable-HTTP client for verify/ (PID S4.7): no SDK, only urllib.

  verify/mcpcall.py <url> tools            # prints one tool name per line
  verify/mcpcall.py <url> call <tool> [json-args]   # prints the tool result JSON

MCP_HEADERS_CMD (optional) is a command whose stdout is a JSON object of headers, sent with every request - the way
Claude Code runs a headersHelper (production: scripts/mcp-client.sh token, ADR 0083). MCP_CA (optional) is the CA file
an https URL is verified with (docs/lab-root-ca.crt). Neither the headers nor a token is ever printed.

Implements initialize -> notifications/initialized -> request, honouring the Mcp-Session-Id
header and both JSON and SSE response bodies of the 2025-03-26 / 2025-11-25 transports."""

from __future__ import annotations

import json
import os
import shlex
import ssl
import subprocess
import sys
import urllib.error
import urllib.request

PROTOCOL = "2025-03-26"


class Client:
    def __init__(self, url: str, timeout: int = 60) -> None:
        self.url = url
        self.timeout = timeout
        self.session: str | None = None
        self._id = 0
        self.headers: dict[str, str] = {}
        cmd = os.environ.get("MCP_HEADERS_CMD")
        if cmd:
            out = subprocess.run(shlex.split(cmd), capture_output=True, text=True, check=False)
            if out.returncode != 0:
                raise SystemExit(f"MCP_HEADERS_CMD failed: {out.stderr.strip()[:300]}")
            self.headers = json.loads(out.stdout)
        ca = os.environ.get("MCP_CA")
        self.ctx = ssl.create_default_context(cafile=ca) if ca else None

    def _post(self, body: dict) -> dict | None:
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        if self.session:
            req.add_header("Mcp-Session-Id", self.session)
        for k, v in self.headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session = sid
                ctype = resp.headers.get("Content-Type", "")
                raw = resp.read().decode()
        except urllib.error.HTTPError as e:
            raise SystemExit(f"HTTP {e.code} from {self.url}: {e.read().decode()[:300]}") from e
        if not raw.strip():
            return None
        if "text/event-stream" in ctype:
            msgs = [json.loads(line[5:].strip()) for line in raw.splitlines() if line.startswith("data:")]
            # the response to our request is the message carrying our id
            for m in msgs:
                if m.get("id") == body.get("id"):
                    return m
            return msgs[-1] if msgs else None
        return json.loads(raw)

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        msg = self._post({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        if msg is None:
            raise SystemExit(f"{method}: empty response")
        if "error" in msg:
            raise SystemExit(f"{method}: {json.dumps(msg['error'])}")
        return msg["result"]

    def notify(self, method: str) -> None:
        self._post({"jsonrpc": "2.0", "method": method})

    def start(self) -> dict:
        info = self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "itential-enterprise-lab verify", "version": "1"}})
        self.notify("notifications/initialized")
        return info


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    url, verb = argv[1], argv[2]
    c = Client(url)
    c.start()
    if verb == "tools":
        tools = c.request("tools/list").get("tools", [])
        for t in tools:
            print(t["name"])
        return 0 if tools else 1
    if verb == "call":
        args = json.loads(argv[4]) if len(argv) > 4 else {}
        res = c.request("tools/call", {"name": argv[3], "arguments": args})
        print(json.dumps(res))
        return 1 if res.get("isError") else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
