"""Phase 6 (PID S4c, ADR 0037) unit tests: itential/versions.yaml gains an `llm` section (provider
profiles, pinned models, the in-lab Ollama image) and itential/agents/ holds the agent project
documents the play creates. These tests hold them to the manifest, the Compose override and the
verify script. They run in CI with no lab access."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "itential" / "versions.yaml"
MANIFEST = ROOT / "docs" / "image-manifest.md"
OVERRIDE = ROOT / "itential" / "compose.override.yml"
AGENTS = ROOT / "itential" / "agents"
VERIFY = ROOT / "verify" / "test-06-flowai.sh"
PLAY = ROOT / "ansible" / "playbooks" / "flowai.yml"
ENV_EXAMPLE = ROOT / ".env.example"


@pytest.fixture(scope="module")
def versions() -> dict:
    return yaml.safe_load(VERSIONS.read_text())


def test_llm_section_pins_providers_and_models(versions: dict) -> None:
    llm = versions["llm"]
    assert llm["default_profile"] == "anthropic"
    profiles = {p["name"]: p for p in llm["profiles"]}
    assert set(profiles) >= {"anthropic", "ollama-lab"}
    assert profiles["anthropic"]["provider"] == "anthropic"
    assert profiles["anthropic"]["model"].startswith("claude-"), "Anthropic model must be pinned by id"
    assert profiles["ollama-lab"]["provider"] == "ollama"
    assert profiles["ollama-lab"]["base_url"] == "http://ollama:11434", "in-lab Ollama is a Compose service"
    assert re.search(r":\d|:[a-z0-9]+-", profiles["ollama-lab"]["model"]), "Ollama model must carry a tag"
    for name, p in profiles.items():
        assert "key" not in p and "apiKey" not in p, f"{name}: keys live in .env only"
    mac = profiles.get("ollama-mac")
    if mac:
        assert mac["optional"] is True and mac["provider"] == "ollama"


def test_ollama_image_pinned_in_versions_manifest_and_override(versions: dict) -> None:
    img = versions["images"]["ollama"]
    assert img["repository"] == "docker.io/ollama/ollama" and re.fullmatch(r"\d+\.\d+\.\d+", str(img["tag"]))
    ov = yaml.safe_load(OVERRIDE.read_text())
    svc = ov["services"]["ollama"]
    assert "${OLLAMA_IMAGE}" in svc["image"] or "ollama/ollama" in svc["image"]
    assert any("11434" in str(p) for p in svc.get("ports", [])) is False or all(str(p).startswith("127.") for p in svc["ports"]), "Ollama is not exposed on the OOB address"
    assert svc["deploy"]["resources"]["limits"]["memory"], "Ollama needs a memory limit on the shared VM"
    text = MANIFEST.read_text()
    assert f"`{img['tag']}`" in text and "ollama/ollama" in text, "manifest must pin the Ollama image"
    for p in versions["llm"]["profiles"]:
        if p["provider"] == "ollama" and not p.get("optional"):
            assert f"`{p['model']}`" in text, f"manifest must pin the Ollama model {p['model']}"


def test_agent_documents(versions: dict) -> None:
    docs = sorted(AGENTS.glob("*.yaml"))
    assert docs, "itential/agents/ has no agent documents"
    names = {p["name"] for p in versions["llm"]["profiles"]}
    for path in docs:
        a = yaml.safe_load(path.read_text())
        assert a["project"] and a["name"] and a["instructions"].strip()
        assert a["profile"] in names, f"{path.name}: unknown profile {a['profile']}"
        assert a["tools"], f"{path.name}: an agent without tools cannot touch the lab"
        for t in a["tools"]:
            assert t["reference"], t
        schema = a["input_schema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        for prop in schema["properties"].values():
            assert prop["type"] in ("string", "number"), "FlowAI input schema allows string/number only"
    assert any(yaml.safe_load(p.read_text())["name"] == "lab-netops" for p in docs)


def test_play_and_verify_exist() -> None:
    assert PLAY.exists(), "ansible/playbooks/flowai.yml missing"
    assert VERIFY.exists() and VERIFY.stat().st_mode & 0o111
    text = VERIFY.read_text()
    for c in [f"S4c.{i}" for i in range(1, 7)]:
        assert re.search(rf'check "{re.escape(c)} ', text) or re.search(rf"# --- {re.escape(c)}", text), f"{c} not covered"
    assert "tokens" in text.lower(), "verify must record token usage / cost"


def test_env_example_has_provider_keys_only_as_placeholders() -> None:
    text = ENV_EXAMPLE.read_text()
    assert re.search(r"^ANTHROPIC_API_KEY=\s*(#.*)?$", text, re.M), "ANTHROPIC_API_KEY placeholder missing or not empty"
