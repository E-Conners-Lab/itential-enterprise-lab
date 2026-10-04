#!/usr/bin/env python3
"""PID S13 criteria 2 and 4 on the tier that runs the AWS VPN (step 12, ADR 0068 amendment 2026-10-03; production since
ADR 0070) for verify/test-13a-aws-vpn.sh. One subcommand per check; each prints evidence and exits 0 on pass, 1 on fail.

The leak sweep counts every AWS VPN secret the tier's Vault holds - each live version, so a key rotated away is still
looked for - whole and in every 16-character piece, in each place a copy could land: the job documents and task
records of every job of the workflows that touch AWS or the lab edge, the tier's container logs, both repositories,
and the router's own records. The router's are counted on the Gateway by cloud-devops-pipeline's
`lab-edge-push --action sweep`, so a router log never reaches a job or this machine (dc1-wan01, 2026-10-01). Only
labels and numbers are printed; an error prints its class.

Environment (never argv, which ps can read): AWS_VPN_TIER (default versions.yaml aws_vpn.tier), VAULT_ADDR,
VAULT_ADMIN_TOKEN (that tier's Vault), PLATFORM_URL and ITENTIAL_ADMIN_USER / ITENTIAL_ADMIN_PASSWORD (its Platform).
"""

from __future__ import annotations

import functools
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from urllib.parse import quote

import yaml

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("vaultcheck", HERE / "vaultcheck.py")
vc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vc)
V = vc.V

WINDOW = 16  # lab-edge-push's: any 16 characters of a secret count as a copy
PAGE = 100
# Every AWS VPN secret in the tier's Vault, by path and field (a key's `version` is not a secret). All are bound to its
# Gateway, so any of them could reach a job or a log through a service.
SECRET_FIELDS = {
    "aws/vpn-psk": ["psk"],
    "aws/terraform": ["access_key_id", "secret_access_key"],
    "aws/psk-writer": ["role_id", "secret_id"],
    "devices/dc1-wan01-aws-vpn": ["password"],
}
# The AWS VPN workflows (every one that runs an AWS VPN service: tests/test_awsvpncheck.py), and the two hand paths to
# the lab edge (the teardown by hand on 2026-10-02, the show reads of the windows)
JOB_WORKFLOWS = [
    V["workflows"][k]
    for k in ("deploy_aws_vpn", "hand_off_aws_vpn", "verify_aws_vpn", "tear_down_aws_vpn", "tear_down_expired_aws_vpn",
              "get_aws_vpn_status", "check_aws_drift", "rotate_aws_vpn_key", "rotate_aws_vpn_key_monthly",
              "config_push_revert", "show_command")
]
TIER = os.environ.get("AWS_VPN_TIER") or V["aws_vpn"]["tier"]
HA2 = yaml.safe_load((HERE.parent / "itential" / "ha2" / "versions.yaml").read_text())
_IP = {vm["name"]: vm["ip"] for vm in HA2["vms"]}
# where each tier's containers log: the dev stack on one VM; production's Platform nodes and its Gateway VM (HA2)
LOG_SOURCES = {
    "dev": [(V["vm"]["ip"], ("gateway5", "gateway5-runner", "platform"))],
    "prod": [(_IP["iap-01"], ("platform",)), (_IP["iap-02"], ("platform",)),
             (_IP["iag-01"], ("gateway5", "gateway5-runner"))],
}
CDP = Path.home() / "PycharmProjects" / "cloud-devops-pipeline"
VERIFY_TARGET = "dc1-wan01"  # the target with a deployment of its own (monitor: aws)
VERIFY_POLLS, VERIFY_POLL_SECONDS = 60, 10  # Verify takes ~1 min (job 0fe8ee24: 46 s); 10 min is the ceiling
TERMINAL = {"complete", "error", "canceled"}


class SweepError(Exception):
    """A sweep that could not look; its message names paths and counts, never a value."""


# --- the counter ---------------------------------------------------------------------------------------------------
def count(text: str, secrets: dict[str, str]) -> dict[str, dict[str, int]]:
    def pieces(s: str) -> int:
        return sum(s[i : i + WINDOW] in text for i in range(len(s) - WINDOW + 1))

    return {label: {"whole": text.count(s), "pieces": pieces(s)} for label, s in secrets.items()}


def hits(counts: dict[str, dict[str, int]]) -> int:
    return sum(c["whole"] + c["pieces"] for c in counts.values())


def found(counts: dict[str, dict[str, int]]) -> str:
    return "; ".join(f"{label}: {c['whole']} whole, {c['pieces']} pieces" for label, c in counts.items()
                     if c["whole"] or c["pieces"])


