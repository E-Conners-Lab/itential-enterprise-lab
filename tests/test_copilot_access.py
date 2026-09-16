"""ADR 0063 / PID S12 unit tests: what svc-copilot may do, and the proof that building it cannot change production.

itential/copilot/roles.yaml is the role oracle. The deny list below is written here independently of that file, from
the measurements of 2026-09-16 (plan finding 4: an inventory read returns device passwords; finding 5: built-in
"read" roles that can write), so an edit to the oracle cannot quietly widen production access. The task file, the
production play, the NetBox token play, verify/prod-snapshot.py and verify/test-05b-dev-copilot.sh are held to the
rules the owner set: passwords on stdin only and never logged, group writes only to copilot-* groups, a snapshot
that can only GET (plus the login POST), and a NetBox token that cannot write. No lab access."""

from __future__ import annotations

import ast
import fnmatch
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path
from types import ModuleType

import jinja2
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
ROLES = ROOT / "itential" / "copilot" / "roles.yaml"
PLAYS = ROOT / "ansible" / "playbooks"
LDAP_TASK = PLAYS / "tasks" / "ldap-service-account.yml"
PROD_PLAY = PLAYS / "copilot-access.yml"
TOKEN_PLAY = PLAYS / "netbox-token-dev.yml"
SNAPSHOT = ROOT / "verify" / "prod-snapshot.py"
VERIFY = ROOT / "verify" / "test-05b-dev-copilot.sh"
LDIF = ROOT / "itential" / "ldap" / "openldap.ldif"

# Never on production, whatever roles.yaml says. Globs over "<provenance>/<name>".
DENY = [
    # finding 5, measured on 6.5.2: carry "read" in the name and can write
    "ConfigurationManager/apiread",  # runCompliance*, runAdapterTask, runTaskInstance, cacheDevices, handlePin
    "LifecycleManager/apiread",  # cancelActionExecution, updateInstanceMetadataHttp
    "LifecycleManager/operator",  # cancelActionExecution
    "JsonForms/apiread",  # createForm, deleteForms, updateForm
    "JsonForms/readonly",
    "Jst/apiread",  # create, delete
    "Jst/readonly",
    "Tags/readonly",  # create, delete, update
    "PrebuiltsRepository/apiread",  # repository config create, delete
    "FormBuilder/apiread",  # preserveFormData
    # finding 4: GET .../inventories/lab/nodes returns AUTOMATION_PASSWORD in clear
    "InventoryManager/*",
    # owner decision: GetBootFlash is a live device read
    "MOP/apiread",
    # identity, sessions, server
    "Authorization/*",
    "Server/apiread",
    "Profiles/*",
    "OAuth/*",
    "SSO*/*",
]
# every write-shaped built-in role name
WRITE_SHAPED = ["*/admin", "*/apiwrite", "*/designer", "*/engineering", "*/operations", "*/support", "*/*:create", "*/*:update", "*/*:delete", "*/*:run", "*/*:code"]
READ_PREFIX = re.compile(r"^(get|search|list|export|lookup|is|validate|render|parse|translate|buildSpec|find)")
# methods that are reads but do not start with a read prefix (none are needed today; add with the reason)
METHOD_ALLOWLIST: set[str] = set()

DEV_ADMIN_ROLES = {
    ("Authorization", "admin"),
    ("UserManagement", "admin"),
    ("AdminEssentials", "admin"),
    ("OAuth", "admin"),
    ("SSO", "admin"),
    ("SSOGroupMappings", "admin"),
    ("Customization", "admin"),
    ("Profiles", "admin"),
    ("Server", "admin"),
    ("Indexes", "admin"),
    ("LDAP", "admin"),
}

# a task that reads, builds or sends one of these holds a secret
SECRET_VARS = re.compile(r"\bsvc_pw\b|svc_password|svc_ldap_bind_pw|ldap_bind_password|admin_pw|mongo_shell_argv|mongo_uri")
NETBOX_SECRETS = re.compile(r"nb_headers|nb_token|ro_token|cur_token|new_token|created\.json")


@pytest.fixture(scope="module")
def roles() -> dict:
    return yaml.safe_load(ROLES.read_text())


