"""PID S13 (amendment 1.36, ADR 0068): Itential runs the AWS site-to-site VPN. These tests hold the PID, the ADR and
the README to each other and to the two owner rules of 2026-09-26: Terraform (not the native OpenTofu service), and
every AWS secret only in Vault. The service, workflows and verify scripts join them as each piece is built."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import shutil
import os
from pathlib import Path

import pytest
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
    s13 = PID.read_text().split("### S13 —", 1)[1].split("\n## ", 1)[0].split("\n### ", 1)[0]
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


def test_the_psk_writer_keeps_to_its_one_path_and_is_bound_to_the_gateway_host() -> None:
    writer = VAULT["approles"]["itential-aws-psk-writer"]
    assert writer["policy_paths"] == [VAULT["aws"]["psk_path"]] == ["aws/vpn-psk"]
    # R4 (owner decision 2026-10-04): it reads its own path (restore-previous) - no delete, list or sudo
    assert writer["capabilities"] == ["create", "update", "read"]
    # prune: the path's metadata (which versions are live) and destroy (the older ones), on the same path only
    assert writer["kv_endpoints"] == {"metadata/aws/vpn-psk": ["read"], "destroy/aws/vpn-psk": ["update"]}
    assert writer["bound_hosts"] == ["iag-01"]


def test_only_the_writer_has_kv_endpoints() -> None:
    assert [r for r, c in VAULT["approles"].items() if "kv_endpoints" in c] == ["itential-aws-psk-writer"]


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
    assert "itential-aws-psk-writer: {data: [create, read, update], metadata: [read], destroy: [update]}" in prod
    assert "itential-gateway: {data: [read], metadata: [deny], destroy: [deny]}" in prod


def test_the_aws_vpn_tiers_vault_verify_checks_the_psk_path() -> None:
    own, other = ("test-09a-vault.sh", "test-09a-vault-dev.sh")[:: 1 if VERSIONS["aws_vpn"]["tier"] == "prod" else -1]
    assert '" vc aws-psk' in (ROOT / "verify" / own).read_text()
    assert '" vc aws-psk' not in (ROOT / "verify" / other).read_text()  # ADR 0070: the other tier has no AWS access


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
    for line in req.splitlines():  # pins, their hashes and comments only: no index URL, find-links or editable
        assert re.fullmatch(r"#.*|[a-z0-9_.-]+==[^ \\]+ \\|\s+--hash=sha256:[0-9a-f]{64}( \\)?|", line), line
    for name, version in pins.items():  # every package carries at least one hash
        block = re.split(rf"^{re.escape(name)}=={re.escape(version)} \\$", req, maxsplit=1, flags=re.M)[1]
        assert re.match(r"\n\s+--hash=sha256:", block), name
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
    # fabric-bgp (R10, ADR 0073) shares the repository and pin; it binds only the shared device password
    assert set(services) == {"terraform-run", "aws-vpn-psk", "fabric-bgp"}
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
    assert names == [("runService", "terraform-run"), ("InteractiveHTML", None), ("runService", "terraform-run"),
                     ("runService", "aws-vpn-psk")]
    # the approver sees the plan summary, on the branded page (ADR 0077): the body task feeds the renderer
    assert tasks["2b3"]["variables"]["incoming"]["value"] == "$var.job.plan"
    assert tasks["2b"]["variables"]["incoming"]["body"] == "$var.2b5.return_data"
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
    # change_note too: the Platform refuses a start without it (measured on dev 2026-09-29); lifetime_hours since R2b
    assert set(inputs["required"]) == {"onprem_public_ip", "enable_nat_gateway", "change_note", "lifetime_hours"}
    assert inputs["properties"]["lifetime_hours"]["enum"] == VERSIONS["aws_vpn"]["lifetime_hours"]["choices"]


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
    assert tasks["1d"]["variables"]["incoming"]["params"] == "$var.17.object"  # data, not parsed text
    for tid, key in (("1b", "onprem_public_ip"), ("1c", "enable_nat_gateway"), ("17", "expires_at")):
        assert tasks[tid]["name"] == "setObjectKey" and tasks[tid]["variables"]["incoming"]["path"] == [key]
    # the end time comes from the runner's own clock and the lifetime, never from a caller (R2b)
    assert tasks["17"]["variables"]["incoming"]["obj"] == "$var.1c.object"
    assert tasks["17"]["variables"]["incoming"]["value"] == "$var.16.return_data"
    assert tasks["11"]["variables"]["incoming"]["obj"] == "$var.17.object"
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


def _trigger_specs() -> dict:
    """platform.yml's loop over tasks/aws-vpn-triggers.yml: {key: spec} and the loop's own vars."""
    play = yaml.safe_load((ROOT / "ansible" / "playbooks" / "platform.yml").read_text())[0]
    task = next(t for t in play["tasks"] if t.get("ansible.builtin.include_tasks") == "tasks/aws-vpn-triggers.yml")
    return {spec["key"]: spec for spec in task["vars"]["aws_vpn_triggers"]}, task


OPEN_TARGETS = sorted(n for n, t in VERSIONS["aws_vpn"]["targets"].items() if t["window"] == "open")


def test_the_trigger_refuses_what_the_workflow_refuses() -> None:
    specs, task = _trigger_specs()
    assert set(specs) == {"deploy_aws_vpn", "hand_off_aws_vpn", "verify_aws_vpn", "tear_down_aws_vpn"}
    assert task["when"] == "(aws_vpn.tier == 'dev') == (dev_overlay | default(false) | bool)" and task["loop_control"]["loop_var"] == "wt"  # ADR 0070
    schema = specs["deploy_aws_vpn"]["schema"]
    want = build.PLAN_INPUTS_SCHEMA["properties"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"onprem_public_ip", "enable_nat_gateway", "change_note", "lifetime_hours"}
    assert schema["properties"]["lifetime_hours"]["enum"] == "{{ aws_vpn.lifetime_hours.choices }}"  # R2b
    for key in ("onprem_public_ip", "enable_nat_gateway", "change_note"):
        got = {k: v for k, v in schema["properties"][key].items() if k != "type"}
        assert got == {k: v for k, v in want[key].items() if k != "type"}, key
    # Hand Off, Verify and Tear Down take only `target`, one of the open routers - the same as the workflows' own
    # gates (Tear Down's routers are the open ones that are also revert targets: today every open one is)
    for key in ("hand_off_aws_vpn", "verify_aws_vpn", "tear_down_aws_vpn"):
        assert specs[key]["schema"] == "{{ aws_vpn_target_schema }}", key
        assert build.INPUT_GATES[VERSIONS["workflows"][key]] == {"target": {"enum": OPEN_TARGETS}}
    target_schema = task["vars"]["aws_vpn_target_schema"]
    assert target_schema["additionalProperties"] is False and target_schema["required"] == ["target"]
    assert target_schema["properties"]["target"] == {"type": "string", "enum": "{{ aws_vpn_open_targets }}"}
    assert "selectattr('value.window', 'equalto', 'open')" in task["vars"]["aws_vpn_open_targets"]
    tasks_file = yaml.safe_load((ROOT / "ansible" / "playbooks" / "tasks" / "aws-vpn-triggers.yml").read_text())
    patch = next(t for t in tasks_file if t.get("name", "").startswith("The endpoint trigger's schema is current"))
    assert patch["ansible.builtin.uri"]["method"] == "PATCH"  # an existing trigger is brought up to date
    created = next(t for t in tasks_file if t.get("name", "").startswith("The branded page's endpoint trigger"))
    assert created["ansible.builtin.uri"]["body"]["schema"] == "{{ dav_schema }}"


def test_each_workflow_has_its_automation_endpoint_and_form_named_in_versions() -> None:
    om, forms = VERSIONS["operations_manager"], VERSIONS["forms"]
    routes = set()
    for key in ("deploy_aws_vpn", "hand_off_aws_vpn", "verify_aws_vpn"):
        assert om[key]["automation"] == VERSIONS["workflows"][key]
        assert (ROOT / "itential" / "forms" / f"{forms[key]}.json").exists(), key
        routes.add(om[key]["endpoint"]["route"])
    assert routes == {"deploy-aws-vpn", "hand-off-aws-vpn", "verify-aws-vpn"}
    for key in ("hand_off_aws_vpn", "verify_aws_vpn"):
        form = json.loads((ROOT / "itential" / "forms" / f"{forms[key]}.json").read_text())["schema"]
        assert form["required"] == ["target"] and form["properties"]["target"]["enum"] == OPEN_TARGETS


# --- the branded page (itential/portal/deploy-aws-vpn) reads the workflow it starts ---------------------------------
PAGE = ROOT / "itential" / "portal" / "deploy-aws-vpn" / "index.html"


def _workflow(file: str) -> tuple[dict, dict]:
    wf = json.loads((ROOT / "itential" / "workflows" / file).read_text())
    return wf, {tid: t for tid, t in wf["tasks"].items() if isinstance(t, dict)}


PAGE_MODES = {"deploy": "deploy-aws-vpn.json", "handoff": "hand-off-aws-vpn.json", "verify": "verify-aws-vpn.json",
              "teardown": "tear-down-aws-vpn.json"}


def _mode_block(page: str, mode: str) -> str:
    """The page's MODES.<mode> entry, up to the next one."""
    start = page.index(f"    {mode}: {{")
    ends = [page.index(f"    {m}: {{") for m in PAGE_MODES if page.index(f"    {m}: {{") > start] + [page.index("  const LEGS")]
    return page[start:min(ends)]


