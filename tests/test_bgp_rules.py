"""BGP session monitoring for the outage loop (R10): one alert, LabBgpSessionDown{device, neighbor, vrf, peer}, from two
sources - gNMIc on the vEOS fabric and SNMP BGP4-MIB on the C8000v routers (owner decision 2026-10-05) - for exactly the
sessions topology/enterprise.yaml declares (the oracle NetBox is seeded from, ADR 0048). observability/bgp_rules.py
renders the PrometheusRule; the committed manifest is what it renders.

The rule behaviour is tested with promtool against the measured shapes (probe P3, 2026-10-05, dc1-spine01): gNMIc keeps
the previous state's series for its 2 m expiry, so on every transition an ESTABLISHED and a non-ESTABLISHED series both
read 1 for up to 2 minutes; and the leaf <-> WAN edge sessions flap on BFD for 1-2 s dozens of times a day."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("bgp_rules", ROOT / "observability" / "bgp_rules.py")
br = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(br)
MANIFEST = ROOT / "k8s" / "observability" / "manifests" / "bgp-rules.yaml"
OBS = yaml.safe_load((ROOT / "observability" / "observability.yaml").read_text())
SNMP_VALUES = yaml.safe_load((ROOT / "k8s" / "observability" / "values" / "snmp-exporter.yaml").read_text())


def _rules() -> list[dict]:
    return br.build(br.intent())["spec"]["groups"][0]["rules"]


def _intent_series() -> list[dict]:
    return [r["labels"] for r in _rules() if r.get("record") == "lab:bgp_neighbor_intent"]


# --- the intent: one series per declared session end -------------------------------------------------------------


def test_every_declared_neighbor_has_one_intent_series() -> None:
    topo = br.derive.load_topology()
    declared = {(name, n["neighbor"]) for name in topo["nodes"]
                for n in br.derive.device_context(topo, name).get("bgp", {}).get("neighbors", [])}
    series = [(s["device"], s["neighbor"]) for s in _intent_series()]
    assert len(series) == len(set(series)) == len(declared) == 38  # 18 vEOS + 20 C8000v (probes P1, 2026-10-05)
    assert set(series) == declared


def test_the_intent_names_the_vrf_the_peer_and_its_asn() -> None:
    by_key = {(s["device"], s["neighbor"]): s for s in _intent_series()}
    assert by_key[("dc1-wan01", "10.103.0.1")] == {
        "device": "dc1-wan01", "neighbor": "10.103.0.1", "vrf": "WAN", "peer": "isp-core01", "remote_as": "65000",
        "pair": "dc1-wan01--isp-core01"}
    assert by_key[("dc1-leaf01", "10.101.3.1")]["vrf"] == "PROD"
    assert by_key[("dc1-spine01", "10.101.254.11")] == {
        "device": "dc1-spine01", "neighbor": "10.101.254.11", "vrf": "default", "peer": "dc1-leaf01",
        "remote_as": "65102", "pair": "dc1-leaf01--dc1-spine01"}


def test_both_ends_of_a_session_share_one_pair_label() -> None:
    """Alertmanager groups LabBgpSessionDown by pair: one fault, two ends, one start of the outage loop."""
    for s in _intent_series():
        assert s["pair"] == "--".join(sorted((s["device"], s["peer"])))


def test_every_session_is_declared_at_both_ends() -> None:
    """A -> B means B -> A: the outage loop pairs the two ends' alerts into one incident (PR B)."""
    ends = {(s["device"], s["peer"]) for s in _intent_series()}
    assert all((peer, device) in ends for device, peer in ends)


def test_every_bgp_device_has_a_source() -> None:
    topo = br.derive.load_topology()
    for device in {s["device"] for s in _intent_series()}:
        assert topo["nodes"][device]["platform"] in br.SOURCES, device


# --- the SNMP side: a bgp module on the IOS-XE scrape --------------------------------------------------------------