def _ansible_env() -> jinja2.Environment:
    """Jinja with the Ansible filters and tests the expressions below use."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("yes", "on", "1", "true")
    env.filters["zip"] = lambda a, b: list(zip(a, b))
    env.tests["match"] = lambda value, pattern: re.match(pattern, value) is not None
    return env


def _evaluate(expression: str, **context: object) -> object:
    return _ansible_env().compile_expression(expression)(**context)


def _key(role: dict) -> str:
    return f"{role['provenance']}/{role['name']}"


def _tasks(path: Path) -> list[dict]:
    doc = yaml.safe_load(path.read_text())
    if doc and "hosts" in doc[0]:
        return [t for play in doc for t in play.get("tasks", [])]
    return doc


def _module(task: dict) -> tuple[str, object]:
    for key, value in task.items():
        if key.startswith("ansible.builtin.") or key.startswith("community."):
            return key.split(".")[-1], value
    return "", None


def _is_write_uri(task: dict) -> bool:
    mod, args = _module(task)
    return mod == "uri" and str(args.get("method", "GET")).upper() != "GET"


# --- The role oracle -----------------------------------------------------------------------------------------


def test_the_oracle_names_the_groups_the_owner_chose(roles: dict) -> None:
    assert roles["prod"]["group"] == "copilot-readonly"
    assert roles["dev"]["group"] == "copilot-builders"
    assert [c["name"] for c in roles["prod"]["custom"]] == ["copilot-cm-read", "copilot-lcm-read", "copilot-jst-read"]


def test_production_builtins_hold_nothing_denied(roles: dict) -> None:
    keys = [_key(r) for r in roles["prod"]["builtin"]]
    assert len(keys) == len(set(keys)), "duplicate built-in role"
    denied = [k for k in keys for pattern in DENY + WRITE_SHAPED if fnmatch.fnmatchcase(k, pattern)]
    assert not denied, f"denied roles in the production set: {denied}"
    assert not any(k.startswith("InventoryManager/") for k in keys)
    assert "MOP/apiread" not in keys


def test_the_oracle_deny_list_is_at_least_as_strict_as_this_test(roles: dict) -> None:
    oracle = [_key(r) for r in roles["prod"]["deny"]]
    for pattern in DENY + WRITE_SHAPED:
        # a concrete role the test pattern stands for must be caught by some oracle glob
        probe = pattern.replace("*", "x")
        assert any(fnmatch.fnmatchcase(probe, o) for o in oracle), f"{pattern} is not denied by roles.yaml"


def test_custom_roles_allow_only_read_methods(roles: dict) -> None:
    for custom in roles["prod"]["custom"]:
        assert custom["name"].startswith("copilot-")
        assert custom["methods"], custom["name"]
        for method in custom["methods"]:
            assert READ_PREFIX.match(method) or method in METHOD_ALLOWLIST, f"{custom['name']}: {method} is not a read"
            assert "*" not in method.rstrip("*"), f"{custom['name']}: only trailing globs ({method})"
        for method in custom.get("exclude", []):
            assert not any(method == m for m in custom["methods"]), f"{custom['name']}: {method} both allowed and excluded"


def test_the_measured_write_methods_are_excluded(roles: dict) -> None:
    by_name = {c["name"]: c for c in roles["prod"]["custom"]}

    def allowed(role: str, method: str) -> bool:
        c = by_name[role]
        inc = any(fnmatch.fnmatchcase(method, m) for m in c["methods"])
        exc = any(fnmatch.fnmatchcase(method, m) for m in c.get("exclude", []))
        return inc and not exc

    for method in ("runCompliance", "runAdapterTask", "runTaskInstance", "cacheDevices", "handlePin", "convertChangesToConfig"):
        assert not allowed("copilot-cm-read", method), method
    for method in ("cancelActionExecution", "updateInstanceMetadataHttp"):
        assert not allowed("copilot-lcm-read", method), method
    assert allowed("copilot-cm-read", "getDevice") and allowed("copilot-lcm-read", "exportResource")
    assert by_name["copilot-jst-read"]["methods"] == ["getTransformation", "searchTransformations"]


def test_dev_excludes_exactly_the_admin_roles(roles: dict) -> None:
    assert {(r["provenance"], r["name"]) for r in roles["dev"]["exclude"]} == DEV_ADMIN_ROLES


# --- tasks/ldap-service-account.yml ---------------------------------------------------------------------------


def test_the_ldap_task_never_writes_the_pinned_ldif() -> None:
    assert LDIF.exists()
    for task in _tasks(LDAP_TASK):
        mod, args = _module(task)
        assert "openldap.ldif" not in json.dumps(args), f"{task['name']} references the pinned LDIF"
        assert mod not in {"copy", "template", "lineinfile", "blockinfile", "replace"} or "ldif" not in json.dumps(args)


def test_the_ldap_task_uses_only_general_options_ldapsearch_accepts() -> None:
    """OpenLDAP's -o options are hyphenated. `-o ldif_wrap=no` made ldapsearch exit 1 with "Invalid general option
    name: ldif_wrap" and stopped make dev-stack (2026-09-16); `ldif-wrap=no` was then run against the dev directory."""
    accepted = {"ldif-wrap", "nettimeout"}  # ldapsearch --help, general options
    options = re.findall(r"'-o',\s*'([^'=]+)=", LDAP_TASK.read_text())
    assert options, "the directory searches pass -o ldif-wrap"
    assert set(options) <= accepted, options


