#!/usr/bin/env python3
"""Phase 9a checks (PID S8.2, S8.3, E15; ADR 0065) for verify/test-09a-vault.sh. One subcommand per check; each prints
evidence and exits 0 on pass, 1 on fail. Never prints a credential: values are compared as SHA-256, and send-config
output (which echoes the line it sent) is never shown.

Environment (never argv, which ps can read):
  VAULT_ADDR         the Vault the readers use                     VAULT_ADMIN_TOKEN  a token that can issue secret IDs
  PLATFORM_URL       the Platform (lab CA)                         ITENTIAL_ADMIN_USER / ITENTIAL_ADMIN_PASSWORD
  DEVICE_PASSWORD    the device password .env seeds Vault from (E15 restores it)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import ssl
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
V = yaml.safe_load((ROOT / "itential" / "versions.yaml").read_text())
VAULT = V["vault"]
CLAB = yaml.safe_load((ROOT / "clab" / "versions.yaml").read_text())
CTX = ssl.create_default_context(cafile=str(ROOT / "docs" / "lab-root-ca.crt"))
CLUSTER = V["stack"]["gateway5_cluster_id"]
INVENTORY = V["stack"]["inventory"]
ALIAS_REF = f"$GATEWAYSECRET_({VAULT['device_password_alias']})"


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# --- Vault ---------------------------------------------------------------------------------------------------------
def vault(method: str, path: str, token: str | None = None, body: dict | None = None) -> tuple[int, dict | None]:
    req = urllib.request.Request(
        os.environ["VAULT_ADDR"].rstrip("/") + "/v1/" + path.lstrip("/"),
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-Vault-Token": token} if token else {},
    )
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, None


def admin() -> str:
    return os.environ["VAULT_ADMIN_TOKEN"]


def kv(path: str) -> str:
    return f"{VAULT['kv_mount']}/data/{path}"


# --- Platform ------------------------------------------------------------------------------------------------------
class Platform:
    def __init__(self) -> None:
        self.base = os.environ["PLATFORM_URL"].rstrip("/")
        body = json.dumps({"username": os.environ.get("ITENTIAL_ADMIN_USER") or "admin@itential",
                           "password": os.environ["ITENTIAL_ADMIN_PASSWORD"]}).encode()
        req = urllib.request.Request(self.base + "/login", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, context=CTX, timeout=60) as r:
            # /login answers with the token as plain text and also sets it as a cookie
            self.cookie = "token=" + r.read().decode().strip().strip('"')

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     method=method, headers={"Content-Type": "application/json", "Cookie": self.cookie})
        with urllib.request.urlopen(req, context=CTX, timeout=240) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}

    def run(self, service: str, params: dict, nodes: list[str], inventory: str = INVENTORY) -> dict[str, bool]:
        d = self.call("POST", "/gateway_manager/v1/services/run",
                      {"serviceName": service, "clusterId": CLUSTER, "params": params,
                       "inventory": [{"inventory": inventory, "nodeNames": nodes}]})
        results = (d.get("result") or {}).get("results") or []
        return {r.get("name"): bool(r.get("success")) for r in results}


def nodes(inventory: str = INVENTORY) -> list[str]:
    """The dev tier's devices are the clab oracle; production's (VAULT_TIER=prod) are whatever the Platform's
    inventory holds, which the play builds from NetBox."""
    if os.environ.get("VAULT_TIER") != "prod":
        return [n["name"] for n in CLAB["nodes"]]
    data = Platform().call("GET", f"/inventory_manager/v1/inventories/{inventory}/nodes?limit=200")["result"]["data"]
    return sorted(n["name"] for n in data)


# --- checks --------------------------------------------------------------------------------------------------------
def c_health() -> bool:
    st, d = vault("GET", "sys/health")
    print(f"sys/health {st}: initialized={d and d.get('initialized')} sealed={d and d.get('sealed')} version={d and d.get('version')}")
    return st == 200 and d is not None and not d["sealed"]


def c_no_token() -> bool:
    st, _ = vault("GET", kv("devices/automation"))
    print(f"read of {kv('devices/automation')} with no token -> {st}")
    return st in (400, 403)


def _policy_token(policy: str) -> str:
    """A short-lived token carrying one reader's policy, issued by the administrator token. It proves the POLICY
    boundary from anywhere; the AppRole login itself is address-bound (S8.2e) and so cannot be used from here."""
    # production's administrator login may issue reader tokens only through the verify-readers token role (its
    # policy grants nothing else); the dev tier's root token uses the plain endpoint
    path = "auth/token/create/verify-readers" if os.environ.get("VAULT_TIER") == "prod" else "auth/token/create"
    _, d = vault("POST", path, admin(),
                 {"policies": [policy], "ttl": "60s", "num_uses": 5, "meta": {"issued_by": "verify/test-09a-vault"}})
    return d["auth"]["client_token"]


def c_policies() -> bool:
    ok = True
    for role, path, want in (("itential-gateway", "devices/automation", 200),
                             ("itential-platform", "devices/automation", 403),
                             ("itential-platform", "services/netbox", 200)):
        token = _policy_token(role)
        try:
            st, d = vault("GET", kv(path), token)
            keys = sorted((d or {}).get("data", {}).get("data", {}))
            print(f"{role:18} policy reads {path:20} -> {st} (want {want}){'  keys ' + str(keys) if keys else ''}")
            ok &= st == want
        finally:
            vault("POST", "auth/token/revoke-self", token)
    return ok


def c_bound() -> bool:
    """The AppRoles are address-bound: each carries the expected bound CIDRs, and a login with a freshly issued, valid
    secret ID is refused from this machine (which is outside them)."""
    def norm(cidrs: list[str]) -> list[str]:  # Vault drops the /32 of a single-host token_bound_cidrs entry
        return sorted(c.removesuffix("/32") for c in cidrs)

    # production binds each role to its own hosts (VAULT_ROLE_CIDRS, JSON {role: [cidr]}); the dev tier binds every
    # role to the one vault-dev Docker gateway (VAULT_BOUND_CIDRS, comma-separated)
    per_role = json.loads(os.environ["VAULT_ROLE_CIDRS"]) if os.environ.get("VAULT_ROLE_CIDRS") else None
    ok = True
    for role in VAULT["approles"]:
        want = norm(per_role[role] if per_role else os.environ["VAULT_BOUND_CIDRS"].split(","))
        _, d = vault("GET", f"auth/{VAULT['approle_mount']}/role/{role}", admin())
        got = {k: norm(d["data"].get(k) or []) for k in ("secret_id_bound_cidrs", "token_bound_cidrs")}
        print(f"{role:18} bound to {got} (want {want})")
        ok &= all(v == want for v in got.values())
        _, rid = vault("GET", f"auth/{VAULT['approle_mount']}/role/{role}/role-id", admin())
        _, sid = vault("POST", f"auth/{VAULT['approle_mount']}/role/{role}/secret-id", admin(),
                       {"metadata": json.dumps({"issued_by": "verify/test-09a-vault"})})  # a string (400 otherwise)
        try:
            st, _ = vault("POST", f"auth/{VAULT['approle_mount']}/login", None,
                          {"role_id": rid["data"]["role_id"], "secret_id": sid["data"]["secret_id"]})
            print(f"{role:18} login with a valid secret ID from this machine -> {st} (want refused)")
            ok &= st in (400, 403)
        finally:
            vault("POST", f"auth/{VAULT['approle_mount']}/role/{role}/secret-id-accessor/destroy", admin(),
                  {"secret_id_accessor": sid["data"]["secret_id_accessor"]})
    return ok


def c_aws_psk() -> bool:
    """ADR 0068 decision 5 (P6) at the policy level, with R4's amendment (2026-10-04): the PSK writer may create, update
    and read lab/aws/vpn-psk, read its metadata and destroy its old versions, and nothing else; the Gateway's reader
    may read it, the Platform may not touch it. Asked with each policy's own token
    (sys/capabilities-self), so nothing is written and no PSK is read. Then the writer's own credentials: in Vault at
    vault.aws.writer_path, with the role's current role ID and a secret ID Vault still knows. Nothing is printed but
    capabilities and yes/no."""
    aws, mount = VAULT["aws"], VAULT["approle_mount"]
    psk, key = kv(aws["psk_path"]), kv(aws["key_path"])
    meta = f"{VAULT['kv_mount']}/metadata/{aws['psk_path']}"
    destroy = f"{VAULT['kv_mount']}/destroy/{aws['psk_path']}"
    want = {"itential-gateway": {psk: ["read"]}, "itential-platform": {psk: ["deny"], key: ["deny"]}}
    # production's verify-readers token role predates the writer and only a root token could widen it; there the
    # writer's policy is proven on iag-01 by make vault-config (its own login, the same capabilities-self question)
    if os.environ.get("VAULT_TIER") != "prod":
        want["itential-aws-psk-writer"] = {psk: ["create", "read", "update"], meta: ["read"], destroy: ["update"],
                                           key: ["deny"]}
    ok = True
    for role, paths in want.items():
        token = _policy_token(role)
        try:
            for path, caps in paths.items():
                _, d = vault("POST", "sys/capabilities-self", token, {"paths": [path]})
                got = sorted((d or {}).get("capabilities", []))
                print(f"{role:24} may {got} on {path} (want {caps})")
                ok &= got == caps
        finally:
            vault("POST", "auth/token/revoke-self", token)
    _, d = vault("GET", kv(aws["writer_path"]), admin())
    creds = (d or {}).get("data", {}).get("data", {})
    _, rid = vault("GET", f"auth/{mount}/role/itential-aws-psk-writer/role-id", admin())
    same_role = bool(creds.get("role_id")) and creds.get("role_id") == (rid or {}).get("data", {}).get("role_id")
    st, look = vault("POST", f"auth/{mount}/role/itential-aws-psk-writer/secret-id/lookup", admin(),
                     {"secret_id": creds.get("secret_id", "")})
    live = st == 200 and bool((look or {}).get("data"))
    print(f"writer credentials at {aws['writer_path']}: role ID current {same_role}, secret ID known to Vault {live}")
    return ok and same_role and live


def c_terraform_run() -> bool:
    """ADR 0068 step 4 (P1, P2, P7) end to end through the Platform's runService: terraform-run's probe clones the
    private repo with the deploy key from Vault, gets the IAM key as environment variables from Vault, and reaches
    AWS as itential-terraform (STS, then init against the S3 state backend). Reads only: no plan, nothing created.
    Catches a Gateway whose deploy key, AWS key or Vault policy is missing before a real job does."""
    d = Platform().call("POST", "/gateway_manager/v1/services/run",
                        {"serviceName": "terraform-run", "clusterId": CLUSTER,
                         "params": {"action": "probe"}})
    if d.get("error"):
        print(f"runService error: {str(d['error'].get('data'))[:300]}")
        return False
    res = d.get("result") or {}
    out = res.get("stdout_json") or {}
    checks = {
        "return code 0": res.get("return_code") == 0,
        "AWS key from Vault": out.get("aws_env_set") == ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"],
        "caller itential-terraform": str(out.get("sts_caller", "")).endswith(f":user/itential/{VAULT['aws']['iam_user']}"),
        "backend init": out.get("backend_init") == "ok",
        f"terraform {V['runner_terraform']['version']}": out.get("terraform") == V["runner_terraform"]["version"],
        # the Gateway ran the pinned commit, not whatever a branch points at (ITL-01; the probe reports 12 characters)
        "clone at the pinned commit": out.get("commit") == V["terraform_run"]["repository"]["reference"][:12],
    }
    print(f"clone at {out.get('commit', '?')}, argv {out.get('argv')}, uid {out.get('uid')}; "
          f"caller {out.get('sts_caller', '?')}")
    for name, ok in checks.items():
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    return all(checks.values())


def gh(*args: str) -> str:
    """A read-only GitHub API call through the workstation's gh login (the repository is private)."""
    run = subprocess.run(["gh", "api", *args], capture_output=True, text=True, timeout=60, check=False)
    if run.returncode != 0:
        raise RuntimeError(f"gh api {args[0]}: {(run.stderr or run.stdout).strip()[:200]}")
    return run.stdout.strip()


