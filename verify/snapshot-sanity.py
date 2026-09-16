#!/usr/bin/env python3
"""Refuse a production fingerprint with an empty section (ADR 0063, PID S12.8).

  verify/snapshot-sanity.py [FILE]   # default: the newest verify/results/prod-snapshot-<ts>.json

verify/prod-snapshot.py compares two fingerprints key by key, so a section that came back empty on both sides
(an endpoint renamed by an upgrade, an envelope `_items` does not recognise, a login that sees nothing) compares
equal and the compare passes while proving nothing. This reads the saved JSON only - no network - and exits 1
naming every required section that is missing, None, empty, or a zero count. `make prod-snapshot` runs it after
both --save and --compare, on the file that run just wrote. prod-snapshot.py itself is frozen, so the check
lives here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "verify" / "results"

# Every section production always has something in. Dotted paths into the "fingerprint" object.
REQUIRED = (
    "workflows",
    "inventories",
    "adapters",
    "integrations",
    "profiles",
    "authorization.roles_total",
    "authorization.groups",
    "authorization.admin_group_roles",
    "netbox.devices",
)
_MISSING = object()


def newest(results: Path = RESULTS) -> Path | None:
    """The newest timestamped snapshot; prod-snapshot-latest.json is the baseline, not the run just made."""
    files = sorted(p for p in results.glob("prod-snapshot-*.json") if p.name != "prod-snapshot-latest.json")
    return files[-1] if files else None


def _at(doc: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if not isinstance(doc, dict) or part not in doc:
            return _MISSING
        doc = doc[part]
    return doc


def problems(doc: dict) -> list[str]:
    """Each required section that holds nothing, as a readable line; [] when every one has content."""
    fingerprint = doc.get("fingerprint") if isinstance(doc, dict) else None
    if not isinstance(fingerprint, dict):
        return ["no fingerprint object"]
    out = []
    for path in REQUIRED:
        value = _at(fingerprint, path)
        if value is _MISSING:
            out.append(f"{path}: missing")
        elif value is None:
            out.append(f"{path}: None")
        elif isinstance(value, bool):
            out.append(f"{path}: {value!r} is not a count or a collection")
        elif isinstance(value, (int, float)) and value <= 0:
            out.append(f"{path}: {value}")
        elif isinstance(value, (dict, list, str)) and len(value) == 0:
            out.append(f"{path}: empty")
    return out


def main(argv: list[str] | None = None, results: Path = RESULTS) -> int:
    args = sys.argv[1:] if argv is None else argv
    path = Path(args[0]) if args else newest(results)
    if path is None or not path.exists():
        print(f"no snapshot to check ({path or results / 'prod-snapshot-<ts>.json'})")
        return 2
    found = problems(json.loads(path.read_text()))
    if found:
        print(f"{path.name}: the fingerprint proves nothing about these sections:")
        for line in found:
            print(f"  {line}")
        return 1
    print(f"{path.name}: every required section has content ({len(REQUIRED)} checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
