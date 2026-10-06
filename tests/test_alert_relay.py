"""The alert relay (R6, ADR 0072): Alertmanager cannot authenticate to an Operations Manager endpoint trigger (the
token goes in the URL and expires; a Bearer header is refused, Basic needs TLS at the Platform - probes 2026-10-05),
so this small in-cluster service takes Alertmanager's webhook, logs in, and starts the workflow the route table names.
Only allow-listed alerts, only firing ones, only pattern-checked label values reach the Platform."""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("alert_relay", ROOT / "observability" / "alert-relay" / "relay.py")
relay = importlib.util.module_from_spec(_SPEC)
sys.modules["alert_relay"] = relay  # @dataclass looks its module up here
_SPEC.loader.exec_module(relay)
# observability.yaml alert_relay.routes: per alert, the trigger route and which payload field each alert label fills
RAW_ROUTES = {"LabAwsTunnelDown": {"route": "diagnose-aws-vpn-outage",
                                   "labels": {"device": "device", "interface": "ifDescr"}},
              # R10: the fabric BGP outage loop (ADR 0073)
              "LabBgpSessionDown": {"route": "diagnose-fabric-bgp-outage",
                                    "labels": {"device": "device", "neighbor": "neighbor", "vrf": "vrf", "peer": "peer"}}}
ROUTES = relay.parse_routes({"LabAwsTunnelDown": RAW_ROUTES["LabAwsTunnelDown"]})
BGP_ROUTES = relay.parse_routes(RAW_ROUTES)


def _doc(status: str = "firing", **labels: str) -> dict:
    alert_labels = {"alertname": "LabAwsTunnelDown", "device": "dc1-wan01", "ifDescr": "Tunnel10", **labels}
    return {"version": "4", "status": status, "receiver": "itential-outage-relay",
            "alerts": [{"status": status, "labels": alert_labels, "annotations": {"summary": "x"},
                        "startsAt": "2026-10-05T02:00:00.000Z", "fingerprint": "3f2b8c1d9e0a4b5c"}]}


def test_a_firing_allow_listed_alert_becomes_one_trigger_call() -> None:
    calls = relay.trigger_calls(_doc(), ROUTES)
    assert calls == [("diagnose-aws-vpn-outage", {
        "alertname": "LabAwsTunnelDown", "device": "dc1-wan01", "interface": "Tunnel10",
        "starts_at": "2026-10-05T02:00:00Z", "fingerprint": "3f2b8c1d9e0a4b5c"})]


@pytest.mark.parametrize("doc", [
    _doc(status="resolved"),                       # the workflow re-verifies itself; resolutions start nothing
    _doc(alertname="LabDeviceDown"),               # not in the route table
    {"version": "3", "alerts": _doc()["alerts"]},  # another payload version
    {"alerts": "nope"},
    {},
])
def test_anything_else_starts_nothing(doc: dict) -> None:
    assert relay.trigger_calls(doc, ROUTES) == []


@pytest.mark.parametrize("labels", [
    {"device": "dc1-wan01; reload"}, {"ifDescr": "Tunnel10\nshutdown"}, {"device": ""}, {"ifDescr": "x" * 65},
])
def test_a_label_value_outside_its_pattern_is_dropped_with_the_alert(labels: dict) -> None:
    assert relay.trigger_calls(_doc(**labels), ROUTES) == []


def _bgp_doc(**labels: str) -> dict:
    alert_labels = {"alertname": "LabBgpSessionDown", "device": "dc1-spine01", "neighbor": "10.101.254.11",
                    "vrf": "default", "peer": "dc1-leaf01", "remote_as": "65102", "severity": "warning", **labels}
    return {"version": "4", "status": "firing", "alerts": [{
        "status": "firing", "labels": alert_labels, "startsAt": "2026-10-05T19:26:53.016Z", "fingerprint": "a1b2c3"}]}


def test_each_route_forwards_only_the_labels_its_map_names() -> None:
    """R10: a second alert with other labels - the map, not the code, says what reaches the workflow."""
    assert relay.trigger_calls(_bgp_doc(), BGP_ROUTES) == [("diagnose-fabric-bgp-outage", {
        "alertname": "LabBgpSessionDown", "device": "dc1-spine01", "neighbor": "10.101.254.11", "vrf": "default",
        "peer": "dc1-leaf01", "starts_at": "2026-10-05T19:26:53Z", "fingerprint": "a1b2c3"})]
    # the tunnel alert is unchanged by the second route
    assert relay.trigger_calls(_doc(), BGP_ROUTES) == relay.trigger_calls(_doc(), ROUTES)


@pytest.mark.parametrize("labels", [
    {"neighbor": "10.101.254.11; reload"}, {"neighbor": "dc1-leaf01"}, {"neighbor": "10.101.254"},
    {"vrf": "PROD\nshutdown"}, {"vrf": ""}, {"peer": "DC1 LEAF"}, {"device": "../etc"},
])
def test_a_bgp_label_outside_its_pattern_drops_the_alert(labels: dict) -> None:
    assert relay.trigger_calls(_bgp_doc(**labels), BGP_ROUTES) == []


