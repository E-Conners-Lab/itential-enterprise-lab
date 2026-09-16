#!/usr/bin/env python3
"""GET-only fingerprint of the production Itential environment and NetBox (ADR 0063, PID S12.8).

  verify/prod-snapshot.py --save                     # before a dev build: writes the baseline
  verify/prod-snapshot.py --compare [--allow FILE]   # after it: exit 1 on any difference not allowed

--save writes verify/results/prod-snapshot-<ts>.json and verify/results/prod-snapshot-latest.json.
--compare reads prod-snapshot-latest.json, fingerprints production again, writes the new fingerprint as
verify/results/prod-snapshot-<ts>.json (latest is left as the baseline) and prints every difference.

--allow FILE is a JSON allowlist for documented, additive changes (the Copilot roles and group):
  {"delta": {"authorization::roles_total": 3, "authorization::groups_total": [1, 2]},
   "added": ["authorization::groups::copilot-*"]}
"delta" lets a numeric value move by exactly that amount (or one of those amounts); "added" lets keys that
match a glob appear. Nothing may ever disappear. A key listed in the allowlist may also stay unchanged.

Only two kinds of request leave this script, and the request function refuses anything else: POST to the
Platform's /login, and GET. It never prints a credential, and the inventory nodes it reads (which carry the
devices' passwords) are reduced to their names before anything is kept.

Credentials come from the environment, or from the repo .env when the variable is not set:
ITENTIAL_ADMIN_USER (default admin@itential), ITENTIAL_ADMIN_PASSWORD, NETBOX_URL, NETBOX_TOKEN.
PROD_PLATFORM_URL overrides https://itential.lab.internal; TLS is verified against docs/lab-root-ca.crt."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "verify" / "results"
CA = ROOT / "docs" / "lab-root-ca.crt"
ENV_FILE = ROOT / ".env"
LATEST = "prod-snapshot-latest.json"
SEP = "::"
PAGE = 100
MAX_PAGES = 50


class Api(Protocol):
    def platform_get(self, path: str) -> Any: ...

    def netbox_get(self, path: str) -> Any: ...


# --- HTTP (the only network code) ------------------------------------------------------------------------


def _dotenv(path: Path = ENV_FILE) -> dict[str, str]:
    """KEY=value lines of .env, the way `set -a; . ./.env` reads the simple ones; comments dropped."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def _setting(name: str, env: dict[str, str], default: str = "") -> str:
    return os.environ.get(name) or env.get(name) or default


class HttpApi:
    def __init__(self) -> None:
        env = _dotenv()
        self.platform = _setting("PROD_PLATFORM_URL", env, "https://itential.lab.internal").rstrip("/")
        self.user = _setting("ITENTIAL_ADMIN_USER", env, "admin@itential")
        self._password = _setting("ITENTIAL_ADMIN_PASSWORD", env)
        self.netbox = _setting("NETBOX_URL", env).rstrip("/")
        self._nb_token = _setting("NETBOX_TOKEN", env)
        missing = [n for n, v in (("ITENTIAL_ADMIN_PASSWORD", self._password), ("NETBOX_URL", self.netbox), ("NETBOX_TOKEN", self._nb_token)) if not v]
        if missing:
            raise SystemExit(f"missing from the environment and .env: {', '.join(missing)}")
        self._tls = ssl.create_default_context(cafile=str(CA))
        self._cookie = ""

    def _request(self, method: str, url: str, headers: dict[str, str], body: bytes | None = None) -> tuple[Any, list[str]]:
        # The whole point of this script is that it cannot change production: refuse any other verb or target.
        if not (method == "GET" or (method == "POST" and url == f"{self.platform}/login")):
            raise RuntimeError(f"refused: {method} {url} (only GET and POST /login are allowed)")
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        ctx = self._tls if url.startswith("https://") else None
        try:
            with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
                raw = resp.read().decode()
                cookies = resp.headers.get_all("Set-Cookie") or []
        except urllib.error.HTTPError as e:
            raise SystemExit(f"HTTP {e.code} for {method} {url.split('?')[0]}") from e
        except urllib.error.URLError as e:
            raise SystemExit(f"{method} {url.split('?')[0]}: {e.reason}") from e
        return (json.loads(raw) if raw.strip() else None), cookies

    def login(self) -> None:
        body = json.dumps({"username": self.user, "password": self._password}).encode()
        _, cookies = self._request("POST", f"{self.platform}/login", {"Content-Type": "application/json"}, body)
        self._cookie = "; ".join(c.split(";", 1)[0] for c in cookies)
        if not self._cookie:
            raise SystemExit(f"login to {self.platform} as {self.user} returned no session")

    def platform_get(self, path: str) -> Any:
        if not self._cookie:
            self.login()
        return self._request("GET", f"{self.platform}{path}", {"Cookie": self._cookie, "Accept": "application/json"})[0]

    def netbox_get(self, path: str) -> Any:
        return self._request("GET", f"{self.netbox}{path}", {"Authorization": f"Token {self._nb_token}", "Accept": "application/json"})[0]


