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
    assert set(profiles) >= {"anthropic", "ollama-mac"}
    assert profiles["anthropic"]["provider"] == "anthropic"
    assert profiles["anthropic"]["model"].startswith("claude-"), "Anthropic model must be pinned by id"
    mac = profiles["ollama-mac"]
    assert mac["provider"] == "ollama"
    # ADR 0060: inference left the lab. The URL must be the DNS name, never a raw home-LAN address -
    # the Mac sits in the router's DHCP pool, so the address is a reservation that can move and DNS
    # is the single place that knows it (topology/ipam.yaml home_lan.inference_host).
    assert mac["base_url"] == "http://ollama.lab.internal:11434", (
        "the inference host is reached by name, not by address"
    )
    assert re.search(r":\d|:[a-z0-9]+-", mac["model"]), "Ollama model must carry a tag"
    assert "optional" not in mac, (
        "the Mac is the only inference host now: marking it optional would let the play skip it and "
        "leave six agents pointing at nothing"
    )
    assert not any(p.get("base_url", "").startswith("http://ollama:") for p in profiles.values()), (
        "http://ollama:11434 was the Compose service on the Platform host; it no longer exists"
    )
    for name, p in profiles.items():
        assert "key" not in p and "apiKey" not in p, f"{name}: keys live in .env only"


def test_no_ollama_runs_in_the_lab_any_more(versions: dict) -> None:
    """ADR 0060: the owner chose to run no inference on the server. The lab has no GPU, and tools-01's
    Ollama sat at 91% of a 6 GiB cap on a 7.8 GiB VM until llama-server crashed mid-verify and took a
    local-twin criterion with it. Nothing should be left that starts a container or pins its image -
    a dormant service definition is how it comes back."""
    assert "ollama" not in versions["images"], (
        "the Ollama image pin is dead: no lab host runs the container"
    )
    assert "ollama" not in versions["stack"]["profiles"], "the compose profile would start it again"
    ov = yaml.safe_load(OVERRIDE.read_text())
    assert "ollama" not in ov["services"], "the Platform host still defines an ollama service"
    assert "ollama-models" not in (ov.get("volumes") or {}), "the model volume would hold GBs for nothing"
    tools = (ROOT / "itential" / "ha2" / "tools.compose.yml.j2").read_text()
    assert "\n  ollama:" not in tools, "tools-01 still defines an ollama service"
    # and the model is pinned where it now lives
    text = MANIFEST.read_text()
    for p in versions["llm"]["profiles"]:
        if p["provider"] == "ollama":
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