def c_cdp_pin() -> bool:
    """Security review 2026-09-29, ITL-01: the commit the Gateway is pinned to is on cloud-devops-pipeline's protected
    main, every required check passed on it, and main still requires those checks with admins included. A pin can
    name ANY commit, an unmerged branch's included, and the Gateway would run it with the AWS key; branch protection
    alone does not stop that. Reads GitHub only."""
    repo, pin = VAULT["git"]["repo"], V["terraform_run"]["repository"]["reference"]
    # a branch name would pass every GitHub read below (main...main is "identical"): the pin must be a full SHA first
    if not re.fullmatch(r"[0-9a-f]{40}", str(pin)):
        print(f"  FAIL the pin is a full commit SHA (it is {pin!r})")
        return False
    # "identical" or "ahead": main contains the pin. "behind" or "diverged": the pin is not on main
    status = gh(f"repos/{repo}/compare/{pin}...main", "--jq", ".status")
    protection = json.loads(gh(f"repos/{repo}/branches/main/protection"))
    # each required check is bound to an app (GitHub Actions); a same-named run from any other app does not count
    required = {(c["context"], c.get("app_id")) for c in (protection.get("required_status_checks") or {}).get("checks") or []}
    runs = json.loads(gh(f"repos/{repo}/commits/{pin}/check-runs?per_page=100", "--jq",
                         "[.check_runs[] | {name, conclusion, app: .app.id}]"))

    def passed(name: str, app: int | None) -> bool:
        mine = [r["conclusion"] for r in runs if r["name"] == name and (app is None or r["app"] == app)]
        return bool(mine) and all(c == "success" for c in mine)  # at least one run, and none that did not succeed

    missing = sorted(f"{n}@{a}" for n, a in required if not passed(n, a))
    checks = {
        "the pin is on main": status in ("identical", "ahead"),
        "main requires checks, admins included": bool(required) and protection["enforce_admins"]["enabled"],
        "every required check passed on the pin, from its bound app": bool(required) and not missing,
    }
    print(f"pin {pin[:12]} vs main: {status}; required {', '.join(sorted(f'{n}@{a}' for n, a in required)) or 'none'}; "
          f"not passed on the pin: {', '.join(missing) or 'none'}")
    for name, ok in checks.items():
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    return all(checks.values())



