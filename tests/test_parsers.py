"""Structured device data (PID S4c.7, ADR 0038): Genie for Cisco, TextFSM (ntc-templates) for Arista,
run as Gateway 5 inline code on a glibc runner node (etcd store + runner container). These tests hold
itential/versions.yaml, the runner Dockerfile, the Compose override, the generated workflow, the play,
the manifest and the verify script to each other. They run in CI with no lab access."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "itential" / "versions.yaml"
MANIFEST = ROOT / "docs" / "image-manifest.md"
OVERRIDE = ROOT / "itential" / "compose.override.yml"
DOCKERFILE = ROOT / "itential" / "gateway5-runner" / "Dockerfile"
WORKFLOW = ROOT / "itential" / "workflows" / "wf-show-command-v1.json"
PLAY = ROOT / "ansible" / "playbooks" / "itential.yml"
VERIFY = ROOT / "verify" / "test-06-flowai.sh"
PID = ROOT / "docs" / "PID.md"
AGENTS = ROOT / "itential" / "agents"


@pytest.fixture(scope="module")
def versions() -> dict:
    return yaml.safe_load(VERSIONS.read_text())


def test_runner_images_pinned(versions: dict) -> None:
    etcd = versions["images"]["etcd"]
    assert etcd["repository"] == "quay.io/coreos/etcd" and re.fullmatch(r"v3\.5\.\d+", etcd["tag"]), "Gateway 5 requires etcd v3.5"
    base = versions["images"]["runner_base"]
    assert base["repository"] == "docker.io/library/python" and re.fullmatch(r"3\.12\.\d+-slim-[a-z]+", base["tag"]), "glibc Python 3.12 (pyATS has no musl or 3.14 wheels)"
    for img in (etcd, base):
        assert img["digest"].startswith("sha256:"), img


def test_parser_packages_pinned(versions: dict) -> None:
    parsers = versions["parsers"]
    assert parsers["cisco"]["engine"] == "genie" and parsers["arista"]["engine"] == "textfsm"
    pkgs = parsers["cisco"]["packages"] + parsers["arista"]["packages"]
    for p in pkgs:
        assert re.fullmatch(r"[a-z0-9-]+==\d+(\.\d+)+", p), f"pip spec must be ==pinned: {p}"
    names = {p.split("==")[0] for p in pkgs}
    assert names == {"pyats", "genie", "textfsm", "ntc-templates"}
    assert parsers["cisco"]["netbox_platforms"]["ios-xe"] == "iosxe"
    assert parsers["arista"]["netbox_platforms"]["eos"] == "arista_eos"


def test_runner_dockerfile_builds_from_the_pinned_images(versions: dict) -> None:
    text = DOCKERFILE.read_text()
    assert re.search(r"^ARG GATEWAY5_IMAGE", text, re.M) and re.search(r"^ARG PYTHON_IMAGE", text, re.M)
    assert "COPY --from=gateway /usr/local/bin/iagctl /usr/local/bin/iagctl" in text
    assert "ln -s /usr/local/bin/python3 /usr/bin/python3" in text, "the netsdk pex services look for /usr/bin/python3"
    for d in ("/var/lib/gateway", "/var/log/gateway", "/var/cache/gateway"):
        assert d in text
    assert 'ENTRYPOINT ["iagctl", "runner"]' in text or 'ENTRYPOINT ["iagctl","runner"]' in text


def test_override_wires_etcd_store_and_runner(versions: dict) -> None:
    ov = yaml.safe_load(OVERRIDE.read_text())
    svcs = ov["services"]
    etcd, gw, runner = svcs["etcd"], svcs["gateway5"], svcs["gateway5-runner"]
    assert "${ETCD_IMAGE}" in etcd["image"] and "etcd-data" in " ".join(etcd["volumes"])
    assert not any(str(p).startswith("${OOB_ADDRESS}") for p in etcd.get("ports", [])), "etcd stays on the Docker network"
    env = gw["environment"]
    assert env["GATEWAY_STORE_BACKEND"] == "etcd" and env["GATEWAY_STORE_ETCD_HOSTS"] == "etcd:2379"
    assert str(env["GATEWAY_SERVER_DISTRIBUTED_EXECUTION"]).lower() == "true"
    renv = runner["environment"]
    assert renv["GATEWAY_APPLICATION_MODE"] == "runner" and "${GATEWAY5_CLUSTER_ID" in str(renv["GATEWAY_APPLICATION_CLUSTER_ID"])
    assert renv["GATEWAY_STORE_BACKEND"] == "etcd" and renv["GATEWAY_RUNNER_ANNOUNCEMENT_ADDRESS"] == "gateway5-runner"
    assert "${GATEWAY5_RUNNER_IMAGE}" in runner["image"]
    assert any(v.startswith("gateway5-runner-data:/var/lib/gateway") for v in runner["volumes"]), "the Genie venv cache must persist"
    assert runner["deploy"]["resources"]["limits"]["memory"]
    assert set(ov["volumes"]) >= {"etcd-data", "gateway5-runner-data"}


def test_show_command_workflow_parses_per_vendor(versions: dict) -> None:
    wf = json.loads(WORKFLOW.read_text())
    assert wf["name"] == "wf-show-command-v1"
    assert set(wf["inputSchema"]["properties"]) == {"device", "command"}
    tasks = wf["tasks"]
    run = [t for t in tasks.values() if t.get("name") == "runCode"]
    assert len(run) == 1, "exactly one parse step"
    inc = run[0]["variables"]["incoming"]
    pinned = versions["parsers"]["cisco"]["packages"] + versions["parsers"]["arista"]["packages"]
    assert inc["packages"] == pinned, "runCode packages must be the pinned parser set"
    assert inc["clusterId"] == versions["stack"]["gateway5_cluster_id"]
    assert inc["code"].startswith("$var."), "the parse code is rendered per device (platform slug replaced)"
    assert inc["data"].startswith("$var.") and inc["safety"]["timeout"] >= 120
    # S4f (ADR 0054): the NetBox lookup is the lab-netbox integration's operation now, not the adapter's
    # method. The intent is unchanged - the parser is chosen from the platform NetBox holds for the device.
    assert any(t.get("name") == "dcim_devices_list" for t in tasks.values()), "the platform comes from NetBox"
    assert set(wf["outputSchema"]["properties"]) >= {"raw", "parsed", "parser"}


def test_play_builds_the_runner_and_waits_for_registration() -> None:
    text = PLAY.read_text()
    for s in ("ETCD_IMAGE=", "GATEWAY5_RUNNER_IMAGE=", "gateway5-runner", "registered runner with database"):
        assert s in text, s


def test_manifest_records_runner_and_parsers(versions: dict) -> None:
    text = MANIFEST.read_text()
    assert f"`{versions['images']['etcd']['tag']}`" in text and "quay.io/coreos/etcd" in text
    assert f"`{versions['images']['runner_base']['tag']}`" in text
    for p in versions["parsers"]["cisco"]["packages"] + versions["parsers"]["arista"]["packages"]:
        assert f"`{p}`" in text, f"manifest must pin {p}"


def test_criterion_and_verify_cover_structured_output() -> None:
    assert "S4c.7" in VERIFY.read_text() and re.search(r"check \"S4c\.7 ", VERIFY.read_text())
    pid = PID.read_text()
    assert re.search(r"^\s+7\. .*Genie.*TextFSM|^\s+7\. .*TextFSM.*Genie", pid, re.M), "PID S4c needs criterion 7"


def test_agents_use_the_structured_tool() -> None:
    """Every agent that reads devices holds the structured show-command workflow (the fleet's NetBox, compliance and
    remediation tiers hold no device read tool at all, ADR 0046)."""
    docs = [yaml.safe_load(p.read_text()) for p in sorted(AGENTS.glob("*.yaml"))]
    readers = [a for a in docs if {t["reference"] for t in a["tools"]} & {"send-command", "wf-show-command-v1", "wf-show-version-v1"}]
    assert readers, "no agent reads devices"
    for a in readers:
        refs = {t["reference"] for t in a["tools"]}
        assert "wf-show-command-v1" in refs, f"{a['name']} lacks the structured show-command tool"
        assert "wf-show-command-v1" in a["instructions"]