def test_the_page_draws_its_route_from_task_ids_the_workflow_has() -> None:
    page = PAGE.read_text()
    keys = []
    for mode, file in PAGE_MODES.items():
        wf, tasks = _workflow(file)
        block = _mode_block(page, mode)
        assert f'workflow: "{wf["name"]}"' in block
        stages = re.findall(r'key: "(\w+)", label: "[^"]+", sub: "[^"]+", tasks: \[([^\]]*)\]', block)
        assert stages, mode
        for key, ids in stages:
            keys.append(key)
            for tid in re.findall(r'"([^"]*)"', ids):  # every quoted id, so a typo is caught too
                assert tid in tasks, f"page stage {key} watches {tid}, which {wf['name']} does not have"
    assert keys == ["plan", "approve", "apply", "psk", "checks", "hoapprove", "push", "tunnel", "verdict",
                    "tdapprove", "remove", "awsapprove", "destroy"]
    assert len(set(keys)) == len(keys)  # one route: a stage key names one waypoint
    wf, _ = _workflow("deploy-aws-vpn.json")
    for var in ("plan", "outputs", "psk", "outcome", "error", "aws_changed", "rejected"):
        assert var in wf["outputSchema"]["properties"] and f"v.{var}" in page, var
    wf, _ = _workflow("hand-off-aws-vpn.json")
    for var in ("sha256", "router_state", "outcome", "error", "changed", "rejected"):
        assert var in wf["outputSchema"]["properties"] and f"v.{var}" in page, var
    wf, _ = _workflow("verify-aws-vpn.json")
    assert "judgement" in wf["outputSchema"]["properties"] and "v.judgement" in page
    wf, _ = _workflow("tear-down-aws-vpn.json")
    for var in ("removal_result", "router_state", "destroy_plan", "outcome", "error", "aws_changed", "router_changed"):
        assert var in wf["outputSchema"]["properties"] and f"v.{var}" in page, var


