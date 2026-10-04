"""verify/awsvpncheck.py, the leak sweep of verify/test-13a-aws-vpn.sh (PID S13 criterion 2, step 12; production since
ADR 0070). It counts
every AWS VPN secret Vault holds in the places a copy could land - job documents and task records, the dev
containers' logs, both repositories, the router's own records (through lab-edge-push sweep, which counts on the
Gateway) - and prints only labels and numbers. The counter must find a planted copy, whole or cut, and no check
may print a value."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
V = yaml.safe_load((ROOT / "itential" / "versions.yaml").read_text())


def _load():
    spec = importlib.util.spec_from_file_location("awsvpncheck", ROOT / "verify" / "awsvpncheck.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ac = _load()
KEY = "Zx9-dev.ONLY_key+for/the=tests-4d7a1c0e9b"  # made-up values, in the shapes Vault holds
OLD_KEY = "Old-dev.ONLY_key+rotated/away=00000000aa"
PASSWORD = "dev-only-edge-password-123"
SECRETS = {"aws/vpn-psk psk v4": KEY, "aws/vpn-psk psk v3": OLD_KEY, "devices/dc1-wan01-aws-vpn password v1": PASSWORD}


# ── the counter ──


def test_a_whole_copy_and_a_cut_are_both_counted() -> None:
    c = ac.count(f"log {KEY} and {KEY[5:25]}", SECRETS)
    assert c["aws/vpn-psk psk v4"] == {"whole": 1, "pieces": len(KEY) - ac.WINDOW + 1}
    assert ac.hits(ac.count(f"x {KEY[5:25]} x", SECRETS)) == 5  # 20 characters: five 16-character pieces
    assert ac.hits(ac.count("nothing here", SECRETS)) == 0


def test_a_secret_shorter_than_a_piece_is_counted_whole_only() -> None:
    c = ac.count("pw=short-pw!", {"short": "short-pw!"})
    assert c["short"] == {"whole": 1, "pieces": 0}


def test_the_control_finds_a_planted_copy_of_every_secret(capsys) -> None:
    assert ac.control(SECRETS)
    out = capsys.readouterr().out
    assert all(v not in out for v in SECRETS.values())


# ── what Vault holds: every live version of every path, labelled, never shown ──


def _vault(versions: dict[str, dict[int, dict]], destroyed: set[tuple[str, int]] = frozenset()):
    def vault(method, path, token=None, body=None):
        mount = V["vault"]["kv_mount"]
        if path.startswith(f"{mount}/metadata/"):
            p = path.removeprefix(f"{mount}/metadata/")
            if p not in versions:
                return 404, None
            meta = {str(n): {"destroyed": (p, n) in destroyed, "deletion_time": ""} for n in versions[p]}
            return 200, {"data": {"versions": meta}}
        p, _, q = path.removeprefix(f"{mount}/data/").partition("?version=")
        return 200, {"data": {"data": versions[p][int(q)]}}

    return vault


def test_every_live_version_of_every_path_is_read_and_destroyed_ones_skipped(monkeypatch) -> None:
    held = {path: {1: {f: f"{path}-{f}-value-v1-padding" for f in fields}} for path, fields in ac.SECRET_FIELDS.items()}
    held["aws/vpn-psk"] = {2: {"psk": OLD_KEY, "version": "a"}, 3: {"psk": "gone"}, 4: {"psk": KEY, "version": "b"}}
    monkeypatch.setattr(ac.vc, "vault", _vault(held, destroyed={("aws/vpn-psk", 3)}))
    monkeypatch.setattr(ac.vc, "admin", lambda: "t")
    got = ac.secrets_from_vault()
    assert got["aws/vpn-psk psk v4"] == KEY and got["aws/vpn-psk psk v2"] == OLD_KEY
    assert "aws/vpn-psk psk v3" not in got and "aws/vpn-psk version v4" not in got  # the version is not a secret
    assert {label.split()[0] for label in got} == set(ac.SECRET_FIELDS)
    assert all(v not in label for label, v in got.items())


def test_a_path_vault_does_not_have_is_an_error_not_a_silent_skip(monkeypatch) -> None:
    held = {path: {1: {f: "x" * 20 for f in fields}} for path, fields in ac.SECRET_FIELDS.items()}
    del held["aws/terraform"]
    monkeypatch.setattr(ac.vc, "vault", _vault(held))
    monkeypatch.setattr(ac.vc, "admin", lambda: "t")
    with pytest.raises(ac.SweepError, match="aws/terraform"):
        ac.secrets_from_vault()


def test_the_secret_paths_are_the_aws_vpn_ones_the_gateway_binds() -> None:
    bound = {a["path"] for a in {**V["vault"]["gateway_aliases"], **V["vault"]["edge_gateway_aliases"]}.values()}
    assert set(ac.SECRET_FIELDS) <= bound
    assert {"aws/vpn-psk", "aws/terraform", "aws/psk-writer", "devices/dc1-wan01-aws-vpn"} <= set(ac.SECRET_FIELDS)


# ── the job documents and task records ──


class FakePlatform:
    def __init__(self, jobs: dict[str, list[dict]], tasks: dict[str, list[dict]]) -> None:
        self.jobs, self.tasks, self.calls = jobs, tasks, []

    def call(self, method: str, path: str, body=None) -> dict:
        self.calls.append((method, path))
        if path.startswith("/operations-manager/jobs?"):
            name = next(n for n in self.jobs if f"equals[name]={ac.quote(n)}" in path)
            skip = int(path.split("skip=")[1].split("&")[0])
            return {"data": [{"_id": j["_id"]} for j in self.jobs[name][skip : skip + ac.PAGE]],
                    "metadata": {"total": len(self.jobs[name])}}
        if path.startswith("/operations-manager/jobs/"):
            jid = path.rsplit("/", 1)[1]
            return {"data": next(j for js in self.jobs.values() for j in js if j["_id"] == jid)}
        if path.startswith("/operations-manager/tasks?"):
            jid = path.split("equals[job._id]=")[1].split("&")[0]
            return {"data": self.tasks.get(jid, [])}
        raise AssertionError(path)


def _jobs(n: int, name: str) -> list[dict]:
    return [{"_id": f"{name[:2]}{i:022d}", "name": name, "variables": {"outcome": "ok"}} for i in range(n)]


def test_every_job_of_every_swept_workflow_and_its_tasks_are_counted(monkeypatch, capsys) -> None:
    jobs = {name: _jobs(3, name) for name in ac.JOB_WORKFLOWS}
    jobs[ac.JOB_WORKFLOWS[0]] = _jobs(ac.PAGE + 2, ac.JOB_WORKFLOWS[0])  # more than one page
    planted = jobs[ac.JOB_WORKFLOWS[1]][1]["_id"]
    p = FakePlatform(jobs, {planted: [{"variables": {"incoming": {"x": f"...{OLD_KEY[2:22]}..."}}}]})
    assert ac.sweep_jobs(p, SECRETS) == {"jobs": ac.PAGE + 2 + 3 * (len(ac.JOB_WORKFLOWS) - 1), "hits": 5}
    out = capsys.readouterr().out
    assert OLD_KEY[2:22] not in out and "LEAK" in out and planted in out


def test_a_job_sweep_that_finds_no_jobs_fails(monkeypatch, capsys) -> None:
    p = FakePlatform({name: [] for name in ac.JOB_WORKFLOWS}, {})
    monkeypatch.setattr(ac.vc, "Platform", lambda: p)
    monkeypatch.setattr(ac, "secrets", lambda: SECRETS)
    assert not ac.c_jobs()


def test_the_swept_workflows_are_the_aws_vpn_ones_and_the_hand_paths_to_the_edge() -> None:
    wf = V["workflows"]
    assert set(ac.JOB_WORKFLOWS) == {wf[k] for k in (
        "deploy_aws_vpn", "hand_off_aws_vpn", "verify_aws_vpn", "tear_down_aws_vpn", "tear_down_expired_aws_vpn",
        "get_aws_vpn_status", "check_aws_drift", "rotate_aws_vpn_key", "rotate_aws_vpn_key_monthly",
        "config_push_revert", "show_command")}


def test_every_aws_vpn_workflow_is_swept() -> None:
    """A workflow that runs an AWS VPN service is swept from the day it exists (R2b's and A1's were missed until R4)."""
    services = {"terraform-run", "aws-vpn-psk", "lab-edge", "lab-edge-push", "aws-vpn-monitor"}
    for path in sorted((ROOT / "itential" / "workflows").glob("*.json")):  # build.py's output, kept in sync by a test
        doc = json.loads(path.read_text())
        runs = {t.get("variables", {}).get("incoming", {}).get("serviceName") for t in doc["tasks"].values()
                if t.get("name") == "runService"}
        if runs & services:
            assert doc["name"] in ac.JOB_WORKFLOWS, doc["name"]


# ── the router: lab-edge-push sweep, counts only ──

# as the lab edges answered on 2026-10-03 (the first run): no change log kept (#106), so its read is refused with one
# `%` line and reported unread, `change_log` false (cloud-devops-pipeline lab-edge-push sweep)
CLEAN_SWEEP = {
    "action": "sweep", "target": "dc1-wan01", "clean": True, "hits": 0, "change_log": False,
    "sources": {n: {"read": n != "archive_log", "lines": 1 if n == "archive_log" else 900,
                    "key": {"whole": 0, "pieces": 0}, "password": {"whole": 0, "pieces": 0}}
                for n in ("logging", "archive_log", "running_config")},
    "pre_shared_key_lines": {"total": 3, "type6": 3},
}


@pytest.fixture
def router(monkeypatch):
    world = {"answer": (0, json.loads(json.dumps(CLEAN_SWEEP))), "runs": []}

    def service(name, params):
        world["runs"].append((name, params))
        rc, out = world["answer"]
        return rc, {**out, "target": json.loads(params["target_json"])["name"]}

    monkeypatch.setattr(ac.vc, "_service", service)
    monkeypatch.setattr(ac.vc, "_open_targets", lambda: {n: V["aws_vpn"]["targets"][n] for n in ("dc1-wan01",)})
    return world


def test_a_clean_router_passes_and_the_sweep_is_asked_for_exactly(router) -> None:
    assert ac.c_router()
    name, params = router["runs"][0]
    assert name == "lab-edge-push" and set(params) == {"action", "target_json", "username", "timeout"}
    assert params["action"] == "sweep" and params["username"] == V["aws_vpn"]["targets"]["dc1-wan01"]["username"]


@pytest.mark.parametrize(
    "change",
    [
        lambda o: o.update(clean=False, hits=2),
        lambda o: o["sources"]["running_config"].update(read=False),
        lambda o: o.update(change_log=True),  # a change log kept but not read: exactly what is swept for
        lambda o: o.pop("change_log"),  # a sweep that does not say: an older lab-edge-push
        lambda o: o["sources"]["logging"].update(lines=0),
        lambda o: o.update(pre_shared_key_lines={"total": 0, "type6": 0}),  # a deployment's key must be there, type 6
    ],
)
def test_anything_short_of_clean_fails(router, change) -> None:
    change(router["answer"][1])
    assert not ac.c_router()


def test_a_change_log_that_is_kept_and_read_clean_passes(router) -> None:
    router["answer"][1].update(change_log=True)
    router["answer"][1]["sources"]["archive_log"].update(read=True, lines=12)
    assert ac.c_router()


def test_a_sweep_that_fails_on_the_gateway_fails(router) -> None:
    router["answer"] = (1, {"action": "sweep", "error": "OSError"})
    assert not ac.c_router()


def test_the_twin_without_a_deployment_needs_no_key_line(router, monkeypatch) -> None:
    monkeypatch.setattr(ac.vc, "_open_targets", lambda: {"clab-rtr1": V["aws_vpn"]["targets"]["clab-rtr1"]})
    router["answer"][1]["pre_shared_key_lines"] = {"total": 0, "type6": 0}
    assert ac.c_router()


# ── Verify AWS VPN, run live through its endpoint trigger ──


class VerifyPlatform:
    def __init__(self, status: str, outcome: str | None, trigger_error: Exception | None = None) -> None:
        self.status, self.outcome, self.trigger_error, self.calls = status, outcome, trigger_error, []

    def call(self, method: str, path: str, body=None) -> dict:
        self.calls.append((method, path, body))
        if method == "POST":
            if self.trigger_error:
                raise self.trigger_error
            return {"_id": "f" * 24}
        return {"data": {"status": self.status, "variables": {"outcome": self.outcome, "error": None}}}


@pytest.mark.parametrize(
    "status, outcome, passed",
    [
        ("complete", "tunnel up: router up, data plane up, AWS monitor up", True),
        ("complete", "tunnel down: router down", False),
        ("error", None, False),
        ("canceled", None, False),
    ],
)
def test_verify_passes_only_on_a_complete_tunnel_up(monkeypatch, status, outcome, passed) -> None:
    p = VerifyPlatform(status, outcome)
    monkeypatch.setattr(ac.vc, "Platform", lambda: p)
    monkeypatch.setattr(ac.time, "sleep", lambda s: None)
    assert ac.c_verify() is passed
    method, path, body = p.calls[0]
    route = V["operations_manager"]["verify_aws_vpn"]["endpoint"]["route"]
    assert (method, path, body) == ("POST", f"/operations-manager/triggers/endpoint/{route}", {"target": ac.VERIFY_TARGET})


def test_verify_gives_up_on_a_job_that_never_ends(monkeypatch) -> None:
    p = VerifyPlatform("running", None)
    monkeypatch.setattr(ac.vc, "Platform", lambda: p)
    monkeypatch.setattr(ac.time, "sleep", lambda s: None)
    assert not ac.c_verify()


def test_the_verify_target_is_an_open_target_with_a_deployment() -> None:
    t = V["aws_vpn"]["targets"][ac.VERIFY_TARGET]
    assert t["window"] == "open" and t["monitor"] == "aws"


# ── the script: production (ADR 0070), opt-in, selected by make verify where it skips without AWS_VPN=1 ──

SCRIPT = ROOT / "verify" / "test-13a-aws-vpn.sh"


def test_the_script_does_nothing_without_aws_vpn_1(tmp_path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "AWS_VPN"}
    run = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30, check=False)
    assert run.returncode == 0 and "SKIP" in run.stdout and "AWS_VPN=1" in run.stdout


def test_the_script_runs_on_production_and_make_verify_selects_it(tmp_path) -> None:
    import shutil
    shutil.copy(ROOT / "verify" / "run.sh", tmp_path / "run.sh")
    (tmp_path / SCRIPT.name).write_text("exit 99\n")
    listed = subprocess.run(["bash", str(tmp_path / "run.sh"), "--list"], capture_output=True, text=True, timeout=30,
                            check=True)
    assert SCRIPT.name in listed.stdout  # opt-in inside: without AWS_VPN=1 it skips, so make verify stays green
    makefile = (ROOT / "Makefile").read_text()
    assert "test-13a" not in makefile.split("\nverify-dev:")[1].split("\n\n")[0]
    assert not (ROOT / "verify" / "test-13a-aws-vpn-dev.sh").exists()
    text = SCRIPT.read_text()
    assert "export AWS_VPN_TIER=prod VAULT_TIER=prod" in text and "d['vault']['prod']['url']" in text
    assert "aws_vpn']['tier']\")\" = prod" in text  # refuses unless production runs the AWS VPN


def test_the_logs_are_read_where_each_tier_keeps_them() -> None:
    ha2 = yaml.safe_load((ROOT / "itential" / "ha2" / "versions.yaml").read_text())
    ip = {vm["name"]: vm["ip"] for vm in ha2["vms"]}
    platform_nodes = [vm["name"] for vm in ha2["vms"] if vm["role"] == "platform"]
    prod = dict(ac.LOG_SOURCES["prod"])
    assert {ip[n] for n in platform_nodes} | {ip["iag-01"]} == set(prod)
    assert all(prod[ip[n]] == ("platform",) for n in platform_nodes)
    assert prod[ip["iag-01"]] == ("gateway5", "gateway5-runner")
    assert ac.TIER == V["aws_vpn"]["tier"]


def test_the_script_runs_verify_before_the_sweeps_so_the_new_job_is_swept() -> None:
    text = SCRIPT.read_text()
    order = [text.index(f"vpn {c}") for c in ("control", "verify", "jobs", "logs", "repos", "router")]
    assert order == sorted(order)
