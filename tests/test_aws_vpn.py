"""PID S13 (amendment 1.36, ADR 0068): Itential runs the AWS site-to-site VPN. These tests hold the PID, the ADR and
the README to each other and to the two owner rules of 2026-09-26: Terraform (not the native OpenTofu service), and
every AWS secret only in Vault. The service, workflows and verify scripts join them as each piece is built."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PID = ROOT / "docs" / "PID.md"
ADR = ROOT / "docs" / "adr" / "0068-itential-runs-the-aws-vpn-through-terraform-on-the-gateway.md"

# The company ECR pull credentials predate S13 and are not part of it (ADR 0035); nothing else may put AWS keys in .env.
ECR_KEYS = {"ECR_AWS_PROFILE", "ECR_AWS_ACCESS_KEY_ID", "ECR_AWS_SECRET_ACCESS_KEY", "ECR_AWS_SESSION_TOKEN"}


def test_pid_carries_amendment_1_36_and_s13() -> None:
    pid = PID.read_text()
    assert "| **Version** | 1.36 |" in pid
    assert "| 1.36 | 2026-09-26 | AWS site-to-site VPN run by Itential (ADR 0068" in pid
    assert "### S13 — AWS site-to-site VPN run by Itential (amendment 1.36, ADR 0068)" in pid
    assert "| `aws-vpn` *(1.36)* | `phase-aws-vpn/*`" in pid
    assert "| aws-vpn | `phase-aws-vpn/*` |" in (ROOT / "README.md").read_text()


def test_s13_has_five_acceptance_criteria() -> None:
    s13 = PID.read_text().split("### S13 —", 1)[1].split("\n## ", 1)[0]
    assert re.findall(r"^  (\d)\. ", s13, re.M) == ["1", "2", "3", "4", "5"]


def test_adr_keeps_the_owner_rules() -> None:
    adr = ADR.read_text()
    assert "**Status:** proposed" in adr
    assert '"can we not just use terraform?"' in adr and "Terraform 1.5.7" in adr
    assert '"i want the aws secrets\n   to be a part of my vault. no plaintext passwords anywhere"' in adr
    # the PSK leaves the Terraform state and the approved plan file is the one applied
    assert "Vault is the source of the PSK; Terraform never sees it" in adr
    assert "`terraform apply <plan file>`" in adr


def test_no_aws_secret_is_added_to_env_example() -> None:
    names = {m.group(1) for m in re.finditer(r"^([A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), re.M)}
    aws = {n for n in names if "AWS" in n or n.startswith(("TF_VAR_", "VPN_PSK"))}
    assert aws <= ECR_KEYS, f"AWS secrets belong in Vault only (ADR 0068 decision 3): {sorted(aws - ECR_KEYS)}"


# --- step 3: Vault (ADR 0068 decisions 3 and 5, owner decision 2026-09-29: each tier its own key) -----------------
VAULT = yaml.safe_load((ROOT / "itential" / "versions.yaml").read_text())["vault"]
KEY_SCRIPT = ROOT / "scripts" / "aws-key-to-vault.sh"


def test_adr_records_that_each_tier_holds_its_own_key() -> None:
    adr = ADR.read_text()
    assert "**D5** Resolved by the owner, 2026-09-29: each tier's Vault holds its own IAM key" in adr
    assert "`make aws-key TIER=dev|prod`" in adr and "`lab/aws/psk-writer`" in adr


def test_the_psk_writer_is_write_only_on_one_path_and_bound_to_the_gateway_host() -> None:
    writer = VAULT["approles"]["itential-aws-psk-writer"]
    assert writer["policy_paths"] == [VAULT["aws"]["psk_path"]] == ["aws/vpn-psk"]
    assert writer["capabilities"] == ["create", "update"]  # no read, delete, list or sudo
    assert writer["bound_hosts"] == ["iag-01"]


def test_the_gateway_reads_the_aws_paths_and_the_platform_none() -> None:
    aws = VAULT["aws"]
    gateway = VAULT["approles"]["itential-gateway"]["policy_paths"]
    for path in (aws["key_path"], aws["psk_path"], aws["writer_path"]):
        assert path in gateway, path
    assert not any(p.startswith("aws") for p in VAULT["approles"]["itential-platform"]["policy_paths"])


def test_no_aws_secret_is_seeded_from_env() -> None:
    assert not any(path.startswith("aws") for path in VAULT["secrets"]), "AWS secrets are never seeded from .env"


def test_the_dev_overlay_binds_the_writer_to_the_dev_vm() -> None:
    overlay = yaml.safe_load((ROOT / "ansible" / "playbooks" / "vars" / "itential-dev.yml").read_text())
    assert overlay["vault_role_cidrs"]["itential-aws-psk-writer"] == ["{{ vault.dev.docker_gateway }}/32"]


def test_the_config_play_keeps_the_writers_credentials_in_vault_and_proves_p6_on_the_gateway_host() -> None:
    tasks = (ROOT / "ansible" / "playbooks" / "tasks" / "vault-config.yml").read_text()
    assert "vault.aws.writer_path" in tasks and "vault_reader: itential-aws-psk-writer" in tasks
    prod = (ROOT / "ansible" / "playbooks" / "vault-prod-config.yml").read_text()
    assert "sys/capabilities-self" in prod
    assert "itential-aws-psk-writer: [create, update]" in prod and "itential-gateway: [read]" in prod


def test_both_vault_verifies_check_the_psk_path() -> None:
    for name in ("test-09a-vault.sh", "test-09a-vault-dev.sh"):
        assert '" vc aws-psk' in (ROOT / "verify" / name).read_text(), name


def test_the_key_script_never_shows_or_saves_the_secret_key() -> None:
    text = KEY_SCRIPT.read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "<<<" not in code  # bash 3.2 writes a here-string to a temporary file
    assert "set -x" not in code and "echo \"$KEY_JSON" not in code and "echo $KEY_JSON" not in code
    assert "X-Vault-Token: %s" in code and "-H @<(" in code  # the token through a file descriptor, never argv
    assert "-o /dev/null" in code  # Vault's answer (which would hold the key on a read) is never read
    assert "unset KEY_JSON" in code and "delete-access-key" in code  # a key Vault did not take is deleted again
    assert 'case "$TIER" in' in code and "usage: $0 dev|prod" in code  # the tier is always named, never defaulted


def test_the_key_script_never_leaves_a_key_vault_does_not_hold() -> None:
    code = KEY_SCRIPT.read_text()
    assert "trap settle INT TERM" in code  # Ctrl-C between create and store
    assert '[ "$(held_key_id)" = "$KEY_ID" ]' in code  # a timeout after Vault stored it keeps the key
    assert 'delete access key $KEY_ID of $USER_NAME by hand' in code  # a failed delete names the key to remove
    assert "cli_history" in code  # AWS CLI history would save the answer, secret included, to disk


def test_vault_config_uses_the_owners_admin_login_now_that_root_is_revoked() -> None:
    target = (ROOT / "Makefile").read_text().split("\nvault-config:", 1)[1].split("\n\n", 1)[0]
    assert "export VAULT_TOKEN=$$(tr -d '\\n' < $(VAULT_ADMIN_FILE))" in target


def test_a_reissued_writer_secret_id_destroys_the_older_ones() -> None:
    tasks = (ROOT / "ansible" / "playbooks" / "tasks" / "vault-config.yml").read_text()
    assert "role/itential-aws-psk-writer/secret-id?list=true" in tasks
    assert "difference([vault_reader_issued.json.data.secret_id_accessor])" in tasks


def test_make_aws_key_names_the_tier() -> None:
    make = (ROOT / "Makefile").read_text()
    assert "scripts/aws-key-to-vault.sh $(TIER)" in make and "TIER ?=" not in make


# --- step 4: terraform-run on the Gateway (ADR 0068 decision 1, probes P1/P2/P7 measured on dev 2026-09-29) ---------
import base64  # noqa: E402
import hashlib  # noqa: E402

VERSIONS = yaml.safe_load((ROOT / "itential" / "versions.yaml").read_text())
RUNNER = ROOT / "itential" / "gateway5-runner"
PLAYS = {name: (ROOT / "ansible" / "playbooks" / name).read_text() for name in ("itential.yml", "platform-ha2-gateway.yml")}


def test_terraform_1_5_7_is_pinned_by_hash_and_the_build_refuses_anything_else() -> None:
    tf = VERSIONS["runner_terraform"]
    assert tf["version"] == "1.5.7" and re.fullmatch(r"[0-9a-f]{64}", tf["sha256"])
    assert "terraform" not in VERSIONS["images"]  # images/fetch.sh pulls every entry there
    docker = (RUNNER / "Dockerfile").read_text()
    assert "is not the pinned" in docker and "CHECKPOINT_DISABLE=1" in docker
    for name, play in PLAYS.items():
        assert "--build-arg TERRAFORM_SHA256={{ runner_terraform.sha256 }}" in play, name
        assert "loop: [Dockerfile, requirements-terraform-run.txt, requirements-lab-edge.txt, known_hosts]" in play, name


def test_boto3_is_installed_only_with_hashes() -> None:
    req = (RUNNER / "requirements-terraform-run.txt").read_text()
    pins = re.findall(r"^([a-z0-9_.-]+)==", req, re.M)
    assert "boto3" in pins and all(f"{p}==" in req for p in pins)
    assert req.count("--hash=sha256:") >= len(pins)
    assert "--require-hashes --only-binary=:all:" in (RUNNER / "Dockerfile").read_text()


def test_netmiko_is_installed_only_with_hashes_and_named_in_the_image_tag() -> None:
    req = (RUNNER / "requirements-lab-edge.txt").read_text()
    pins = dict(re.findall(r"^([a-z0-9_.-]+)==([^ \\]+)", req, re.M))
    assert pins.get("netmiko") and {"paramiko", "cryptography"} <= set(pins)
    for name in pins:  # every package carries at least one hash
        block = req.split(f"{name}=={pins[name]}", 1)[1].split("==", 1)[0]
        assert "--hash=sha256:" in block, name
    assert not set(pins) & set(re.findall(r"^([a-z0-9_.-]+)==", (RUNNER / "requirements-terraform-run.txt").read_text(), re.M))
    docker = (RUNNER / "Dockerfile").read_text()
    assert "--require-hashes --only-binary=:all: -r /tmp/requirements-lab-edge.txt" in docker
    assert docker.index("requirements-lab-edge.txt") < docker.index("USER itential")  # installed as root, run as the runner user
    assert VERSIONS["stack"]["runner_image"].endswith(f"-nm{pins['netmiko']}")  # a new pin is a new tag: the play rebuilds


def test_githubs_host_keys_are_the_three_pinned_fingerprints() -> None:
    def fp(blob: str) -> str:
        return "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(blob)).digest()).decode().rstrip("=")

    lines = [ln.split() for ln in (RUNNER / "known_hosts").read_text().splitlines() if ln and not ln.startswith("#")]
    assert all(host == "github.com" for host, _, _ in lines)
    assert {fp(blob) for _, _, blob in lines} == {
        "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU",  # ed25519
        "SHA256:p2QAMXNIC1TJYWeIOttrVc98/R1BUFWu3/LiyKgUfQM",  # ecdsa
        "SHA256:uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s",  # rsa
    }
    assert "install -d -o itential -g itential -m 700 /home/itential/.gateway.d" in (RUNNER / "Dockerfile").read_text()


def test_the_secrets_key_exists_before_anything_runs_compose_up() -> None:
    dev = PLAYS["itential.yml"]
    assert dev.index("tasks/gateway-secrets-key.yml") < dev.index("name: Vendored Compose file and the lab override")
    assert dev.index("tasks/gateway-secrets-key.yml") < dev.index("ansible.builtin.meta: flush_handlers")
    prod = PLAYS["platform-ha2-gateway.yml"]
    assert prod.index("tasks/gateway-secrets-key.yml") < prod.index("name: Containers up")
    for f in ("itential/compose.override.yml", "itential/ha2/gateway.compose.yml.j2"):
        text = (ROOT / f).read_text()
        assert text.count("GATEWAY_SECRETS_ENCRYPT_KEY_FILE: /etc/gateway-secrets/encrypt.key") == 2, f
        assert "secrets/server.key:/etc/gateway-secrets/encrypt.key:ro" in text, f
        assert "secrets/runner.key:/etc/gateway-secrets/encrypt.key:ro" in text, f
    task = (ROOT / "ansible" / "playbooks" / "tasks" / "gateway-secrets-key.yml").read_text()
    assert "-type d -empty -delete" in task and "--force-recreate --no-deps" in task


def test_terraform_run_uses_reviewed_code_and_vault_secrets_only() -> None:
    tr = VERSIONS["terraform_run"]
    # pinned to one commit: a branch or tag would let code nobody pinned run with the AWS key (ITL-01)
    assert re.fullmatch(r"[0-9a-f]{40}", tr["repository"]["reference"]), tr["repository"]["reference"]
    aliases = VAULT["gateway_aliases"]
    services = {s["name"]: s for s in tr["services"]}
    assert set(services) == {"terraform-run", "aws-vpn-psk"}
    for svc in services.values():
        for secret in svc["secrets"]:
            assert secret["name"] in aliases and secret["type"] == "env", secret
    tf = {s["target"] for s in services["terraform-run"]["secrets"]}
    assert tf == {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}  # terraform-run never gets the writer's credentials
    psk = {s["target"] for s in services["aws-vpn-psk"]["secrets"]}
    assert psk == {"PSK_WRITER_ROLE_ID", "PSK_WRITER_SECRET_ID", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}
    assert aliases["aws-psk-writer-role-id"]["path"] == VAULT["aws"]["writer_path"]
    assert aliases["cloud-devops-pipeline-deploy-key"]["path"] == VAULT["git"]["deploy_key_path"]
    for name, play in PLAYS.items():
        assert play.index("tasks/gateway-vault.yml") < play.index("tasks/gateway-terraform-run.yml"), name


def test_the_runner_reaches_its_tiers_vault_over_the_lab_ca() -> None:
    dev = (ROOT / "itential" / "compose.override.yml").read_text()
    assert "VAULT_ADDR: ${RUNNER_VAULT_ADDR:-}" in dev and "VAULT_CACERT: /etc/ssl/lab/lab-root-ca.crt" in dev
    assert "RUNNER_VAULT_ADDR={{ vault_url }}" in PLAYS["itential.yml"]
    prod = (ROOT / "itential" / "ha2" / "gateway.compose.yml.j2").read_text()
    assert 'VAULT_ADDR: "{{ vault.prod.url }}"' in prod


# --- step 5: the Deploy AWS VPN workflow (solution design, deploy-aws-vpn delivery 2026-09-29) --------------------
def _deploy() -> tuple[dict, dict]:
    import json
    wf = json.loads((ROOT / "itential" / "workflows" / "deploy-aws-vpn.json").read_text())
    return wf, {tid: t for tid, t in wf["tasks"].items() if isinstance(t, dict)}


def _svc(tasks: dict, action: str) -> str:
    """The runService task whose params template carries this action."""
    for tid, t in tasks.items():
        if t.get("name") == "replace" and f'"action": "{action}"' in str(t["variables"]["incoming"]["str"]):
            return tid
    raise AssertionError(action)


def test_deploy_plans_then_asks_then_applies_exactly_that_plan() -> None:
    wf, tasks = _deploy()
    assert wf["name"] == "Deploy AWS VPN"
    order = [tid for tid in ("1d", "2b", "3e", "4d")]
    names = [(tasks[t]["name"], tasks[t]["variables"]["incoming"].get("serviceName")) for t in order]
    assert names == [("runService", "terraform-run"), ("ViewData", None), ("runService", "terraform-run"),
                     ("runService", "aws-vpn-psk")]
    assert tasks["2b"]["variables"]["incoming"]["body"] == "$var.job.plan"  # the approver sees the plan summary
    plan_fixed = tasks["1a"]["variables"]["incoming"]["text"]  # the inputs are added as data (WEB-01)
    assert '"job": "new"' in plan_fixed and '"enable_vpn": "true"' in plan_fixed and '"action": "plan"' in plan_fixed
    # the apply's plan ID and SHA-256 are the plan result's own, never typed in
    assert tasks["10"]["variables"]["incoming"] == {"pass_on_null": False, "query": "result.stdout_json.job", "obj": "$var.1d.result"}
    assert tasks["3a"]["variables"]["incoming"]["query"] == "result.stdout_json.plan_sha256"
    assert tasks["3b"]["variables"]["incoming"]["newSubstr"] == "$var.10.return_data"
    assert tasks["3c"]["variables"]["incoming"]["newSubstr"] == "$var.3a.return_data"
    tr = wf["transitions"]
    assert tr["2b"] == {"3a": {"state": "success", "type": "standard"}, "7a": {"state": "failure", "type": "standard"}}


def test_reject_discards_the_plan_and_says_nothing_changed() -> None:
    _, tasks = _deploy()
    assert tasks["7c"]["variables"]["incoming"]["serviceName"] == "terraform-run"
    assert '"action": "discard"' in tasks["7a"]["variables"]["incoming"]["str"]
    assert tasks["7d"]["variables"]["outgoing"]["output"] == "$var.job.rejected"
    assert tasks["7e"]["variables"]["incoming"]["input"] == "false"
    assert tasks["7e"]["variables"]["outgoing"]["output"] == "$var.job.aws_changed"


def test_the_psk_step_follows_the_apply_and_returns_versions_only() -> None:
    wf, tasks = _deploy()
    assert tasks["4a"]["variables"]["incoming"]["query"] == "result.stdout_json.outputs.psk_secret_arn"
    assert tasks["4a"]["variables"]["incoming"]["obj"] == "$var.3e.result"
    assert set(wf["outputSchema"]["properties"]) >= {"plan", "outputs", "psk", "aws_changed", "rejected", "outcome", "error"}
    assert not any("psk" in k and k not in ("psk", "psk_result") for k in wf["outputSchema"]["properties"])


def test_the_nat_gateway_is_off_unless_asked() -> None:
    wf, _ = _deploy()
    inputs = wf["inputSchema"]
    assert inputs["properties"]["enable_nat_gateway"]["enum"] == ["false", "true"]
    # change_note too: the Platform refuses a start without it (measured on dev 2026-09-29)
    assert set(inputs["required"]) == {"onprem_public_ip", "enable_nat_gateway", "change_note"}


# --- WEB-01 (security review 2026-09-29): no input reaches the plan's parameters as text ------------------------------
_SPEC = importlib.util.spec_from_file_location("wf_build", ROOT / "itential" / "workflows" / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)


def test_no_job_input_is_templated_into_json() -> None:
    """replace() + parse() let an input close a JSON string and add keys ("action": "apply"). Every text that is parsed
    must be built only from fixed text and service outputs, never from a $var.job input."""
    _, tasks = _deploy()
    for tid, t in tasks.items():
        if t["name"] != "parse":
            continue
        ref, seen = t["variables"]["incoming"]["text"], []
        while ref.startswith("$var.") and ref.endswith(".replacedString"):
            src = tasks[ref.split(".")[1]]
            seen.append(src["variables"]["incoming"]["newSubstr"])
            ref = src["variables"]["incoming"]["str"]
        assert not any(str(v).startswith("$var.job.") for v in seen), f"parse {tid} is built from a job input: {seen}"


def test_the_inputs_are_checked_before_anything_runs() -> None:
    wf, tasks = _deploy()
    tr = wf["transitions"]
    assert tasks["1d"]["variables"]["incoming"]["params"] == "$var.1c.object"  # data, not parsed text
    for tid, key in (("1b", "onprem_public_ip"), ("1c", "enable_nat_gateway")):
        assert tasks[tid]["name"] == "setObjectKey" and tasks[tid]["variables"]["incoming"]["path"] == [key]
    assert tasks["12"]["name"] == "validateJsonSchema"
    assert tasks["12"]["variables"]["incoming"]["schema"] == build.PLAN_INPUTS_SCHEMA
    # validateJsonSchema completes even for invalid data (measured): only the evaluate after it can refuse
    assert tasks["13"]["variables"]["incoming"]["evaluation_groups"][0]["evaluations"][0]["query"] == "valid"
    assert tr["12"] == {"13": {"state": "success", "type": "standard"}, "8f": {"state": "error", "type": "standard"}}
    assert tr["13"] == {"1d": {"state": "success", "type": "standard"}, "8f": {"state": "failure", "type": "standard"}}
    assert list(tr["8f"]) == ["8c"] and list(tr["8c"]) == ["workflow_end"]
    # the plan step has one way in: through the check
    assert [src for src, dst in tr.items() if "1d" in dst] == ["13"]
    schema = build.PLAN_INPUTS_SCHEMA
    assert schema["additionalProperties"] is False and schema["properties"]["action"] == {"const": "plan"}
    ip = re.compile(schema["properties"]["onprem_public_ip"]["pattern"])
    assert ip.fullmatch("8.8.8.8") and ip.fullmatch("255.255.255.255")
    for bad in ('1.2.3.4", "action": "apply', "01.2.3.4", "256.1.1.1", "1.2.3", "1.2.3.4 ", "1.2.3.4\n"):
        assert not ip.fullmatch(bad), bad


def test_the_trigger_refuses_what_the_workflow_refuses() -> None:
    task_file = yaml.safe_load((ROOT / "ansible" / "playbooks" / "tasks" / "deploy-aws-vpn-triggers.yml").read_text())
    trigger = next(t for t in task_file if t.get("name") == "The endpoint trigger's schema")
    schema = trigger["ansible.builtin.set_fact"]["dav_schema"]
    want = build.PLAN_INPUTS_SCHEMA["properties"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"onprem_public_ip", "enable_nat_gateway", "change_note"}
    for key in ("onprem_public_ip", "enable_nat_gateway", "change_note"):
        got = {k: v for k, v in schema["properties"][key].items() if k != "type"}
        assert got == {k: v for k, v in want[key].items() if k != "type"}, key
    patch = next(t for t in task_file if t.get("name") == "The endpoint trigger's schema is current")
    assert patch["ansible.builtin.uri"]["method"] == "PATCH"  # an existing trigger is brought up to date


# --- the branded page (itential/portal/deploy-aws-vpn) reads the workflow it starts ---------------------------------
PAGE = ROOT / "itential" / "portal" / "deploy-aws-vpn" / "index.html"


def test_the_page_draws_its_route_from_task_ids_the_workflow_has() -> None:
    wf, tasks = _deploy()
    page = PAGE.read_text()
    stages = re.findall(r'key: "(\w+)", label: "[^"]+", sub: "[^"]+", tasks: \[([^\]]*)\]', page)
    assert [k for k, _ in stages] == ["plan", "approve", "apply", "psk", "done"]
    for key, ids in stages:
        for tid in re.findall(r'"([0-9a-f]{1,4})"', ids):
            assert tid in tasks, f"page stage {key} watches {tid}, which Deploy AWS VPN does not have"
    for var in ("plan", "outputs", "psk", "outcome", "error", "aws_changed", "rejected"):
        assert var in wf["outputSchema"]["properties"] and f"v.{var}" in page, var


def test_the_page_marks_a_stop_from_the_branches_the_workflow_takes() -> None:
    """The workflow's failures are branches, so its tasks end "complete": the page finds a failed stage by the failure
    branch that ran, and a rejection by the discard branch, and both must be the workflow's real edges."""
    wf, _ = _deploy()
    page = PAGE.read_text()
    branches = dict(re.findall(r'(\w+): \["([0-9a-f]{1,4})", "[0-9a-f]{1,4}"\]', page.split("FAIL_BRANCH = ")[1].split(";")[0]))
    starts = {"plan": ("1d", "1e"), "apply": ("3e", "3f"), "psk": ("4d", "4e")}
    assert branches.keys() == starts.keys()
    for stage, first in branches.items():
        exits = {t for src in starts[stage] for t in wf["transitions"][src]}
        assert first in exits, f"{stage}: {first} is not where {starts[stage]} go on failure"
    assert wf["transitions"]["2b"]["7a"]["state"] == "failure"  # Reject in Work Center
    assert 'ran("7a")' in page