def test_the_ldap_task_passes_passwords_only_on_stdin() -> None:
    commands = [t for t in _tasks(LDAP_TASK) if _module(t)[0] == "command"]
    assert commands
    for task in commands:
        args = _module(task)[1]
        argv = json.dumps(args.get("argv", ""))
        if "mongo_shell_argv" in argv:
            continue  # the fallback's argv is the caller's mongosh prefix (tasks/ldap-admin.yml); no_log below
        assert "cmd" not in args, f"{task['name']}: a shell string, not argv"
        assert not SECRET_VARS.search(argv) and "'-w'" not in argv, f"{task['name']}: a password in argv"
        if "ldap" in argv or "slappasswd" in argv:
            assert "stdin" in args, f"{task['name']}: the directory tools read the password from stdin"
    # the wrapper hands the stdin line to -y as a 0600 temp file and removes it
    text = LDAP_TASK.read_text()
    assert "umask 077" in text and '-y "$f"' in text and 'rm -f "$f"' in text
    assert "-T, /dev/stdin" in text and "stdin_add_newline: false" in text


def test_every_password_task_is_no_log() -> None:
    checked = 0
    for path, pattern in ((LDAP_TASK, SECRET_VARS), (PROD_PLAY, SECRET_VARS), (TOKEN_PLAY, NETBOX_SECRETS)):
        for task in _tasks(path):
            mod, args = _module(task)
            if mod == "assert" and "no_log" not in task:
                # an assert holding a secret would print it on failure
                assert not pattern.search(json.dumps(args["that"])), f"{path.name}: '{task['name']}' asserts on a secret"
                continue
            if mod == "include_tasks":
                continue
            body = json.dumps({k: v for k, v in task.items() if k not in {"name", "loop_control"}})
            if pattern.search(body) or '"stdin"' in body:
                checked += 1
                assert task.get("no_log") is True, f"{path.name}: '{task['name']}' holds a secret without no_log"
    assert checked >= 20


def test_the_task_writes_only_copilot_groups() -> None:
    tasks = _tasks(LDAP_TASK)
    text = LDAP_TASK.read_text()
    assert "svc_group is match('copilot-')" in text
    guard = next(t for t in tasks if t["name"].startswith("Only a copilot- group"))
    assert "svc_group is match('copilot-')" in guard["ansible.builtin.assert"]["that"]
    for task in tasks:
        if not _is_write_uri(task):
            continue
        url = _module(task)[1]["url"]
        assert "admin_group" not in json.dumps(task), f"{task['name']} names admin_group in a write"
        if "/authorization/groups" in url:
            when = json.dumps(task.get("when", ""))
            assert "match('copilot-')" in when, f"{task['name']} writes a group without the copilot- guard"
        else:
            assert url.endswith("/login"), f"{task['name']}: unexpected write {url}"


def test_the_fallback_is_gated_on_the_measurement() -> None:
    tasks = _tasks(LDAP_TASK)
    fallback = [t for t in tasks if t["name"].startswith("Fallback")]
    assert len(fallback) >= 5
    for task in fallback:
        assert "svc_group_persisted" in json.dumps(task.get("when", "")), f"{task['name']} is not gated"
    assert any("aaaManaged: false" in json.dumps(t) for t in fallback), "the database membership of ldap-admin.yml"


# --- copilot-access.yml ----------------------------------------------------------------------------------------


def test_the_production_play_refuses_the_dev_overlay_first() -> None:
    doc = yaml.safe_load(PROD_PLAY.read_text())
    assert len(doc) == 1 and doc[0]["hosts"] == "ha2-platform[0]"
    first = doc[0]["tasks"][0]
    that = first["ansible.builtin.assert"]["that"]
    assert that == "not (dev_overlay | default(false) | bool)"
    assert _evaluate(that) is True and _evaluate(that, dev_overlay="false") is True
    assert _evaluate(that, dev_overlay=True) is False and _evaluate(that, dev_overlay="true") is False, "-e dev_overlay=true"


def test_production_fails_instead_of_taking_the_fallback() -> None:
    """Review L5: an unexpected measurement on production must stop the play, not write a -local group and a
    MongoDB membership. The dev call keeps the fallback (the task's default)."""
    tasks = yaml.safe_load(PROD_PLAY.read_text())[0]["tasks"]
    include = next(t for t in tasks if t.get("ansible.builtin.include_tasks") == "tasks/ldap-service-account.yml")
    assert include["vars"]["ldap_service_account_fallback"] is False
    gate = next(t for t in _tasks(LDAP_TASK) if t["name"] == "Fallback is allowed and stays a copilot- group")
    allowed = gate["ansible.builtin.assert"]["that"][0]
    assert _evaluate(allowed, ldap_service_account_fallback=False) is False, "production: the assert fails"
    assert _evaluate(allowed) is True, "dev (no override): the fallback stays available"
    # the gate comes before every fallback write
    names = [t["name"] for t in _tasks(LDAP_TASK)]
    writes = [i for i, t in enumerate(_tasks(LDAP_TASK)) if t["name"].startswith("Fallback") and (_is_write_uri(t) or _module(t)[0] == "command")]
    assert writes and names.index(gate["name"]) < min(writes)
    dev_include = next(t for t in _tasks(PLAYS / "itential.yml") if t.get("ansible.builtin.include_tasks") == "tasks/ldap-service-account.yml")
    assert "ldap_service_account_fallback" not in dev_include["vars"]


