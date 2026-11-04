#!/usr/bin/env python3
"""NetClaw's Anthropic usage for the token meter (ADR 0049).

NetClaw (OpenClaw in Docker on the Mac) calls Claude itself, so its spend never reaches the Agent Session Manager that
verify/tokens.sh reads. OpenClaw records each run's usage in a model.completed event in the container's own SQLite
store, and that store goes away with the container: every `netclaw.sh start` recreates it. So this keeps a ledger
outside the container and outside the repo. `harvest` copies each run's usage row once (run id, time, provider,
model, token counts; never any content) from every container started from a netclaw-lab image, running or stopped.
tokens.sh harvests first, then adds the window's totals to the weekly spend.

    verify/netclawusage.py harvest [CONTAINER ...]   every netclaw-lab container when none is named
    verify/netclawusage.py report [DAYS]             the window's totals per provider|model, as JSON (default 7 days)

Standard library only: netclaw.sh runs it with the system python3 before it removes a container.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LEDGER = Path(
    os.environ.get(
        "NETCLAW_USAGE_LEDGER",
        str(Path.home() / ".config/itential-enterprise-lab/netclaw-usage.jsonl"),
    )
)
IMAGE_PREFIX = "netclaw-lab"
AGENTS_DIR = "/home/node/.openclaw/agents"
STORE = "agent/openclaw-agent.sqlite"
FIELDS = (
    "run",
    "ts",
    "provider",
    "model",
    "input",
    "output",
    "cache_read",
    "cache_write",
)
TOKENS = ("input", "output", "cache_read", "cache_write")
QUERY = (
    "select json_extract(event_json, '$.runId'), created_at, json_extract(event_json, '$.provider'),"
    " json_extract(event_json, '$.modelId'), json_extract(event_json, '$.data.usage.input'),"
    " json_extract(event_json, '$.data.usage.output'), json_extract(event_json, '$.data.usage.cacheRead'),"
    " json_extract(event_json, '$.data.usage.cacheWrite')"
    " from trajectory_runtime_events where json_extract(event_json, '$.type') = 'model.completed'"
)
# runs inside a running container (python3 is in the image): opens each store read-only and prints only the rows
IN_CONTAINER = f"""
import glob, json, sqlite3
rows = []
for path in glob.glob({AGENTS_DIR + "/*/" + STORE!r}):
    db = sqlite3.connect("file:" + path + "?mode=ro", uri=True)
    rows += db.execute({QUERY!r}).fetchall()
    db.close()