def test_the_page_starts_the_job_under_the_engineers_session_and_holds_no_credential() -> None:
    page = PAGE.read_text()
    route = VERSIONS["operations_manager"]["deploy_aws_vpn"]["endpoint"]["route"]
    assert f'"/operations-manager/triggers/endpoint/{route}"' in page
    assert 'credentials: "same-origin"' in page and "body: JSON.stringify(formData)" in page
    assert 'enable_nat_gateway: $("nat").checked ? "true" : "false"' in page
    # no credential handling at all: no auth header, no storage, no login call, no credential field, no token in a URL
    for forbidden in ("Authorization", "localStorage", "sessionStorage", "/login", 'name="password"', "?token=", "X-Vault-Token"):
        assert forbidden not in page, forbidden
    assert "<script src=" not in page and "<link" not in page  # self-contained, no CDN


def test_presenter_mode_hides_every_octet_of_a_public_address() -> None:
    # the first dev run showed "136.•••.•••.•••": one octet of the home address in a screenshot meant for LinkedIn
    page = PAGE.read_text()
    assert '"•••.•••.•••.•••"' in page
    assert "${a}.•••" not in page


def test_the_page_says_approved_only_once_the_apply_has_started() -> None:
    # Work Center completes the approval task on Reject as well: the first rejected run logged "Approved" (2026-09-29)
    page = PAGE.read_text()
    line = next(ln for ln in page.splitlines() if '"Approved. Applying exactly that plan."' in ln and "log(" in ln)
    assert 'state.apply === "active"' in line


