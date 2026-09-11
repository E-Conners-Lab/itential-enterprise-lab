"""Phase 8 (PID S11, ADR 0053): the production Itential environment in Itential's HA2 shape.

`itential/ha2/versions.yaml` is the single oracle for the topology; these tests hold it to `topology/ipam.yaml`,
`docs/ip-plan.md` and `docs/resource-budget.md`, prove the images come from the Phase 5 pins rather than a second
list, and prove the build artifacts (tofu module, plays, compose templates, verify) exist and read the oracle.
They run in CI with no lab access.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
HA2 = yaml.safe_load((ROOT / "itential" / "ha2" / "versions.yaml").read_text())
ITENTIAL = yaml.safe_load((ROOT / "itential" / "versions.yaml").read_text())
IPAM = yaml.safe_load((ROOT / "topology" / "ipam.yaml").read_text())
BUDGET = (ROOT / "docs" / "resource-budget.md").read_text()
IP_PLAN = (ROOT / "docs" / "ip-plan.md").read_text()
TOFU = ROOT / "tofu" / "platform-ha2"
PLAYS = ROOT / "ansible" / "playbooks"
HA2_DIR = ROOT / "itential" / "ha2"


def vms() -> dict[str, dict]:
    return {v["name"]: v for v in HA2["vms"]}


# --- the oracle itself ----------------------------------------------------------------------------------


def test_shape_is_itentials_ha2() -> None:
    """Two Platform nodes, a three-member MongoDB replica set, three Redis with Sentinel, Gateway on its own
    server: the counts of the guide's HA2 architecture."""
    roles: dict[str, int] = {}
    for v in HA2["vms"]:
        roles[v["role"]] = roles.get(v["role"], 0) + 1
    assert roles["platform"] == 2, "HA2 has two Platform servers"
    assert roles["mongodb"] == 3, "HA2 has a three-member MongoDB replica set"
    assert roles["redis"] == 3, "HA2 has three Redis servers with Sentinel"
    assert roles["gateway"] == 1 and roles["loadbalancer"] == 1
    assert len(HA2["vms"]) == 11, "nine Itential VMs plus the load balancer and the tools VM"
    # every component on its own server (the guide's production rule): no two roles share a VM
    assert len({v["name"] for v in HA2["vms"]}) == len(HA2["vms"])
    assert len({v["vm_id"] for v in HA2["vms"]}) == len(HA2["vms"])
    assert len({v["ip"] for v in HA2["vms"]}) == len(HA2["vms"])


def test_mongodb_and_redis_settings_follow_the_guide() -> None:
    m = HA2["mongodb"]
    assert m["replica_set"] and m["port"] == 27017 and m["tls"] is True
    assert {"admin", "itential", "monitor"} <= set(m["users"]), "the Deployer's MongoDB accounts"
    for u in m["users"].values():
        assert u["env"].startswith("MONGO_") and u["env"].endswith("_PASSWORD"), u
    assert m["keyfile_env"] == "MONGO_KEYFILE", "intra-cluster authentication needs a keyfile"
    priorities = sorted(v.get("priority", 0) for v in HA2["vms"] if v["role"] == "mongodb")
    assert priorities == [5, 5, 10], "one member carries the higher election priority"
    r = HA2["redis"]
    assert r["port"] == 6379 and r["sentinel_port"] == 26379
    assert r["quorum"] == 2, "quorum of 2 in a three-Sentinel cluster"
    assert {"itential", "repluser", "sentineluser", "monitor"} <= set(r["users"]), "the Deployer's Redis ACL users"
    assert sum(1 for v in HA2["vms"] if v.get("master")) == 1, "exactly one initial Redis master"


def test_load_balancer_forwards_to_the_platform_http_port() -> None:
    lb, p = HA2["loadbalancer"], HA2["platform"]
    assert lb["listen_port"] == 443 and lb["backend_port"] == p["http_port"] == 3000
    assert lb["sticky"] is True, "the Deployer's nginx guide uses sticky sessions"
    assert p["nodes"] == ["iap-01", "iap-02"]


