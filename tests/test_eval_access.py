"""ADR 0084 unit tests: what the skills eval suite's two service accounts may do on production, and the plays that make
them.

itential/eval/run-roles.yaml (svc-eval) and approve-roles.yaml (svc-eval-approver) are the role oracles. The methods and
the deny lists below are written here independently of those files, so an edit to an oracle cannot quietly widen what
either account holds. The runner's account starts the agent tools, reads jobs and sessions and deletes the sessions it
scored, and can never release a card; the approver releases or rejects cards and runs the restore, cancels no job and
never starts an agent. No lab access."""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "itential" / "eval"
VERSIONS = yaml.safe_load((ROOT / "itential" / "versions.yaml").read_text())
PLAYS = ROOT / "ansible" / "playbooks"
TASKS = PLAYS / "tasks"

RUN_METHODS = {"OperationsManager/runEndpointTriggerWithPost", "OperationsManager/getJob", "OperationsManager/getJobs",
               "AgentSessionManager/deleteSession"}  # a scored session's record keeps the procedure
RUN_BUILTIN = {"AgentSessionManager/session:read", "AgentSessionManager/operator"}
APPROVE_METHODS = {"OperationsManager/runEndpointTriggerWithPost", "OperationsManager/getJob",
                   "OperationsManager/getJobs"}  # no cancelJobs: it reaches every job on production
APPROVE_BUILTIN = {"WorkCenterService/work-items:write"}  # claim, complete, cancel a card (measured 2026-10-23)
DENY = ["*/admin", "*/apiwrite", "*/designer", "*/engineering", "*/operations", "*/support", "*/builder", "*/*:create",
        "*/*:update", "*/*:delete", "*/*:run", "*/*:code", "*/*:manage", "MOP/*", "ConfigurationManager/*",
        "GatewayManager/*", "InventoryManager/*", "AutomationStudio/*", "WorkFlowEngine/*", "Authorization/*"]
RUN_DENY = DENY + ["WorkCenterService/*"]  # the card is the approver's
APPROVE_DENY = DENY + ["AgentSessionManager/*"]  # the approver never runs an agent
# what releases or moves a held job: never the runner's
TASK_METHODS = {"OperationsManager/finishManualTask", "OperationsManager/claimTask", "OperationsManager/assignTask",
                "OperationsManager/cancelJobs", "OperationsManager/pauseJobs", "OperationsManager/resumeJobs",
                "OperationsManager/continueJobFrom", "OperationsManager/startJob"}

ACCOUNTS = {
    "run": ("run-roles.yaml", "svc-eval", "eval", "SVC_EVAL_PROD_PASSWORD", "eval", RUN_METHODS, RUN_BUILTIN, RUN_DENY),
    "approve": ("approve-roles.yaml", "svc-eval-approver", "eval-approvers", "SVC_EVAL_APPROVER_PROD_PASSWORD",
                "eval-approver", APPROVE_METHODS, APPROVE_BUILTIN, APPROVE_DENY),
}


def _oracle(file: str) -> dict:
    return yaml.safe_load((EVAL / file).read_text())


def _keys(entries: list[dict]) -> set[str]:
    return {f"{e['provenance']}/{e['name']}" for e in entries}


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


# --- the role oracles ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("account", list(ACCOUNTS))
def test_each_oracle_is_production_only_and_names_its_accounts_group(account: str) -> None:
    file, user, group, password_env, kind, *_ = ACCOUNTS[account]
    oracle = _oracle(file)
    assert set(oracle) == {"prod"}, "the scratch lab is production's: no dev half"
    assert oracle["prod"]["group"] == group
    assert VERSIONS["eval_access"][account] == {"user": user, "group": group, "password_env": password_env,
                                                "kind": kind}


@pytest.mark.parametrize("account", list(ACCOUNTS))
def test_each_custom_role_holds_exactly_its_methods(account: str) -> None:
    file, _, _, _, _, methods, _, _ = ACCOUNTS[account]
    custom = _oracle(file)["prod"]["custom"]
    assert [c["name"] for c in custom] == [{"run": "eval-run", "approve": "eval-approve"}[account]]
    assert _keys(custom[0]["methods"]) == methods
    assert not any("*" in m["name"] for m in custom[0]["methods"]), "exact names, never a glob"


@pytest.mark.parametrize("account", list(ACCOUNTS))
def test_the_builtins_are_exact_and_nothing_denied(account: str) -> None:
    file, _, _, _, _, _, builtin, deny = ACCOUNTS[account]
    prod = _oracle(file)["prod"]
    keys = _keys(prod["builtin"])
    assert keys == builtin
    for key in keys | {f"Custom/{c['name']}" for c in prod["custom"]}:
        assert not any(fnmatch.fnmatchcase(key, pattern) for pattern in deny), key
    # the oracle's own deny list covers at least this test's (the play asserts the resolved roles against it)
    assert set(deny) <= _keys(prod["deny"])