# lab-edge-push's own checks on what the Gateway hands it (cloud-devops-pipeline itential/lab-edge-push.py PSK, VERSION)
PUSH_PSK = re.compile(r"[A-Za-z0-9._+/=-]{32,}")
PUSH_VERSION = re.compile(r"[A-Za-z0-9-]{1,64}")


def _open_targets() -> dict[str, dict]:
    return {n: t for n, t in V["aws_vpn"]["targets"].items() if t["window"] == "open"}


def _pinned_source() -> str:
    """cloud-devops-pipeline's lab_edge_render.py at the Gateway's pin (stdlib only), read from GitHub."""
    import base64

    repo, pin = VAULT["git"]["repo"], V["terraform_run"]["repository"]["reference"]
    return base64.b64decode(gh(f"repos/{repo}/contents/itential/lab_edge_render.py?ref={pin}", "--jq", ".content")).decode()


# run in its own interpreter with an empty environment: this process holds the Vault administrator token and the
# Platform password, and fetched code never runs beside them
_RENDER_DRIVER = """
import json, sys, types
d = json.load(sys.stdin)
r = types.ModuleType("lab_edge_render")
exec(compile(d["code"], "lab_edge_render.py", "exec"), r.__dict__)
t = r.check_target(d["target"])
print(r.sha256(r.render(t, r.check_outputs(t, d["outputs"]))))
"""


