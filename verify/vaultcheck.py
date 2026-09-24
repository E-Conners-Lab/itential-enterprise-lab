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
import secrets
import ssl
import string
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
    """The AppRoles are address-bound: both carry the expected bound CIDRs, and a login with a freshly issued, valid
    secret ID is refused from this machine (which is outside them)."""
    def norm(cidrs: list[str]) -> list[str]:  # Vault drops the /32 of a single-host token_bound_cidrs entry
        return sorted(c.removesuffix("/32") for c in cidrs)

    # production binds each role to its own hosts (VAULT_ROLE_CIDRS, JSON {role: [cidr]}); the dev tier binds both
    # roles to the one vault-dev Docker gateway (VAULT_BOUND_CIDRS, comma-separated)
    per_role = json.loads(os.environ["VAULT_ROLE_CIDRS"]) if os.environ.get("VAULT_ROLE_CIDRS") else None
    ok = True
    for role in ("itential-platform", "itential-gateway"):
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
    aliases = {s["name"]: (s["secret"], s.get("key")) for s in exp.get("secrets") or []
               if s.get("provider") == VAULT["gateway_provider"]}
    want = {k: (v["path"], v["key"]) for k, v in VAULT["gateway_aliases"].items()}
    print(f"aliases: {sorted(aliases)}")
    return ok and aliases == want


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


CHECKS = {"sealed": c_sealed, "health": c_health, "no-token": c_no_token, "policies": c_policies, "bound": c_bound, "seeded": c_seeded,
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
