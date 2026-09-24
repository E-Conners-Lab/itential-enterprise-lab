"""PID S8 criteria 2-3, Phase 9a (ADR 0065): Vault holds the lab's device and API credentials, read through Itential's
built-in clients. These tests hold the PID and the ADR to each other; the play, oracle and verify checks join them as
each piece is built."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PID = ROOT / "docs" / "PID.md"
ADR = ROOT / "docs" / "adr" / "0065-vault-first-through-itentials-built-in-clients.md"


def test_pid_records_the_phase_9a_split_and_its_evals() -> None:
    pid = PID.read_text()
    assert "| **Version** | 1.34 |" in pid
    assert "| 1.34 | 2026-09-23 |" in pid and "Amendment 1.34 (ADR 0065)" in pid
    assert "| 9a *(1.34)* | `phase-9a/vault` | `verify/test-09a-vault.sh`" in pid
    # the sealed-Vault and rotation evals, and the pre-mortem row, are the reason 9a is safe to run
    assert "| E14 *(1.34)* | Vault sealed" in pid and "| E15 *(1.34)* |" in pid
    assert "| Vault sealed or lost *(1.34, ADR 0065)* |" in pid


def test_adr_keeps_the_decisions_the_build_depends_on() -> None:
    adr = ADR.read_text()
    assert "**Status:** accepted" in adr
    # no custom plugin: the Gateway has Vault built in since 5.5.0
    assert "--type vault" in adr and "No custom plugin" in adr
    # the Platform: AppRole, read-only, address-bound, a secret ID that never expires or runs out of uses
    for fact in ("ITENTIAL_VAULT_READ_ONLY=true", "secret_id_bound_cidrs", "`secret_id_ttl` 0", "`secret_id_num_uses` 0"):
        assert fact in adr, fact
    # the sealed Vault is visible, not notified: Alertmanager routes to lab-null
    assert "notifies nobody" in adr and "lab-null" in adr
    assert "make vault-snapshot" in adr and "make vault-unseal" in adr


# --- the oracle, the plays and the dev overlay (ADR 0065) ------------------------------------------------------
VERSIONS = ROOT / "itential" / "versions.yaml"
MANIFEST = ROOT / "docs" / "image-manifest.md"
CONFIG_TASKS = ROOT / "ansible" / "playbooks" / "tasks" / "vault-config.yml"
DEV_PLAY = ROOT / "ansible" / "playbooks" / "vault-dev.yml"
DEV_OVERLAY = ROOT / "ansible" / "playbooks" / "vars" / "itential-dev.yml"
DEV_HCL = ROOT / "itential" / "vault" / "vault-dev.hcl"


def _vault() -> dict:
    return yaml.safe_load(VERSIONS.read_text())["vault"]


def test_oracle_pins_the_manifest_versions() -> None:
    v = _vault()
    manifest = MANIFEST.read_text()
    assert v["image"]["tag"] == "2.0.4" and "`hashicorp/vault:2.0.4`" in manifest
    assert v["chart"]["version"] == "0.34.1" and "`hashicorp/vault` 0.34.1" in manifest


def test_every_gateway_alias_points_at_a_seeded_key() -> None:
    v = _vault()
    for alias, ref in v["gateway_aliases"].items():
        assert ref["key"] in v["secrets"][ref["path"]], alias


def test_each_reader_reads_only_what_it_resolves() -> None:
    roles = _vault()["approles"]
    # the Platform resolves service credentials only; device passwords are the Gateway's (ADR 0065 decisions 5-6)
    assert roles["itential-platform"]["policy_paths"] == ["services/*"]
    assert "devices/*" in roles["itential-gateway"]["policy_paths"]
    assert "services/servicenow" not in roles["itential-gateway"]["policy_paths"]


def test_servicenow_is_never_seeded_on_dev() -> None:
    assert _vault()["secrets"]["services/servicenow"].get("prod_only") is True


def test_policies_grant_read_and_nothing_else() -> None:
    text = CONFIG_TASKS.read_text()
    assert 'capabilities = ["read"]' in text
    for verb in ("create", "update", "delete", "list", "sudo"):
        assert f'"{verb}"' not in text, verb


def test_approles_never_expire_or_run_out_and_are_address_bound() -> None:
    text = CONFIG_TASKS.read_text()
    for fact in ("secret_id_ttl: 0", "secret_id_num_uses: 0",
                 "secret_id_bound_cidrs: \"{{ vault_role_cidrs[item.item.key] }}\"",
                 "token_bound_cidrs: \"{{ vault_role_cidrs[item.item.key] }}\""):
        assert fact in text, fact


def test_every_task_that_sends_a_token_hides_its_output() -> None:
    tasks = yaml.safe_load(CONFIG_TASKS.read_text())
    for t in tasks:
        body = yaml.safe_dump(t)
        if "vault_token" in body or "vault_seed" in body:
            assert t.get("no_log") is True, t["name"]


def test_the_dev_play_refuses_production_and_keeps_its_init_output_root_only() -> None:
    text = DEV_PLAY.read_text()
    assert "dev_overlay | default(false) | bool" in text and "platform_target is not defined" in text
    assert "secret_shares: 1" in text and 'mode: "0600"' in text
    plays = yaml.safe_load(text)
    for t in plays[0]["tasks"]:
        if any(k in yaml.safe_dump(t) for k in ("init.json", "root_token", "unseal", "key.pem")):
            assert t.get("no_log") is True, t["name"]


def test_the_dev_overlay_names_every_role() -> None:
    overlay = yaml.safe_load(DEV_OVERLAY.read_text())
    assert set(overlay["vault_role_cidrs"]) == set(_vault()["approles"])


def test_the_dev_vault_serves_tls_only() -> None:
    hcl = DEV_HCL.read_text()
    assert "tls_cert_file" in hcl and "tls_disable" not in hcl


# --- the readers' wiring (itential.yml, gateway-vault.yml, platform-assets.yml, integrations.yml) -----------------
PLAYS = ROOT / "ansible" / "playbooks"
PROD_OVERLAY = PLAYS / "vars" / "itential-prod.yml"


def test_vault_is_on_for_dev_and_for_production_since_the_cut_over() -> None:
    """Production turned it on at the cut-over (step 5, ADR 0065); before that it was off by leaving it out."""
    assert yaml.safe_load(DEV_OVERLAY.read_text())["vault_enabled"] is True
    assert yaml.safe_load(PROD_OVERLAY.read_text())["vault_enabled"] is True


def test_platform_references_are_whole_field_values() -> None:
    # an adapter or integration field resolves $SECRET_ only when the reference is the entire value (measured)
    for name, ref in _vault()["platform_refs"].items():
        assert ref.startswith("$SECRET_") and " $KEY_" in ref, name
    seeds = _vault()["secrets"]["services/netbox"]
    assert seeds["header"] == {"env": "NETBOX_TOKEN", "prefix": "Token "}


def test_the_platform_env_is_read_only_approle_with_the_lab_ca() -> None:
    text = (PLAYS / "itential.yml").read_text()
    block = text[text.index("{% if vault_enabled | default(false) | bool %}"):text.index("{% endif %}")]
    for line in ("ITENTIAL_VAULT_AUTH_METHOD=approle", "ITENTIAL_VAULT_READ_ONLY=true",
                 "ITENTIAL_VAULT_SECRETS_ENDPOINT={{ vault.kv_mount }}/data", "NODE_EXTRA_CA_CERTS=/opt/vault/lab-root-ca.crt"):
        assert line in block, line
    assert "ITENTIAL_VAULT_TOKEN" not in text


def test_every_task_that_handles_a_secret_id_hides_its_output() -> None:
    exempt = ("ansible.builtin.include_tasks", "ansible.builtin.assert")
    for name in ("vault-secret-id.yml", "gateway-vault.yml"):
        for t in yaml.safe_load((PLAYS / "tasks" / name).read_text()):
            if any(k in t for k in exempt):
                continue
            body = yaml.safe_dump(t)
            # the secret itself, not the *_changed flag whose name contains it
            if re.search(r"vault_admin_token|vault_reader_secret_id(?!_changed)|gw_secret_now|configuration/export", body):
                assert t.get("no_log") is True, f"{name}: {t['name']}"


def test_the_gateway_import_never_carries_users_and_restarts_server_then_runner() -> None:
    tasks = yaml.safe_load((PLAYS / "tasks" / "gateway-vault.yml").read_text())
    wanted = next(t for t in tasks if t["name"].startswith("Provider and aliases wanted"))
    assert set(wanted["ansible.builtin.set_fact"]["gw_vault_wanted"]) == {"secret-providers", "secrets"}
    names = [t["name"] for t in tasks]
    server = names.index("Gateway server restarted to load the provider (it caches providers)")
    runner = names.index("Runner restarted after the server (the server's start rewrote the netsdk pex paths)")
    assert server < runner
    assert any(n.startswith("The export matches what was sent") for n in names)


def test_nodes_adapter_and_integrations_switch_to_references_only_when_vault_is_on() -> None:
    assets = (PLAYS / "tasks" / "platform-assets.yml").read_text()
    assert "('$GATEWAYSECRET_(' ~ vault.device_password_alias ~ ')') if vault_enabled" in assets
    assert "vault.platform_refs.netbox_adapter_token if vault_enabled" in assets
    assert assets.count("'itential_password': device_password") == 3
    integrations = (PLAYS / "tasks" / "integrations.yml").read_text()
    assert "vault.platform_refs.netbox_integration_value if vault_enabled" in integrations
    assert "vault.platform_refs.servicenow_password if vault_enabled" in integrations


def test_the_netbox_adapter_stores_an_empty_password_so_the_ui_shows_no_default() -> None:
    assets = yaml.safe_load((PLAYS / "tasks" / "platform-assets.yml").read_text())
    adapters = next(t for t in assets if t["name"] == "Adapter instances")["ansible.builtin.set_fact"]["adapter_instances"]
    netbox = next(a for a in adapters if a["name"] == "NetBox")
    assert netbox["properties"]["authentication"]["password"] == ""


# --- verify/test-09a-vault-dev.sh: the dev tier's exit test (PID section 3) -------------------------------------------------
VERIFY = ROOT / "verify" / "test-09a-vault-dev.sh"
VAULTCHECK = ROOT / "verify" / "vaultcheck.py"


def test_the_exit_test_runs_in_verify_dev_and_never_in_verify() -> None:
    assert VERIFY.exists() and VERIFY.stat().st_mode & 0o111, "test-09a must exist and be executable"
    make = (ROOT / "Makefile").read_text()
    start = make.index("verify-dev:")
    verify_dev = make[start:make.index("\n\n", start)].splitlines()
    assert "\tverify/test-09a-vault-dev.sh" in verify_dev
    # the `-dev` suffix is what keeps verify/run.sh (make verify, production) from selecting it
    assert VERIFY.name.endswith("-dev.sh")


def test_the_exit_test_covers_s8_e14_and_e15() -> None:
    text = VERIFY.read_text()
    for check in ("S8.2a", "S8.2b", "S8.2c", "S8.2d", "S8.2e", "S8.3a", "S8.3b", "S8.3c", "S8.3d", "E14", "E15"):
        assert f'check "{check} ' in text, check
    assert " -k " not in text and "--insecure" not in text
    # E14 unseals in a finally: a failing check must never leave the dev Vault sealed
    assert "finally:" in VAULTCHECK.read_text().split("def c_sealed")[1].split("def ")[0]


def test_vaultcheck_keeps_credentials_out_of_argv_and_output() -> None:
    text = VAULTCHECK.read_text()
    for var in ("VAULT_ADMIN_TOKEN", "ITENTIAL_ADMIN_PASSWORD", "DEVICE_PASSWORD"):
        assert f'os.environ["{var}"]' in text, var
    assert "sys.argv[1]" in text and "sys.argv[2]" not in text, "the only argument is the check name"
    # send-config echoes the line it sent (with the password): only its success flag may be used
    assert 'p.run("send-config"' in text and ".get(node, False)" in text


def test_secret_id_metadata_is_a_json_string_not_an_object() -> None:
    tasks = yaml.safe_load((PLAYS / "tasks" / "vault-secret-id.yml").read_text())
    issue = next(t for t in tasks if t["name"].startswith("Issue a secret ID"))
    meta = issue["ansible.builtin.uri"]["body"]["metadata"]
    assert isinstance(meta, str) and "{{" not in meta, "Vault answers 400 to an object (measured 2026-09-23)"


def test_external_integrations_only_ever_get_their_credential_rewritten() -> None:
    """Owner decision 2026-09-23: production's hand-made netbox-latest keeps its credential in Vault, but no play owns
    that instance - so the task may only replace the authentication value, never create, delete or re-model."""
    assert _vault()["external_integrations"]["netbox-latest"] == {"auth": "tokenAuth", "ref": "netbox_integration_value"}
    tasks = yaml.safe_load((PLAYS / "tasks" / "vault-external-integrations.yml").read_text())
    methods = {t.get("ansible.builtin.uri", {}).get("method", "GET") for t in tasks if "ansible.builtin.uri" in t}
    assert methods == {"GET", "PUT"}, f"only a read and a properties update, got {methods}"
    put = next(t for t in tasks if t.get("ansible.builtin.uri", {}).get("method") == "PUT")
    assert put["ansible.builtin.uri"]["url"].endswith("/properties")
    wanted = next(t for t in tasks if "ext_int_wanted" in t.get("ansible.builtin.set_fact", {}))
    assert "inner | combine({'authentication'" in wanted["ansible.builtin.set_fact"]["ext_int_wanted"]
    platform = (PLAYS / "platform.yml").read_text()
    assert "tasks/vault-external-integrations.yml" in platform


def test_dev_approles_are_bound_to_the_pinned_vault_network_gateway() -> None:
    """Measured 2026-09-23: every request from the dev VM's containers reaches Vault from the vault-dev network's gateway.
    The subnet is pinned in the oracle and passed to Compose, and the overlay binds both roles to that gateway."""
    dev = _vault()["dev"]
    import ipaddress
    assert ipaddress.ip_address(dev["docker_gateway"]) in ipaddress.ip_network(dev["docker_subnet"])
    overlay = yaml.safe_load(DEV_OVERLAY.read_text())["vault_role_cidrs"]
    for role, cidrs in overlay.items():
        assert cidrs == ["{{ vault.dev.docker_gateway }}/32"], role
    compose = (ROOT / "itential" / "vault" / "compose.dev.yml").read_text()
    assert "subnet: ${VAULT_SUBNET" in compose and "gateway: ${VAULT_GATEWAY" in compose


def test_the_c8000v_vendor_admin_is_removed_before_it_is_replaced_and_the_verify_proves_it() -> None:
    """IOS-XE refuses a `secret` for a user that already has a `password`: the vendor admin/admin (plain text) survived
    the template on both dev routers until 2026-09-23. The template removes it first; S10.7 proves the refusal."""
    lines = (ROOT / "clab" / "configs" / "c8000v.cfg.j2").read_text().splitlines()
    assert lines.index("no username admin") + 1 == lines.index("username admin privilege 15 secret 0 {{ clab_password }}")
    verify = (ROOT / "verify" / "test-12a-clab-dev.sh").read_text()
    assert 'username="admin", password="admin"' in verify and '[ "$adm" = refused ]' in verify
    assert "admin/admin refused on every node" in verify
    assert "admin`/`admin` login is refused on every node" in (ROOT / "docs" / "PID.md").read_text()


# --- production cut-over (step 5) ----------------------------------------------------------------------------------
def test_production_has_one_vault_switch_in_both_places_that_read_it() -> None:
    """The HA2 plays read itential/ha2/versions.yaml, the replay reads vars/itential-prod.yml: they must agree, or a
    plain `make phase-platform-ha2` would put plaintext credentials back after the cut-over."""
    ha2 = yaml.safe_load((ROOT / "itential" / "ha2" / "versions.yaml").read_text())
    prod = yaml.safe_load((ROOT / "ansible" / "playbooks" / "vars" / "itential-prod.yml").read_text())
    assert ha2.get("vault_enabled") is True and prod.get("vault_enabled") is True


def test_the_production_platform_gets_its_vault_client_only_behind_the_switch() -> None:
    compose = (ROOT / "itential" / "ha2" / "platform.compose.yml.j2").read_text()
    block = compose[compose.index("{% if vault_enabled"):compose.index("{% endif %}")]
    for var in ("ITENTIAL_VAULT_URL", "ITENTIAL_VAULT_AUTH_METHOD: approle", "ITENTIAL_VAULT_READ_ONLY: \"true\"",
                "ITENTIAL_VAULT_ROLE_ID: ${ITENTIAL_VAULT_ROLE_ID:?", "ITENTIAL_VAULT_SECRET_ID: ${ITENTIAL_VAULT_SECRET_ID:?",
                "NODE_EXTRA_CA_CERTS: /etc/ssl/lab/ca.crt"):
        assert var in block, var
    play = (ROOT / "ansible" / "playbooks" / "platform-ha2-platform.yml").read_text()
    assert "tasks/vault-secret-id.yml" in play and "ITENTIAL_VAULT_SECRET_ID={{ vault_reader_secret_id }}" in play


def test_the_production_gateway_trusts_the_lab_ca_and_runs_the_proven_vault_tasks() -> None:
    compose = (ROOT / "itential" / "ha2" / "gateway.compose.yml.j2").read_text()
    assert "/usr/local/share/ca-certificates:/etc/ssl/lab:ro" in compose and "SSL_CERT_DIR: /etc/ssl/certs:/etc/ssl/lab" in compose
    play = (ROOT / "ansible" / "playbooks" / "platform-ha2-gateway.yml").read_text()
    assert "tasks/gateway-vault.yml" in play and "VAULT_TOKEN" in play


def test_a_replay_without_an_admin_token_keeps_what_the_readers_hold() -> None:
    """After the cut-over the root token is revoked: a routine replay must neither need a token nor churn a secret ID."""
    task = (ROOT / "ansible" / "playbooks" / "tasks" / "vault-secret-id.yml").read_text()
    assert "holds no Vault credentials yet: run with VAULT_TOKEN" in task
    assert task.count("vault_admin_token | default('') | length > 0") >= 3