# --- The fingerprint ---------------------------------------------------------------------------------------


def _items(doc: Any) -> list:
    """The list inside a Platform response, whichever envelope the application uses."""
    if isinstance(doc, list):
        return doc
    if not isinstance(doc, dict):
        return []
    for key in ("items", "results", "data", "result", "profiles"):
        value = doc.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            inner = _items(value)
            if inner:
                return inner
    return []


def _total(doc: Any) -> int | None:
    if isinstance(doc, dict):
        for key in ("total", "count"):
            if isinstance(doc.get(key), int):
                return doc[key]
        for key in ("data", "result"):
            inner = _total(doc.get(key))
            if inner is not None:
                return inner
    return None


def _name(item: dict) -> str:
    return str(item.get("name") or item.get("id") or (item.get("properties") or {}).get("name") or item.get("_id") or "?")


def _first(item: dict, *keys: str) -> Any:
    for key in keys:
        if item.get(key) is not None:
            return item[key]
    return None


def _paged(api: Api, path: str) -> list:
    out: list = []
    for page in range(MAX_PAGES):
        doc = api.platform_get(f"{path}?limit={PAGE}&skip={page * PAGE}")
        batch = _items(doc)
        out.extend(batch)
        total = _total(doc)
        if not batch or len(batch) < PAGE or (total is not None and len(out) >= total):
            return out
    raise SystemExit(f"{path}: more than {MAX_PAGES * PAGE} entries")


def collect(api: Api) -> dict:
    fp: dict[str, Any] = {}
    fp["workflows"] = {w["name"]: _first(w, "lastUpdated", "last_updated", "updated") for w in _paged(api, "/automation-studio/workflows")}

    inventories = {}
    for inv in _items(api.platform_get("/inventory_manager/v1/inventories")):
        name = _name(inv)
        # node documents carry the device credentials: keep the names, drop everything else immediately
        nodes = sorted(_name(n) for n in _items(api.platform_get(f"/inventory_manager/v1/inventories/{name}/nodes")))
        inventories[name] = {"groups": sorted(str(g) for g in inv.get("groups") or []), "nodes": nodes}
    fp["inventories"] = inventories

    fp["device_groups"] = {_name(g): sorted(str(d) for d in g.get("devices") or []) for g in _items(api.platform_get("/configuration_manager/deviceGroups"))}
    fp["agents"] = {_name(a): _first(a, "updated", "updatedAt", "lastUpdated") for a in _items(api.platform_get("/agent-project-service/operable-agents"))}
    fp["profiles"] = sorted(_name(p) for p in _items(api.platform_get("/model-registry-service/profiles")))
    fp["integrations"] = sorted(_name(i) for i in _items(api.platform_get("/integrations?limit=100")))
    fp["adapters"] = sorted(_name(a) for a in _items(api.platform_get("/adapters?limit=100")))

    roles_doc = api.platform_get("/authorization/roles?limit=1")
    groups = _paged(api, "/authorization/groups")
    by_name = {g["name"]: len(g.get("assignedRoles") or []) for g in groups}
    fp["authorization"] = {
        "roles_total": _total(roles_doc),
        "groups_total": len(groups),
        "groups": by_name,
        "admin_group_roles": by_name.get("admin_group"),
    }

    fp["netbox"] = {
        "devices": api.netbox_get("/api/dcim/devices/?limit=1")["count"],
        "vlans": api.netbox_get("/api/ipam/vlans/?limit=1")["count"],
    }
    return fp