def test_the_production_play_writes_only_roles_and_copilot_readonly() -> None:
    doc = yaml.safe_load(PROD_PLAY.read_text())
    tasks = doc[0]["tasks"]
    for task in tasks:
        if not _is_write_uri(task):
            continue
        url = _module(task)[1]["url"]
        assert "admin_group" not in json.dumps(task)
        assert url.endswith("/login") or url.endswith("/authorization/roles"), f"{task['name']}: {url}"
        if url.endswith("/authorization/roles"):
            assert _module(task)[1]["method"] == "POST"
            assert "length == 0" in json.dumps(task["when"]), "custom roles are created only when absent"
    include = next(t for t in tasks if t.get("ansible.builtin.include_tasks") == "tasks/ldap-service-account.yml")
    assert include["vars"]["svc_group"] == "copilot-readonly"
    assert include["vars"]["svc_role_set"] == "prod"
    assert include["vars"]["password_env"] == "SVC_COPILOT_PROD_PASSWORD"
    # the body shape is read from an existing role before any POST
    names = [t["name"] for t in tasks]
    shape = names.index("The role document shape, read from an existing role")
    post = next(i for i, t in enumerate(tasks) if _is_write_uri(t) and _module(t)[1]["url"].endswith("/authorization/roles"))
    assert shape < post
    for forbidden in ("inventory_manager", "gateway_manager"):
        assert forbidden not in PROD_PLAY.read_text()


# --- netbox-token-dev.yml --------------------------------------------------------------------------------------


def test_the_dev_netbox_token_cannot_write() -> None:
    tasks = _tasks(TOKEN_PLAY)
    create = next(t for t in tasks if _is_write_uri(t) and _module(t)[1]["url"].endswith("/api/users/tokens/"))
    body = _module(create)[1]["body"]
    assert body["write_enabled"] is False
    assert body["description"], "netbox-token.yml deletes every token without a description"
    assert "365 * 86400" in body["expires"]
    user = next(t for t in tasks if _is_write_uri(t) and _module(t)[1]["url"].endswith("/api/users/users/"))
    assert _module(user)[1]["body"]["is_superuser"] is False
    perm = next(t for t in tasks if _module(t)[0] == "uri" and "/api/users/permissions/" in _module(t)[1]["url"] and _is_write_uri(t))
    assert _module(perm)[1]["body"]["actions"] == ["view"]
    # persisted before use, then the 403 proof
    names = [t["name"] for t in tasks]
    persist = next(i for i, t in enumerate(tasks) if _module(t)[0] == "lineinfile")
    assert yaml.safe_load(TOKEN_PLAY.read_text())[0]["vars"]["env_key"] == "NETBOX_DEV_RO_TOKEN"
    assert _module(tasks[persist])[1]["regexp"] == "^{{ env_key }}="
    probe = next(i for i, t in enumerate(tasks) if _module(t)[0] == "uri" and "vlan-groups" in _module(t)[1]["url"])
    assert _module(tasks[probe])[1]["status_code"] == 403
    reads = names.index("The token reads (GET /api/status/ and a device list)")
    assert persist < reads < probe
    # deletes only this user's tokens
    delete = next(t for t in tasks if _module(t)[0] == "uri" and _module(t)[1].get("method") == "DELETE")
    assert "user.id" in json.dumps(delete["loop"])


def test_the_dev_netbox_permission_never_covers_users_objects() -> None:
    """View on users.token would expose legacy token keys (credentials); users.* is excluded from the permission."""
    tasks = _tasks(TOKEN_PLAY)
    labels = next(t for t in tasks if "all_object_types" in (t.get("ansible.builtin.set_fact") or {}))
    expr = labels["ansible.builtin.set_fact"]["all_object_types"].strip().removeprefix("{{").removesuffix("}}")
    types = [("dcim", "device"), ("ipam", "vlan"), ("users", "token"), ("users", "user"), ("users", "group"),
             ("users", "objectpermission"), ("extras", "tag"), ("dcim", "users")]  # a model named users is not the app
    results = [{"app_label": a, "model": m} for a, m in types]
    got = _evaluate(expr, nb_types={"json": {"results": results}})
    assert got == ["dcim.device", "dcim.users", "extras.tag", "ipam.vlan"], got
    # the permission POST/PATCH sends exactly that list, and the re-read refuses a users.* type NetBox kept
    perm = next(t for t in tasks if _is_write_uri(t) and "/api/users/permissions/" in _module(t)[1]["url"])
    assert _module(perm)[1]["body"]["object_types"] == "{{ all_object_types }}"
    proof = next(t for t in tasks if t["name"].startswith("The permission allows view and nothing else"))
    check = next(c for c in proof["ansible.builtin.assert"]["that"] if "object_types" in c)
    reread = {"json": {"results": [{"object_types": ["dcim.device", "users.token"]}]}}
    assert _evaluate(check, nb_perm2=reread) is False
    reread["json"]["results"][0]["object_types"] = ["dcim.device", "dcim.users"]
    assert _evaluate(check, nb_perm2=reread) is True