def _pinned_sha(target: dict, outputs: dict) -> str:
    """The second source for the block the Gateway renders: the pinned module's SHA-256 for the same inputs."""
    run = subprocess.run([sys.executable, "-I", "-c", _RENDER_DRIVER], env={}, capture_output=True, text=True,
                         timeout=60, check=False,
                         input=json.dumps({"code": _pinned_source(), "target": target, "outputs": outputs}))
    if run.returncode != 0:
        raise RuntimeError(f"the pinned render failed: {run.stderr.strip().splitlines()[-1:]}")
    return run.stdout.strip()


def _service(name: str, params: dict) -> tuple[int | None, dict]:
    d = Platform().call("POST", "/gateway_manager/v1/services/run",
                        {"serviceName": name, "clusterId": CLUSTER, "params": params})
    if d.get("error"):
        print(f"  {name}: runService error: {str(d['error'].get('data'))[:300]}")
        return None, {}
    res = d.get("result") or {}
    return res.get("return_code"), res.get("stdout_json") or {}


def c_lab_edge() -> bool:
    """AWS VPN step 6 (S13.2d) on the tier that runs the AWS VPN: each open target's tunnel key in Vault, the Gateway's
    reader may read it and the Platform's may not, and the edge services run on its Gateway with their aliases resolved.
    The probes never reach a device or AWS: lab-edge renders (no device), lab-edge-push is handed a SHA-256 that
    cannot match and refuses before it reads the key or logs in, and aws-vpn-monitor is handed an instance ID it
    refuses before any AWS call. A refused run still proves the Gateway resolved every alias bound to the service.
    The rendered block's SHA-256 must equal the pinned render module's, run here. Prints yes/no and hashes only."""
    if V["aws_vpn"]["tier"] != _tier():
        print(f"  FAIL the edge services and aliases are on the {V['aws_vpn']['tier']} tier only (aws_vpn.tier, ADR 0070): "
              "run this there")
        return False
    targets, ok = _open_targets(), True
    if not targets:
        print("  FAIL no target is open in aws_vpn.targets")
        return False
    for name, entry in sorted(targets.items()):
        path = entry["psk_path"]
        _, d = vault("GET", kv(path), admin())
        data = (d or {}).get("data", {}).get("data", {})
        checks = {
            f"{path} holds a key lab-edge-push accepts": bool(PUSH_PSK.fullmatch(str(data.get("psk", "")))),
            f"{path} holds a valid key version": bool(PUSH_VERSION.fullmatch(str(data.get("version", "")))),
        }
        for role, want in (("itential-gateway", ["read"]), ("itential-platform", ["deny"])):
            token = _policy_token(role)
            try:
                _, c = vault("POST", "sys/capabilities-self", token, {"paths": [kv(path)]})
            finally:
                vault("POST", "auth/token/revoke-self", token)
            got = sorted((c or {}).get("capabilities", []))
            checks[f"{role} may {want} on {path} (has {got})"] = got == want
        # a target without pinned outputs (dc1-wan01, monitored by AWS) renders against its deployment's own, read
        # as Hand Off reads them: terraform-run's outputs action (a state read; nothing is planned or applied)
        outputs = entry.get("outputs")
        if outputs is None:
            rc, out = _service("terraform-run", {"action": "outputs", "timeout": "120"})
            outputs = out.get("outputs") if rc == 0 else None
            checks[f"the deployment's outputs read for {name} (terraform-run outputs)"] = bool(outputs)
            if not outputs:
                for check, passed in checks.items():
                    print(f"  {'ok  ' if passed else 'FAIL'} {check}")
                ok = False
                continue
        target_json, outputs_json = json.dumps(entry["target"]), json.dumps(outputs)
        want_sha = _pinned_sha(entry["target"], outputs)
        rc, out = _service("lab-edge", {"action": "render", "target_json": target_json, "outputs_json": outputs_json})
        checks["lab-edge render answers 0 for " + name] = rc == 0 and out.get("target") == name
        checks[f"lab-edge's SHA-256 equals the pinned module's ({want_sha[:12]})"] = out.get("sha256") == want_sha
        psk = data.get("psk")
        checks["the masked block holds no key"] = bool(out.get("block_masked")) and not (psk and psk in json.dumps(out))
        wrong = "0" * 64 if want_sha != "0" * 64 else "1" * 64
        rc, out = _service("lab-edge-push", {"action": "push", "target_json": target_json, "outputs_json": outputs_json,
                                             "sha256": wrong, "username": entry["username"]})
        checks["lab-edge-push ran with its aliases and refused before the device"] = (
            rc == 1 and out.get("sent") is False and out.get("key_sent") is False
            and "SHA-256 differs" in str(out.get("error")) and out.get("router") == "unchanged: nothing was sent")
        for check, passed in checks.items():
            print(f"  {'ok  ' if passed else 'FAIL'} {check}")
            ok &= passed
    rc, out = _service("aws-vpn-monitor", {"action": "ready", "instance_id": "not-an-instance"})
    monitor = rc == 1 and "not an EC2 instance ID" in str(out.get("error"))
    print(f"  {'ok  ' if monitor else 'FAIL'} aws-vpn-monitor ran with its aliases and refused before AWS")
    return ok and monitor