def test_the_snmp_exporter_walks_the_bgp4_mib_peer_table() -> None:
    config = yaml.safe_load(SNMP_VALUES["config"])
    module = config["modules"]["bgp"]
    assert set(module["walk"]) == {"1.3.6.1.2.1.15.3.1.2", "1.3.6.1.2.1.15.3.1.3"}  # bgpPeerState, bgpPeerAdminStatus
    for metric in module["metrics"]:
        # BGP4-MIB indexes the table by the peer's address, which P1 found unique per router across its VRFs
        assert metric["indexes"] == [{"labelname": "bgpPeerRemoteAddr", "type": "InetAddressIPv4"}], metric["name"]
    assert {m["name"] for m in module["metrics"]} == {"bgpPeerState", "bgpPeerAdminStatus"}


def test_only_the_ios_xe_scrape_asks_for_the_bgp_module() -> None:
    """vEOS answers BGP4-MIB for its default VRF only (probe P3: no VRF PROD peer): gNMIc stays its source."""
    tpl = (ROOT / "ansible" / "playbooks" / "templates" / "obs-scrape.yaml.j2").read_text()
    assert "module: [if_mib{% if platform == 'ios-xe' %}, bgp{% endif %}]" in tpl


# --- the rules ------------------------------------------------------------------------------------------------------


def test_the_alert_is_declared_where_the_verify_reads_it_and_only_here() -> None:
    alerts = [r for r in _rules() if "alert" in r]
    assert [a["alert"] for a in alerts] == ["LabBgpSessionDown"]
    assert {"name": "LabBgpSessionDown", "for": alerts[0]["for"]} in OBS["prometheus"]["alerts"]
    lab = yaml.safe_load((ROOT / "k8s" / "observability" / "manifests" / "prometheus-rules.yaml").read_text())
    lab_alerts = {r.get("alert") for g in lab["spec"]["groups"] for r in g["rules"]}
    assert "LabBgpSessionDown" not in lab_alerts  # the gNMIc-only rule it replaces (grouped by a label gNMIc never sets)


def test_the_rules_are_picked_up_like_the_lab_rules() -> None:
    doc = br.build(br.intent())
    lab = yaml.safe_load((ROOT / "k8s" / "observability" / "manifests" / "prometheus-rules.yaml").read_text())
    assert doc["metadata"]["labels"] == lab["metadata"]["labels"]
    assert doc["metadata"]["namespace"] == lab["metadata"]["namespace"]
    assert doc["spec"]["groups"][0]["name"].startswith("lab-")
    assert "interval" not in doc["spec"]["groups"][0]


def test_the_committed_manifest_is_what_the_topology_renders() -> None:
    run = subprocess.run([sys.executable, str(ROOT / "observability" / "bgp_rules.py"), "--check"],
                         capture_output=True, text=True, timeout=60, check=False)
    assert run.returncode == 0, run.stdout + run.stderr
    assert yaml.safe_load(MANIFEST.read_text()) == br.build(br.intent())


def test_the_play_applies_the_manifest() -> None:
    play = (ROOT / "ansible" / "playbooks" / "observability.yml").read_text()
    assert 'src: "{{ k8s_dir }}/manifests/bgp-rules.yaml"' in play


# --- behaviour, through promtool (PROMTOOL=<path>, or promtool on PATH) ------------------------------------------

PROMTOOL = os.environ.get("PROMTOOL") or shutil.which("promtool")
GNMIC = "gnmic_bgp_network_instances_network_instance_protocols_protocol_bgp_neighbors_neighbor_state_session_state"


def _gnmic(source: str, neighbor: str, state: str, vrf: str = "default") -> str:
    return (f'{GNMIC}{{job="gnmic", source="{source}", neighbor_neighbor_address="{neighbor}", '
            f'network_instance_name="{vrf}", session_state="{state}"}}')


def _snmp(device: str, neighbor: str) -> str:
    return f'bgpPeerState{{job="snmp-ios-xe", device="{device}", bgpPeerRemoteAddr="{neighbor}"}}'