# --- verify/prod-snapshot.py -----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def snapshot() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prod_snapshot", SNAPSHOT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # importing does no network: everything runs from main()
    return mod


def test_the_snapshot_only_gets_and_logs_in() -> None:
    source = SNAPSHOT.read_text()
    tree = ast.parse(source)
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in (n.names if isinstance(n, ast.Import) else [ast.alias(n.module or "")])}
    assert "requests" not in imported and "http" not in imported
    verbs = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}}
    assert verbs == {"GET", "POST"}, verbs
    # every POST is the login
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and any(isinstance(a, ast.Constant) and a.value == "POST" for a in node.args):
            assert "/login" in ast.unparse(node), ast.unparse(node)
    # urllib requests are built in exactly one place, the guarded one
    requests = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("urllib.request.Request")]
    assert len(requests) == 1
    assert "--save" in source and "--compare" in source and "--allow" in source
    assert "docs\" / \"lab-root-ca.crt" in source


def test_the_request_guard_refuses_anything_but_get_and_login(snapshot: ModuleType) -> None:
    api = object.__new__(snapshot.HttpApi)
    api.platform = "https://itential.example"
    for method, url in (("DELETE", "https://itential.example/x"), ("PATCH", "https://itential.example/authorization/groups/1"), ("POST", "https://itential.example/authorization/roles")):
        with pytest.raises(RuntimeError, match="refused"):
            api._request(method, url, {})