def c_seeded() -> bool:
    _, d = vault("GET", kv("devices/automation"), admin())
    same = sha(d["data"]["data"]["password"]) == sha(os.environ["DEVICE_PASSWORD"])
    print(f"devices/automation equals the .env seed: {same} (version {d['data']['metadata']['version']})")
    return same


def c_references() -> bool:
    p = Platform()
    ok = True
    # production's lab-hosts carry the alias too; dev's stays empty (ADR 0063)
    for inventory in [INVENTORY] + (["lab-hosts"] if os.environ.get("VAULT_TIER") == "prod" else []):
        inv = p.call("GET", f"/inventory_manager/v1/inventories/{inventory}/nodes?limit=200")["result"]["data"]
        kinds = {n["name"]: n["attributes"].get("itential_password") == ALIAS_REF for n in inv}
        print(f"inventory {inventory}: {sum(kinds.values())}/{len(kinds)} nodes carry {ALIAS_REF}")
        ok &= bool(kinds) and all(kinds.values())
    adapters = {r["data"]["name"]: r["data"] for r in p.call("GET", "/adapters?limit=100")["results"]}
    token = adapters["NetBox"]["properties"]["properties"]["authentication"]["token"]
    print(f"adapter NetBox token = {token if token.startswith('$SECRET_') else '<not a reference>'}")
    ok &= token == VAULT["platform_refs"]["netbox_adapter_token"]
    integrations = {r["data"]["name"]: r["data"] for r in p.call("GET", "/integrations?limit=100")["results"]}
    instance = V["integrations"]["models"]["netbox"]["instance"]
    value = next(iter(integrations[instance]["properties"]["properties"]["authentication"].values()))["value"]
    print(f"integration {instance} value = {value if value.startswith('$SECRET_') else '<not a reference>'}")
    ok &= value == VAULT["platform_refs"]["netbox_integration_value"]
    # hand-made instances no play creates (production's netbox-latest): checked wherever they exist
    for name, cfg in VAULT["external_integrations"].items():
        if name not in integrations:
            print(f"integration {name}: not on this Platform (external, skipped)")
            continue
        ext = integrations[name]["properties"]["properties"]["authentication"][cfg["auth"]]["value"]
        print(f"integration {name} value = {ext if ext.startswith('$SECRET_') else '<not a reference>'}")
        ok &= ext == VAULT["platform_refs"][cfg["ref"]]
    return ok


