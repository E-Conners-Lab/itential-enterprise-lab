"""Phase 5 (PID S4/S4b) unit tests: itential/versions.yaml is the single oracle the Phase 5
plays read for image tags, the VM size and the workflow names. These tests hold it to the
human-verified records (docs/image-manifest.md 3.5, docs/resource-budget.md 2, topology/ipam.yaml,
docs/ip-plan.md), to the tofu module and to the vendored Compose file, so none of them can drift.
They run in CI with no lab access."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "itential" / "versions.yaml"
MANIFEST = ROOT / "docs" / "image-manifest.md"
BUDGET = ROOT / "docs" / "resource-budget.md"
IPAM = ROOT / "topology" / "ipam.yaml"
IP_PLAN = ROOT / "docs" / "ip-plan.md"
TOFU_VARS = ROOT / "tofu" / "itential" / "variables.tf"
COMPOSE = ROOT / "itential" / "docker-compose.yml"
OVERRIDE = ROOT / "itential" / "compose.override.yml"
WORKFLOWS = ROOT / "itential" / "workflows"
MCP_JSON = ROOT / ".mcp.json"
VERIFY = ROOT / "verify" / "test-05-itential.sh"
NETBOX_VMS = ROOT / "ansible" / "playbooks" / "netbox-vms.yml"
ENV_EXAMPLE = ROOT / ".env.example"

ECR = "497639811223.dkr.ecr.us-east-2.amazonaws.com"


@pytest.fixture(scope="module")
def versions() -> dict:
    assert VERSIONS.exists(), f"{VERSIONS} is missing"
    return yaml.safe_load(VERSIONS.read_text())


# --- image pins -----------------------------------------------------------------------------


def test_every_image_is_pinned(versions: dict) -> None:
    images = versions["images"]
    for role in ("platform", "gateway5", "gateway4", "mongodb", "redis", "mcp", "ldap"):
        assert role in images, f"images.{role} missing"
    for name, spec in images.items():
        assert spec["repository"] and spec["tag"], f"{name}: repository/tag missing"
        tag = str(spec["tag"])
        assert tag != "latest" and not tag.endswith("-latest"), f"{name} is not pinned: {tag!r}"
        assert re.search(r"\d", tag), f"{name}.tag has no digits: {tag!r}"


def test_itential_images_come_from_the_private_ecr(versions: dict) -> None:
    images = versions["images"]
    assert images["platform"]["repository"] == f"{ECR}/automation-platform-config-lcm-flowai"
    assert images["gateway5"]["repository"] == f"{ECR}/automation-gateway5"
    assert images["gateway4"]["repository"] == f"{ECR}/automation-gateway"
    assert images["mcp"]["repository"] == "ghcr.io/itential/itential-mcp"
    # manifest 3.5: never the leftovers from the laptop
    assert images["gateway5"]["tag"] != "5.1.0-amd64" and images["gateway4"]["tag"] != "4.3.7"
    # manifest 3.2: MongoDB 7.0 is the fully supported line for Platform 6; Redis 7.x
    assert str(images["mongodb"]["tag"]).startswith("7.0"), "Platform 6 wants MongoDB 7.0 (manifest 3.2)"
    assert str(images["redis"]["tag"]).startswith("7."), "Platform 6 wants Redis 7.x (manifest 3.2)"


def _manifest_35_pins() -> dict[str, str]:
    """repository -> pinned tag from the 'Pinned (Phase 5)' column of manifest table 3.5."""
    text = MANIFEST.read_text()
    start = text.index("### 3.5 Itential container images")
    end = text.index("### 3.4 ServiceNow")
    header: list[str] = []
    pins: dict[str, str] = {}
    for line in text[start:end].splitlines():
        if not line.startswith("| "):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells[0] == "Image":
            header = cells
            continue
        if not header or cells[0].startswith("---") or len(cells) != len(header):
            continue
        col = header.index("Pinned (Phase 5)")
        repo = cells[0].strip("`")
        m = re.search(r"`([^`]+)`", cells[col])
        if m:
            pins[repo] = m.group(1)
    return pins


def test_pins_match_manifest_35(versions: dict) -> None:
    pins = _manifest_35_pins()
    assert pins, "manifest 3.5 has no 'Pinned (Phase 5)' column with backticked tags"
    for role in ("platform", "gateway5", "gateway4", "mcp"):
        spec = versions["images"][role]
        assert spec["repository"] in pins, f"{spec['repository']} not in manifest 3.5"
        assert pins[spec["repository"]] == str(spec["tag"]), f"{role}: manifest {pins[spec['repository']]} vs versions.yaml {spec['tag']}"


def test_licence_state_recorded_in_manifest() -> None:
    """S4.5: licence state is recorded; the owner approved 'none needed' on 2026-09-07."""
    text = MANIFEST.read_text()
    sec = text[text.index("### 3.5 Itential container images"):text.index("### 3.4 ServiceNow")]
    assert "UNVERIFIED" not in sec, "manifest 3.5 still says the licence is unverified"
    assert re.search(r"[Ll]icen[cs]e.*2026-09-07", sec), "manifest 3.5 must record the licence decision and date"


# --- the vendored Compose file and its override ---------------------------------------------


def test_vendored_compose_matches_recorded_upstream(versions: dict) -> None:
    up = versions["upstream"]
    assert re.fullmatch(r"[0-9a-f]{40}", up["dev_stack_commit"]), "upstream commit must be a full sha"
    assert COMPOSE.exists(), f"{COMPOSE} missing (vendored from itential-dev-stack)"
    digest = hashlib.sha256(COMPOSE.read_bytes()).hexdigest()
    assert digest == up["compose_sha256"], "itential/docker-compose.yml changed; re-vendor or update versions.yaml"


def test_override_pins_images_ports_and_mongo_cache(versions: dict) -> None:
    ov = yaml.safe_load(OVERRIDE.read_text())
    svc = ov["services"]
    # Platform on 443 from the lab CA (S4.1); Gateway Manager stays on the Docker network
    ports = [str(p) for p in svc["platform"]["ports"]]
    assert any(p.endswith("443:3443") for p in ports), ports
    assert not any(p.endswith(":8080") and not p.startswith("127.") for p in ports), "Gateway Manager 8080 must not be exposed on the OOB address"
    # MongoDB cache capped so the 24 GB VM is not eaten by WiredTiger (S4.6)
    cmd = " ".join(svc["mongodb"]["command"]) if isinstance(svc["mongodb"]["command"], list) else svc["mongodb"]["command"]
    assert "--wiredTigerCacheSizeGB" in cmd
    cache_gb = float(re.search(r"--wiredTigerCacheSizeGB[= ]([\d.]+)", cmd).group(1))
    assert cache_gb <= versions["vm"]["memory_mb"] / 1024 * 0.25
    # Gateway 4 is deferred (owner decision 2026-09-07): staged image, not in the running profiles
    assert "gateway4" not in versions["stack"]["profiles"] and "full" not in versions["stack"]["profiles"]
    # MCP over streamable HTTP for Claude Code (S4.7)
    assert svc["mcp"]["environment"]["ITENTIAL_MCP_SERVER_TRANSPORT"] == "http"


# --- VM sizing: versions.yaml == budget == tofu == NetBox registration ------------------------


def _budget_vm_rows() -> dict[str, tuple[int, int, int]]:
    text = BUDGET.read_text()
    sec = text[text.index("## 2. Proxmox VMs"):text.index("## 3.")]
    rows = {}
    for line in sec.splitlines():
        m = re.match(r"\| (?:\d+ )?`([a-z0-9-]+)`[^|]*\| [^|]+ \| [^|]+ \| (\d+) \| (\d+) \| (\d+) \|", line)
        if m:
            rows[m.group(1)] = (int(m.group(2)), int(m.group(3)), int(m.group(4)))
    return rows


def test_the_dev_stack_vm_is_back_in_the_budget(versions: dict) -> None:
    """S11.8 deleted VM 205 on 2026-09-10 (ADR 0053); ADR 0063 brings it back as `itential-dev`, the Copilot
    sandbox, so it counts against the host again - under the new name only. A row called `itential` would
    mean the production service name had been handed back to the sandbox."""
    rows = _budget_vm_rows()
    vm = versions["vm"]
    assert vm["name"] == "itential-dev", "the dev stack never takes the production name `itential` (ADR 0063)"
    assert "itential-dev" in rows, "VM 205 returned by ADR 0063 must count against the host"
    assert rows["itential-dev"] == (vm["cores"], vm["memory_mb"] // 1024, vm["disk_gb"]) == (8, 24, 160), (
        f"budget {rows['itential-dev']} vs versions.yaml {vm}"
    )
    assert "itential" not in rows, "the name `itential` belongs to production's load balancer since S11"
    assert "~~205" not in BUDGET.read_text(), "the struck-through retirement row is replaced, not duplicated"
    assert "iag" not in rows, "budget still lists the separate iag VM (both gateways are containers on one VM)"


def _budget_ceilings() -> tuple[int, int, int]:
    """(vCPU, RAM GB, disk GB) from the Ceiling row of section 2, so the arithmetic follows the document."""
    m = re.search(r"^\| Ceiling \| \| \| (\d+) \| (\d+) \| ([\d,]+) \|", BUDGET.read_text(), re.M)
    assert m, "budget Ceiling row not found in section 2"
    return int(m.group(1)), int(m.group(2)), int(m.group(3).replace(",", ""))


def test_budget_total_and_headroom_are_arithmetically_right() -> None:
    rows = _budget_vm_rows()
    text = BUDGET.read_text()
    m = re.search(r"\| \*\*Total\*\* \| \| \| \*\*(\d+)\*\* \| \*\*(\d+)\*\* \| \*\*([\d,]+)\*\* \|", text)
    assert m, "budget total row not found"
    total = (int(m.group(1)), int(m.group(2)), int(m.group(3).replace(",", "")))
    assert total[0] == sum(v[0] for v in rows.values()), f"vCPU total {total[0]} != {sum(v[0] for v in rows.values())}"
    assert total[1] == sum(v[1] for v in rows.values()), f"RAM total {total[1]} != {sum(v[1] for v in rows.values())}"
    assert total[2] == sum(v[2] for v in rows.values()), f"disk total {total[2]} != {sum(v[2] for v in rows.values())}"
    cpu, ram, disk = _budget_ceilings()
    h = re.search(r"\| \*\*Headroom\*\* \| \| \| \*\*(-?\d+) vCPU\*\* \| \*\*(-?\d+) GB\*\* \|", text)
    assert h and int(h.group(1)) == cpu - total[0] and int(h.group(2)) == ram - total[1], "headroom = ceiling - total"
    assert total[1] <= ram and total[0] <= cpu and total[2] <= disk, f"plan {total} is over the ceilings {(cpu, ram, disk)}"


def test_budget_ceilings_are_the_owner_approved_ones() -> None:
    """ADR 0063: the owner raised RAM 280 -> 296 GB and thin disk allocation 1,400 -> 1,500 GB for the dev stack.
    Section 1 states the rule and section 2 does the arithmetic; they must name the same numbers, or a later
    edit to one quietly re-opens a decision the other still records."""
    cpu, ram, disk = _budget_ceilings()
    assert (cpu, ram, disk) == (108, 296, 1500), f"section 2 ceilings {(cpu, ram, disk)} are not the approved 108/296/1,500"
    text = BUDGET.read_text()
    assert "**108 vCPU allocated**" in text and "**296 GB allocated**" in text, "section 1 must state the same ceilings"
    assert "**thin allocation <= 1.5 TB;" in text, "section 1 must state the 1.5 TB thin allocation ceiling"


def test_tofu_module_matches_versions(versions: dict) -> None:
    assert TOFU_VARS.exists(), f"{TOFU_VARS} missing"
    tf = TOFU_VARS.read_text()
    vm = versions["vm"]
    for key in ("vm_id", "cores", "memory_mb", "disk_gb"):
        m = re.search(rf"{key}\s*=\s*(\d+)", tf)
        assert m and int(m.group(1)) == vm[key], f"tofu {key}: {m.group(1) if m else None} vs versions.yaml {vm[key]}"
    assert f'"{vm["ip"]}/24"' in tf
    # ADR 0063: the name is read from the variable, never a literal, so the VM cannot come back as `itential`
    m = re.search(r'\bname\s*=\s*"([a-z0-9-]+)"', tf)
    assert m and m.group(1) == vm["name"], f"tofu variables name {m.group(1) if m else None} vs versions.yaml {vm['name']}"
    main = (TOFU_VARS.parent / "itential.tf").read_text()
    assert re.search(r"^\s*name\s*=\s*var\.vm\.name\s*$", main, re.M), "tofu/itential/itential.tf must set name = var.vm.name"


def test_netbox_registers_the_dev_stack(versions: dict) -> None:
    """ADR 0063 brings VM 205 back as `itential-dev`, so netbox-vms.yml lists it again - it is the source of
    the `itential-host` group the dev plays target. It must never list `itential` or `iag`, and the play keeps
    pruning what it stops listing (the S11.8 contract, ADR 0053)."""
    text = NETBOX_VMS.read_text()
    play = yaml.safe_load(text)
    vms = play[0]["vars"]["vms"]
    vm = versions["vm"]
    dev = [v for v in vms if v["name"] == "itential-dev"]
    assert len(dev) == 1, f"netbox-vms.yml must list itential-dev exactly once, found {len(dev)}"
    dev = dev[0]
    assert dev["role"] == "itential-host", "the dev plays reach the VM through the itential-host group"
    assert dev["ips"] == [vm["ip"]] == ["10.100.0.65"]
    assert (dev["vcpus"], dev["memory"], dev["disk"]) == (vm["cores"], vm["memory_mb"], vm["disk_gb"]), dev
    assert not any(v["name"] in ("itential", "iag") for v in vms), "the production name and the dropped iag VM stay out"
    assert "Retired virtual machines deleted" in text, "the play must prune what it no longer lists"


# --- addresses: .65 is itential-dev with the mcp-dev alias; itential/mcp stay on production ---------


def test_ipam_itential_alias_and_released_iag(versions: dict) -> None:
    """The S11 cut-over (ADR 0053/0055) moved itential.lab.internal to the load balancer and mcp.lab.internal to
    tools-01. ADR 0063 returns .65 to the dev stack with new names only: if `itential` or `mcp` were ever
    attached to it, unbound would publish two A records and Claude Code or a verify could land on the sandbox."""
    ipam = yaml.safe_load(IPAM.read_text())
    rows = ipam["addresses"]
    by_name = {r["hostname"]: r for r in rows}
    dev = versions["dev"]
    assert "iag" not in by_name, "10.100.0.66 iag must be released (option 1, approved 2026-09-07)"
    assert not any("10.100.0.66" == r["address"] for r in rows)
    dev_row = by_name.get("itential-dev")
    assert dev_row, "10.100.0.65 itential-dev missing from topology/ipam.yaml (ADR 0063)"
    assert dev_row["address"] == versions["vm"]["ip"] == "10.100.0.65"
    assert dev_row["hostname"] == dev["hostname"] == versions["vm"]["name"]
    assert dev_row.get("aliases") == [dev["mcp_alias"]] == ["mcp-dev"], dev_row.get("aliases")
    assert "itential" not in by_name, "`itential` is an alias of the load balancer, never a hostname"
    carriers = {r["hostname"] for r in rows if "itential" in r.get("aliases", [])}
    assert carriers == {"iap-lb"}, f"only iap-lb may carry the itential alias, found {carriers}"
    carriers = {r["hostname"] for r in rows if "mcp" in r.get("aliases", [])}
    assert carriers == {"tools-01"}, f"only tools-01 may carry the mcp alias, found {carriers}"


def test_dev_block_names_its_own_secrets(versions: dict) -> None:
    """ADR 0063: every dev secret is a new .env key, so no dev run can overwrite a production one - above all
    ITENTIAL_ENCRYPTION_KEY, whose loss loses every stored adapter secret on production."""
    dev = versions["dev"]
    production_keys = {"ITENTIAL_ENCRYPTION_KEY", "NETBOX_TOKEN", "ITENTIAL_ADMIN_PASSWORD"}
    named = {dev["encryption_key_env"], dev["netbox_token_env"], dev["copilot"]["password_env"]}
    assert named == {"ITENTIAL_DEV_ENCRYPTION_KEY", "NETBOX_DEV_RO_TOKEN", "SVC_COPILOT_DEV_PASSWORD"}, named
    assert not named & production_keys, f"the dev block reuses a production key: {named & production_keys}"
    assert dev["copilot"]["user"] == "svc-copilot" and dev["copilot"]["group"] == "copilot-builders"


def test_vendored_ldif_is_pinned(versions: dict) -> None:
    """The upstream LDIF is vendored unchanged; svc-copilot is added with ldapadd by a task file, never by editing
    it (ADR 0063). The pin was recorded but nothing checked it until now."""
    ldif = ROOT / "itential" / "ldap" / "openldap.ldif"
    assert ldif.exists(), f"{ldif} missing"
    digest = hashlib.sha256(ldif.read_bytes()).hexdigest()
    assert digest == versions["stack"]["ldif_sha256"], "itential/ldap/openldap.ldif changed; the vendored LDIF is never edited"


def test_ip_plan_markdown_agrees() -> None:
    text = IP_PLAN.read_text()
    # ADR 0063: .65 is the dev stack again, under new names; the service name and the mcp alias stay on production
    assert re.search(r"\| 10\.100\.0\.65 \| itential-dev \|.*mcp-dev", text), "ip-plan .65 row is itential-dev with mcp-dev"
    assert re.search(r"\| 10\.100\.0\.71 \| iap-lb \|.*itential\.lab\.internal", text), "the .71 row carries the service name"
    assert re.search(r"\| 10\.100\.0\.81 \| tools-01 \|.*mcp", text), "the .81 row carries the mcp alias"
    assert re.search(r"\| 10\.100\.0\.66 \| \*\(reserved\)\*", text), "ip-plan .66 row must be reserved"


# --- workflows, MCP client config, verify script, env template --------------------------------


def test_workflows_exported_and_shaped(versions: dict) -> None:
    names = versions["workflows"]
    assert "wf-branch-vlan-v1" in names.values()
    for key, name in names.items():
        path = WORKFLOWS / f"{name}.json"
        assert path.exists(), f"{path} missing (export from the platform after building)"
        wf = json.loads(path.read_text())
        assert wf["name"] == name, f"{path}: name {wf.get('name')!r}"
        assert wf.get("tasks"), f"{path}: no tasks"
    vlan = json.loads((WORKFLOWS / "wf-branch-vlan-v1.json").read_text())
    tasks = list(vlan["tasks"].values())
    assert any(t.get("type") == "manual" for t in tasks), "S4.4 needs a manual approval task"
    blob = json.dumps(vlan).lower()
    assert "netbox" in blob, "S4.4 reserves the VLAN in NetBox"


def test_mcp_client_config_points_at_lab_mcp() -> None:
    cfg = json.loads(MCP_JSON.read_text())
    srv = cfg["mcpServers"]["itential"]
    assert srv["type"] == "http" and srv["url"] == "http://mcp.lab.internal:8000/mcp"


def test_mcp_server_hides_the_tools_that_return_node_credentials() -> None:
    """ADR 0039 amendment: describe_inventory and get_devices return itential_password to any MCP
    client; the server excludes them by tag until the credentials become references (Phase 10)."""
    ov = yaml.safe_load(OVERRIDE.read_text())
    tags = str(ov["services"]["mcp"]["environment"]["ITENTIAL_MCP_SERVER_EXCLUDE_TAGS"]).split(",")
    assert {"describe_inventory", "get_devices"} <= set(tags), tags
    assert {"experimental", "beta"} <= set(tags), "keep the upstream defaults when overriding the list"
    assert "describe_inventory" in VERIFY.read_text(), "verify 05 S4.7 must prove the tool is hidden"


def test_verify_script_covers_every_criterion() -> None:
    assert VERIFY.exists() and VERIFY.stat().st_mode & 0o111, "verify/test-05-itential.sh missing or not executable"
    text = VERIFY.read_text()
    for c in [f"S4.{i}" for i in range(1, 8)] + [f"S4b.{i}" for i in range(1, 6)]:
        assert re.search(rf'check "{re.escape(c)} ', text) or re.search(rf"# --- {re.escape(c)}", text), f"{c} not covered"
    assert "HIBERNATED" in text, "S4b must fail loud, not pass, when the PDI is asleep"


def test_env_example_lists_phase5_secrets() -> None:
    text = ENV_EXAMPLE.read_text()
    for key in ("ECR_AWS_PROFILE", "ITENTIAL_ENCRYPTION_KEY", "ITENTIAL_ADMIN_USER", "ITENTIAL_ADMIN_PASSWORD", "SNOW_INSTANCE", "SNOW_USER", "SNOW_PASSWORD"):
        assert re.search(rf"^{key}=", text, re.M), f"{key} missing from .env.example"


def test_makefile_wires_the_phase() -> None:
    mk = (ROOT / "Makefile").read_text()
    assert re.search(r"^phase-itential:.*##", mk, re.M), "Makefile phase-itential target still the stub"
    assert "test: " in mk


def test_play_registers_inventory_as_configuration_manager_provider(versions: dict) -> None:
    """ADR 0039: Configuration Manager, Golden Config, compliance and MCP run_command consume
    devices through Device Broker; the built-in Inventory Manager adapter is the provider on a
    Gateway 5-only stack (no Gateway 4)."""
    # the asset half of the play is the shared task file both environments include (ADR 0055)
    text = (ROOT / "ansible" / "playbooks" / "tasks" / "platform-assets.yml").read_text()
    assert "InventoryBroker" in text and 'type: InventoryManager' in text
    assert f'inventories: ["{{{{ stack.inventory }}}}"]' in text
    assert "prepend_inventory_name: false" in text
    assert "configuration_manager/devices" in text, "the play must prove the devices reach Configuration Manager"