class _FakeResponse:
    def __init__(self, body: str, cookies: list[str]) -> None:
        self._body, self._cookies = body, cookies
        self.headers = self

    def get_all(self, name: str) -> list[str]:
        return self._cookies if name == "Set-Cookie" else []

    def read(self) -> bytes:
        return self._body.encode()

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_login_accepts_a_plain_text_token_and_gets_still_parse_json(snapshot: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Measured on production 2026-09-16: POST /login answers with a plain-text token, not JSON. The first
    `make prod-snapshot MODE=save` crashed on json.loads before its first GET; the fake HTTP layer above had
    never exercised the real response body."""
    bodies = {"/login": "eyJhbGciOiJIUzI1NiJ9.token-not-json", "/health/status": '{"ok": true}'}
    monkeypatch.setattr(
        snapshot.urllib.request, "urlopen",
        lambda req, timeout, context: _FakeResponse(bodies[req.full_url.split("example")[1]], ["token=abc; Path=/"]),
    )
    api = object.__new__(snapshot.HttpApi)
    api.platform, api.user, api._password, api._tls, api._cookie = "http://itential.example", "u", "p", None, ""
    api.login()
    assert api._cookie == "token=abc"
    assert api._request("GET", "http://itential.example/health/status", {})[0] == {"ok": True}


class FakeApi:
    def __init__(self, roles_total: int = 186, extra_group: bool = False, wf_updated: str = "2026-09-10") -> None:
        self.calls: list[str] = []
        self.roles_total = roles_total
        self.extra_group = extra_group
        self.wf_updated = wf_updated

    def platform_get(self, path: str) -> object:
        self.calls.append(path)
        base = path.split("?")[0]
        if base == "/automation-studio/workflows":
            return {"items": [{"name": "wf-a", "lastUpdated": self.wf_updated}, {"name": "wf-b", "lastUpdated": "2026-09-01"}], "total": 2}
        if base == "/inventory_manager/v1/inventories":
            return {"result": {"data": [{"name": "lab", "groups": ["admin_group"]}]}}
        if base.endswith("/nodes"):
            return {"result": {"data": [{"name": "br1-sw01", "attributes": {"itential_password": "s3cret-device-pw"}}]}}
        if base == "/configuration_manager/deviceGroups":
            return [{"name": "site-br1", "devices": ["br1-sw01"]}]
        if base == "/agent-project-service/operable-agents":
            return {"data": {"items": [{"name": "netbox-sot", "updated": "2026-09-09"}]}}
        if base == "/model-registry-service/profiles":
            return {"profiles": [{"name": "anthropic"}, {"name": "ollama-mac"}]}
        # the envelope production really returns (measured 2026-09-16): the name is in data.name
        if base == "/integrations":
            return {"results": [{"metadata": {"IsActive": True}, "data": {"name": "netbox-api", "model": "lab-netbox:1.0.0"}}], "total": 1}
        if base == "/adapters":
            return {"results": [{"metadata": {"isActive": True}, "data": {"name": "NetBox", "model": "adapter-netbox"}}], "total": 1}
        if base == "/authorization/roles":
            return {"results": [], "total": self.roles_total}
        if base == "/authorization/groups":
            groups = [{"name": "admin_group", "assignedRoles": [{"roleId": str(i)} for i in range(186)]}, {"name": "admins", "assignedRoles": []}]
            if self.extra_group:
                groups.append({"name": "copilot-readonly", "assignedRoles": [{"roleId": "1"}]})
            return {"results": groups, "total": len(groups)}
        raise AssertionError(f"unexpected GET {path}")

    def netbox_get(self, path: str) -> object:
        return {"count": 12 if "devices" in path else 40}


def test_the_fingerprint_keeps_no_node_attributes(snapshot: ModuleType) -> None:
    fp = snapshot.collect(FakeApi())
    assert fp["inventories"]["lab"] == {"groups": ["admin_group"], "nodes": ["br1-sw01"]}
    assert "s3cret-device-pw" not in json.dumps(fp)
    assert fp["authorization"]["admin_group_roles"] == 186 and fp["authorization"]["roles_total"] == 186
    assert fp["netbox"] == {"devices": 12, "vlans": 40}
    assert fp["workflows"] == {"wf-a": "2026-09-10", "wf-b": "2026-09-01"}


def test_save_then_compare_exits_non_zero_on_any_difference(snapshot: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert snapshot.main(["--compare"], api=FakeApi(), results=tmp_path) == 2, "no baseline yet"
    assert snapshot.main(["--save"], api=FakeApi(), results=tmp_path) == 0
    assert (tmp_path / "prod-snapshot-latest.json").exists()
    assert snapshot.main(["--compare"], api=FakeApi(), results=tmp_path) == 0
    assert snapshot.main(["--compare"], api=FakeApi(wf_updated="2026-09-16"), results=tmp_path) == 1
    assert "workflows::wf-a" in capsys.readouterr().out
    # the Copilot additions fail without the allowlist and pass with it
    assert snapshot.main(["--compare"], api=FakeApi(roles_total=189, extra_group=True), results=tmp_path) == 1
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps({"delta": {"authorization::roles_total": 3, "authorization::groups_total": [1, 2]}, "added": ["authorization::groups::copilot-*"]}))
    assert snapshot.main(["--compare", "--allow", str(allow)], api=FakeApi(roles_total=189, extra_group=True), results=tmp_path) == 0
    # an allowlist never excuses a different amount or another change
    assert snapshot.main(["--compare", "--allow", str(allow)], api=FakeApi(roles_total=190, extra_group=True), results=tmp_path) == 1
    assert snapshot.main(["--compare", "--allow", str(allow)], api=FakeApi(roles_total=189, extra_group=True, wf_updated="x"), results=tmp_path) == 1
    # the baseline is still the saved one
    assert json.loads((tmp_path / "prod-snapshot-latest.json").read_text())["fingerprint"]["authorization"]["roles_total"] == 186


def test_diff_never_allows_a_removal(snapshot: ModuleType) -> None:
    old = {"authorization": {"groups": {"admins": 0, "copilot-readonly": 3}}}
    new = {"authorization": {"groups": {"admins": 0}}}
    assert snapshot.diff(old, new, {"added": ["authorization::groups::copilot-*"]}) == ["removed  authorization::groups::copilot-readonly (was 3)"]


# --- verify/snapshot-sanity.py (prod-snapshot.py is frozen) --------------------------------------------------


@pytest.fixture(scope="module")
def sanity() -> ModuleType:
    spec = importlib.util.spec_from_file_location("snapshot_sanity", ROOT / "verify" / "snapshot-sanity.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sanity_accepts_the_fingerprint_prod_snapshot_really_writes(sanity: ModuleType, snapshot: ModuleType, tmp_path: Path) -> None:
    """End to end on the frozen script's own output: a full fingerprint passes."""
    assert snapshot.main(["--save"], api=FakeApi(), results=tmp_path) == 0
    assert sanity.newest(tmp_path).name != "prod-snapshot-latest.json"
    assert sanity.main([], results=tmp_path) == 0


def test_adapters_and_integrations_are_fingerprinted_by_name(snapshot: ModuleType) -> None:
    """With production's real envelope the first baseline recorded ['?', '?', '?'] and ['?', '?']: counts only, so a
    swapped adapter would have compared equal. The names must come through."""
    fp = snapshot.collect(FakeApi())
    assert fp["adapters"] == ["NetBox"]
    assert fp["integrations"] == ["netbox-api"]


def _fingerprint(**overrides: object) -> dict:
    fp = {
        "workflows": {"wf-a": "2026-09-10"},
        "inventories": {"lab": {"groups": ["admin_group"], "nodes": ["br1-sw01"]}},
        "device_groups": {},  # not required: a fresh production may have none
        "agents": {},
        "profiles": ["anthropic"],
        "integrations": ["netbox-api"],
        "adapters": ["NetBox"],
        "authorization": {"roles_total": 186, "groups_total": 2, "groups": {"admin_group": 186}, "admin_group_roles": 186},
        "netbox": {"devices": 12, "vlans": 40},
    }
    for dotted, value in overrides.items():
        *parents, leaf = dotted.split("__")
        node = fp
        for part in parents:
            node = node[part]
        if value is KeyError:
            del node[leaf]
        else:
            node[leaf] = value
    return {"taken": "20260916T000000Z", "fingerprint": fp}


@pytest.mark.parametrize(
    ("override", "problem"),
    [
        ({"workflows": {}}, "workflows: empty"),
        ({"inventories": {}}, "inventories: empty"),
        ({"adapters": []}, "adapters: empty"),
        ({"integrations": []}, "integrations: empty"),
        ({"profiles": []}, "profiles: empty"),
        ({"authorization__roles_total": None}, "authorization.roles_total: None"),
        ({"authorization__roles_total": 0}, "authorization.roles_total: 0"),
        ({"authorization__groups": {}}, "authorization.groups: empty"),
        ({"authorization__admin_group_roles": None}, "authorization.admin_group_roles: None"),
        ({"netbox__devices": 0}, "netbox.devices: 0"),
        ({"netbox__devices": KeyError}, "netbox.devices: missing"),
        ({"adapters": ["?", "?", "?"]}, "adapters: an entry has no name ('?'), so identity changes would go unseen"),
        ({"integrations": ["netbox-api", "?"]}, "integrations: an entry has no name ('?'), so identity changes would go unseen"),
        ({"workflows": {"?": "2026-09-10"}}, "workflows: an entry has no name ('?'), so identity changes would go unseen"),
    ],
)
def test_sanity_refuses_every_empty_required_section(sanity: ModuleType, tmp_path: Path, override: dict, problem: str, capsys: pytest.CaptureFixture) -> None:
    assert sanity.problems(_fingerprint()) == []
    path = tmp_path / "prod-snapshot-20260916T000000Z.json"
    path.write_text(json.dumps(_fingerprint(**override)))
    assert sanity.problems(json.loads(path.read_text())) == [problem]
    assert sanity.main([str(path)]) == 1
    assert problem in capsys.readouterr().out


def test_sanity_reads_the_newest_run_not_the_baseline(sanity: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "prod-snapshot-20260101T000000Z.json").write_text(json.dumps(_fingerprint()))
    (tmp_path / "prod-snapshot-20260916T120000Z.json").write_text(json.dumps(_fingerprint(adapters=[])))
    (tmp_path / "prod-snapshot-latest.json").write_text(json.dumps(_fingerprint()))
    assert sanity.newest(tmp_path).name == "prod-snapshot-20260916T120000Z.json"
    assert sanity.main([], results=tmp_path) == 1, "the run just written is the one checked"
    assert sanity.main([], results=tmp_path / "empty") == 2, "no snapshot at all is not a pass"
    assert sanity.problems({"taken": "x"}) == ["no fingerprint object"]


def test_make_prod_snapshot_takes_an_allowlist_and_runs_the_sanity_check() -> None:
    lines = (ROOT / "Makefile").read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("prod-snapshot:"))
    recipe = []
    for ln in lines[start + 1 :]:
        if not ln.startswith("\t"):
            break
        recipe.append(ln.strip())
    run = next(i for i, ln in enumerate(recipe) if "--$(MODE)" in ln)
    assert recipe[run].endswith("--$(MODE)$(if $(ALLOW), --allow $(ALLOW),)"), "ALLOW is passed only when set"
    assert any("ALLOW applies to MODE=compare only" in ln for ln in recipe[:run]), "ALLOW with MODE=save is refused first"
    assert recipe[run + 1] == ".venv/bin/python verify/snapshot-sanity.py", "the sanity check follows save and compare"
    # the make function itself: with ALLOW set the flag appears, without it nothing is appended
    make = subprocess.run(
        ["make", "-f", "-", "show"], input="show:\n\t@echo x$(if $(ALLOW), --allow $(ALLOW),)y\n", capture_output=True, text=True,
        env={**os.environ, "ALLOW": "/tmp/allow.json"},
    )
    assert make.stdout.strip() == "x --allow /tmp/allow.jsony"
    make = subprocess.run(["make", "-f", "-", "show"], input="show:\n\t@echo x$(if $(ALLOW), --allow $(ALLOW),)y\n", capture_output=True, text=True,
                          env={k: v for k, v in os.environ.items() if k != "ALLOW"})
    assert make.stdout.strip() == "xy"