def test_the_runner_can_never_release_move_or_cancel_a_held_job() -> None:
    prod = _oracle("run-roles.yaml")["prod"]
    methods = _keys([m for c in prod["custom"] for m in c["methods"]])
    assert not methods & TASK_METHODS
    assert not any(b["provenance"] == "WorkCenterService" for b in prod["builtin"])


def test_the_approver_cancels_no_job_and_rejects_a_card_instead() -> None:
    prod = _oracle("approve-roles.yaml")["prod"]
    methods = _keys([m for c in prod["custom"] for m in c["methods"]])
    assert not methods & TASK_METHODS, "a waiting push is rejected through its card, which Work Center scopes"


def test_the_only_delete_either_account_holds_is_the_runners_session_delete() -> None:
    deletes = {account: {k for k in _keys([m for c in _oracle(spec[0])["prod"]["custom"] for m in c["methods"]])
                         if "delete" in k.split("/")[1].lower()}
               for account, spec in ACCOUNTS.items()}
    assert deletes == {"run": {"AgentSessionManager/deleteSession"}, "approve": set()}
    # a method, not the built-in session:delete role, so the deny list's */*:delete keeps holding for every role
    assert "*:delete" in {d["name"] for d in _oracle("run-roles.yaml")["prod"]["deny"]}


def test_the_two_accounts_share_no_group_and_neither_is_an_admin() -> None:
    groups = {VERSIONS["eval_access"][a]["group"] for a in ACCOUNTS}
    assert groups == {"eval", "eval-approvers"}
    assert not groups & {"admin_group", "admins", "builders", "operators"}
    assert not groups & set(VERSIONS["automation_access"]["read"]["prod"]), "the base read groups are not eval groups"


def test_the_password_keys_are_documented_and_never_set_in_the_example() -> None:
    example = (ROOT / ".env.example").read_text()
    for account in ACCOUNTS.values():
        assert re.search(rf"^{account[3]}=\s", example, re.M), account[3]


# --- the plays -------------------------------------------------------------------------------------------------------


def test_the_shared_account_task_knows_both_kinds_and_their_oracles() -> None:
    task = _tasks(TASKS / "ldap-service-account.yml")[0]
    kinds = task["vars"]["svc_kinds"]
    for account in ACCOUNTS.values():
        file, _, group, password_env, kind, *_ = account
        assert kinds[kind]["oracle"] == f"itential/eval/{file}"
        assert group.startswith(kinds[kind]["group_prefix"]) and password_env.startswith(kinds[kind]["password_prefix"])
    guard = next(t for t in _tasks(TASKS / "ldap-service-account.yml") if _module(t)[0] == "assert")
    assert "'eval', 'eval-approver'" in json.dumps(guard)


def test_eval_access_is_production_only_and_writes_only_its_own_roles() -> None:
    task_file = TASKS / "eval-access.yml"
    tasks = _tasks(task_file)
    first = _module(tasks[0])
    assert first[0] == "assert" and "dev_overlay" in json.dumps(first[1]), "the first task stops a dev run"
    for task in tasks:
        mod, args = _module(task)
        if mod == "uri" and str(args.get("method", "GET")).upper() != "GET":
            assert "/authorization/roles" in args["url"], f"{task['name']}: unexpected write {args['url']}"
            assert task["loop"] == "{{ eva_custom }}", "only the oracles' custom roles"
            assert "'provenance', 'equalto', 'Custom'" in json.dumps(task), "only a Custom role is ever reset"
    assert "admin_group" not in task_file.read_text()
    include = next(t for t in tasks if t.get("ansible.builtin.include_tasks") == "tasks/ldap-service-account.yml")
    assert include["loop"] == ["run", "approve"]
    assert include["vars"]["svc_role_set"] == "prod" and include["vars"]["ldap_service_account_fallback"] is False


def test_the_accounts_exist_before_the_import_and_before_the_gbac() -> None:
    replay = [t.get("ansible.builtin.include_tasks") or t.get("ansible.builtin.import_tasks")
              for t in _tasks(PLAYS / "platform-ha2-replay.yml")]
    assert replay.index("tasks/eval-access.yml") < replay.index("tasks/platform-assets.yml"), (
        "the skills-lab inventory and the card's group markers need the eval groups")
    platform = _tasks(PLAYS / "platform.yml")
    includes = [t.get("ansible.builtin.include_tasks") for t in platform]
    eval_at = includes.index("tasks/eval-access.yml")
    assert eval_at < includes.index("tasks/automation-access.yml")
    assert platform[eval_at]["when"] == "not (dev_overlay | default(false) | bool)"