print(json.dumps(rows))
"""


def docker(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def netclaw_containers(names: list[str] | None = None) -> dict[str, bool]:
    """name -> running, for every container started from a netclaw-lab image (Config.Image keeps the name it was
    started with, even after the tag has moved to a newer build)."""
    if not names:
        listed = docker("ps", "-a", "--format", "{{.Names}}")
        if listed.returncode:
            raise RuntimeError(f"docker ps failed ({listed.stderr.strip()[:200]})")
        names = listed.stdout.split()
    if not names:
        return {}
    inspected = docker(
        "inspect", "--format", "{{.Name}} {{.Config.Image}} {{.State.Running}}", *names
    )
    # docker says "error: no such object: <name>" for a name it does not know (2026-11-04)
    if inspected.returncode and "no such" not in inspected.stderr.lower():
        raise RuntimeError(f"docker inspect failed ({inspected.stderr.strip()[:200]})")
    found = {}
    for line in inspected.stdout.splitlines():
        # a name docker does not know gives an empty line: nothing to harvest
        if len(line.split()) != 3:
            continue
        name, image, running = line.split()
        if image.startswith(IMAGE_PREFIX):
            found[name.lstrip("/")] = running == "true"
    return found


def read_rows(name: str, running: bool) -> list[list]:
    if running:
        r = docker("exec", "-i", name, "python3", "-", stdin=IN_CONTAINER)
        if r.returncode:
            raise RuntimeError(
                f"{name}: reading its store failed ({r.stderr.strip()[:200]})"
            )
        return json.loads(r.stdout)
    rows: list[list] = []
    # a stopped container: copy its stores out (the WAL files come along)
    with tempfile.TemporaryDirectory() as tmp:
        r = docker("cp", f"{name}:{AGENTS_DIR}", tmp)
        # "Could not find the file ... in container" (measured 2026-11-04): it never ran OpenClaw
        if r.returncode and "could not find the file" in r.stderr.lower():
            return []
        if r.returncode:
            raise RuntimeError(
                f"{name}: copying its store failed ({r.stderr.strip()[:200]})"
            )
        for path in Path(tmp).glob(f"agents/*/{STORE}"):
            db = sqlite3.connect(path)
            rows += db.execute(QUERY).fetchall()
            db.close()
    return rows


def load(ledger: Path = LEDGER) -> list[dict]:
    if not ledger.exists():
        return []
    return [
        json.loads(line) for line in ledger.read_text().splitlines() if line.strip()
    ]


def record(rows: dict[str, list[list]], ledger: Path = LEDGER) -> int:
    """Appends each run not yet in the ledger (keyed by run id and time); returns how many were new."""
    seen = {(r["run"], r["ts"]) for r in load(ledger)}
    new = []
    for name, found in rows.items():
        for row in found:
            if len(row) != len(FIELDS):
                raise ValueError(
                    f"{name}: a usage row has {len(row)} fields, not {len(FIELDS)}"
                )
            # width checked above: Python 3.9 (macOS /usr/bin/python3) has no zip(strict=)
            rec = dict(zip(FIELDS, row))
            if rec["run"] and (rec["run"], rec["ts"]) not in seen:
                seen.add((rec["run"], rec["ts"]))
                new.append({**rec, "container": name})
    if new:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(ledger, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as fh:
            for rec in new:
                fh.write(json.dumps(rec, sort_keys=True) + "\n")
    return len(new)


def totals(
    days: float, now: float | None = None, ledger: Path = LEDGER
) -> dict[str, dict[str, int]]:
    """The window's sums per "provider|model": runs and the four token counts."""
    since = ((now if now is not None else time.time()) - days * 86400) * 1000
    per: dict[str, dict[str, int]] = {}
    for r in load(ledger):
        if (r.get("ts") or 0) < since:
            continue
        p = per.setdefault(
            f"{r.get('provider') or '?'}|{r.get('model') or '?'}",
            dict.fromkeys(("runs", *TOKENS), 0),
        )
        p["runs"] += 1
        for k in TOKENS:
            p[k] += r.get(k) or 0
    return per


def cost(p: dict[str, int], budget: dict) -> float:
    """USD at the budget's prices: cache writes and reads priced as multiples of the input price."""
    per_in = budget["input_usd_per_mtok"] / 1e6
    return (
        p["input"] * per_in
        + p["output"] * budget["output_usd_per_mtok"] / 1e6
        + p["cache_write"] * per_in * budget["cache_write_multiplier"]
        + p["cache_read"] * per_in * budget["cache_read_multiplier"]
    )


def main(argv: list[str]) -> int:
    if argv[:1] == ["harvest"]:
        found = netclaw_containers(argv[1:])
        rows, failed = {}, []
        for name, running in found.items():
            try:
                rows[name] = read_rows(name, running)
            except (
                OSError,
                ValueError,
                RuntimeError,
                sqlite3.Error,
                subprocess.SubprocessError,
            ) as exc:
                failed.append(f"{name} ({exc})")
        new = record(rows, LEDGER)
        print(
            f"NetClaw usage: {new} new run(s) from {', '.join(sorted(rows)) or 'no netclaw-lab container'}"
        )
        if failed:
            print(f"NetClaw usage NOT read from: {'; '.join(failed)}")
            return 1
        return 0
    if argv[:1] == ["report"]:
        print(
            json.dumps(
                totals(float(argv[1]) if len(argv) > 1 else 7), indent=1, sort_keys=True
            )
        )
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