def test_tear_downs_two_cards_each_reject_on_their_own_branch_and_the_page_never_shows_the_lines() -> None:
    page = PAGE.read_text()
    wf, tasks = _workflow("tear-down-aws-vpn.json")
    block = _mode_block(page, "teardown")
    assert 'reject: [{ stage: "tdapprove", task: "a0" }, { stage: "awsapprove", task: "a2" }]' in block
    assert wf["transitions"]["2c"]["a0"]["state"] == "failure" and wf["transitions"]["5f"]["a2"]["state"] == "failure"
    assert 'trigger: "/operations-manager/triggers/endpoint/tear-down-aws-vpn"' in block
    render = page.split("function renderTeardown")[1].split("\n  }\n")[0]
    assert "removal.sha256" in render and "removal.lines" not in render  # the SHA-256 only, never the lines
    assert 'id="tab-teardown"' in page and 'id="teardown-target"' in page and 'const PICKERS = ["handoff-target", "verify-target", "teardown-target"];' in page


def test_the_page_marks_a_stop_from_the_branches_the_workflow_takes() -> None:
    """The workflows' failures are branches, so their tasks end "complete": the page finds a failed stage by the failure
    branch that ran, and a rejection by the reject branch; every one must be a real task that ends the job with a
    reason (or, for Deploy, the branch its stage's tasks take on failure)."""
    page = PAGE.read_text()
    for mode, file in PAGE_MODES.items():
        wf, tasks = _workflow(file)
        block = _mode_block(page, mode)
        fail = block.split("fail: {")[1].split("}")[0]
        branches = {k: re.findall(r'"([^"]*)"', v) for k, v in re.findall(r'(\w+): \[([^\]]*)\]', fail)}
        assert branches, mode
        stage_keys = re.findall(r'key: "(\w+)"', block)
        for stage, ids in branches.items():
            assert stage in stage_keys, (mode, stage)
            for tid in ids:
                assert tid in tasks, f"{mode} {stage}: {tid} is not a task of {wf['name']}"
                # a failure branch ends the job (directly or through the outcome/error notes after it)
                reach, todo = set(), [tid]
                while todo:
                    n = todo.pop()
                    if n in reach:
                        continue
                    reach.add(n)
                    todo.extend(wf["transitions"].get(n, {}))
                assert "workflow_end" in reach, (mode, tid)
    # deploy: each stage's first failure branch is where its tasks go on failure
    wf, _ = _workflow("deploy-aws-vpn.json")
    starts = {"plan": ("1d", "1e"), "apply": ("3e", "3f"), "psk": ("4d", "4e")}
    fail = _mode_block(page, "deploy").split("fail: {")[1].split("}")[0]
    for stage, first in re.findall(r'(\w+): \["([0-9a-f]{1,4})"', fail):
        exits = {t for src in starts[stage] for t in wf["transitions"][src]}
        assert first in exits, f"{stage}: {first} is not where {starts[stage]} go on failure"
    # Reject in Work Center: the approval's failure edge, for both workflows that ask
    for mode, file, approval, reject in (("deploy", "deploy-aws-vpn.json", "2b", "7a"), ("handoff", "hand-off-aws-vpn.json", "6f", "a0")):
        wf, _ = _workflow(file)
        assert wf["transitions"][approval][reject]["state"] == "failure"
        assert f'reject: {{ stage: "' in _mode_block(page, mode) and f'task: "{reject}" }}' in _mode_block(page, mode)


def test_the_page_starts_each_workflow_through_its_own_trigger_and_lists_the_open_routers() -> None:
    page = PAGE.read_text()
    for key, mode in (("deploy_aws_vpn", "deploy"), ("hand_off_aws_vpn", "handoff"), ("verify_aws_vpn", "verify")):
        route = VERSIONS["operations_manager"][key]["endpoint"]["route"]
        assert f'trigger: "/operations-manager/triggers/endpoint/{route}"' in _mode_block(page, mode)
    targets = json.loads(page.split("const TARGETS = ")[1].split(";")[0])
    assert targets == OPEN_TARGETS
    # Hand Off and Verify send only the router: what their endpoint triggers accept
    assert 'start("handoff", { target: $("handoff-target").value }' in page
    assert 'start("verify", { target: $("verify-target").value }' in page


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
    assert set(form["required"]) == {"onprem_public_ip", "enable_nat_gateway", "change_note", "lifetime_hours"}
    lifetimes = VERSIONS["aws_vpn"]["lifetime_hours"]
    assert form["properties"]["lifetime_hours"]["enum"] == lifetimes["choices"]
    assert form["properties"]["lifetime_hours"]["default"] == lifetimes["default"] == "8"  # owner, 2026-10-04
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


