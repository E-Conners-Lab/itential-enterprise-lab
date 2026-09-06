#!/usr/bin/env python3
"""Build the EVE-NG lab from topology/enterprise.yaml (ADR 0002, 0034). Idempotent: existing
nodes and networks are matched by name and left alone; missing ones are created. Startup configs
are rendered from topology/configs/<platform>.j2 and uploaded when present.

  eve/build.py plan                 # read-only: what would be created; fails on missing images
  eve/build.py apply                # create lab, networks, nodes, links, configs (no start)
  eve/build.py start [--waves]      # start nodes: non-firewalls, then firewalls in two waves
  eve/build.py stop | status

Credentials from the environment (.env via make): EVE_HOST, EVE_USERNAME, EVE_PASSWORD.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
import urllib3
import yaml

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ROOT = Path(__file__).resolve().parent.parent
TOPO = ROOT / "topology" / "enterprise.yaml"
CONFIG_DIR = ROOT / "topology" / "configs"

# manifest platform key -> EVE-NG template name (from /opt/unetlab/html/templates/intel/*.yml)
TEMPLATE = {"c8000v": "c8000v", "veos": "veos", "pa-vm": "paloalto", "ubuntu": "linux", "win11": "win"}
CONSOLE = {"c8000v": "telnet", "veos": "telnet", "pa-vm": "telnet", "ubuntu": "vnc", "win11": "vnc"}
STARTUP_CONFIG_PLATFORMS = {"c8000v", "veos", "pa-vm"}  # templates with a config_script


def iface_index(platform: str, iface: str) -> int:
    """EVE-NG interface index for a named port. Index 0 is management on every template."""
    if platform == "c8000v":
        m = re.fullmatch(r"Gi(\d+)", iface)
        if not m:
            raise ValueError(f"{platform}: bad interface {iface}")
        return int(m.group(1)) - 1  # Gi1 = 0 (mgmt), Gi2 = 1 ...
    if platform == "veos":
        if iface == "Mgmt1":
            return 0
        m = re.fullmatch(r"Eth(\d+)", iface)
    elif platform == "pa-vm":
        if iface == "mgmt":
            return 0
        m = re.fullmatch(r"eth1/(\d+)", iface)
    else:
        m = re.fullmatch(r"eth(\d+)", iface)
    if not m:
        raise ValueError(f"{platform}: bad interface {iface}")
    return int(m.group(1))


def link_network_name(link: dict) -> str:
    a, b = link["a"].replace(":", "_").replace("/", "-"), link["b"].replace(":", "_").replace("/", "-")
    return f"link-{a}--{b}"


class Eve:
    def __init__(self, host: str, user: str, password: str, lab_path: str):
        self.base = f"https://{host}/api"
        self.s = requests.Session()
        self.s.verify = False
        r = self.s.post(f"{self.base}/auth/login", json={"username": user, "password": password, "html5": "-1"}, timeout=20)
        if r.json().get("status") != "success":
            raise SystemExit(f"EVE-NG login failed: {r.text[:200]}")
        self.lab = lab_path

    def _req(self, method: str, path: str, **kw):
        r = self.s.request(method, f"{self.base}{path}", timeout=60, **kw)
        try:
            body = r.json()
        except ValueError:
            raise SystemExit(f"{method} {path}: HTTP {r.status_code} non-JSON: {r.text[:200]}")
        if body.get("status") != "success" and not (method == "GET" and body.get("code") == 404):
            hint = "  (60061 = stale /opt/unetlab/labs/<lab>.unl.lock from an interrupted API call; remove it when nothing is running)" if body.get("code") == 400 and "lock" in str(body.get("message")) else ""
            raise SystemExit(f"{method} {path}: {body.get('code')} {body.get('message')}{hint}")
        return body

    # ---- lab
    def lab_exists(self) -> bool:
        return self._req("GET", f"/labs{self.lab}").get("code") != 404

    def create_lab(self, name: str, description: str) -> None:
        self._req("POST", "/labs", json={"path": "/", "name": name, "version": "1", "author": "itential-enterprise-lab", "description": description, "body": ""})

    # ---- networks
    def networks(self) -> dict[str, dict]:
        data = self._req("GET", f"/labs{self.lab}/networks").get("data") or {}
        if isinstance(data, list):  # EVE-NG returns [] when the lab has no networks yet
            data = {str(n.get("id", i)): n for i, n in enumerate(data)}
        return {n["name"]: {**n, "id": int(k)} for k, n in data.items()}

    def add_network(self, name: str, ntype: str, left: int, top: int, visible: int) -> None:
        self._req("POST", f"/labs{self.lab}/networks", json={"type": ntype, "name": name, "left": left, "top": top, "visibility": visible})

    # ---- nodes
    def nodes(self) -> dict[str, dict]:
        data = self._req("GET", f"/labs{self.lab}/nodes").get("data") or {}
        if isinstance(data, list):
            data = {str(n.get("id", i)): n for i, n in enumerate(data)}
        return {n["name"]: {**n, "id": int(k)} for k, n in data.items()}

    def template_images(self, template: str) -> list[str]:
        opts = self._req("GET", f"/list/templates/{template}").get("data", {}).get("options", {})
        lst = (opts.get("image") or {}).get("list") or {}
        return list(lst.keys()) if isinstance(lst, dict) else list(lst)  # EVE-NG returns [] when empty

    def add_node(self, payload: dict) -> int:
        return int(self._req("POST", f"/labs{self.lab}/nodes", json=payload)["data"]["id"])

    def set_interfaces(self, node_id: int, mapping: dict[int, int]) -> None:
        self._req("PUT", f"/labs{self.lab}/nodes/{node_id}/interfaces", json={str(k): v for k, v in mapping.items()})

    def interfaces(self, node_id: int) -> dict:
        return self._req("GET", f"/labs{self.lab}/nodes/{node_id}/interfaces").get("data", {})

    # EVE-NG Pro keeps startup configs in config *sets*; uploads need the set id in the body and
    # answer 201 with a PHP array dump appended (non-JSON tail), so parse leniently here.
    def config_set(self, name: str = "startup") -> int:
        data = self._req("GET", f"/labs{self.lab}/configsets").get("data") or {}
        sets = list(data.values()) if isinstance(data, dict) else list(data)
        for cs in sets:
            if cs.get("name") == name:
                return int(cs["id"])
        r = self.s.post(f"{self.base}/labs{self.lab}/configsets", json={"name": name}, timeout=60)
        if r.status_code != 200 or '"success"' not in r.text:
            raise SystemExit(f"create config set: HTTP {r.status_code} {r.text[:160]}")
        return int(r.json()["id"])

    def upload_config(self, node_id: int, text: str, cfsid: int) -> None:
        r = self.s.put(f"{self.base}/labs{self.lab}/configs/{node_id}", json={"id": node_id, "cfsid": cfsid, "data": text}, timeout=60)
        if r.status_code not in (200, 201) or '"success"' not in r.text[:200]:
            raise SystemExit(f"upload config node {node_id}: HTTP {r.status_code} {r.text[:120]}")

    def enable_config(self, node_id: int, cfsid: int) -> None:
        # Pro stores the selected config set id in the node's 'config' field; answers 201
        r = self.s.put(f"{self.base}/labs{self.lab}/nodes/{node_id}", json={"id": node_id, "config": cfsid}, timeout=60)
        if r.status_code not in (200, 201) or '"success"' not in r.text:
            raise SystemExit(f"enable config node {node_id}: HTTP {r.status_code} {r.text[:120]}")

    def start(self, node_id: int) -> None:
        self._req("GET", f"/labs{self.lab}/nodes/{node_id}/start")

    def stop(self, node_id: int) -> None:
        # EVE-NG Pro: plain /stop is "Request not valid"; stopmode=3 = hard stop, 1 = graceful
        self._req("GET", f"/labs{self.lab}/nodes/{node_id}/stop/stopmode=3")


def load_topology() -> dict:
    return yaml.safe_load(TOPO.read_text())


def render_config(platform: str, name: str, node: dict, topo: dict) -> str | None:
    tpl = CONFIG_DIR / f"{platform}.j2"
    if not tpl.exists():
        return None
    from jinja2 import Environment, FileSystemLoader, StrictUndefined

    import secrets as _secrets
    import subprocess

    secret = os.environ.get("AUTOMATION_PASSWORD")
    if not secret:
        raise SystemExit("AUTOMATION_PASSWORD missing in .env (device-local automation account, moved to Vault in phase 9)")
    env = Environment(loader=FileSystemLoader(str(CONFIG_DIR)), undefined=StrictUndefined, keep_trailing_newline=True)
    links = [lk for lk in topo["links"] if lk["a"].startswith(name + ":") or lk["b"].startswith(name + ":")]
    return env.get_template(f"{platform}.j2").render(
        name=name, node=node, lab=topo["lab"], routing=topo["routing"], links=links, nodes=topo["nodes"],
        automation_password=secret,
        # md5-crypt ($1$) is the phash format PAN-OS accepts; python's crypt module is gone in 3.13+
        pan_password_hash=subprocess.run(["openssl", "passwd", "-1", "-salt", _secrets.token_hex(4), secret], capture_output=True, text=True, check=True).stdout.strip(),
    )


def plan(eve: Eve, topo: dict) -> dict:
    """Compute what apply would do. Fails loudly on images that EVE-NG does not have."""
    missing_images = []
    for plat in sorted({n["platform"] for n in topo["nodes"].values()}):
        have = eve.template_images(TEMPLATE[plat])
        for name, n in topo["nodes"].items():
            if n["platform"] == plat and n["image"] not in have:
                missing_images.append(f"{name}: {n['image']} (template {TEMPLATE[plat]} has {have or 'none'})")
    have_nodes = eve.nodes() if eve.lab_exists() else {}
    have_nets = eve.networks() if eve.lab_exists() else {}
    want_nets = [topo["lab"]["mgmt_network"]] + [link_network_name(lk) for lk in topo["links"]]
    return {
        "lab_exists": eve.lab_exists(),
        "missing_images": missing_images,
        "nodes_to_create": [n for n in topo["nodes"] if n not in have_nodes],
        "networks_to_create": [n for n in want_nets if n not in have_nets],
        "existing_nodes": sorted(have_nodes),
    }


def apply(eve: Eve, topo: dict, allow_missing: bool) -> None:
    p = plan(eve, topo)
    if p["missing_images"] and not allow_missing:
        raise SystemExit("missing images on EVE-NG (stage them with images/import-eve.sh):\n  " + "\n  ".join(p["missing_images"]))
    if not p["lab_exists"]:
        eve.create_lab(topo["lab"]["name"], "itential-enterprise-lab (ADR 0034). Built by eve/build.py; do not edit by hand.")
        print(f"created lab {eve.lab}")
    # networks: management cloud + one hidden bridge per link
    nets = eve.networks()
    mgmt = topo["lab"]["mgmt_network"]
    if mgmt not in nets:
        eve.add_network(mgmt, mgmt, 560, 20, 1)
        print(f"created network {mgmt}")
    # Link networks are visible (visibility 1): EVE-NG Pro 6.5 acknowledges hidden bridges but
    # does not persist them to the lab file. They sit in a grid below the topology.
    for i, lk in enumerate(topo["links"]):
        nm = link_network_name(lk)
        if nm not in nets:
            eve.add_network(nm, "bridge", 40 + (i % 10) * 110, 900 + (i // 10) * 70, 1)
            print(f"created network {nm}")
    nets = eve.networks()
    # nodes
    skipped = {m.split(":")[0] for m in p["missing_images"]}
    have = eve.nodes()
    for name, n in topo["nodes"].items():
        if name in have:
            continue
        if name in skipped:
            print(f"SKIP {name}: image {n['image']} not on EVE-NG")
            continue
        payload = {
            "type": "qemu", "template": TEMPLATE[n["platform"]], "image": n["image"], "name": name,
            "cpu": n["cpu"], "ram": n["ram"], "ethernet": n["ethernet"], "console": CONSOLE[n["platform"]],
            "left": n["eve"]["left"], "top": n["eve"]["top"], "config": 0, "delay": 0,
        }
        nid = eve.add_node(payload)
        print(f"created node {name} (id {nid})")
    have = eve.nodes()
    # interfaces: index 0 -> mgmt cloud; link ends -> their bridge
    wiring: dict[str, dict[int, int]] = {name: {0: nets[mgmt]["id"]} for name in have if name in topo["nodes"]}
    for lk in topo["links"]:
        nid = nets[link_network_name(lk)]["id"]
        for end in (lk["a"], lk["b"]):
            node, iface = end.split(":")
            if node in wiring:
                wiring[node][iface_index(topo["nodes"][node]["platform"], iface)] = nid
    for name, mapping in wiring.items():
        current = eve.interfaces(have[name]["id"]).get("ethernet") or {}
        items = current.items() if isinstance(current, dict) else enumerate(current)  # list = index order
        cur = {int(k): int((v or {}).get("network_id", 0) or 0) for k, v in items}
        if any(cur.get(k) != v for k, v in mapping.items()):
            eve.set_interfaces(have[name]["id"], mapping)
            print(f"wired {name}: {len(mapping)} interfaces")
    # startup configs (config set "startup"); only for nodes that are stopped
    cfsid = eve.config_set()
    for name, n in topo["nodes"].items():
        if name not in have or n["platform"] not in STARTUP_CONFIG_PLATFORMS:
            continue
        cfg = render_config(n["platform"], name, n, topo)
        if cfg and str(have[name].get("config")) in ("0", "None") and have[name].get("status") != 2:
            eve.upload_config(have[name]["id"], cfg, cfsid)
            eve.enable_config(have[name]["id"], cfsid)
            print(f"config {name}: {len(cfg)} bytes (set {cfsid})")
    export_nodes(eve, topo)


def start(eve: Eve, topo: dict, waves: bool) -> None:
    have = eve.nodes()
    order = [n for n in topo["nodes"] if topo["nodes"][n]["role"] != "firewall"]
    fws = [n for n in topo["nodes"] if topo["nodes"][n]["role"] == "firewall"]
    groups = [order, fws[:2], fws[2:]] if waves else [order + fws]
    for g in groups:
        for name in g:
            if name in have and have[name].get("status") != 2:
                eve.start(have[name]["id"])
                print(f"start {name}")
        if waves and g is not groups[-1]:
            print("wave started; waiting 300 s before the next (PAN-OS boot storm, PID E3)")
            time.sleep(300)


def export_nodes(eve: Eve, topo: dict) -> Path:
    """Write topology/generated/eve-nodes.yaml: EVE-NG node ids and management MACs
    (EVE-NG derives MAC 50:00:00:<node id>:00:<interface index>). Consumed by the oob-gw
    DHCP reservations for endpoints that cannot take a startup config."""
    out = ROOT / "topology" / "generated" / "eve-nodes.yaml"
    out.parent.mkdir(exist_ok=True)
    rows = {}
    for name, n in sorted(eve.nodes().items()):
        if name not in topo["nodes"]:
            continue
        rows[name] = {"id": n["id"], "mac0": f"50:00:00:{n['id']:02x}:00:00", "mgmt_ip": topo["nodes"][name]["mgmt_ip"], "platform": topo["nodes"][name]["platform"]}
    out.write_text("# Generated by eve/build.py export; do not edit. Node ids depend on creation order.\n" + yaml.safe_dump({"lab": topo["lab"]["path"], "nodes": rows}, sort_keys=False))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["plan", "apply", "start", "stop", "status", "export"])
    ap.add_argument("--allow-missing", action="store_true", help="apply: skip nodes whose image is absent (loud)")
    ap.add_argument("--waves", action="store_true", help="start: firewalls last, in two waves 300 s apart")
    a = ap.parse_args()
    topo = load_topology()
    eve = Eve(os.environ["EVE_HOST"], os.environ["EVE_USERNAME"], os.environ["EVE_PASSWORD"], topo["lab"]["path"])
    if a.action == "plan":
        print(json.dumps(plan(eve, topo), indent=2))
        sys.exit(1 if plan(eve, topo)["missing_images"] else 0)
    if a.action == "apply":
        apply(eve, topo, a.allow_missing)
    elif a.action == "start":
        start(eve, topo, a.waves)
    elif a.action == "stop":
        for name, n in eve.nodes().items():
            eve.stop(n["id"])
            print(f"stop {name}")
    elif a.action == "export":
        print(export_nodes(eve, topo))
    elif a.action == "status":
        for name, n in sorted(eve.nodes().items()):
            print(f"{name:14s} status={n.get('status')} image={n.get('image')} ram={n.get('ram')}")


if __name__ == "__main__":
    main()