def test_every_service_result_is_read_through_the_json_rpc_envelope() -> None:
    """In a workflow, runService publishes {id, jsonrpc, result: {return_code, stdout_json, ...}} (measured 2026-09-29):
    a path without the `result.` step reads nothing, and an evaluation on it fails every run."""
    _, tasks = _deploy()
    services = {tid for tid, t in tasks.items() if t.get("name") == "runService"}
    for tid, t in tasks.items():
        inc = (t.get("variables") or {}).get("incoming") or {}  # workflow_start / workflow_end have none
        obj = str(inc.get("obj", ""))
        if any(obj == f"$var.{s}.result" for s in services):
            assert inc["query"].startswith("result."), (tid, inc["query"])
        for g in inc.get("evaluation_groups") or []:
            for e in g["evaluations"]:
                if e["operand_1"]["task"] in services:
                    assert e["query"].startswith("result."), (tid, e["query"])


def test_the_page_says_what_each_running_stage_is_doing_and_can_resume_a_job() -> None:
    # the first real apply showed "Waiting for approval" under the route while Terraform applied (2026-09-29)
    page = PAGE.read_text()
    assert 'state.apply === "active") status(' in page and 'state.psk === "active") status(' in page
    # ?job=<id> picks a job back up after a reload; only a Platform job ID is accepted
    assert 'url.searchParams.set("job", jobId)' in page and "/^[0-9a-f]{24}$/.test(resume)" in page