def control(secrets: dict[str, str]) -> bool:
    """The positive control: a planted copy of every secret must be counted whole with all its pieces, and a cut of
    20 characters as its 5 pieces."""
    ok = True
    for label, s in secrets.items():
        whole = count(f"planted {s} here", {label: s})[label]
        cut = count(f"planted {s[2:22]} here", {label: s})[label]
        seen = whole == {"whole": 1, "pieces": max(0, len(s) - WINDOW + 1)} and (
            len(s) < 22 or cut == {"whole": 0, "pieces": 20 - WINDOW + 1})
        ok &= seen
        print(f"  {'ok  ' if seen else 'FAIL'} {label} ({len(s)} characters): a planted copy counted "
              f"{whole['whole']} whole, {whole['pieces']} pieces; a 20-character cut {cut['pieces']} pieces")
    return ok


# --- what Vault holds ----------------------------------------------------------------------------------------------
def secrets_from_vault() -> dict[str, str]:
    """`<path> <field> v<N>` -> value, for every live version (a destroyed or deleted one cannot leak again)."""
    mount, out = V["vault"]["kv_mount"], {}
    for path, fields in SECRET_FIELDS.items():
        st, meta = vc.vault("GET", f"{mount}/metadata/{path}", vc.admin())
        if st != 200 or not meta:
            raise SweepError(f"{path}: Vault answered {st} for its versions")
        for n, m in sorted(meta["data"]["versions"].items(), key=lambda kv: int(kv[0])):
            if m.get("destroyed") or m.get("deletion_time"):
                continue
            st, d = vc.vault("GET", f"{mount}/data/{path}?version={n}", vc.admin())
            if st != 200 or not d:
                raise SweepError(f"{path} v{n}: Vault answered {st}")
            for f in fields:
                value = (d["data"]["data"] or {}).get(f)
                if isinstance(value, str) and value:
                    out[f"{path} {f} v{n}"] = value
    return out


@functools.cache
def secrets() -> dict[str, str]:
    return secrets_from_vault()


# --- the checks ----------------------------------------------------------------------------------------------------
def c_control() -> bool:
    s = secrets()
    paths = {label.split()[0] for label in s}
    print(f"  {len(s)} secrets from {len(paths)} of {len(SECRET_FIELDS)} Vault paths: {', '.join(sorted(s))}")
    return paths == set(SECRET_FIELDS) and control(s)


def c_verify() -> bool:
    """S13 criterion 4, live: Verify AWS VPN through its endpoint trigger, as the branded page starts it."""
    route = V["operations_manager"]["verify_aws_vpn"]["endpoint"]["route"]
    p = vc.Platform()
    try:
        body = p.call("POST", f"/operations-manager/triggers/endpoint/{route}", {"target": VERIFY_TARGET})
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL the trigger {route} refused: {type(e).__name__} {getattr(e, 'code', '')}")
        return False
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    job_id = body.get("_id") or body.get("job_id") or body.get("jobId") or data.get("_id") or data.get("job_id")
    if not job_id:
        print(f"  FAIL the trigger answered without a job ID (keys: {sorted(body)})")
        return False
    job = {}
    for _ in range(VERIFY_POLLS):
        job = p.call("GET", f"/operations-manager/jobs/{job_id}")["data"]
        if job.get("status") in TERMINAL:
            break
        time.sleep(VERIFY_POLL_SECONDS)
    variables = job.get("variables") or {}
    outcome = variables.get("outcome") or variables.get("error")
    print(f"  Verify AWS VPN on {VERIFY_TARGET}: job {job_id}, status {job.get('status')}, outcome: {outcome}")
    return job.get("status") == "complete" and str(variables.get("outcome", "")).startswith("tunnel up")


def _job_ids(p, name: str) -> list[str]:
    ids, skip = [], 0
    while True:
        d = p.call("GET", f"/operations-manager/jobs?equals[name]={quote(name)}&limit={PAGE}&skip={skip}"
                          "&sort=metrics.start_time&order=-1")
        ids += [j["_id"] for j in d.get("data") or []]
        skip += PAGE
        if skip >= (d.get("metadata") or {}).get("total", 0):
            return ids


def sweep_jobs(p, s: dict[str, str]) -> dict[str, int]:
    jobs = total = 0
    for name in JOB_WORKFLOWS:
        ids, leaked = _job_ids(p, name), 0
        for jid in ids:
            text = json.dumps(p.call("GET", f"/operations-manager/jobs/{jid}"))
            text += json.dumps(p.call("GET", f"/operations-manager/tasks?equals[job._id]={jid}&limit=1000"))
            c = count(text, s)
            if hits(c):
                leaked += hits(c)
                print(f"  LEAK  {name} job {jid}: {found(c)}")
        print(f"  {'LEAK ' if leaked else 'clean'} {name}: {len(ids)} jobs (documents and task records), {leaked} hits")
        jobs, total = jobs + len(ids), total + leaked
    return {"jobs": jobs, "hits": total}


