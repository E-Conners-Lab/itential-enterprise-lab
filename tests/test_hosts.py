"""PID S4d element 6 (ADR 0047): the three Ubuntu hosts as Gateway 5 inventory nodes in their own inventory (never
published to Configuration Manager), reached as the automation user with a password enabled per host by
ansible/playbooks/lab-endpoints.yml. These tests hold the oracle, the plays, the verify script and the PID to each other."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "itential" / "versions.yaml"
# the host inventory is created in the task file itential.yml and the production replay share (ADR 0055)
ITENTIAL_PLAY = ROOT / "ansible" / "playbooks" / "tasks" / "platform-assets.yml"
ENDPOINTS_PLAY = ROOT / "ansible" / "playbooks" / "lab-endpoints.yml"
VERIFY = ROOT / "verify" / "test-06b-platform.sh"
PID = ROOT / "docs" / "PID.md"
ADR = ROOT / "docs" / "adr" / "0047-ubuntu-hosts-gateway5-inventory.md"


@pytest.fixture(scope="module")
def versions() -> dict:
    return yaml.safe_load(VERSIONS.read_text())


def test_oracle_keeps_hosts_in_their_own_inventory(versions: dict) -> None:
    assert versions["stack"]["host_inventory"] == "lab-hosts" and versions["stack"]["inventory"] == "lab"
    hosts = versions["hosts"]
    assert hosts["netbox_platforms"] == {"ubuntu-24-04": "linux"}, "netmiko's linux driver for the Ubuntu hosts"
    assert set(hosts["roles"]) == {"server", "client"}
    assert hosts["probe"] and all(c.startswith(("uptime", "hostname")) for c in hosts["probe"]), "read-only probe commands"


def test_itential_play_builds_the_host_inventory_without_a_broker(versions: dict) -> None:
    text = ITENTIAL_PLAY.read_text()
    assert "stack.host_inventory" in text and "hosts.netbox_platforms" in text and "createBrokerActions: false" in text
    # Configuration Manager keeps seeing only the network inventory
    broker = text[text.index("Device Broker adapter exposes the inventory"):]
    assert 'inventories: ["{{ stack.inventory }}"]' in broker and "host_inventory" not in broker.split("changed_when")[0]
    assert "hosts.probe" in text, "the play proves Gateway 5 reaches one host"
    for s in ("itential_platform", "itential_user", "itential_password"):
        assert s in text


def test_endpoints_play_enables_password_login_for_the_automation_user_only() -> None:
    text = ENDPOINTS_PLAY.read_text()
    assert "sshd_config.d/10-lab-automation.conf" in text, "a drop-in that sorts before 60-cloudimg-settings.conf"
    assert re.search(r"Match User automation\n\s+PasswordAuthentication yes", text)
    assert "Reload sshd" in text and "state: reloaded" in text
    assert "PasswordAuthentication yes\n" in text and "PermitRootLogin" not in text


def test_verify_pid_and_adr_cover_s4d6() -> None:
    text = VERIFY.read_text()
    assert re.search(r'check "S4d\.6 ', text) and "S4d.6 hosts and firewalls (element 6, not built yet)" not in text
    for s in ("lab-hosts", "gateway_manager/v1/services/run", "uptime", "devcmd.py", "configuration_manager/devices"):
        assert s in text, f"verify 06b S4d.6 lacks {s}"
    assert "| 1.12 |" in PID.read_text() and ADR.exists()