def c_gateway() -> bool:
    p = Platform()
    exp = p.call("GET", f"/gateway_manager/v1/gateways/{CLUSTER}/configuration/export")
    prov = [x for x in exp.get("secret-providers") or [] if x.get("name") == VAULT["gateway_provider"]]
    ok = len(prov) == 1 and prov[0].get("type") == "vault" and prov[0].get("auth-method") == "approle" \
        and prov[0].get("url") == os.environ["VAULT_ADDR"] \
        and prov[0].get("secrets-endpoint") == f"{VAULT['kv_mount']}/data" \
        and prov[0].get("secret-id-file") == VAULT["gateway_secret_id_file"]
    # the role ID can be compared only with an administrator token (production has none once the root is revoked)
    if ok and os.environ.get("VAULT_ADMIN_TOKEN"):
        _, rid = vault("GET", f"auth/{VAULT['approle_mount']}/role/itential-gateway/role-id", admin())
        ok = prov[0].get("role-id") == rid["data"]["role_id"]
    print(f"provider {VAULT['gateway_provider']}: {'as the oracle says' if ok else [{k: v for k, v in x.items() if k != 'role-id'} for x in prov]}"
          f"{', role ID matches Vault' if ok and os.environ.get('VAULT_ADMIN_TOKEN') else ''}")
    got = {s["name"]: (s["secret"], s.get("key")) for s in exp.get("secrets") or []
           if s.get("provider") == VAULT["gateway_provider"]}
    want = {k: (v["path"], v["key"]) for k, v in {**VAULT["gateway_aliases"], **_edge_bound()}.items()}
    # inert leftovers the import cannot remove (versions.yaml vault.retired_gateway_aliases; ADR 0070): named, not compared
    leftovers = set(VAULT["retired_gateway_aliases"]) | (set() if V["aws_vpn"]["tier"] == _tier()
                                                         else set(VAULT["edge_gateway_aliases"]))
    held = sorted(set(got) & leftovers)
    got = {k: v for k, v in got.items() if k not in leftovers}
    if held:
        print(f"inert leftovers (not compared): {held}")
    print(f"aliases: {sorted(got)}" + ("" if got == want else
          f"; missing {sorted(set(want) - set(got))}, extra {sorted(set(got) - set(want))}, "
          f"different {sorted(k for k in set(got) & set(want) if got[k] != want[k])}"))
    return ok and got == want


def _tier() -> str:
    return "prod" if os.environ.get("VAULT_TIER") == "prod" else "dev"


