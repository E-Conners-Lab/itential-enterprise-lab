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
    # Gateway 4 needs netmiko for show commands through IAG (S4.3)
    env = svc["gateway4"]["environment"]
    assert str(env["automation_gateway_netmiko_enabled"]).lower() == "true"
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


def test_one_itential_vm_in_budget_sized_like_versions(versions: dict) -> None:
    rows = _budget_vm_rows()
    assert "iag" not in rows, "budget still lists the separate iag VM (both gateways are containers on itential)"
    vm = versions["vm"]
    assert rows["itential"] == (vm["cores"], vm["memory_mb"] // 1024, vm["disk_gb"]), f"budget {rows['itential']} vs versions.yaml"
    assert vm["cores"] == 8 and vm["memory_mb"] == 24576 and vm["disk_gb"] == 160, "sizing approved 2026-09-07: 8 vCPU / 24 GB / 160 GB"


def test_budget_total_and_headroom_are_arithmetically_right() -> None:
    rows = _budget_vm_rows()
    text = BUDGET.read_text()
    m = re.search(r"\| \*\*Total\*\* \| \| \| \*\*(\d+)\*\* \| \*\*(\d+)\*\* \| \*\*([\d,]+)\*\* \|", text)
    assert m, "budget total row not found"
    total = (int(m.group(1)), int(m.group(2)), int(m.group(3).replace(",", "")))
    assert total[0] == sum(v[0] for v in rows.values()), f"vCPU total {total[0]} != {sum(v[0] for v in rows.values())}"
    assert total[1] == sum(v[1] for v in rows.values()), f"RAM total {total[1]} != {sum(v[1] for v in rows.values())}"
    assert total[2] == sum(v[2] for v in rows.values()), f"disk total {total[2]} != {sum(v[2] for v in rows.values())}"
    h = re.search(r"\| \*\*Headroom\*\* \| \| \| \*\*(\d+) vCPU\*\* \| \*\*(\d+) GB\*\* \|", text)
    assert h and int(h.group(1)) == 108 - total[0] and int(h.group(2)) == 280 - total[1]


def test_tofu_module_matches_versions(versions: dict) -> None:
    assert TOFU_VARS.exists(), f"{TOFU_VARS} missing"
    tf = TOFU_VARS.read_text()
    vm = versions["vm"]
    for key in ("vm_id", "cores", "memory_mb", "disk_gb"):
        m = re.search(rf"{key}\s*=\s*(\d+)", tf)
        assert m and int(m.group(1)) == vm[key], f"tofu {key}: {m.group(1) if m else None} vs versions.yaml {vm[key]}"
    assert f'"{vm["ip"]}/24"' in tf


def test_netbox_registration_matches_versions(versions: dict) -> None:
    play = yaml.safe_load(NETBOX_VMS.read_text())
    vms = play[0]["vars"]["vms"]
    row = next((v for v in vms if v["name"] == "itential"), None)
    assert row, "netbox-vms.yml does not register the itential VM"
    vm = versions["vm"]
    assert (row["vcpus"], row["memory"], row["disk"]) == (vm["cores"], vm["memory_mb"], vm["disk_gb"])
    assert row["ips"] == [vm["ip"]]
    assert not any(v["name"] == "iag" for v in vms)


# --- addresses: .65 is itential with the mcp alias, .66 is released -----------------------------


def test_ipam_itential_alias_and_released_iag(versions: dict) -> None:
    ipam = yaml.safe_load(IPAM.read_text())
    by_name = {r["hostname"]: r for r in ipam["addresses"]}
    assert "iag" not in by_name, "10.100.0.66 iag must be released (option 1, approved 2026-09-07)"
    it = by_name["itential"]
    assert it["address"] == versions["vm"]["ip"] == "10.100.0.65"
    assert "mcp" in it.get("aliases", []), "mcp.lab.internal must be an alias of itential (S4.7)"
    assert not any("10.100.0.66" == r["address"] for r in ipam["addresses"])


def test_ip_plan_markdown_agrees() -> None:
    text = IP_PLAN.read_text()
    assert re.search(r"\| 10\.100\.0\.65 \| itential \|.*mcp", text), "ip-plan .65 row must mention the mcp alias"
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