def test_images_come_from_the_phase_5_pins_not_a_second_list() -> None:
    text = (HA2_DIR / "versions.yaml").read_text()
    assert "images:" not in text, "images live in itential/versions.yaml; this file owns the topology"
    for key in ("platform", "mongodb", "redis", "gateway5", "etcd", "mcp", "ollama"):
        assert key in ITENTIAL["images"], key
    # the versions the guide's system-requirements page allows for Platform 6
    assert ITENTIAL["images"]["mongodb"]["tag"].startswith(("6.0", "7.0", "8.0")), "MongoDB 6.0/7.0/8.0"
    assert ITENTIAL["images"]["redis"]["tag"].startswith(("7.0", "7.2", "7.4")), "Redis 7.0-7.4"


# --- the oracle against the IP plan and the budget ------------------------------------------------------


def test_every_vm_is_in_the_ip_plan() -> None:
    by_name = {a["hostname"]: a for a in IPAM["addresses"]}
    for name, v in vms().items():
        assert name in by_name, f"{name} missing from topology/ipam.yaml"
        assert by_name[name]["address"] == v["ip"], name
        assert by_name[name]["placement"] == "proxmox-vm" and by_name[name]["phase"] == 8, name
        assert f"| {v['ip']} | {name} |" in IP_PLAN, f"{name} missing from docs/ip-plan.md 3.2"
    assert "mcp" in by_name["tools-01"].get("aliases", []), "the MCP alias follows the tools VM"