def test_no_job_data_ever_becomes_html() -> None:
    """Security review of build step 9: the Verify status line put the job's `error` into innerHTML (stored XSS through a
    shared ?job= link). Now only constants reach HTML: the waypoints (stage constants and numbers), an empty reset, and
    the log row's frame (its time and a constant class); every text from a job goes through textContent."""
    page = PAGE.read_text()
    script = page.split("<script>")[1].split("</script>")[0]
    html_lines = [ln.strip() for ln in script.splitlines() if "innerHTML" in ln]
    allowed = ("g.innerHTML = `", 'li.innerHTML = `<time>${t.toTimeString().slice(0, 5)}</time><span${cls ? ` class="${cls}"` : ""}></span>`;',
               'const dl = $("outputs"); dl.innerHTML = "";', 'seen.clear(); $("log").innerHTML = "";')
    for ln in html_lines:
        assert ln.startswith(allowed), ln
    # the waypoint markup is built from the stage constants only
    wp = script.split("g.innerHTML = `")[1].split("`;")[0]
    assert set(re.findall(r"\$\{([^}]*)\}", wp)) <= {"s.x", "s.y", "s.y + 5", "i + 1", "s.label", "s.y + labelBelow", "s.sub", "s.y + labelBelow + 17"}
    # status(): text only, and no call hands it markup
    status_fn = script.split("function status(")[1].split("\n  }\n")[0]
    assert "innerHTML" not in status_fn and "textContent" in status_fn and 'r.dataset.mask = "1"; setMaskable(r, rest' in status_fn
    assert not re.search(r'status\([^)]*<', script)
    # a resumed (or shared) job must be a job of the tab's own workflow before anything of it is read
    follow = script.split("function follow(")[1].split("\n  }\n")[0]
    assert follow.index("job.name !== MODES[m].workflow") < follow.index("const r = read(job, m)")
    assert "Object.hasOwn(MODES, askedMode)" in script


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_the_journey_orders_legs_by_the_platforms_own_start_time() -> None:
    """Re-check of build step 9: the Platform sends metrics.start_time as epoch milliseconds (an int, read on the dev
    tier 2026-10-01), so Date.parse made every leg 0 and a stale leg was never skipped. Runs the page's own expression."""
    script = PAGE.read_text().split("<script>")[1].split("</script>")[0]
    expr = re.search(r"const startOf = \(job\) => (.+?); //", script).group(1)
    probe = f"""
      const at = (job) => {expr};
      console.log(JSON.stringify([
        at({{metrics: {{start_time: 1790829409132}}}}),
        at({{metrics: {{start_time: "2026-10-01T04:36:49.132Z"}}}}),
        at({{metrics: {{}}}}), at({{}})]));"""
    out = subprocess.run(["node", "-e", probe], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == [1790829409132, 1790829409132, 0, 0]


def _chrome() -> str | None:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if shutil.which(name):
            return shutil.which(name)
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    return mac if os.path.exists(mac) else None


# The page in headless Chrome against a stubbed Platform: `latest` maps a workflow name to its latest job's id (a missing
# name has no runs); `jobs` holds the job documents, or for a job that moves, its documents in the order the page reads
# them (the last one repeats); `triggers` maps a trigger route to the id of the job it starts; `list_delay` holds the job
# lists back that long (ms); `actions` run page code at given times (ms); signed_out answers every call 401. The probe
# reads the route, the status line, the log, the buttons, the address and every URL the page fetched. Served over HTTP:
# as a file the page runs its preview instead.
PROBE = """<script>
window.__calls = [];
const LATEST = %s, JOBS = %s, TRIGGERS = %s, LIST_DELAY = %s, SIGNED_OUT = %s, ACTIONS = %s;
const reads = {};
window.fetch = async (url, opts) => {
  url = String(url); window.__calls.push(((opts && opts.method) || "GET") + " " + url);
  const res = (code, body) => new Response(typeof body === "string" ? body : JSON.stringify(body), { status: code });
  if (SIGNED_OUT) return res(401, "The specified authorization token is malformed or does not correspond with an active session");
  const list = url.match(/contains\\[name\\]=([^&]*)/);
  if (list) {
    await new Promise((ok) => setTimeout(ok, LIST_DELAY));
    const wf = decodeURIComponent(list[1]); return res(200, { data: LATEST[wf] ? [{ _id: LATEST[wf], name: wf }] : [] });
  }
  const trigger = url.match(/\\/triggers\\/endpoint\\/([a-z-]+)$/);
  if (trigger) return TRIGGERS[trigger[1]] ? res(200, { _id: TRIGGERS[trigger[1]] }) : res(404, {});
  const one = url.match(/\\/operations-manager\\/jobs\\/([0-9a-f]{24})$/);
  if (!one || !JOBS[one[1]]) return res(404, {});
  const n = (reads[one[1]] = (reads[one[1]] || 0) + 1), docs = JOBS[one[1]];
  return res(200, { data: docs[Math.min(n, docs.length) - 1] });
};
ACTIONS.forEach(([ms, code]) => setTimeout(() => new Function(code)(), ms));
setTimeout(() => {
  const wp = {}, q = (id) => document.getElementById(id);
  document.querySelectorAll(".wp").forEach((g) => { wp[g.id.slice(3)] = [...g.classList].filter((c) => c !== "wp").join(" "); });
  document.body.dataset.probe = JSON.stringify({ wp, status: q("chart-status").textContent,
    log: [...document.querySelectorAll("#log li span")].map((s) => s.textContent), jobid: q("jobid").textContent,
    disabled: Object.fromEntries(["deploy", "handoff", "verify"].map((m) => [m, q("go-" + m).disabled])),
    router: q("lab-router").textContent,
    pickers: Object.fromEntries(["handoff", "verify", "teardown"].map((m) => [m, q(m + "-target").value])),
    search: location.search, calls: window.__calls });
}, 12000);
</script>
"""
DEPLOY_DONE = ["1d", "1e", "1f", "2b", "3e", "3f", "30", "4d", "4e", "4f", "5e"]
HANDOFF_CHECKS = ["10", "4b", "4c", "4d", "5b", "5c"]
HANDOFF_DONE = [*HANDOFF_CHECKS, "6f", "7c", "74", "7e", "72", "8a", "ee", "92"]


def _job(jid: str, workflow: str, start: int, done: list[str], status: str = "complete", running: list[str] = (),
         variables: dict | None = None) -> dict:
    tasks = {t: {"status": "complete"} for t in done} | {t: {"status": "running"} for t in running}
    return {"_id": jid, "name": workflow, "status": status, "metrics": {"start_time": start}, "tasks": tasks,
            "variables": variables or {}}


def _run_page(tmp_path: Path, latest: dict, jobs: list, signed_out: bool = False, query: str = "",
              triggers: dict | None = None, list_delay: int = 0, actions: list | None = None) -> dict:
    import html
    import threading
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    docs = {}
    for j in jobs:
        seq = j if isinstance(j, list) else [j]
        docs[seq[0]["_id"]] = seq
    page = PAGE.read_text()
    probe = PROBE % tuple(json.dumps(x) for x in (latest, docs, triggers or {}, list_delay, signed_out, actions or []))
    assert page.count("<script>\n(() => {") == 1
    (tmp_path / "index.html").write_text(page.replace("<script>\n(() => {", probe + "<script>\n(() => {"))

    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(tmp_path)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        # no --user-data-dir: headless already uses a throwaway profile, and with one Chrome never exits after the dump
        url = f"http://127.0.0.1:{server.server_port}/index.html{query}"
        run = subprocess.run([_chrome(), "--headless=new", "--disable-gpu", "--no-sandbox", "--virtual-time-budget=15000",
                              "--dump-dom", url], capture_output=True, text=True, timeout=120)
    finally:
        server.shutdown()
        server.server_close()
    found = re.search(r'data-probe="([^"]*)"', run.stdout)
    assert found, f"the probe never ran (Chrome exit {run.returncode}): {run.stderr[-600:]}"
    return json.loads(html.unescape(found.group(1)))


def _ids(n: int) -> str:
    return f"{n:024x}"


@pytest.mark.skipif(_chrome() is None, reason="needs Chrome")
def test_the_page_says_signed_out_instead_of_an_empty_route(tmp_path: Path) -> None:
    """Dev check 2026-10-01: an expired Platform session answered 401 to every job query and the page showed an empty
    route under "Ready. Pick a step below." - as if nothing had ever run."""
    out = _run_page(tmp_path, {}, [], signed_out=True)
    assert out["status"].startswith("Signed out. Log in to the Platform"), out["status"]
    assert not any(out["wp"].values())


@pytest.mark.skipif(_chrome() is None, reason="needs Chrome")
def test_the_route_has_no_gap_when_a_middle_leg_is_older(tmp_path: Path) -> None:
    """A Deploy re-run after the last Hand Off makes that Hand Off stale; a Verify run after both must not be drawn
    on its own beyond the gap (the re-check of build step 9)."""
    jobs = [_job(_ids(1), "Deploy AWS VPN", 2000, DEPLOY_DONE), _job(_ids(2), "Hand Off AWS VPN", 1000, HANDOFF_DONE),
            _job(_ids(3), "Verify AWS VPN", 3000, ["1a", "ee", "5f"])]
    out = _run_page(tmp_path, {j["name"]: j["_id"] for j in jobs}, jobs)
    assert [out["wp"][k] for k in ("plan", "approve", "apply", "psk")] == ["done"] * 4
    assert [out["wp"][k] for k in ("checks", "hoapprove", "push", "tunnel", "verdict")] == [""] * 5


@pytest.mark.skipif(_chrome() is None, reason="needs Chrome")
def test_the_route_ends_where_the_latest_run_stopped(tmp_path: Path) -> None:
    """A Hand Off rejected today ends the route at its approval, even with a later Verify (from the earlier tunnel)."""
    jobs = [_job(_ids(1), "Deploy AWS VPN", 1000, DEPLOY_DONE),
            _job(_ids(2), "Hand Off AWS VPN", 2000, [*HANDOFF_CHECKS, "a0", "a2"], variables={"rejected": True}),
            _job(_ids(3), "Verify AWS VPN", 3000, ["1a", "ee", "5f"])]
    out = _run_page(tmp_path, {j["name"]: j["_id"] for j in jobs}, jobs)
    assert out["wp"]["checks"] == "done" and out["wp"]["hoapprove"] == "rejected"
    assert out["wp"]["push"] == out["wp"]["tunnel"] == out["wp"]["verdict"] == ""


def _reads(out: dict, jid: str) -> int:
    return sum(c == f"GET /operations-manager/jobs/{jid}" for c in out["calls"])


@pytest.mark.skipif(_chrome() is None, reason="needs Chrome")
def test_only_the_newest_running_job_writes_the_log(tmp_path: Path) -> None:
    """Two running legs (a Hand Off waiting for approval, a Verify after it) each had a poller writing the log, the
    status line and the address. Both are still polled - their stops and buttons stay true - but only the newest is
    on show."""
    jobs = [_job(_ids(1), "Deploy AWS VPN", 1000, DEPLOY_DONE),
            _job(_ids(2), "Hand Off AWS VPN", 2000, HANDOFF_CHECKS, status="paused", running=["6f"]),
            _job(_ids(3), "Verify AWS VPN", 3000, ["1a"], status="running", running=["ee"])]
    out = _run_page(tmp_path, {j["name"]: j["_id"] for j in jobs}, jobs)
    assert out["jobid"] == f"Verify AWS VPN: job {_ids(3)}" and f"job={_ids(3)}" in out["search"]
    assert not any("approval" in line for line in out["log"]), out["log"]
    assert out["status"].startswith("Verifying."), out["status"]
    assert _reads(out, _ids(2)) >= 2 and _reads(out, _ids(3)) >= 2
    assert out["disabled"] == {"deploy": False, "handoff": True, "verify": True}
    assert out["wp"]["hoapprove"] == "active" and out["wp"]["verdict"] == "active"


@pytest.mark.skipif(_chrome() is None, reason="needs Chrome")
def test_a_running_leg_after_a_stopped_one_is_followed(tmp_path: Path) -> None:
    """A Hand Off started after a rejected Deploy runs against the earlier deployment: it is followed and on show, and
    its button is off, though the finished route ends at the rejection (review of the follow-ups)."""
    jobs = [_job(_ids(1), "Deploy AWS VPN", 1000, ["1a", "1d", "1e", "1f", "2b", "7a", "7d", "7f"], variables={"rejected": True}),
            _job(_ids(2), "Hand Off AWS VPN", 2000, HANDOFF_CHECKS, status="paused", running=["6f"])]
    out = _run_page(tmp_path, {j["name"]: j["_id"] for j in jobs}, jobs)
    assert out["jobid"] == f"Hand Off AWS VPN: job {_ids(2)}"
    assert out["disabled"]["handoff"] is True and out["disabled"]["deploy"] is False
    assert out["wp"]["approve"] == "rejected" and out["wp"]["hoapprove"] == "active"


@pytest.mark.skipif(_chrome() is None, reason="needs Chrome")
def test_a_job_started_here_owns_the_log_while_another_runs(tmp_path: Path) -> None:
    """Starting a Hand Off while a Verify is on show: the Verify's poller must stop writing the log at once (it used
    to refill the cleared log until the POST came back), and its button comes back on when it finishes."""
    verify = [_job(_ids(3), "Verify AWS VPN", 3000, ["1a"], status="running", running=["ee"])] * 2 + [
        _job(_ids(3), "Verify AWS VPN", 3000, ["1a", "ee", "5f"], variables={"outcome": "tunnel up: from the old job"})]
    handoff = _job(_ids(5), "Hand Off AWS VPN", 4000, HANDOFF_CHECKS, status="paused", running=["6f"])
    jobs = [_job(_ids(1), "Deploy AWS VPN", 1000, DEPLOY_DONE), _job(_ids(2), "Hand Off AWS VPN", 2000, HANDOFF_DONE),
            verify, handoff]
    out = _run_page(tmp_path, {"Deploy AWS VPN": _ids(1), "Hand Off AWS VPN": _ids(2), "Verify AWS VPN": _ids(3)}, jobs,
                    triggers={"hand-off-aws-vpn": _ids(5)},
                    actions=[[1000, 'document.getElementById("tab-handoff").click(); document.getElementById("go-handoff").click();']])
    assert out["jobid"] == f"Hand Off AWS VPN: job {_ids(5)}" and f"job={_ids(5)}" in out["search"]
    assert not any("old job" in line for line in out["log"]), out["log"]
    assert any("Waiting for approval" in line for line in out["log"]), out["log"]
    assert out["disabled"] == {"deploy": False, "handoff": True, "verify": False}
    assert out["wp"]["verdict"] == "done"


@pytest.mark.skipif(_chrome() is None, reason="needs Chrome")
def test_the_journey_never_takes_the_page_from_a_job_started_here(tmp_path: Path) -> None:
    """The journey reads up to six documents one after another; a Verify started meanwhile used to be dropped for an
    older running job when the journey finished. The older job is still polled, off show."""
    handoff = _job(_ids(2), "Hand Off AWS VPN", 2000, HANDOFF_CHECKS, status="paused", running=["6f"])
    verify = _job(_ids(6), "Verify AWS VPN", 5000, ["1a"], status="running", running=["ee"])
    jobs = [_job(_ids(1), "Deploy AWS VPN", 1000, DEPLOY_DONE), handoff, verify]
    out = _run_page(tmp_path, {"Deploy AWS VPN": _ids(1), "Hand Off AWS VPN": _ids(2)}, jobs,
                    triggers={"verify-aws-vpn": _ids(6)}, list_delay=1500,
                    actions=[[300, 'document.getElementById("tab-verify").click(); document.getElementById("go-verify").click();']])
    assert out["jobid"] == f"Verify AWS VPN: job {_ids(6)}" and f"job={_ids(6)}" in out["search"]
    assert not any("approval" in line for line in out["log"]), out["log"]
    assert _reads(out, _ids(2)) >= 2
    assert out["disabled"]["handoff"] is True and out["disabled"]["verify"] is True
    assert out["wp"]["hoapprove"] == "active"


@pytest.mark.skipif(_chrome() is None, reason="needs Chrome")
def test_the_lab_names_the_router_of_the_job_on_show(tmp_path: Path) -> None:
    """Recorded cycle 2026-10-02: a Tear Down of dc1-wan01, resumed by its link, showed "clab-rtr1" on the lab's shore
    and in the pickers (the first router in the list), so three captures of that run could not be used."""
    td = _job(_ids(7), "Tear Down AWS VPN", 1000, ["1e", "17"], status="paused", running=["2c"],
              variables={"target": "dc1-wan01"})
    out = _run_page(tmp_path, {}, [td], query=f"?job={_ids(7)}&mode=teardown")
    assert out["router"] == "dc1-wan01", out["router"]
    assert out["pickers"] == {"handoff": "dc1-wan01", "verify": "dc1-wan01", "teardown": "dc1-wan01"}


@pytest.mark.skipif(_chrome() is None, reason="needs Chrome")
def test_the_route_names_the_router_its_latest_leg_ran_on(tmp_path: Path) -> None:
    """Without a link, the lab's shore names the router of the newest run drawn on the route, not the list's first."""
    jobs = [_job(_ids(1), "Deploy AWS VPN", 1000, DEPLOY_DONE),
            _job(_ids(2), "Hand Off AWS VPN", 2000, HANDOFF_DONE, variables={"target": "dc1-wan01"}),
            _job(_ids(3), "Verify AWS VPN", 3000, ["1a", "ee", "5f"], variables={"target": "dc1-wan01"})]
    out = _run_page(tmp_path, {j["name"]: j["_id"] for j in jobs}, jobs)
    assert out["router"] == "dc1-wan01" and out["pickers"]["verify"] == "dc1-wan01", out


@pytest.mark.skipif(_chrome() is None, reason="needs Chrome")
def test_picking_a_router_names_it_everywhere(tmp_path: Path) -> None:
    """One router choice for the page: the shore and every picker follow the one just made."""
    pick = 'const s = document.getElementById("verify-target"); s.value = "dc1-wan01"; s.dispatchEvent(new Event("change"));'
    out = _run_page(tmp_path, {}, [], actions=[[500, pick]])
    assert out["router"] == "dc1-wan01"
    assert out["pickers"] == {"handoff": "dc1-wan01", "verify": "dc1-wan01", "teardown": "dc1-wan01"}


@pytest.mark.skipif(_chrome() is None, reason="needs Chrome")
def test_starting_a_leg_clears_the_legs_after_it(tmp_path: Path) -> None:
    """Recorded cycle 2026-10-02: a Hand Off started from the page kept the earlier Verify's stop drawn as done until a
    reload (the journey's own rule: a leg older than the one before it is stale)."""
    jobs = [_job(_ids(1), "Deploy AWS VPN", 1000, DEPLOY_DONE), _job(_ids(2), "Hand Off AWS VPN", 2000, HANDOFF_DONE),
            _job(_ids(3), "Verify AWS VPN", 3000, ["1a", "ee", "5f"]),
            _job(_ids(5), "Hand Off AWS VPN", 4000, HANDOFF_CHECKS, status="paused", running=["6f"])]
    out = _run_page(tmp_path, {"Deploy AWS VPN": _ids(1), "Hand Off AWS VPN": _ids(2), "Verify AWS VPN": _ids(3)}, jobs,
                    triggers={"hand-off-aws-vpn": _ids(5)},
                    actions=[[1500, 'document.getElementById("tab-handoff").click(); document.getElementById("go-handoff").click();']])
    assert out["jobid"] == f"Hand Off AWS VPN: job {_ids(5)}"
    assert out["wp"]["plan"] == "done" and out["wp"]["hoapprove"] == "active"
    assert out["wp"]["verdict"] == "" and out["wp"]["tunnel"] == "", out["wp"]


@pytest.mark.skipif(_chrome() is None, reason="needs Chrome")
def test_a_failed_start_forgets_the_job_shown_before(tmp_path: Path) -> None:
    """A refused start left the earlier job's ID in the address, so a reload resumed that job (re-check nit)."""
    verify = _job(_ids(3), "Verify AWS VPN", 3000, ["1a", "ee", "5f"], variables={"outcome": "tunnel up"})
    out = _run_page(tmp_path, {}, [verify], query=f"?job={_ids(3)}&mode=verify",
                    actions=[[1000, 'document.getElementById("go-verify").click();']])
    assert "POST /operations-manager/triggers/endpoint/verify-aws-vpn" in out["calls"]
    assert "job=" not in out["search"] and out["status"].startswith("The Platform refused the request"), out
    assert out["disabled"]["verify"] is False


@pytest.mark.skipif(_chrome() is None, reason="needs Chrome")
def test_a_rejected_deploy_is_never_logged_as_stopped(tmp_path: Path) -> None:
    """Deploy's reject branch sets `error` only when the discard fails (task 70); the page logged "Stopped: ..." before
    "Rejected." Now the rejection comes first and the discard failure is a note under it."""
    error = "the rejected plan could not be discarded; it stays in the state bucket until removed"
    job = _job(_ids(4), "Deploy AWS VPN", 1000, ["1a", "1d", "1e", "1f", "2b", "7a", "7b", "7c", "71", "70", "7d", "7e", "7f"],
               variables={"rejected": True, "aws_changed": False, "error": error,
                          "outcome": "rejected in Work Center: the plan was discarded, nothing changed in AWS"})
    out = _run_page(tmp_path, {}, [job], query=f"?job={_ids(4)}&mode=deploy")
    assert not any(line.startswith("Stopped") for line in out["log"]), out["log"]
    assert out["log"][-2:] == ["Rejected. Nothing changed in AWS.", f"Note: {error}."]
    assert out["status"] == "Rejected. Nothing changed in AWS; the plan could not be discarded."
    assert out["wp"]["approve"] == "rejected"


def test_each_failure_branch_belongs_to_its_own_stage() -> None:
    """A branch listed under a stage must not be reachable from the next stage's first task: `b9` (the push could not
    start, after the approval) was listed under Checks and would have drawn Checks failed after an approval."""
    page = PAGE.read_text()

    def reach(wf, start):
        seen, todo = set(), [start]
        while todo:
            n = todo.pop()
            if n not in seen:
                seen.add(n)
                todo.extend(wf["transitions"].get(n, {}))
        return seen

    for mode, file in PAGE_MODES.items():
        wf, tasks = _workflow(file)
        block = _mode_block(page, mode)
        stages = re.findall(r'key: "(\w+)", label: "[^"]+", sub: "[^"]+", tasks: \["([^"]+)"', block)
        starts = re.findall(r'key: "(\w+)"[^\n]*start: \[([^\]]*)\]', block)
        for key, ids in starts:
            for tid in re.findall(r'"([^"]*)"', ids):
                assert tid in tasks, f"{mode} {key}: start task {tid} is not in {wf['name']}"
        fail = dict(re.findall(r'(\w+): \[([^\]]*)\]', block.split("fail: {")[1].split("}")[0]))
        order = [k for k, _ in stages]
        for i, (key, _) in enumerate(stages[:-1]):
            later = reach(wf, stages[i + 1][1])
            for tid in re.findall(r'"([^"]*)"', fail.get(key, "")):
                assert tid not in later, f"{mode}: {tid} is listed under {key} but only happens from {order[i + 1]} on"


@pytest.mark.skipif(not shutil.which("ansible"), reason="needs ansible on PATH")
def test_the_target_schema_renders_to_the_open_targets() -> None:
    # rendered by Ansible itself: the endpoint triggers of Hand Off and Verify accept exactly the open routers
    _, task = _trigger_specs()
    open_expr = task["vars"]["aws_vpn_open_targets"].strip()[2:-2]
    cmd = ["ansible", "localhost", "-i", "localhost,", "-c", "local", "-o", "-m", "ansible.builtin.debug", "-a",
           "msg={{ {'targets': (" + open_expr + ")} | to_json }}", "-e", f"@{ROOT / 'itential' / 'versions.yaml'}"]
    run = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=ROOT, env={**os.environ, "ANSIBLE_NOCOLOR": "1"})
    assert run.returncode == 0, (run.stdout + run.stderr)[-400:]
    result = json.loads(run.stdout.split("localhost | SUCCESS => ", 1)[1])
    got = result["msg"] if isinstance(result["msg"], dict) else json.loads(result["msg"])
    assert got["targets"] == OPEN_TARGETS


