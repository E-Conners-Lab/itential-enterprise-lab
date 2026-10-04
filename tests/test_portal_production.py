"""The branded AWS VPN page on production (ADR 0070): production's load balancer serves /portal/ from the same files and
with the same headers as the dev tier's portal nginx, on the Platform's own origin, so the engineer's own session
starts the jobs and the page never holds a password or token (ADR 0068 step 5, feasibility C3)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PROD_CONF = (ROOT / "itential" / "ha2" / "nginx.conf.j2").read_text()
DEV_CONF = (ROOT / "itential" / "portal" / "nginx.dev.conf").read_text()
COMPOSE = (ROOT / "itential" / "ha2" / "loadbalancer.compose.yml.j2").read_text()


def _portal_block(conf: str) -> str:
    return conf.split("location /portal/ {", 1)[1].split("}", 1)[0]


def test_production_serves_the_portal_with_the_dev_tiers_headers() -> None:
    prod, dev = _portal_block(PROD_CONF), _portal_block(DEV_CONF)
    assert "alias /usr/share/nginx/portal/;" in prod and "index index.html;" in prod
    headers = lambda block: sorted(re.findall(r"add_header .+;", block))  # noqa: E731
    assert headers(prod) == headers(dev) and any("Content-Security-Policy" in h for h in headers(prod))
    assert "frame-ancestors 'none'" in prod and "connect-src 'self'" in prod


def test_the_portal_comes_before_the_platform_in_the_same_server() -> None:
    server = PROD_CONF.split("listen {{ ip }}:{{ loadbalancer.listen_port }} ssl;", 1)[1]
    assert server.index("location /portal/ {") < server.index("location / {")


def test_the_page_files_are_mounted_read_only_and_copied_by_the_play() -> None:
    assert "- ./portal:/usr/share/nginx/portal:ro" in COMPOSE
    plays = yaml.safe_load((ROOT / "ansible" / "playbooks" / "platform-ha2-platform.yml").read_text())
    lb = next(p for p in plays if p["hosts"] == "ha2-loadbalancer")
    copy = next(t for t in lb["tasks"] if t["name"].startswith("Branded pages") and "ansible.builtin.copy" in t)
    copy = copy["ansible.builtin.copy"]
    assert copy["src"] == "../../itential/portal/deploy-aws-vpn" and copy["dest"] == "{{ dir }}/portal/"
    names = [t["name"] for t in lb["tasks"]]
    assert names.index(next(n for n in names if n.startswith("Branded pages"))) < names.index("Containers up")
