"""Phase 7 (PID S7, ADR 0051): the observability documents never drift from their oracles.

k8s/observability/versions.yaml is held to docs/image-manifest.md 4.3 and topology/ipam.yaml; Zabbix stays removed
(ADR 0071) and the plays delete what it left;
the device telemetry lines in the topology templates and Golden Config read the same addresses; the
Platform exporter parses the measured API shapes. Runs in CI with no lab access.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import os
import re
import ssl
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
VERSIONS = yaml.safe_load((ROOT / "k8s" / "observability" / "versions.yaml").read_text())
OBS = yaml.safe_load((ROOT / "observability" / "observability.yaml").read_text())
EXPIRIES = yaml.safe_load((ROOT / "observability" / "expiries.yaml").read_text())
IPAM = yaml.safe_load((ROOT / "topology" / "ipam.yaml").read_text())
MANIFEST = (ROOT / "docs" / "image-manifest.md").read_text()
TOPO = yaml.safe_load((ROOT / "topology" / "enterprise.yaml").read_text())


def _manifest_rows(section: str, nxt: str) -> dict[str, list[str]]:
    text = MANIFEST[MANIFEST.index(section) : MANIFEST.index(nxt)]
    rows = {}
    for line in text.splitlines():
        if line.startswith("| ") and not line.startswith("| Component") and "|---" not in line:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            rows[cells[0]] = cells
    return rows


# --- versions.yaml vs manifest 4.3 and ipam.yaml -------------------------------------------------------


def test_every_pin_is_explicit() -> None:
    for name, spec in VERSIONS["components"].items():
        for key in ("chart_version", "app_version", "image_tag", "agent2_version"):
            if key in spec:
                val = str(spec[key])
                assert val != "latest" and re.search(r"\d", val), f"{name}.{key} not pinned: {val!r}"


@pytest.mark.parametrize(
    ("key", "label"),
    [
        ("kube_prometheus_stack", "kube-prometheus-stack"),
        ("snmp_exporter", "SNMP exporter"),
        ("blackbox_exporter", "Blackbox exporter"),
        ("loki", "Loki"),
        ("alloy", "Alloy"),
    ],
)
def test_chart_versions_match_manifest(key: str, label: str) -> None:
    rows = _manifest_rows("### 4.3 Observability", "### 4.4")
    spec = VERSIONS["components"][key]
    cell = rows[label][2]
    assert str(spec["chart_version"]) in cell, f"{key}: chart {spec['chart_version']} not in {cell!r}"
    assert spec["chart_repo"] in cell or spec["chart_repo"] in rows[label][2] or "same repo" in cell


def test_images_match_manifest() -> None:
    rows = _manifest_rows("### 4.3 Observability", "### 4.4")
    c = VERSIONS["components"]
    assert f"{c['gnmic']['image']}:{c['gnmic']['image_tag']}" in rows["gNMIc"][3]
    assert c["loki"]["app_version"] in rows["Loki"][3] and c["alloy"]["app_version"] in rows["Alloy"][3]
    assert c["kube_prometheus_stack"]["grafana_version"] in rows["kube-prometheus-stack"][3]
    assert c["snmp_exporter"]["app_version"] in rows["SNMP exporter"][3]
    assert c["blackbox_exporter"]["app_version"] in rows["Blackbox exporter"][3]
    assert not [r for r in rows if "zabbix" in r.lower()], "docs/image-manifest.md 4.3 still lists Zabbix (ADR 0071)"


def test_exporter_image_is_the_runner_base() -> None:
    it = yaml.safe_load((ROOT / "itential" / "versions.yaml").read_text())["images"]["runner_base"]
    ex = VERSIONS["components"]["itential_exporter"]
    assert (ex["image"], ex["image_tag"], ex["digest"]) == (it["repository"], it["tag"], it["digest"])


def test_vips_match_ipam() -> None:
    by_name = {a["hostname"]: a for a in IPAM["addresses"]}
    for name, ip in VERSIONS["vips"].items():
        assert by_name[name]["address"] == ip and by_name[name]["placement"] == "k3s-vip", name
        assert by_name[name]["phase"] == 7, f"{name} is a phase-7 VIP (ADR 0050)"
    assert "alertmanager" in by_name["prometheus"].get("aliases", [])
    pool = next(r for r in IPAM["ranges"] if r["role"] == "metallb-pool")
    for ip in VERSIONS["vips"].values():
        assert pool["start"] <= ip <= pool["end"]


def test_ip_plan_phases_follow_adr_0050() -> None:
    by_name = {a["hostname"]: a["phase"] for a in IPAM["addresses"]}
    # order of amendment 1.18 (ADR 0050 + 0053): 8 platform-ha2, 9 config/secrets, 10 identity, 11 DDI, 12 clab, 13 firewalls
    assert by_name["keycloak"] == 10 and by_name["tacacs"] == 10
    assert by_name["oxidized"] == 9 and by_name["vault"] == 9 and by_name["gitea"] == 9
    assert by_name["ddi-fallback"] == 11 and by_name["nios"] == 13 and by_name["panorama"] == 13 and by_name["clab"] == 12
    for h in ("iap-lb", "iap-01", "iap-02", "mongo-01", "mongo-02", "mongo-03", "redis-01", "redis-02", "redis-03", "iag-01", "tools-01"):
        assert by_name[h] == 8, h
    assert "dc01" not in by_name and "iag" not in by_name
    plan = (ROOT / "docs" / "ip-plan.md").read_text()
    assert "identity (phase 10)" in plan and "Oxidized (phase 9)" in plan
    assert "zabbix" not in by_name and "| 10.100.0.35 | *(released)* |" in plan  # ADR 0071


# --- the documents -----------------------------------------------------------------------------------


def test_web_checks_cover_every_ui_vip() -> None:
    names = [w["name"] for w in OBS["web_checks"]]
    assert len(names) == len(set(names))
    for vip in VERSIONS["vips"]:
        assert vip in names, f"no HTTP check for the {vip} UI"
    assert {"netbox", "itential", "eve", "alertmanager"} <= set(names)
    for w in OBS["web_checks"]:
        assert w["url"].startswith(("http://", "https://")) and ".lab.internal" in w["url"]
        assert w["expect"] == 200


def test_expiries_dates_parse_and_match_their_sources() -> None:
    assert EXPIRIES["warn_days"] == 14
    keys = [e["key"] for e in EXPIRIES["expiries"]]
    assert len(keys) == len(set(keys)) and len(keys) >= 5
    for e in EXPIRIES["expiries"]:
        d = datetime.date.fromisoformat(e["expires"])
        assert d > datetime.date(2026, 9, 9), e["key"]
    by_key = {e["key"]: e for e in EXPIRIES["expiries"]}
    pem = (ROOT / "docs" / "lab-root-ca.crt").read_text()
    not_after = ssl.cert_time_to_seconds(_not_after(pem))
    assert datetime.datetime.fromtimestamp(not_after, datetime.UTC).date().isoformat() == by_key["lab-root-ca"]["expires"]
    assert by_key["eve-ng-pro-licence"]["expires"] in (ROOT / "docs" / "resource-budget.md").read_text()
    for pc in ("br1-pc01", "br2-pc01"):
        assert datetime.date.fromisoformat(by_key[f"win11-{pc}"]["expires"]) == datetime.date(2026, 9, 7) + datetime.timedelta(days=90)


def _not_after(pem: str) -> str:
    out = subprocess.run(["openssl", "x509", "-noout", "-enddate"], input=pem, capture_output=True, text=True, check=True).stdout
    return out.strip().split("=", 1)[1]


def test_prometheus_jobs_and_alerts_are_declared_once() -> None:
    jobs = [j["job"] for j in OBS["prometheus"]["jobs"]]
    assert len(jobs) == len(set(jobs))
    # ha2-<role> counts the production VMs of that role: the official dashboard's exporters follow the
    # production environment rather than the dev-stack (ADR 0055), so their counts come from the HA2 oracle
    ha2 = yaml.safe_load((ROOT / "itential" / "ha2" / "versions.yaml").read_text())
    roles = {f"ha2-{v['role']}" for v in ha2["vms"]}
    for j in OBS["prometheus"]["jobs"]:
        assert isinstance(j["count"], int) or j["count"] in {"nodes", "devices", "eos", "ios-xe", "web_checks"} | roles, j
    rules = [doc for f in ("prometheus-rules.yaml", "expiry-rules.yaml", "bgp-rules.yaml")  # the lab's and the generated ones
             for doc in yaml.safe_load_all((ROOT / "k8s" / "observability" / "manifests" / f).read_text()) if doc]
    alerts = {r["alert"]: r for doc in rules for g in doc["spec"]["groups"] for r in g["rules"] if "alert" in r}
    for a in OBS["prometheus"]["alerts"]:
        assert a["name"] in alerts, a
        assert alerts[a["name"]]["for"] == a["for"]
    assert 'job="blackbox-icmp"' in alerts["LabDeviceDown"]["expr"]


# --- device lines: topology templates and Golden Config read the same addresses --------------------------


def test_lab_observability_block_matches_ipam() -> None:
    o = TOPO["lab"]["observability"]
    loki = next(a for a in IPAM["addresses"] if a["hostname"] == "loki")
    assert o["syslog"] == loki["address"] == VERSIONS["vips"]["loki"]
    # snmp-exporter polls as the device user, still named for Zabbix (renaming it is a push to every device, ADR 0071)
    snmp = yaml.safe_load((ROOT / "k8s" / "observability" / "values" / "snmp-exporter.yaml").read_text())
    assert o["snmp"]["user"] == "zabbix" and f"username: {o['snmp']['user']}" in yaml.safe_dump(snmp)
    assert o["snmp"]["group"] and o["snmp"]["view"]
    assert o["gnmi"]["port"] == OBS["gnmic"]["port"] == 6030  # EOS default: the snippet does not set it (not shown in a running config)


def test_rendered_configs_carry_the_telemetry_lines_and_no_secret() -> None:
    os.environ["AUTOMATION_PASSWORD"] = "unit-test-only"
    from eve.build import load_topology, render_config

    topo = load_topology()
    syslog = topo["lab"]["observability"]["syslog"]
    for name, node in topo["nodes"].items():
        if node["platform"] == "c8000v":
            cfg = render_config("c8000v", name, node, topo)
            assert f"logging host {syslog} vrf MGMT" in cfg and "logging source-interface GigabitEthernet1 vrf MGMT" in cfg
            assert "snmp-server view LAB iso included" in cfg and "snmp-server group LAB v3 priv read LAB" in cfg
            assert "snmp-server user" not in cfg, "the SNMPv3 user (with passphrases) is pushed from .env only"
        elif node["platform"] == "veos":
            cfg = render_config("veos", name, node, topo)
            assert f"logging vrf MGMT host {syslog}" in cfg and "logging vrf MGMT source-interface Management1" in cfg
            assert "snmp-server vrf MGMT" in cfg and "snmp-server group LAB v3 priv read LAB" in cfg
            assert "management api gnmi" in cfg and "transport grpc default" in cfg
            assert "snmp-server user" not in cfg


def test_generated_configs_are_current() -> None:
    """topology/generated/configs/ is the committed reference tests/test_topology.py compares against."""
    os.environ["AUTOMATION_PASSWORD"] = "unit-test-only"
    from eve.build import load_topology, render_config

    topo = load_topology()
    for name, node in topo["nodes"].items():
        if node["platform"] not in ("c8000v", "veos"):
            continue
        ref = ROOT / "topology" / "generated" / "configs" / f"{name}.cfg"
        assert ref.exists(), ref
        assert "snmp-server" in ref.read_text(), f"{ref} predates the observability lines: regenerate it"


def test_golden_config_checks_the_telemetry_lines() -> None:
    syslog = TOPO["lab"]["observability"]["syslog"]
    ios = (ROOT / "itential" / "golden-config" / "cisco-ios" / "base.gc").read_text()
    eos = (ROOT / "itential" / "golden-config" / "arista-eos" / "base.gc").read_text()
    assert f"<e/>logging host {syslog} vrf MGMT" in ios and "<e/>snmp-server group LAB v3 priv read LAB" in ios
    assert f"<e/>logging vrf MGMT host {syslog}" in eos and "<e/>snmp-server vrf MGMT" in eos and "<e/>management api gnmi" in eos


def test_snippet_render_for_the_push_play_includes_the_user_only_with_secrets() -> None:
    from eve.build import load_topology, render_snippet

    topo = load_topology()
    plain = render_snippet("c8000v", "br1-wan01", topo)
    assert "snmp-server user" not in plain and "logging host" in plain
    secret = render_snippet("c8000v", "br1-wan01", topo, snmpv3={"auth": "A" * 12, "priv": "P" * 12})
    assert "snmp-server user zabbix LAB v3 auth sha AAAAAAAAAAAA priv aes 128 PPPPPPPPPPPP" in secret
    eos = render_snippet("veos", "dc1-spine01", topo, snmpv3={"auth": "A" * 12, "priv": "P" * 12})
    assert "snmp-server user zabbix LAB v3 auth sha AAAAAAAAAAAA priv aes PPPPPPPPPPPP" in eos


# --- wiring -----------------------------------------------------------------------------------------------


def test_plays_verify_and_make_are_wired() -> None:
    plays = ROOT / "ansible" / "playbooks"
    for p in ("observability.yml", "observability-hosts.yml", "observability-devices.yml"):
        assert (plays / p).exists(), p
    mk = (ROOT / "Makefile").read_text()
    assert "phase-observability:" in mk and "observability-devices.yml" in mk
    v = ROOT / "verify" / "test-07-observability.sh"
    assert v.exists() and os.access(v, os.X_OK)
    text = v.read_text()
    for crit in ("S7.1", "S7.2", "S7.3", "S7.4", "S7.6", "S7.7", "S7.8"):
        assert crit in text, crit
    assert "VERIFY_DRILLS" in text and "tokens.sh" not in text
    req = yaml.safe_load((ROOT / "requirements.yml").read_text())
    assert "community.zabbix" not in {c["name"] for c in req["collections"]}  # ADR 0071


def test_versions_oracle_is_the_only_place_charts_are_pinned() -> None:
    play = (ROOT / "ansible" / "playbooks" / "observability.yml").read_text()
    assert "k8s/observability/versions.yaml" in play
    for spec in VERSIONS["components"].values():
        if "chart_version" in spec:
            assert str(spec["chart_version"]) not in play, "chart versions come from versions.yaml, never the play"


# --- the Platform exporter parses the measured API shapes (ADR 0051 context) ----------------------------


def _exporter():
    spec = importlib.util.spec_from_file_location("exporter", ROOT / "observability" / "itential-exporter" / "exporter.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_exporter_renders_the_measured_shapes() -> None:
    ex = _exporter()
    jobs = json.loads(
        '{"results":[{"workflow":{"name":"Back Up All Device Configs"},"metrics":[{"jobsComplete":8,"totalRunTime":536701,'
        '"startDate":"2026-09-08T00:21:07.603Z"}],"jobsComplete":8,"totalRunTime":536701,"totalTimeSaved":1863299},'
        '{"workflow":{"name":"Add Branch VLAN"},"metrics":[{"jobsComplete":37,"totalRunTime":1347488,'
        '"startDate":"2026-09-07T17:09:59.152Z","slaTargetsMissed":1}],"jobsComplete":37,"totalRunTime":1347488}],'
        '"skip":0,"limit":3,"total":2}'
    )
    tasks = json.loads(
        '{"results":[{"global":true,"name":"sendCommand","taskType":"automatic","app":"GatewayManager","metrics":'
        '[{"totalErrorRunTime":278,"totalErrors":3,"startDate":"2026-09-07T16:37:19.393Z","totalSuccessRunTime":1014114,'
        '"totalSuccesses":181}]},{"global":false,"name":"sendCommand","app":"GatewayManager","workflow":{"name":"wf-x"},'
        '"taskId":"1a","metrics":[{"totalSuccesses":5}]}],"skip":0,"limit":2,"total":2}'
    )
    apps = json.loads('{"results":[{"id":"AgentExecutionEngine","state":"RUNNING","connection":null}]}')
    adapters = json.loads('{"results":[{"id":"NetBox","state":"RUNNING","connection":{"state":"ONLINE"}}]}')
    text = ex.render(jobs["results"], tasks["results"], apps["results"], adapters["results"], up=1,
                     job_status={"complete": 300, "error": 4, "canceled": 7, "running": 2})
    assert 'itential_workflow_jobs_complete_total{workflow="Back Up All Device Configs"} 8' in text
    assert 'itential_workflow_sla_missed_total{workflow="Add Branch VLAN"} 1' in text
    assert 'itential_workflow_sla_missed_total{workflow="Back Up All Device Configs"} 0' in text
    assert 'itential_task_errors_total{app="GatewayManager",task="sendCommand"} 3' in text
    assert 'itential_task_successes_total{app="GatewayManager",task="sendCommand"} 181' in text
    assert text.count('itential_task_successes_total{app="GatewayManager",task="sendCommand"}') == 1, "per-workflow rows must not duplicate the series"
    assert 'itential_application_running{app="AgentExecutionEngine"} 1' in text
    assert 'itential_adapter_online{adapter="NetBox"} 1' in text and "itential_up 1" in text
    assert "# TYPE itential_workflow_jobs_complete_total gauge" in text
    # the official dashboard's series (ADR 0052)
    assert 'itential_job_status_total{status="running"} 2' in text and "itential_job_start 313" in text
    assert "itential_job_complete 300" in text and "itential_job_error 4" in text and "itential_job_cancel 7" in text
    assert "itential_task_complete 181" in text and "itential_task_error 3" in text and "itential_task_start 184" in text
    assert "itential_watcher_reconnects_total 0" in text


def test_vendored_dashboard_is_the_published_revision() -> None:
    import hashlib

    for name, pin in VERSIONS["vendored_dashboards"].items():
        path = ROOT / "observability" / "grafana" / "dashboards" / f"{name}.json"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == pin["sha256"], f"{name}: not the pinned grafana.com revision"
        doc = json.loads(path.read_text())
        assert doc["uid"] == pin["uid"]
        assert name in OBS["grafana"]["vendored"]
    rows = _manifest_rows("### 4.3 Observability", "### 4.4")
    assert "25527" in rows["Itential Platform Monitoring dashboard"][1]
    it = yaml.safe_load((ROOT / "itential" / "versions.yaml").read_text())
    assert "monitoring" in it["stack"]["profiles"]
    for key, label in (("node_exporter", "node_exporter"), ("process_exporter", "process_exporter"), ("redis_exporter", "redis_exporter"), ("mongodb_exporter", "mongodb_exporter")):
        assert it["images"][key]["tag"] in rows[label][1], label
        assert it["images"][key]["repository"].split("/", 1)[1] in rows[label][3], label
    override = (ROOT / "itential" / "compose.override.yml").read_text()
    for svc in ("node-exporter", "process-exporter", "redis-exporter", "mongodb-exporter"):
        assert f"  {svc}:" in override and 'profiles: ["monitoring"]' in override
    jobs = {j["job"] for j in OBS["prometheus"]["jobs"]}
    assert {"iap_exporter", "node_exporter", "process_exporter", "redis_exporter", "mongo_exporter"} <= jobs


# --- ADR 0057 / PID 1.23: monitoring follows the estate ----------------------------------------------------
MAKEFILE = ROOT / "Makefile"
# A phase that registers a host: the NetBox VM play is the repo's one way in (ADR 0002 - nothing gets an
# address that is not reserved in NetBox first), so naming it is what makes a phase host-adding.
REGISTERS_A_HOST = "netbox-vms.yml"
REFRESH = "observability-refresh"


def _phase_order() -> list[str]:
    mk = MAKEFILE.read_text()
    line = next(ln for ln in mk.splitlines() if ln.startswith("PHASES"))
    return line.split(":=", 1)[1].split()


def _phase_bodies() -> dict[str, str]:
    """Each implemented `phase-*:` target and the recipe under it. The unimplemented phases are a pattern
    rule that fails loud, so they never appear here - a phase joins this test when it is built."""
    mk = MAKEFILE.read_text().splitlines()
    bodies, current = {}, None
    for ln in mk:
        m = re.match(r"^(phase-[a-z0-9-]+):", ln)
        if m:
            current = m.group(1)
            bodies[current] = ""
        elif current and (ln.startswith("\t") or ln.startswith("    ")):
            bodies[current] += ln + "\n"
        elif ln and not ln.startswith((" ", "\t", "#")):
            current = None
    return bodies


def test_the_refresh_target_exists_and_skips_the_governed_device_pushes() -> None:
    mk = MAKEFILE.read_text()
    assert re.search(rf"^{REFRESH}:", mk, re.M), f"the Makefile needs an `{REFRESH}` target (ADR 0057)"
    body = _phase_bodies().get(REFRESH, "") or mk.split(f"{REFRESH}:", 1)[1].split("\n\n", 1)[0]
    assert "observability.yml" in body and "observability-hosts.yml" in body
    assert "observability-devices.yml" not in body, \
        "the refresh must not start governed device pushes: they raise a Work Center card per device (ADR 0057 decision 1)"


def test_every_later_phase_that_adds_a_host_refreshes_observability() -> None:
    """The defect ADR 0057 closes. Phase 8 registered eleven VMs after phase 7 built the monitoring that
    sizes itself from NetBox, and nothing re-ran it - so S7.1 and S7.2 were red from the moment it merged
    and the phase that broke them could not see it. Five later phases would each do the same."""
    order = _phase_order()
    after_obs = order[order.index("observability") + 1:]
    bodies = _phase_bodies()
    for phase in after_obs:
        body = bodies.get(f"phase-{phase}")
        if body is None or REGISTERS_A_HOST not in body:
            continue  # not implemented yet, or it adds no host
        assert REFRESH in body, (
            f"phase-{phase} registers a host but never refreshes observability, so Prometheus - sized from "
            f"NetBox - will not know about it. Append `make {REFRESH}` (ADR 0057)."
        )


def test_zabbix_stays_removed_and_the_plays_delete_what_it_left() -> None:
    """ADR 0071 (owner, 2026-10-04): Zabbix served no purpose and its database volume had filled. Nothing installs
    it again, and because dropping a chart or a manifest deletes nothing live, the plays name every leftover."""
    assert "zabbix" not in VERSIONS["components"] and "zabbix" not in VERSIONS["vips"] and "zabbix_db" not in VERSIONS
    assert "zabbix" not in OBS and not (ROOT / "observability" / "zabbix").exists()
    for f in ("values/zabbix.yaml", "manifests/zabbix-db.yaml"):
        assert not (ROOT / "k8s" / "observability" / f).exists(), f
    kps = (ROOT / "k8s" / "observability" / "values" / "kube-prometheus-stack.yaml").read_text()
    values = yaml.safe_load(kps)["grafana"]
    assert values["plugins"] == [] and "envFromSecret" not in values
    assert {"name": "Zabbix", "orgId": 1} in values["deleteDatasources"]
    assert not [d for d in values["additionalDataSources"] if "zabbix" in d["type"]]
    play = (ROOT / "ansible" / "playbooks" / "observability.yml").read_text()
    for removal in ("name: zabbix\n        release_namespace: \"{{ obs_namespace }}\"\n        state: absent",
                    "kind: Cluster\n        namespace: \"{{ obs_namespace }}\"\n        name: zabbix-db",
                    "label_selectors: [cnpg.io/cluster=zabbix-db]", "name: traefik-vip-zabbix", "name: grafana-env",
                    "kind: Probe, namespace: \"{{ obs_namespace }}\", name: http-zabbix"):
        assert removal in play, removal
    assert "community.zabbix" not in play and "ZABBIX_" not in play
    hosts = (ROOT / "ansible" / "playbooks" / "observability-hosts.yml").read_text()
    assert "name: [zabbix-agent2, zabbix-release]" in hosts and "purge: true" in hosts
    assert "zabbix_agent2.d" not in hosts and "rsyslog" in hosts


def test_grafana_is_recreated_not_rolled() -> None:
    """2026-10-04: Grafana's Longhorn volume attaches to one node at a time, so a rolling update deadlocks - the new pod
    waits for the volume the old pod keeps until the new one is ready - and the kps upgrade timed out at 15 minutes.
    Recreate stops the old pod first (Grafana is down for about a minute per change)."""
    values = yaml.safe_load((ROOT / "k8s" / "observability" / "values" / "kube-prometheus-stack.yaml").read_text())
    grafana = values["grafana"]
    assert grafana["persistence"]["enabled"] is True and grafana["persistence"]["storageClassName"] == "longhorn"
    assert grafana["deploymentStrategy"] == {"type": "Recreate"}


def test_the_retained_zabbix_db_volume_is_deleted_too() -> None:
    """The longhorn StorageClass reclaims with Retain, so deleting the PVC left zabbix-db's PV Released and its ~21 GB
    Longhorn volume on disk (measured 2026-10-04). The play deletes the PV bound to zabbix-db-1, then that volume."""
    play = (ROOT / "ansible" / "playbooks" / "observability.yml").read_text()
    pv = play.index("The retained zabbix-db PersistentVolume deleted")
    lh = play.index("Its Longhorn volume deleted")
    assert play.index("No zabbix-db volume left") < pv < lh
    assert "selectattr('spec.claimRef.name', 'equalto', 'zabbix-db-1')" in play  # only that PV, never another
    assert "kind: Volume" in play and "api_version: longhorn.io/v1beta2" in play


def test_the_zabbix_app_files_are_removed_from_grafanas_volume() -> None:
    """An empty plugin list stops new installs but loads what is already on the volume (measured 2026-10-04: the app
    still loaded). The image has no shell, so Grafana's own CLI removes it - only when it is listed - and the pod
    is replaced to unload it."""
    play = (ROOT / "ansible" / "playbooks" / "observability.yml").read_text()
    listed = play.index("The Zabbix app on Grafana's volume")
    removed = play.index("The Zabbix app removed from Grafana's volume")
    restart = play.index("Grafana restarted without it")
    assert listed < removed < restart
    assert "plugins remove alexanderzobnin-zabbix-app" in play and "plugins ls" in play
    assert "'alexanderzobnin-zabbix-app' in zbx_app.stdout" in play


def test_grafana_never_replaces_its_bundled_plugins_at_runtime() -> None:
    """Production 2026-10-04 to 10-06: two seconds after every start, Grafana 13's background installer tried to update
    the bundled datasource plugins (prometheus 13.1.7 -> 13.2.2, loki ...), took each out of service first, then failed
    to delete it ("unlinkat /usr/share/grafana/data/plugins-bundled/prometheus: read-only file system", the distroless
    image). Every Prometheus and Loki panel answered 404 "Plugin not registered" for ~38 h. Grafana runs the plugins its
    pinned image ships, nothing fetched at runtime."""
    values = yaml.safe_load((ROOT / "k8s" / "observability" / "values" / "kube-prometheus-stack.yaml").read_text())
    assert values["grafana"]["grafana.ini"]["plugins"]["preinstall_disabled"] is True
    assert values["grafana"]["plugins"] == []


def test_the_verify_asks_each_grafana_datasource_to_answer() -> None:
    """Provisioned is not working: S7.5-prep passed for the whole outage above. Each datasource's health must be OK."""
    text = (ROOT / "verify" / "test-07-observability.sh").read_text()
    c2b = text[text.index("c2b() {"):text.index('check "S7.5-prep')]
    assert "/api/datasources/uid/" in c2b and "/health" in c2b and '"OK"' in c2b