def c_jobs() -> bool:
    r = sweep_jobs(vc.Platform(), secrets())
    print(f"  {r['jobs']} jobs swept, {r['hits']} hits")
    return r["jobs"] > 0 and r["hits"] == 0


def c_logs() -> bool:
    ok, s = True, secrets()
    for host, containers in LOG_SOURCES[TIER]:
        for c in containers:
            run = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new",
                 f"ubuntu@{host}", f"sudo docker logs {c} 2>&1"],
                capture_output=True, text=True, timeout=600, check=False)
            lines, counts = len(run.stdout.splitlines()), count(run.stdout, s)
            good = run.returncode == 0 and lines > 0 and not hits(counts)
            ok &= good
            print(f"  {'ok  ' if good else 'FAIL'} {host} {c}: {lines} lines since the container started, "
                  f"{hits(counts)} hits" + (f" ({found(counts)})" if hits(counts) else "")
                  + ("" if run.returncode == 0 else ", ssh failed"))
    return ok


def _archive_files(cmd: list[str]) -> dict[str, str]:
    raw = subprocess.run(cmd, capture_output=True, timeout=300, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        return {m.name: tar.extractfile(m).read().decode("utf-8", "ignore") for m in tar.getmembers() if m.isfile()}


def c_repos() -> bool:
    """Every tracked file of this repository (HEAD) and of cloud-devops-pipeline's main, as committed."""
    ok, s = True, secrets()
    subprocess.run(["git", "-C", str(CDP), "fetch", "-q", "origin"], timeout=120, check=True)
    for label, cmd in (("itential-enterprise-lab HEAD", ["git", "-C", str(HERE.parent), "archive", "HEAD"]),
                       ("cloud-devops-pipeline origin/main", ["git", "-C", str(CDP), "archive", "origin/main"])):
        files = _archive_files(cmd)
        leaked = {name: count(text, s) for name, text in files.items()}
        leaked = {name: c for name, c in leaked.items() if hits(c)}
        good = len(files) > 0 and not leaked
        ok &= good
        print(f"  {'ok  ' if good else 'FAIL'} {label}: {len(files)} files, {sum(map(hits, leaked.values()))} hits")
        for name, c in leaked.items():
            print(f"        {name}: {found(c)}")
    return ok


def c_router() -> bool:
    """The router's own records, counted on the Gateway (lab-edge-push sweep); a target with a deployment must
    hold its key, as type 6."""
    ok = True
    for name, entry in sorted(vc._open_targets().items()):
        rc, out = vc._service("lab-edge-push", {"action": "sweep", "target_json": json.dumps(entry["target"]),
                                                "username": entry["username"], "timeout": "300"})
        sources = out.get("sources") or {}
        keys = out.get("pre_shared_key_lines") or {}
        checks = {
            "the sweep ran on the Gateway (exit 0)": rc == 0,
            # a router with no change log refuses that read with one `%` line: nothing there to sweep (2026-10-03)
            "every record read (the change log only if one is kept) and not empty": bool(sources)
            and all((src.get("read") or (n == "archive_log" and out.get("change_log") is False))
                    and src.get("lines", 0) > 0 for n, src in sources.items()),
            "no copy of the key or the password": out.get("clean") is True and out.get("hits") == 0,
        }
        if entry["monitor"] == "aws":
            checks["the deployment's key is on the router as type 6"] = (
                keys.get("type6", 0) >= 1 and keys.get("type6") == keys.get("total"))
        print(f"  {name}: " + ("; ".join(f"{n} {'read' if src.get('read') else 'refused'} {src.get('lines')} lines, key {src.get('key')}, password "
                                         f"{src.get('password')}" for n, src in sources.items())
                               or f"error: {out.get('error')}")
              + f"; pre-shared-key lines {keys}; change log kept: {out.get('change_log')}")
        for check, passed in checks.items():
            print(f"    {'ok  ' if passed else 'FAIL'} {check}")
            ok &= passed
    return ok


CHECKS = {"control": c_control, "verify": c_verify, "jobs": c_jobs, "logs": c_logs, "repos": c_repos,
          "router": c_router}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in CHECKS:
        sys.exit(f"usage: {sys.argv[0]} {{{'|'.join(CHECKS)}}}")
    try:
        sys.exit(0 if CHECKS[sys.argv[1]]() else 1)
    except SweepError as e:
        print(f"SweepError: {e}")
        sys.exit(1)
    except Exception as e:  # noqa: BLE001 - its class only: this process holds the secrets it counts
        print(f"{type(e).__name__} {getattr(e, 'code', '')}")
        sys.exit(1)