def test_a_failed_apply_says_aws_may_have_changed() -> None:
    # the first real apply stopped part-way with aws_changed unset, and the page said only "Stopped." (2026-09-29)
    page = PAGE.read_text()
    assert page.count('state.apply === "failed" ?') == 2 and "AWS may have changed part-way" in page


def test_the_manual_form_asks_for_what_the_workflow_accepts() -> None:
    import json
    form = json.loads((ROOT / "itential" / "forms" / "lab-deploy-aws-vpn.json").read_text())["schema"]
    want = build.PLAN_INPUTS_SCHEMA["properties"]
    assert set(form["required"]) == {"onprem_public_ip", "enable_nat_gateway", "change_note"}
    assert form["properties"]["onprem_public_ip"]["pattern"] == want["onprem_public_ip"]["pattern"]
    assert form["properties"]["onprem_public_ip"]["maxLength"] == want["onprem_public_ip"]["maxLength"]
    assert form["properties"]["change_note"]["maxLength"] == want["change_note"]["maxLength"]
    assert form["properties"]["enable_nat_gateway"]["enum"] == want["enable_nat_gateway"]["enum"]


def test_deploy_keeps_the_key_strongswan_already_has() -> None:
    """Step 6 feasibility C7: strongSwan reads the PSK only at boot, so Deploy runs `ensure` (a key only when the
    secret has none), never `write` (a new key on every run would leave the box and the router on different keys)."""
    _, tasks = _deploy()
    assert '"action": "ensure"' in tasks["4b"]["variables"]["incoming"]["str"]
    assert '"action": "write"' not in json.dumps(tasks)
    # the close-out says what happened to the key from the service's own summary, on one straight path to 5e
    assert tasks["5b"]["variables"]["incoming"]["query"] == "summary"
    assert tasks["5e"]["variables"]["incoming"]["newSubstr"] == "$var.5b.return_data"