DOWN_SPINE = {"device": "dc1-spine01", "neighbor": "10.101.254.11", "vrf": "default", "peer": "dc1-leaf01",
              "remote_as": "65102", "pair": "dc1-leaf01--dc1-spine01", "severity": "warning"}
DOWN_WAN = {"device": "dc1-wan01", "neighbor": "10.101.3.2", "vrf": "default", "peer": "dc1-leaf01",
            "remote_as": "65102", "pair": "dc1-leaf01--dc1-wan01", "severity": "warning"}



def _annotations(labels: dict) -> dict:
    return {
        "summary": f"BGP session {labels['device']} -> {labels['neighbor']} ({labels['peer']}, VRF {labels['vrf']}) "
                   "is not Established",
        "description": "Not Established for 2 minutes (SNMP BGP4-MIB on IOS-XE, gNMIc on vEOS); "
                       "declared in topology/enterprise.yaml.",
    }


CASES = {
    # SNMP: established (6), then idle (1) from 30 s on: fires once it has been down for 2 m
    "snmp_down": {
        "series": [{"series": _snmp("dc1-wan01", "10.101.3.2"), "values": "6 6 1x30"}],
        "alerts": {"2m": [], "3m": [DOWN_WAN]},
        "exprs": {"1m": [{"labels": "lab:bgp_session_up{" + ", ".join(
            f'{k}="{v}"' for k, v in DOWN_WAN.items() if k != "severity") + "}", "value": 0}]},
    },
    # gNMIc, measured: the IDLE series appears at 30 s NEXT TO the old ESTABLISHED one, which expires at 1m30s.
    # ESTABLISHED wins while it is there, so the session reads down from 1m45s and the alert fires 2 m later
    "gnmic_down_with_the_stale_established_series": {
        "series": [{"series": _gnmic("dc1-spine01", "10.101.254.11", "ESTABLISHED"), "values": "1x6 stale"},
                   {"series": _gnmic("dc1-spine01", "10.101.254.11", "IDLE"), "values": "_ _ 1x30"}],
        "alerts": {"3m": [], "4m": [DOWN_SPINE]},
        "exprs": {"1m": [{"labels": "lab:bgp_session_up{" + ", ".join(
            f'{k}="{v}"' for k, v in DOWN_SPINE.items() if k != "severity") + "}", "value": 1}]},
    },
    # recovery: ESTABLISHED is back at 1 m while the IDLE series lingers to its expiry: up at once
    "gnmic_recovery_reads_up_at_once": {
        "series": [{"series": _gnmic("dc1-spine01", "10.101.254.11", "IDLE"), "values": "1x12 stale"},
                   {"series": _gnmic("dc1-spine01", "10.101.254.11", "ESTABLISHED"), "values": "_x3 1x30"}],
        "alerts": {"5m": []},
        "exprs": {"1m": [{"labels": "lab:bgp_session_up{" + ", ".join(
            f'{k}="{v}"' for k, v in DOWN_SPINE.items() if k != "severity") + "}", "value": 1}]},
    },
    # a 1-2 s BFD flap caught by one gNMIc sample: the IDLE series lingers 2 m, ESTABLISHED never stops: no alert
    "gnmic_bfd_flap_never_fires": {
        "series": [{"series": _gnmic("dc1-leaf01", "10.101.3.1", "ESTABLISHED", vrf="PROD"), "values": "1x40"},
                   {"series": _gnmic("dc1-leaf01", "10.101.3.1", "ACTIVE", vrf="PROD"), "values": "_x4 1x8 stale"}],
        "alerts": {"3m": [], "6m": []},
    },
    # an SNMP flap caught by one 60 s scrape: never down for 2 m
    "snmp_flap_never_fires": {
        "series": [{"series": _snmp("dc1-wan02", "10.101.3.6"), "values": "6x4 1x3 6x30"}],
        "alerts": {"3m": [], "6m": []},
    },
    # a peer the topology does not declare is not this alert's (a stray session is a config question, not an outage)
    "an_undeclared_peer_is_ignored": {
        "series": [{"series": _snmp("dc1-wan01", "10.9.9.9"), "values": "1x30"}],
        "alerts": {"4m": []},
    },
}