@pytest.mark.parametrize("raw", [
    {"LabX": "diagnose-x"},                                                   # the old flat form
    {"LabX": {"route": "diagnose-x", "labels": {"command": "cmd"}}},           # a payload field with no pattern
    {"LabX": {"route": "diagnose x?token=", "labels": {"device": "device"}}},  # not a route name
    {"LabX": {"route": "diagnose-x", "labels": {}}},                           # nothing to identify the fault
    {"Lab X": {"route": "diagnose-x", "labels": {"device": "device"}}},        # not an alert name
])
def test_a_route_table_the_relay_cannot_check_stops_it_at_start(raw: dict) -> None:
    with pytest.raises(ValueError):
        relay.parse_routes(raw)


class _FakePlatform(BaseHTTPRequestHandler):
    seen: list = []
    trigger_status = 200

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"] or 0))
        type(self).seen.append((self.path, json.loads(body or b"{}")))
        if self.path == "/login":
            out, code = b'"session-token-123"', 200
        else:
            out, code = b'{"_id": "job1"}', type(self).trigger_status
        self.send_response(code)
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture
def platform():
    _FakePlatform.seen, _FakePlatform.trigger_status = [], 200
    srv = HTTPServer(("127.0.0.1", 0), _FakePlatform)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def _relay(platform) -> HTTPServer:
    cfg = relay.Config(url=f"http://127.0.0.1:{platform.server_port}", user="u", password="p", ca=None, routes=ROUTES)
    srv = HTTPServer(("127.0.0.1", 0), relay.handler(cfg))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _post(srv: HTTPServer, doc: dict) -> int:
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/alert", data=json.dumps(doc).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_the_relay_logs_in_and_calls_the_trigger_with_the_token_in_the_url(platform) -> None:
    srv = _relay(platform)
    try:
        assert _post(srv, _doc()) == 200
    finally:
        srv.shutdown()
    (login_path, login_body), (trigger_path, trigger_body) = _FakePlatform.seen
    assert login_path == "/login" and login_body == {"username": "u", "password": "p"}
    assert trigger_path == "/operations-manager/triggers/endpoint/diagnose-aws-vpn-outage?token=session-token-123"
    assert trigger_body["device"] == "dc1-wan01" and trigger_body["interface"] == "Tunnel10"


def test_a_platform_refusal_asks_alertmanager_to_retry(platform) -> None:
    _FakePlatform.trigger_status = 500
    srv = _relay(platform)
    try:
        assert _post(srv, _doc()) == 502
    finally:
        srv.shutdown()


def test_nothing_to_forward_logs_nobody_in(platform) -> None:
    srv = _relay(platform)
    try:
        assert _post(srv, _doc(status="resolved")) == 200
    finally:
        srv.shutdown()
    assert _FakePlatform.seen == []


def test_the_route_table_is_the_oracle(platform) -> None:
    """The relay's routes come from observability.yaml, the same names the Prometheus rule and the trigger carry."""
    obs = yaml.safe_load((ROOT / "observability" / "observability.yaml").read_text())
    assert obs["alert_relay"]["routes"] == RAW_ROUTES
    assert relay.parse_routes(obs["alert_relay"]["routes"]) == BGP_ROUTES
    assert set(RAW_ROUTES) <= {a["name"] for a in obs["prometheus"]["alerts"]}


def test_alertmanager_sends_only_the_routed_alerts_to_the_relay_and_never_resolutions() -> None:
    values = yaml.safe_load((ROOT / "k8s" / "observability" / "values" / "kube-prometheus-stack.yaml").read_text())
    cfg = values["alertmanager"]["config"]
    assert cfg["route"]["receiver"] == "lab-null"  # everything else still goes nowhere
    to_relay = [r for r in cfg["route"]["routes"] if r["receiver"] == "itential-outage-relay"]
    assert [r["matchers"] for r in to_relay] == [["alertname = LabAwsTunnelDown"], ["alertname = LabBgpSessionDown"]]
    # both ends of one broken session fire at once (probe P3): one group per device pair, so the loop starts once
    # and a later alert from the same pair comes no sooner than group_interval (the open incident then notes it)
    (bgp,) = [r for r in to_relay if r["matchers"] == ["alertname = LabBgpSessionDown"]]
    assert bgp["group_by"] == ["alertname", "pair"] and bgp["group_interval"] == "5m"
    (receiver,) = [r for r in cfg["receivers"] if r["name"] == "itential-outage-relay"]
    (hook,) = receiver["webhook_configs"]
    assert hook["url"] == "http://itential-alert-relay.observability.svc:9121/alert"
    assert hook["send_resolved"] is False and hook["max_alerts"] == 1


def test_the_play_deploys_the_relay_in_cluster_with_the_exporters_login() -> None:
    play = (ROOT / "ansible" / "playbooks" / "observability.yml").read_text()
    assert "template: templates/obs-alert-relay.yaml.j2" in play and "/alert-relay/relay.py" in play
    tpl = (ROOT / "ansible" / "playbooks" / "templates" / "obs-alert-relay.yaml.j2").read_text()
    assert "secretRef: {name: itential-exporter}" in tpl and "type: ClusterIP" in tpl
    assert "readOnlyRootFilesystem: true" in tpl and "RELAY_ROUTES" in tpl