def _edge_bound() -> dict:
    """The edge aliases this tier's Gateway should hold (tasks/gateway-vault.yml): none unless this tier runs the AWS
    VPN (versions.yaml aws_vpn.tier, ADR 0070); there, each whose Vault entry has its key - an after_deploy one is
    bound only once the first Deploy AWS VPN has written it."""
    if V["aws_vpn"]["tier"] != _tier():
        return {}
    out = {}
    for name, ref in VAULT["edge_gateway_aliases"].items():
        _, d = vault("GET", kv(ref["path"]), admin())
        if ref["key"] in ((d or {}).get("data") or {}).get("data", {}) or {}:
            out[name] = ref
    return out


def c_devices() -> bool:
    p = Platform()
    names = nodes()
    res = p.run("send-command", {"commands": ["show clock"]}, names)
    print(f"show clock with the Vault-held password: {res}")
    failed = [n for n in names if not res.get(n)]
    if failed:
        # The nested-KVM C8000v routers sometimes drop SSH session setup under a burst of logins ("No existing session",
        # a transport timeout, measured 2026-09-23) - one retry after a pause, shown here. A wrong password fails twice.
        time.sleep(10)
        again = p.run("send-command", {"commands": ["show clock"]}, failed)
        print(f"retried once after 10 s: {again}")
        res.update(again)
    return bool(res) and all(res.values()) and set(res) == set(names)


def c_hosts() -> bool:
    """Production's lab-hosts (Ubuntu, the automation account) carry the same alias as the devices."""
    p = Platform()
    names = nodes("lab-hosts")
    res = p.run("send-command", {"commands": ["hostname"]}, names, inventory="lab-hosts")
    print(f"hostname on lab-hosts with the Vault-held password: {res}")
    return bool(res) and all(res.values()) and set(res) == set(names)


def c_platform_read() -> bool:
    d = Platform().call("POST", "/NetBox/getDcimDevices", {"queryData": {"limit": 1}})
    count = (d.get("response") or {}).get("count")
    print(f"NetBox adapter (token from Vault through the Platform): {count} devices")
    return isinstance(count, int) and count > 0


def c_rotation() -> bool:
    """E15: the device alone changed -> the next job fails; Vault alone changed -> the next job succeeds with nothing
    changed on the Platform or the Gateway; then both restored. Running config only, never saved."""
    node = next(n["name"] for n in CLAB["nodes"] if n["netmiko"] == "cisco_ios")
    user = CLAB["credentials"]["user"]
    old = os.environ["DEVICE_PASSWORD"]
    new = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(24))
    p = Platform()

    def clock() -> bool:
        return p.run("send-command", {"commands": ["show clock"]}, [node]).get(node, False)

    def device(pw: str) -> bool:
        return p.run("send-config", {"config": f"username {user} privilege 15 secret 0 {pw}"}, [node]).get(node, False)

    def set_vault(pw: str) -> bool:
        return vault("POST", kv("devices/automation"), admin(), {"data": {"username": user, "password": pw}})[0] == 200

    def step(label: str, got: bool, want: bool) -> bool:
        print(f"  {label:<60} {'PASS' if got == want else 'FAIL'} (job succeeded: {got})")
        return got == want

    print(f"node {node}")
    ok = True
    device_new = vault_new = False
    try:
        ok &= step("baseline show clock", clock(), True)
        device_new = device(new)
        ok &= step("password changed on the device only (running config)", device_new, True)
        ok &= step("next job must FAIL: Vault still holds the old value", clock(), False)
        vault_new = set_vault(new)
        ok &= step("same password written to Vault only", vault_new, True)
        ok &= step("next job must SUCCEED: nothing changed on Platform/Gateway", clock(), True)
    finally:
        if device_new:
            if not vault_new:
                vault_new = set_vault(new)
            ok &= step("restore: device back to the original password", device(old), True)
        if vault_new:
            ok &= step("restore: Vault back to the .env value", set_vault(old), True)
        ok &= step("final show clock with the original password", clock(), True)
    return ok


