"""PID S13 (amendment 1.36, ADR 0068): Itential runs the AWS site-to-site VPN. These tests hold the PID, the ADR and
the README to each other and to the two owner rules of 2026-09-26: Terraform (not the native OpenTofu service), and
every AWS secret only in Vault. The service, workflows and verify scripts join them as each piece is built."""

from __future__ import annotations

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
        assert "loop: [Dockerfile, requirements-terraform-run.txt, known_hosts]" in play, name


def test_boto3_is_installed_only_with_hashes() -> None:
    req = (RUNNER / "requirements-terraform-run.txt").read_text()
    pins = re.findall(r"^([a-z0-9_.-]+)==", req, re.M)
    assert "boto3" in pins and all(f"{p}==" in req for p in pins)
    assert req.count("--hash=sha256:") >= len(pins)
    assert "--require-hashes --only-binary=:all:" in (RUNNER / "Dockerfile").read_text()


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
    assert tr["repository"]["reference"] == "main"
    aliases = VAULT["gateway_aliases"]
    for secret in tr["service"]["secrets"]:
        assert secret["name"] in aliases and aliases[secret["name"]]["path"] == VAULT["aws"]["key_path"]
    assert {s["target"] for s in tr["service"]["secrets"]} == {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}
    assert aliases["cloud-devops-pipeline-deploy-key"]["path"] == VAULT["git"]["deploy_key_path"]
    for name, play in PLAYS.items():
        assert play.index("tasks/gateway-vault.yml") < play.index("tasks/gateway-terraform-run.yml"), name