# --- Comparison --------------------------------------------------------------------------------------------


def flatten(doc: Any, prefix: str = "") -> dict[str, Any]:
    """Nested dicts become SEP-joined keys; lists and scalars are leaves (lists compare as a whole)."""
    if isinstance(doc, dict):
        out: dict[str, Any] = {}
        for key, value in doc.items():
            out.update(flatten(value, f"{prefix}{SEP}{key}" if prefix else str(key)))
        return out
    return {prefix: doc}


def diff(old: dict, new: dict, allow: dict | None = None) -> list[str]:
    """Every difference between two fingerprints that the allowlist does not cover, as readable lines."""
    allow = allow or {}
    deltas = allow.get("delta", {})
    added_ok = allow.get("added", [])
    a, b = flatten(old), flatten(new)
    problems: list[str] = []
    for key in sorted(a.keys() - b.keys()):
        problems.append(f"removed  {key} (was {a[key]!r})")
    for key in sorted(b.keys() - a.keys()):
        if not any(fnmatch.fnmatchcase(key, pattern) for pattern in added_ok):
            problems.append(f"added    {key} = {b[key]!r}")
    for key in sorted(a.keys() & b.keys()):
        if a[key] == b[key]:
            continue
        allowed = deltas.get(key)
        steps = allowed if isinstance(allowed, list) else [allowed]
        numeric = all(isinstance(v, int) and not isinstance(v, bool) for v in (a[key], b[key]))
        if allowed is not None and numeric and (b[key] - a[key]) in steps:
            continue
        problems.append(f"changed  {key}: {a[key]!r} -> {b[key]!r}")
    return problems


# --- CLI ---------------------------------------------------------------------------------------------------


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")


def main(argv: list[str] | None = None, api: Api | None = None, results: Path = RESULTS) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--save", action="store_true", help="write the baseline fingerprint")
    mode.add_argument("--compare", action="store_true", help="compare production against the baseline")
    parser.add_argument("--allow", type=Path, help="JSON allowlist of documented additions (with --compare)")
    args = parser.parse_args(argv)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    baseline_path = results / LATEST
    if args.compare and not baseline_path.exists():
        print(f"no baseline at {baseline_path}: run --save first")
        return 2
    allow = json.loads(args.allow.read_text()) if args.allow else None
    if allow is not None and not args.compare:
        print("--allow applies to --compare only")
        return 2

    fp = collect(api or HttpApi())
    doc = {"taken": ts, "fingerprint": fp}
    _write(results / f"prod-snapshot-{ts}.json", doc)
    if args.save:
        _write(baseline_path, doc)
        print(f"saved {len(flatten(fp))} values to {results / f'prod-snapshot-{ts}.json'} and {baseline_path}")
        return 0

    baseline = json.loads(baseline_path.read_text())
    problems = diff(baseline["fingerprint"], fp, allow)
    if problems:
        print(f"production differs from the baseline taken {baseline.get('taken')}:")
        for line in problems:
            print(f"  {line}")
        return 1
    print(f"production matches the baseline taken {baseline.get('taken')} ({len(flatten(fp))} values{', allowlist applied' if allow else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