def _job(p: Platform, workflow: str, variables: dict, wait: int = 240) -> dict:
    """Start a job and wait for it to leave `running`; returns status, whether it reached workflow_end, its task
    errors, its variables and how long it took."""
    started = time.time()
    job_id = p.call("POST", "/operations-manager/jobs/start",
                    {"workflow": workflow, "options": {"type": "automation", "variables": variables}})["data"]["_id"]
    d: dict = {}
    while time.time() - started < wait:
        time.sleep(3)
        d = p.call("GET", f"/operations-manager/jobs/{job_id}")["data"]
        if d["status"] != "running":
            break
    errors = [e for e in d.get("error") or [] if isinstance(e, dict)]
    dead_end = any("no available transitions" in str(e.get("message", "")) for e in errors)
    return {"id": job_id, "status": d.get("status"), "dead_end": dead_end, "variables": d.get("variables") or {},
            "errors": [(e.get("task"), str(e.get("message"))[:140]) for e in errors], "seconds": round(time.time() - started)}


def _running_config(p: Platform, device: str) -> str:
    """The device's running config through Configuration Manager, minus the lines that change on every read."""
    raw = p.call("GET", f"/configuration_manager/devices/{device}/configuration").get("config") or ""
    keep = [ln for ln in raw.splitlines() if not ln.startswith(("! Last configuration change", "! NVRAM config last",
                                                                "Current configuration :", "Building configuration"))]
    return "\n".join(keep)


def c_sealed() -> bool:
    """E14: with Vault sealed, a device job (Gateway resolves the password) and a NetBox job (the Platform resolves
    the token) must each reach workflow_end through their error path - never dead-end, which leaves a retryable job
    that hangs a calling agent - and change nothing on the device; unsealed again, the same jobs succeed."""
    device = next(n["name"] for n in CLAB["nodes"] if n["netmiko"] == "cisco_ios")
    jobs = ((V["workflows"]["show_version"], {"device": device}), (V["workflows"]["netbox_devices"], {"filter": ""}))
    p = Platform()
    ok = True
    config_before = sha(_running_config(p, device))
    for wf, var in jobs:
        r = _job(p, wf, var)
        print(f"baseline   {wf:28} {r['status']:9} {r['seconds']:>3}s")
        ok &= r["status"] == "complete"
    st, _ = vault("PUT", "sys/seal", admin())
    print(f"Vault sealed: PUT sys/seal -> {st}; health sealed={vault('GET', 'sys/health')[0] == 503}")
    try:
        for wf, var in jobs:
            r = _job(p, wf, var)
            flag = next((k for k in ("device_error", "netbox_error") if r["variables"].get(k)), None)
            clean = r["status"] == "complete" and not r["dead_end"] and flag is not None
            print(f"sealed     {wf:28} {r['status']:9} {r['seconds']:>3}s  reached end via error path: {clean}"
                  f"{'  (' + flag + ')' if flag else ''}{'  DEAD-END' if r['dead_end'] else ''}  errors={r['errors']}")
            ok &= clean
            if r["status"] != "complete":
                p.call("POST", "/operations-manager/jobs/cancel", {"jobIds": [r["id"]]})  # never leave a retryable job
    finally:
        st, d = vault("PUT", "sys/unseal", None, {"key": os.environ["VAULT_UNSEAL_KEY"]})
        print(f"Vault unsealed: {st} sealed={d and d.get('sealed')}")
    time.sleep(5)
    for wf, var in jobs:
        r = _job(p, wf, var)
        print(f"unsealed   {wf:28} {r['status']:9} {r['seconds']:>3}s")
        ok &= r["status"] == "complete" and not any(r["variables"].get(k) for k in ("device_error", "netbox_error"))
    same = sha(_running_config(p, device)) == config_before
    print(f"{device} running config unchanged: {same}")
    return ok and same


CHECKS = {"sealed": c_sealed, "health": c_health, "no-token": c_no_token, "policies": c_policies, "bound": c_bound, "aws-psk": c_aws_psk, "terraform-run": c_terraform_run, "cdp-pin": c_cdp_pin, "lab-edge": c_lab_edge, "seeded": c_seeded,
          "references": c_references, "gateway": c_gateway, "devices": c_devices, "platform-read": c_platform_read,
          "rotation": c_rotation, "hosts": c_hosts}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in CHECKS:
        sys.exit(f"usage: {sys.argv[0]} {{{'|'.join(CHECKS)}}}")
    try:
        sys.exit(0 if CHECKS[sys.argv[1]]() else 1)
    except Exception as e:  # noqa: BLE001 - a check that throws is a failed check, with the reason shown
        print(f"{type(e).__name__}: {e}")
        sys.exit(1)