# --- verify/test-05b-dev-copilot.sh ----------------------------------------------------------------------------


def test_the_dev_copilot_verify_covers_s12() -> None:
    assert VERIFY.exists() and os.access(VERIFY, os.X_OK)
    text = VERIFY.read_text()
    for n in range(1, 10):
        assert re.search(rf'check "S12\.{n} ', text), f"S12.{n} has no check"
    assert "set -uo pipefail" in text
    assert "NETBOX_DEV_RO_TOKEN" in text
    # S12.7 and S12.8 need the production step, so they run only when asked
    gate = re.search(r'^if \[ "\$\{PROD:-0\}" = 1 \]; then$(.*?)^else$(.*?)^fi$', text, re.M | re.S)
    assert gate, "no PROD=1 block"
    for n in (7, 8):
        assert f'check "S12.{n} ' in gate.group(1), f"S12.{n} is not behind PROD=1"
        assert f"S12.{n} " in gate.group(2), f"S12.{n} is not reported as skipped without PROD=1"
    outside = text[: gate.start()] + text[gate.end() :]
    assert "SVC_COPILOT_PROD_PASSWORD" not in outside and "${PROD_URL}" not in outside and '"$PROD_URL"' not in outside
    assert "prod-snapshot.py --compare --allow" in gate.group(1)
    assert "itential/copilot/roles.yaml" in text
    # production is never written: the only non-GET calls to it are the login, the CM device search and the
    # refused delete of a workflow that does not exist
    prod_writes = re.findall(r'-X (POST|PUT|PATCH|DELETE) "\$\{PROD_URL\}([^"]*)"', text)
    assert sorted(prod_writes) == [("DELETE", "/workflow_builder/workflows/delete/copilot-probe-does-not-exist"), ("POST", "/configuration_manager/devices")]
    assert subprocess.run(["bash", "-n", str(VERIFY)], capture_output=True).returncode == 0