def test_every_vm_is_in_the_resource_budget() -> None:
    for name, v in vms().items():
        m = re.search(rf"\| `{re.escape(name)}` \| 8 \| [^|]+ \| (\d+) \| (\d+) \| (\d+) \|", BUDGET)
        assert m, f"{name} missing from docs/resource-budget.md section 2"
        assert (int(m.group(1)), int(m.group(2)), int(m.group(3))) == (v["cores"], v["memory_mb"] // 1024, v["disk_gb"]), name


def test_the_environment_fits_once_the_dev_stack_retires() -> None:
    """The nine production VMs plus what runs today must be under the 280 GB ceiling once VM 205 is gone."""
    new_ram = sum(v["memory_mb"] for v in HA2["vms"]) // 1024
    assert new_ram <= 60, f"the HA2 environment is {new_ram} GB; the design budgeted about 51"
    dev = ITENTIAL["vm"]["memory_mb"] // 1024
    assert new_ram - dev <= 40, "retiring VM 205 must offset most of the new RAM"


# --- the build artifacts read the oracle -----------------------------------------------------------------


def test_tofu_module_exists_and_is_driven_by_the_oracle() -> None:
    assert (TOFU / "versions.tf").exists() and (TOFU / "platform-ha2.tf").exists(), "tofu/platform-ha2 module"
    tf = (TOFU / "platform-ha2.tf").read_text()
    assert "ha2/versions.yaml" in tf, "the module reads itential/ha2/versions.yaml, it does not repeat the VMs"
    assert "for_each" in tf, "one resource per VM from the oracle"


@pytest.mark.parametrize(
    "play",
    [
        "platform-ha2-hosts.yml",
        "platform-ha2-mongodb.yml",
        "platform-ha2-redis.yml",
        "platform-ha2-platform.yml",
        "platform-ha2-tools.yml",
        "platform-ha2-identity.yml",
        "platform-ha2-gateway.yml",
    ],
)
def test_plays_exist_and_read_the_oracle(play: str) -> None:
    p = PLAYS / play
    assert p.exists(), f"{play} missing"
    text = p.read_text()
    assert "ha2/versions.yaml" in text, f"{play} must read the oracle"
    # every play targets the NetBox-derived groups of the production environment, not a hand-written host list
    assert re.search(r"hosts: (ha2-|itential-ha2)", text), f"{play} must address the ha2 inventory groups"
    for v in vms():
        assert f"ansible_host: {v}" not in text, f"{play} hard-codes a host"


def test_compose_documents_exist_for_every_role() -> None:
    for name in ("mongodb", "redis", "platform", "loadbalancer", "gateway", "tools"):
        assert (HA2_DIR / f"{name}.compose.yml.j2").exists(), f"itential/ha2/{name}.compose.yml.j2 missing"


def test_verify_and_make_are_wired() -> None:
    v = ROOT / "verify" / "test-08-platform-ha2.sh"
    assert v.exists(), "verify/test-08-platform-ha2.sh missing"
    text = v.read_text()
    for crit in ("S11.1", "S11.2", "S11.3", "S11.4", "S11.5", "S11.6", "S11.7"):
        assert crit in text, crit
    assert "VERIFY_DRILLS" in text, "the HA drills are gated (PIS-09)"
    mk = (ROOT / "Makefile").read_text()
    assert "phase-platform-ha2:" in mk and "platform-ha2" in mk


def test_secrets_are_declared_in_env_example() -> None:
    env = (ROOT / ".env.example").read_text()
    wanted = {u["env"] for u in HA2["mongodb"]["users"].values()} | {u["env"] for u in HA2["redis"]["users"].values()}
    wanted.add(HA2["mongodb"]["keyfile_env"])
    for key in sorted(wanted):
        assert f"{key}=" in env, f"{key} missing from .env.example"


# --- the replay of phases 5-7 onto production (ADR 0055, PID amendment 1.20) -----------------------------

TASKS = PLAYS / "tasks"
PROD_VARS = PLAYS / "vars" / "itential-prod.yml"
ASSET_PLAYS = ("itential.yml", "platform.yml", "flowai.yml")


def test_the_asset_halves_live_in_shared_task_files() -> None:
    """One definition per asset: the dev-stack play and the replay include the same file."""
    for name in ("platform-assets.yml", "flowai-assets.yml", "roles-to-admin.yml", "roles-to-admin-ldap.yml"):
        assert (TASKS / name).exists(), f"ansible/playbooks/tasks/{name} missing"
    assets = (TASKS / "platform-assets.yml").read_text()
    for marker in ("InventoryBroker", "inventory_manager/v1/nodes/bulk", "automation-studio/automations/import", "vlan-groups"):
        assert marker in assets, f"platform-assets.yml must own {marker}"
    assert "docker" not in assets, "the dev-stack's database re-sync belongs in roles-to-admin-ldap.yml"
    flowai = (TASKS / "flowai-assets.yml").read_text()
    for marker in ("model-registry-service/profiles", "agent-project-service/projects"):
        assert marker in flowai, f"flowai-assets.yml must own {marker}"


@pytest.mark.parametrize("play", ASSET_PLAYS)
def test_the_asset_plays_take_their_target_from_a_variable(play: str) -> None:
    """The default stays the dev-stack; an extra-vars overlay selects production (ADR 0055 decision 2)."""
    text = (PLAYS / play).read_text()
    assert "hosts: \"{{ platform_target | default('itential-host') }}\"" in text, f"{play} must take platform_target"
    assert "hosts: itential-host" not in text, f"{play} still hard-codes the dev-stack group"


@pytest.mark.parametrize("play", ASSET_PLAYS)
def test_the_asset_plays_include_the_shared_task_files(play: str) -> None:
    text = (PLAYS / play).read_text()
    if play == "itential.yml":
        assert "tasks/platform-assets.yml" in text
    if play == "flowai.yml":
        assert "tasks/flowai-assets.yml" in text
    if play == "platform.yml":  # entirely assets: the target variable is all it needs
        assert "golden_config" in text or "configuration_manager" in text


def test_the_production_overlay_is_held_to_the_oracle() -> None:
    """`vars/itential-prod.yml` repeats the oracle's addresses and account; the test keeps them equal."""
    assert PROD_VARS.exists(), "ansible/playbooks/vars/itential-prod.yml missing"
    prod = yaml.safe_load(PROD_VARS.read_text())
    assert prod["platform_target"] == "ha2-platform[0]", "the replay runs on the first Platform node"
    first = HA2["platform"]["nodes"][0]
    assert prod["platform_api"] == f"http://{vms()[first]['ip']}:{HA2['platform']['http_port']}", "the first node's API"
    assert prod["platform_admin_user"] == HA2["platform"]["admin_user"], "the local administrator of the oracle"
    assert HA2["platform"]["bootstrap_user"] != HA2["platform"]["admin_user"], "the bootstrap user is not the automation account"
    assert "memberOf:" not in (TASKS / "roles-to-admin.yml").read_text(), "the default user cannot hold a membership"
    # the directory account is provisioned by its first login and its roles are written to the replica set
    assert prod["role_resync_tasks"] == "roles-to-admin-ldap.yml", "production re-syncs through the database, as dev does"
    assert prod["mongo_shell_host"] == [v["name"] for v in HA2["vms"] if v["role"] == "mongodb"][0]
    uri = prod["mongo_admin_uri"]
    assert f"replicaSet={HA2['mongodb']['replica_set']}" in uri and "tls=true" in uri, "the write must reach the primary over TLS"
    assert " " not in prod["mongo_hosts"], "a folded scalar would put spaces inside the URI"
    for member in (v["name"] for v in HA2["vms"] if v["role"] == "mongodb"):
        assert f"{member}.{HA2['domain']}:{HA2['mongodb']['port']}" in prod["mongo_hosts"], f"{member} missing"
    assert prod["mongo_shell_argv"][-1] == "--eval" and "{{ mongo_admin_uri }}" in prod["mongo_shell_argv"]
    tools = vms()["tools-01"]
    assert prod["ollama_base_url"] == f"http://{tools['ip']}:{HA2['tools']['ollama_port']}", "Ollama runs on tools-01"


def test_the_replay_entry_point_runs_the_three_halves_in_order() -> None:
    """ADR 0038's order: the workflows, then the Platform applications, then the agents that reference them."""
    replay = PLAYS / "platform-ha2-replay.yml"
    assert replay.exists(), "ansible/playbooks/platform-ha2-replay.yml missing"
    text = replay.read_text()
    order = [text.index(m) for m in ("tasks/platform-assets.yml", "import_playbook: platform.yml", "tasks/flowai-assets.yml")]
    assert order == sorted(order), "platform assets, then platform.yml, then the FlowAI assets"
    assert "ha2/versions.yaml" in text, "the replay reads the oracle"
    mk = (ROOT / "Makefile").read_text()
    assert "replay-platform-ha2:" in mk and "itential-prod.yml" in mk, "make replay-platform-ha2 passes the overlay"


def test_the_dev_stack_play_keeps_building_the_dev_stack() -> None:
    """The extraction must not move the dev-stack out of itential.yml (ADR 0055 decision 5)."""
    text = (PLAYS / "itential.yml").read_text()
    for marker in ("compose.override.yml", "gateway5", "ldif", "docker-compose.yml"):
        assert marker in text, f"itential.yml must keep building the dev-stack ({marker})"


def test_the_phase_target_builds_the_directory_before_the_administrator_and_the_gateway() -> None:
    """OpenLDAP lives on tools-01, the directory account needs it, and Gateway Manager filters what it
    returns by that account's group membership (ADR 0055 decision 7)."""
    mk = (ROOT / "Makefile").read_text()
    order = [mk.index(f"playbooks/platform-ha2-{p}.yml") for p in ("platform", "tools", "identity", "gateway")]
    assert order == sorted(order), "platform, then tools (the directory), then identity, then gateway"
    tools = (PLAYS / "platform-ha2-tools.yml").read_text()
    assert "openldap.ldif" in tools and "ha2-tools" in tools, "the directory bootstrap ships with the tools VM"
    ident = (PLAYS / "platform-ha2-identity.yml").read_text()
    assert "tasks/ldap-admin.yml" in ident and "tasks/roles-to-admin.yml" in ident


def test_the_load_balancer_keeps_traffic_and_the_gateway_on_one_node() -> None:
    """A Gateway 5.5.2 holds one Platform connection, and only that node can reach a device, so both
    upstreams name the first Platform node as the only primary (ADR 0055 decision 8)."""
    conf = (HA2_DIR / "nginx.conf.j2").read_text()
    assert "ip_hash;" not in conf, "ip_hash cannot be combined with backup, and it sent clients to the wrong node"
    assert conf.count("' backup' if not loop.first else ''") == 2, "both upstreams are primary + backup"
    play = (PLAYS / "platform-ha2-platform.yml").read_text()
    assert "--force-recreate nginx" in play, "a single-file bind mount survives a reload; the container is recreated"
    compose = (HA2_DIR / "platform.compose.yml.j2").read_text()
    assert "curl" not in compose, "the Platform image carries wget, not curl"
    assert "{{ platform.gateway_manager_port }}:{{ platform.gateway_manager_port }}" in compose, "8080 is published"


def test_only_the_first_platform_node_runs_the_workers() -> None:
    """Gateway Manager holds one connection per gateway cluster, so only one node can reach a device; the
    others must not pick up job or task work (ADR 0055 decision 9)."""
    compose = (HA2_DIR / "platform.compose.yml.j2").read_text()
    for var in ("ITENTIAL_JOB_WORKER_ENABLED", "ITENTIAL_TASK_WORKER_ENABLED"):
        assert f"{var}: \"{{{{ 'true' if inventory_hostname == platform.nodes[0] else 'false' }}}}\"" in compose, var
    assert HA2["platform"]["nodes"][0] == "iap-01", "the worker node is the first node of the oracle"
    # and the standby is parked by the play, not by hand (ADR 0055 decision 9)
    play = (PLAYS / "platform-ha2-platform.yml").read_text()
    assert "Standby nodes parked until a failover" in play, "the play parks every node but the first"
    assert "when: inventory_hostname != platform.nodes[0]" in play, "parked by the oracle's node order"
    verify = (ROOT / "verify" / "test-08-platform-ha2.sh").read_text()
    assert "the standby is built and parked" in verify, "S11.4 asserts the Active/Standby shape"


# --- S11.9 (ADR 0058, PID 1.24): the production MongoDB is backed up, and the verify restores it ------------
MONGO_PLAY = PLAYS / "platform-ha2-mongodb.yml"
VERIFY_08 = ROOT / "verify" / "test-08-platform-ha2.sh"


def test_the_oracle_names_a_least_privilege_backup_account() -> None:
    """ADR 0058 decision 2: not `admin`. A credential that reads everything nightly should not also be
    able to write everything."""
    users = HA2["mongodb"]["users"]
    assert "backup" in users, "itential/ha2/versions.yaml must name the backup account (ADR 0058)"
    b = users["backup"]
    assert b["role"] == "backup", f"the built-in `backup` role, not {b['role']!r}"
    assert b["env"].startswith("MONGO_") and b["env"].endswith("_PASSWORD")
    assert b["env"] != users["admin"]["env"], "the backup account has its own generated password"


def test_the_play_dumps_on_a_secondary_chosen_by_the_oracle() -> None:
    """ADR 0058 decision 1: on the last member, connected directly so the driver cannot redirect the dump
    to the primary, and never with the member hard-coded."""
    play = MONGO_PLAY.read_text()
    assert "mongodump" in play, "platform-ha2-mongodb.yml takes no backup (ADR 0058)"
    assert re.search(r"selectattr\('role', 'equalto', 'mongodb'\).*last", play), \
        "the member comes from the oracle's last entry, not from a hard-coded hostname"
    assert "mongodb.backup.dir" in play, "the directory comes from the oracle (ADR 0058 decision 3)"
    assert "no_log: true" in play, "the backup credential is never logged"

    # The dump itself lives in the template the play renders.
    script = (PLAYS / "templates" / "mongo-backup.sh.j2").read_text()
    assert "mongodump" in script and "--gzip" in script and "--archive" in script
    # A direct connection, which is what `--host <host>:<port>` is: a replica-set URI would let the driver
    # discover the set and send the dump to the PRIMARY, defeating the point of running it on a secondary.
    assert re.search(r'--host\s+"\{\{ backup_member \}\}', script), \
        "dump this member directly by its own FQDN (the TLS cert is issued for it), not through a set URI"
    assert "replicaSet=" not in script, "a replica-set URI would be redirected to the primary"
    assert "--readPreference=secondary" in script
    assert "users.backup.name" in script and "users.backup.env" in script, "the least-privilege account"
    assert "mtime" in script and "keep_days" in script, "seven-day rotation, from the oracle"
    assert ".part" in script, "a half-written archive must never be the newest one S11.9 restores"


def test_s11_9_restores_the_archive_rather_than_asserting_a_filename() -> None:
    """ADR 0058 decision 4, and the whole point of the criterion. A dump that has never been restored is a
    claim about a filename."""
    v = VERIFY_08.read_text()
    assert "S11.9" in v, "verify/test-08-platform-ha2.sh does not check S11.9"
    body = v[v.index("S11.9"):]
    assert "mongorestore" in v, "S11.9 must restore the archive, not stat it"
    # ADR 0058 decision 4: into a throw-away mongod, never into the replica set. The first version restored
    # into a database on production and took all three members down.
    assert "docker run -d --rm" in v and "wiredTigerCacheSizeGB" in v, \
        "the restore must run in a standalone container, not against the live replica set"
    assert "docker rm -f" in v, "the throw-away instance is removed again"
    assert "rs.status" not in body.split("check ")[0] or "--host '${rs_uri}'" not in v, \
        "S11.9 must not write to the replica set"
    pid = (ROOT / "docs" / "PID.md").read_text()
    assert "| 1.24 |" in pid and "and it restores" in pid
    assert (ROOT / "docs" / "adr" / "0058-production-mongodb-backup.md").exists()