# --- R2b: a deployment's end time, chosen at Deploy and approved on its card ------------------------------------------

NOW = __import__("datetime").datetime(2026, 10, 4, 12, 34, 56, tzinfo=__import__("datetime").timezone.utc)
CHOICES = VERSIONS["aws_vpn"]["lifetime_hours"]["choices"]


@pytest.mark.parametrize("hours, end", [("8", "2026-10-04T20:34:00Z"), ("72", "2026-10-07T12:34:00Z"),
                                        ("none", "none")])
def test_the_end_time_is_now_to_the_minute_plus_the_lifetime(hours: str, end: str) -> None:
    out = build.expires_from({"lifetime_hours": hours}, NOW, CHOICES)
    assert out["expires_at"] == end
    assert re.fullmatch(build.PLAN_INPUTS_SCHEMA["properties"]["expires_at"]["pattern"], end)


@pytest.mark.parametrize("hours", ["9", "", "-1", "8; x", None, 8])
def test_a_lifetime_not_offered_is_refused_by_the_input_check(hours) -> None:
    out = build.expires_from({"lifetime_hours": hours}, NOW, CHOICES)
    assert out["expires_at"] == "invalid"
    assert not re.fullmatch(build.PLAN_INPUTS_SCHEMA["properties"]["expires_at"]["pattern"], out["expires_at"])