def _shell_code(text: str) -> str:
    """The script without comment lines."""
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def test_the_prod_gate_variable_is_only_ever_read() -> None:
    """Review H1: `PROD="https://..."` clobbered the PROD=1 gate, so S12.7/S12.8 could never run. PROD is the
    caller's switch: the script may read it, never assign it (the URL is PROD_URL)."""
    code = _shell_code(VERIFY.read_text())
    # an assignment at the start of a command: line start, after ; && || { or `local`/`export`/`declare`/`readonly`
    # (the log header's "(PROD=${PROD:-0})" is text inside an echo, not an assignment)
    assigned = re.findall(r"(?:^\s*|[;&|{]\s*|\b(?:local|export|declare|readonly)\s+)PROD(\+?=)", code, re.M)
    assert not assigned, "the script assigns PROD"
    assert re.search(r'^if \[ "\$\{PROD:-0\}" = 1 \]; then$', code, re.M), "the gate reads PROD"
    assert re.search(r'^PROD_URL="https://\$\{PROD_HOST\}"$', code, re.M)
    # and the gate really opens: evaluate the script's own gate line with PROD=1 and without it
    gate = next(ln for ln in code.splitlines() if ln.startswith('if [ "${PROD:-0}" = 1 ]'))
    probe = f'PROD_URL="https://x"\n{gate} echo open; else echo closed; fi'
    for env, want in (({"PROD": "1"}, "open"), ({}, "closed")):
        run = subprocess.run(["bash", "-c", probe], capture_output=True, text=True, env={"PATH": os.environ["PATH"], **env})
        assert run.stdout.strip() == want, (env, run.stdout, run.stderr)


def test_the_dev_copilot_verify_passes_no_secret_in_argv(tmp_path: Path) -> None:
    """Review L2: argv is visible in `ps`. login() hands the credentials to python through its environment, and the
    credential scan hands each secret to grep through a printf-fed file."""
    text = VERIFY.read_text()
    code = _shell_code(text)
    # python -c '...' "$x": no shell variable follows the program when it names a password or the login's pw
    for m in re.finditer(r"\$\{PY\} -c '[^']*'((?:\s+\"[^\"]*\")+)", code):
        assert not re.search(r"\$\{?(pw|[A-Z_]*PASSWORD[A-Z_]*|secret|[A-Z_]*TOKEN)\b", m.group(1)), f"a secret in python argv: {m.group(1)}"
    scans = [ln for ln in code.splitlines() if "grep" in ln and "$secret" in ln]
    assert scans, "the credential scan is gone"
    for ln in scans:
        # the only "$secret" on a grep line is inside the printf process substitution that feeds -f
        assert ln.count('"$secret"') == ln.count("<(printf '%s\\n' \"$secret\")"), f"a secret in grep argv: {ln.strip()}"
    assert "grep -qF -f <(printf '%s\\n' \"$secret\")" in code
    # behaviour: run the script's own login() with a python and a curl that record what they were given
    login = next(ln for ln in text.splitlines() if ln.startswith("login() {"))
    body = text.splitlines()[text.splitlines().index(login) + 1]
    record = tmp_path / "argv"
    fake_py = tmp_path / "py"
    fake_py.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{record}"\nexec python3 "$@"\n')
    fake_py.chmod(0o755)
    script = "\n".join([
        f'PY="{fake_py}"; CA=/dev/null',
        # curl stands in for the Platform: it keeps the body it was given on stdin and answers 200
        f'curl() {{ cat > "{tmp_path}/body"; echo 200; }}',
        login,
        body,
        'login https://dev.example svc-copilot "Pw-in-env-not-argv-7" /dev/null && echo LOGGED_IN',
    ])
    run = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert "LOGGED_IN" in run.stdout, run.stdout + run.stderr
    assert "Pw-in-env-not-argv-7" not in record.read_text(), "the password reached python's argv"
    assert json.loads((tmp_path / "body").read_text()) == {"username": "svc-copilot", "password": "Pw-in-env-not-argv-7"}
