"""Expiries in Prometheus (ADR 0071, replacing Zabbix's lab-expiries template): observability/expiries.yaml stays the
one list; observability/expiry_rules.py turns it into a PrometheusRule - a days-left series per entry and an alert
under warn_days - and the committed manifest is what it renders. Static arithmetic on time(): no exporter."""

from __future__ import annotations

import datetime
import importlib.util
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("expiry_rules", ROOT / "observability" / "expiry_rules.py")
er = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(er)
EXPIRIES = yaml.safe_load((ROOT / "observability" / "expiries.yaml").read_text())
MANIFEST = ROOT / "k8s" / "observability" / "manifests" / "expiry-rules.yaml"
OBS = yaml.safe_load((ROOT / "observability" / "observability.yaml").read_text())


def _rules() -> list[dict]:
    return er.build(EXPIRIES)["spec"]["groups"][0]["rules"]


def test_every_entry_has_its_days_left_series_with_the_date_as_midnight_utc() -> None:
    series = [r for r in _rules() if r.get("record") == "lab:expiry_days_left"]
    assert [s["labels"]["key"] for s in series] == [e["key"] for e in EXPIRIES["expiries"]]
    for s, e in zip(series, EXPIRIES["expiries"], strict=True):
        epoch = int(datetime.datetime.fromisoformat(e["expires"] + "T00:00:00+00:00").timestamp())
        assert s["expr"] == f"({epoch} - time()) / 86400"
        assert s["labels"]["name"] == e["name"] and s["labels"]["expires"] == e["expires"]


def test_one_alert_fires_under_warn_days_for_any_entry() -> None:
    alerts = [r for r in _rules() if "alert" in r]
    assert len(alerts) == 1
    a = alerts[0]
    assert a["alert"] == "LabExpirySoon" and a["expr"] == f"lab:expiry_days_left < {EXPIRIES['warn_days']}"
    assert "{{ $labels.name }}" in a["annotations"]["summary"]
    assert a["alert"] in {x["name"] for x in OBS["prometheus"]["alerts"]}  # the verify checks every listed alert


def test_the_rules_are_picked_up_like_the_lab_rules_and_named_for_the_verify() -> None:
    doc = er.build(EXPIRIES)
    lab = yaml.safe_load((ROOT / "k8s" / "observability" / "manifests" / "prometheus-rules.yaml").read_text())
    assert doc["metadata"]["labels"] == lab["metadata"]["labels"]  # the same ruleSelector label
    assert doc["metadata"]["namespace"] == lab["metadata"]["namespace"]
    assert doc["spec"]["groups"][0]["name"].startswith("lab-")  # test-07 counts groups named lab-*


def test_the_committed_manifest_is_what_the_list_renders() -> None:
    run = subprocess.run([sys.executable, str(ROOT / "observability" / "expiry_rules.py"), "--check"],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stdout + run.stderr
    assert yaml.safe_load(MANIFEST.read_text()) == er.build(EXPIRIES)


def test_the_play_applies_the_manifest() -> None:
    play = (ROOT / "ansible" / "playbooks" / "observability.yml").read_text()
    assert 'src: "{{ k8s_dir }}/manifests/expiry-rules.yaml"' in play