def test_the_end_time_step_runs_as_the_gateway_runs_it() -> None:
    import json
    import subprocess
    import sys
    run = subprocess.run([sys.executable, "-I", "-c", build.EXPIRES_CODE], input=json.dumps({"lifetime_hours": "2"}),
                         capture_output=True, text=True, timeout=30, env={}, check=True)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00Z", json.loads(run.stdout)["expires_at"])


def test_every_lifetime_fits_terraform_runs_bound() -> None:
    assert all(h == "none" or 0 < int(h) <= 31 * 24 for h in CHOICES) and "none" in CHOICES


def test_the_card_shows_the_end_time_and_what_approving_it_means() -> None:
    _, tasks = _deploy()
    end = tasks["2c"]["variables"]["incoming"]
    assert end["substr"] == "__END__" and end["newSubstr"] == "$var.job.expires_at" and end["str"] == "$var.2a.replacedString"
    assert tasks["2b2"]["variables"]["incoming"]["value"] == "$var.2c.replacedString"  # the page's lede (ADR 0077)
    message = tasks["2a"]["variables"]["incoming"]["str"]
    assert "__END__" in message and "Tear Down Expired AWS VPN" in message and "no further card" in message


def test_the_page_offers_the_same_lifetimes_with_the_same_default() -> None:
    page = PAGE.read_text()
    select = page.split('<select id="lifetime" name="lifetime_hours"')[1].split("</select>")[0]
    assert re.findall(r'<option value="([^"]+)"', select) and sorted(re.findall(r'<option value="([^"]+)"', select)) == sorted(CHOICES)
    assert re.findall(r'<option value="([^"]+)" selected', select) == [VERSIONS["aws_vpn"]["lifetime_hours"]["default"]]
    assert 'lifetime_hours: $("lifetime").value' in page
