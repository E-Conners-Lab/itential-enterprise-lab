"""netops-knowledge on k3s and FlowMCP Gateway (ADR 0074): the pins, the Vault paths, the VIP, the pod's lock-down,
the network policy, the audit retention and the registration hold together."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import ipaddress
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import jinja2
import yaml

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = yaml.safe_load((ROOT / "itential" / "versions.yaml").read_text())
NK = VERSIONS["netops_knowledge"]
VAULT = VERSIONS["vault"]
IPAM = yaml.safe_load((ROOT / "topology" / "ipam.yaml").read_text())
PLAYS = ROOT / "ansible" / "playbooks"
PLAY = PLAYS / "netops-knowledge.yml"
TEMPLATE = PLAYS / "templates" / "netops-knowledge.yaml.j2"
MCP_TASKS = PLAYS / "tasks" / "gateway-mcp.yml"


def _address(hostname: str) -> str:
    return next(a["address"] for a in IPAM["addresses"] if a["hostname"] == hostname)


def _render() -> list[dict[str, Any]]:
    # ansible.builtin.template's options (tests/test_clab.py ANSIBLE_TEMPLATE_OPTIONS); `hash` is Ansible's filter
    env = jinja2.Environment(trim_blocks=True, lstrip_blocks=False, undefined=jinja2.StrictUndefined)
    env.filters["hash"] = lambda value, algo: hashlib.new(algo, str(value).encode()).hexdigest()
    text = env.from_string(TEMPLATE.read_text()).render(
        nk=NK,
        nk_ns=NK["namespace"],
        nk_fqdn=f"{NK['hostname']}.{IPAM['domain']}",
        nk_vip=_address(NK["hostname"]),
        nk_gateway_ip=_address(NK["gateway_host"]),
        nk_image=f"{NK['image']['repository']}@{NK['image']['digest']}",
        nk_token_sha256="0" * 64,
    )
    return [d for d in yaml.safe_load_all(text) if d]


def _kind(kind: str) -> dict[str, Any]:
    return next(d for d in _render() if d["kind"] == kind)


def test_the_pins_come_from_a_release() -> None:
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", NK["image"]["digest"]), "copy image_digest from release.json"
    assert re.fullmatch(r"[0-9a-f]{64}", NK["index_sha256"]) and re.fullmatch(r"[0-9a-f]{64}", NK["corpus_sha256"])
    assert re.fullmatch(r"[0-9a-f]{40}", NK["source_commit"])
    assert NK["image"]["repository"] == "ghcr.io/e-conners-lab/netops-knowledge"
    assert NK["signer"]["identity"].startswith(NK["repository"] + "/.github/workflows/")
    assert NK["signer"]["identity"].endswith("@refs/heads/main")


def test_flowmcps_token_is_a_local_secret_and_the_old_alias_is_retired() -> None:
    # ADR 0074 amendment (measured 2026-10-06, Gateway 5.5.2): an MCP header cannot resolve the Vault provider, so the
    # header names a local secret; no alias may point at the token any more, and the old one is named as a leftover
    assert "token_alias" not in NK and NK["mcp_secret"] == "netops-knowledge-mcp-token"
    assert NK["mcp_secret"] not in VAULT["gateway_aliases"]
    assert not [a for a, ref in VAULT["gateway_aliases"].items() if ref["path"] == VAULT["knowledge"]["token_path"]]
    assert "netops-knowledge-token" in VAULT["retired_gateway_aliases"]
    assert VAULT["gateway_admin_path"] == "gateway/iagctl-admin"


def test_the_url_is_the_vip_name_and_port() -> None:
    url = urlparse(NK["url"])
    assert url.scheme == "https" and url.path == "/mcp"
    assert url.hostname == f"{NK['hostname']}.{IPAM['domain']}" and url.port == NK["port"]
    entry = next(a for a in IPAM["addresses"] if a["hostname"] == NK["hostname"])
    assert entry["placement"] == "k3s-vip"
    pool = next(r for r in IPAM["ranges"] if "metallb" in str(r).lower())
    assert ipaddress.ip_address(pool["start"]) <= ipaddress.ip_address(entry["address"]) <= ipaddress.ip_address(pool["end"])


def test_the_pod_is_locked_down() -> None:
    deployment = _kind("Deployment")
    pod = deployment["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False and pod["enableServiceLinks"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True and pod["securityContext"]["runAsUser"] == 10001
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    (container,) = pod["containers"]
    assert container["image"] == f"{NK['image']['repository']}@{NK['image']['digest']}"
    sc = container["securityContext"]
    assert sc == {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}
    # no secret in the environment: the only credential material is the token's hash, as a read-only file
    assert all("valueFrom" not in e for e in container["env"]) and "envFrom" not in container
    secret_volumes = {v["secret"]["secretName"] for v in pod["volumes"]}
    assert secret_volumes == {"netops-knowledge-tls", "netops-knowledge-token-hash"}
    assert container["readinessProbe"]["httpGet"]["scheme"] == "HTTPS"


def test_no_secret_is_in_the_template() -> None:
    kinds = [d["kind"] for d in _render()]
    assert "Secret" not in kinds
    assert sorted(kinds) == ["Certificate", "Deployment", "NetworkPolicy", "Service"]


def test_only_the_gateway_reaches_it_and_it_reaches_nothing() -> None:
    gateway = f"{_address(NK['gateway_host'])}/32"
    service = _kind("Service")
    assert service["spec"]["type"] == "LoadBalancer" and service["spec"]["externalTrafficPolicy"] == "Local"
    assert service["metadata"]["annotations"]["metallb.io/loadBalancerIPs"] == _address(NK["hostname"])
    assert service["spec"]["loadBalancerSourceRanges"] == [gateway]
    policy = _kind("NetworkPolicy")["spec"]
    assert policy["policyTypes"] == ["Ingress", "Egress"] and policy["egress"] == []
    assert policy["ingress"] == [{"from": [{"ipBlock": {"cidr": gateway}}], "ports": [{"protocol": "TCP", "port": NK["port"]}]}]


def test_the_certificate_names_the_service_and_the_vip() -> None:
    cert = _kind("Certificate")["spec"]
    assert cert["issuerRef"] == {"name": "lab-ca", "kind": "ClusterIssuer"}
    assert cert["dnsNames"] == [urlparse(NK["url"]).hostname] and cert["ipAddresses"] == [_address(NK["hostname"])]


def test_the_signature_is_verified_before_anything_is_applied() -> None:
    tasks = yaml.safe_load(PLAY.read_text())[0]["tasks"]
    names = [t["name"] for t in tasks]
    verify = names.index("The pinned digest is signed by netops-knowledge's CI on main")
    first_apply = next(i for i, t in enumerate(tasks) if "kubernetes.core.k8s" in t)
    assert verify < first_apply
    argv = tasks[verify]["block"][2]["ansible.builtin.command"]["argv"]
    assert argv[:2] == ["cosign", "verify"] and "{{ nk.signer.identity }}" in argv and "{{ nk.signer.issuer }}" in argv


def test_every_task_that_touches_a_secret_hides_its_output() -> None:
    tasks = yaml.safe_load(PLAY.read_text())[0]["tasks"]
    for task in tasks:
        text = yaml.safe_dump(task)
        if any(s in text for s in ("vault_admin_token", "nk_pull_token", "nk_token_sha256 }}\"", "json.data.data")):
            if "block" not in task:
                assert task.get("no_log") is True, task["name"]


def test_the_audit_stream_is_kept_90_days() -> None:
    loki = yaml.safe_load((ROOT / "k8s" / "observability" / "values" / "loki.yaml").read_text())
    streams = loki["loki"]["limits_config"]["retention_stream"]
    assert streams == [{"selector": f'{{namespace="{NK["namespace"]}"}}', "priority": 1, "period": NK["audit_retention"]}]
    assert NK["audit_retention"] == "2160h"


def test_the_registration_sends_the_alias_never_a_token() -> None:
    text = MCP_TASKS.read_text()
    assert "secret \\\"{{ netops_knowledge.mcp_secret }}\\\"" in text
    tasks = yaml.safe_load(text)
    for task in tasks:
        if "configuration/export" in yaml.safe_dump(task):
            assert task.get("no_log") is True, task["name"]
    gateway_play = (PLAYS / "platform-ha2-gateway.yml").read_text()
    assert "tasks/gateway-mcp.yml" in gateway_play
    assert "netops_knowledge.register_with_gateway | bool" in gateway_play
    assert isinstance(NK["register_with_gateway"], bool)


def test_the_registration_has_the_import_shape_the_gateway_parses() -> None:
    # measured on production 2026-10-06 (Gateway 5.5.2, ADR 0074 amendment): the address is `command` (no `url` is
    # stored), the transport is the enum name, `headers` is a map, the import is YAML text, and it waits for the
    # Gateway's connection (a restart earlier in the play answers 503 until it reconnects)
    tasks = yaml.safe_load(MCP_TASKS.read_text())
    wanted = next(t for t in tasks if "nk_mcp_wanted" in t.get("ansible.builtin.set_fact", {}))
    server = wanted["ansible.builtin.set_fact"]["nk_mcp_wanted"]
    assert server["command"] == "{{ netops_knowledge.url }}" and server["transport"] == "STREAMABLE_HTTP"
    assert not {"url", "command_or_url"} & set(server)
    headers = server["headers"]
    assert isinstance(headers, dict) and list(headers) == ["Authorization"]
    assert headers["Authorization"].startswith("Bearer ") and "netops_knowledge.mcp_secret" in headers["Authorization"]
    names = [t["name"] for t in tasks]
    register = names.index("Register netops-knowledge (new, changed or asked to re-register)")
    body = tasks[register]["ansible.builtin.uri"]["body"]["options"]["content"]
    assert "to_nice_yaml" in body and "nk_mcp_wanted" in body
    connected = next(i for i, n in enumerate(names) if "connected to Gateway Manager" in n)
    local_secret = next(i for i, n in enumerate(names) if "local secret exists" in n)
    assert local_secret < register and connected < register
    assert "make\n      knowledge-mcp-secret" in MCP_TASKS.read_text() or "knowledge-mcp-secret" in yaml.safe_dump(tasks[local_secret])


def test_discovery_expects_the_names_flowmcp_gives() -> None:
    # FlowMCP names each discovered tool <mcp_server>_<tool> (measured 2026-10-06); the play and S15.5 both check that
    text = MCP_TASKS.read_text()
    assert "regex_replace', '^', netops_knowledge.mcp_server ~ '_'" in text
    verify = (ROOT / "verify" / "test-15-knowledge.sh").read_text()
    assert 'server + "_" + t' in verify and "re.sub" not in verify.split("c5()", 1)[1].split("check ", 1)[0]


def test_the_mcp_secret_script_never_shows_or_leaves_the_token() -> None:
    script = (ROOT / "scripts" / "knowledge-mcp-secret.sh").read_text()
    assert script.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in script
    assert "trap cleanup EXIT" in script and 'rm -rf "$tmp"' in script and "pbcopy </dev/null" in script
    assert "O_EXCL, 0o600" in script and "umask 077" in script
    # the token moves only as a file: never echoed, never an argument
    assert "--value @/tmp/nk-tok" in script and "print(token" not in script and "echo \"$token" not in script
    assert "trap 'sudo docker exec gateway5 rm -f /tmp/nk-tok; rm -f $remote' EXIT" in script
    for key in ('vault["knowledge"]["token_path"]', 'vault["gateway_admin_path"]', 'nk["mcp_secret"]', 'vault["prod"]["url"]'):
        assert key in script, key
    makefile = (ROOT / "Makefile").read_text()
    assert "knowledge-mcp-secret: ##" in makefile and "\tscripts/knowledge-mcp-secret.sh" in makefile
    import subprocess
    subprocess.run(["bash", "-n", str(ROOT / "scripts" / "knowledge-mcp-secret.sh")], check=True)


def test_the_tools_are_the_two_read_only_ones() -> None:
    assert NK["tools"] == ["search_scenarios", "get_scenario"]


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("ks", ROOT / "scripts" / "knowledge-secrets-to-vault.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_token_script_never_shows_the_token() -> None:
    ks = _script()
    calls: list[tuple[str, str, dict | None]] = []

    def fake_vault(tok: str, method: str, path: str, body: dict | None = None) -> int:
        calls.append((method, path, body))
        return 404 if method == "GET" else 200

    ks.vault = fake_vault
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert ks.write("admin", VAULT["knowledge"]["token_path"], {"token": "s3cr3t-canary"}, replace=False) == 0
    assert "s3cr3t-canary" not in out.getvalue()
    method, path, body = calls[-1]
    assert method == "POST" and path == f"{VAULT['kv_mount']}/data/{VAULT['knowledge']['token_path']}"
    assert body == {"data": {"token": "s3cr3t-canary"}, "options": {"cas": 0}}


def test_the_token_script_refuses_to_overwrite_without_rotate() -> None:
    ks = _script()
    ks.vault = lambda tok, method, path, body=None: 200
    with contextlib.redirect_stdout(io.StringIO()):
        assert ks.write("admin", VAULT["knowledge"]["token_path"], {"token": "x"}, replace=False) == 1