@pytest.mark.skipif(PROMTOOL is None, reason="promtool not found: set PROMTOOL or put promtool on PATH")
@pytest.mark.parametrize("name", sorted(CASES))
def test_the_rules_behave_as_measured(name: str, tmp_path: Path) -> None:
    case = CASES[name]
    rules = tmp_path / "rules.yaml"
    rules.write_text(yaml.safe_dump({"groups": br.build(br.intent())["spec"]["groups"]}, sort_keys=False))
    test = {
        "rule_files": [str(rules)],
        "evaluation_interval": "15s",
        "tests": [{
            "interval": "15s",
            "input_series": case["series"],
            "alert_rule_test": [{"eval_time": t, "alertname": "LabBgpSessionDown",
                                 "exp_alerts": [{"exp_labels": a, "exp_annotations": _annotations(a)} for a in alerts]}
                                for t, alerts in case["alerts"].items()],
            "promql_expr_test": [{"expr": "lab:bgp_session_up", "eval_time": t, "exp_samples": samples}
                                 for t, samples in case.get("exprs", {}).items()],
        }],
    }
    spec = tmp_path / "test.yaml"
    spec.write_text(yaml.safe_dump(test, sort_keys=False))
    run = subprocess.run([PROMTOOL, "test", "rules", str(spec)], capture_output=True, text=True, timeout=60, check=False)
    assert run.returncode == 0, run.stdout + run.stderr


def test_the_fabric_dashboard_shows_every_declared_session_from_both_sources() -> None:
    """The BGP panels read the combined series, not raw gNMIc (which shows stale states and no C8000v session)."""
    import json

    dash = json.loads((ROOT / "observability" / "grafana" / "dashboards" / "fabric.json").read_text())
    bgp = [p for p in dash["panels"] if "BGP" in p["title"]]
    assert [t["expr"] for p in bgp for t in p["targets"]] == [
        "sum by (device) (lab:bgp_session_up)", "lab:bgp_session_up == 0"]
    # drill 2 (2026-10-06): as a range query the table showed one series and history rows; instant: one row per end
    down = bgp[1]["targets"][0]
    assert down["instant"] is True and down["format"] == "table" and down["range"] is False


def test_the_interface_errors_panel_queries_in_and_out_apart() -> None:
    """increase() drops the metric name: in and out errors in one query collided on the same label set."""
    import json

    dash = json.loads((ROOT / "observability" / "grafana" / "dashboards" / "fabric.json").read_text())
    (errors,) = [p for p in dash["panels"] if "errors" in p["title"]]
    assert [t["expr"] for t in errors["targets"]] == [
        'increase({__name__=~"gnmic_interfaces.*in_errors"}[5m])',
        'increase({__name__=~"gnmic_interfaces.*out_errors"}[5m])']


def test_the_verify_checks_production_against_the_topology_and_snmp() -> None:
    text = (ROOT / "verify" / "test-07-observability.sh").read_text()
    assert 'check "S7.3b' in text and "lab:bgp_session_up" in text and "1.3.6.1.2.1.15.3.1.2" in text


def test_the_snmp_exporter_restarts_when_its_modules_change() -> None:
    """Production 2026-10-05: the R10 converge rewrote the exporter's ConfigMap, but the pod kept its 25-day-old
    config (snmp_exporter does not watch the file), so every IOS-XE scrape asking for module bgp got 400 until a manual
    rollout restart. A checksum of the config as a pod annotation makes Helm roll the pod exactly when it changes."""
    play = yaml.safe_load((ROOT / "ansible" / "playbooks" / "observability.yml").read_text())
    charts = next(p["vars"]["charts"] for p in play if "charts" in p.get("vars", {}))
    (snmp,) = [c for c in charts if c["key"] == "snmp_exporter"]
    annotation = snmp["overrides"]["podAnnotations"]["checksum/config"]
    assert "values/snmp-exporter.yaml" in annotation and ".config" in annotation and "hash('sha256')" in annotation
