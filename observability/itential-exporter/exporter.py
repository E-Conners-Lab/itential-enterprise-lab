#!/usr/bin/env python3
"""Prometheus exporter for the Itential Platform's job and task metrics (PID S7.7, ADR 0051 decision 10).

The Platform's own /prometheus_metrics route (docs.itential.com, scraped as job itential-platform) carries
process-level iap_* gauges only; the job, task and health metrics sit behind the session-authenticated API. This script
(standard library only, served by http.server) logs in, refreshes every INTERVAL seconds and exposes:

  itential_workflow_jobs_complete_total{workflow}   GET /workflow_engine/jobs/metrics  results[].jobsComplete
  itential_workflow_run_time_ms_total{workflow}     results[].totalRunTime
  itential_workflow_sla_missed_total{workflow}      results[].metrics[].slaTargetsMissed (summed)
  itential_task_successes_total{app,task}           GET /workflow_engine/tasks/metrics results[global=true].metrics[].totalSuccesses
  itential_task_errors_total{app,task}              results[].metrics[].totalErrors
  itential_application_running{app}                 GET /health/applications results[].state == RUNNING
  itential_adapter_online{adapter}                  GET /health/adapters results[].connection.state == ONLINE and RUNNING
  itential_up                                       1 when the last refresh succeeded

Environment: ITENTIAL_URL, ITENTIAL_USER, ITENTIAL_PASSWORD, ITENTIAL_CA (PEM file), PORT (9120), INTERVAL (60).
The values are what the API reports (cumulative since the metrics document started), so they are exposed as
gauges: Prometheus functions like increase() still work on a monotonically growing gauge.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

PAGE = 200


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render(jobs: list, tasks: list, apps: list, adapters: list, up: int) -> str:
    """Prometheus text format from the four API result lists (the shapes measured on 2026-09-09)."""
    out: list[str] = []

    def head(name: str, help_: str, kind: str = "gauge") -> None:
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} {kind}")

    head("itential_workflow_jobs_complete_total", "Jobs completed per workflow (Platform jobs metrics)")
    for r in jobs:
        out.append(f'itential_workflow_jobs_complete_total{{workflow="{_esc(r["workflow"]["name"])}"}} {int(r.get("jobsComplete") or 0)}')
    head("itential_workflow_run_time_ms_total", "Total run time per workflow in milliseconds")
    for r in jobs:
        out.append(f'itential_workflow_run_time_ms_total{{workflow="{_esc(r["workflow"]["name"])}"}} {int(r.get("totalRunTime") or 0)}')
    head("itential_workflow_sla_missed_total", "SLA targets missed per workflow")
    for r in jobs:
        missed = sum(int(m.get("slaTargetsMissed") or 0) for m in r.get("metrics") or [])
        out.append(f'itential_workflow_sla_missed_total{{workflow="{_esc(r["workflow"]["name"])}"}} {missed}')
    head("itential_task_successes_total", "Successful task runs per application and task (global metrics)")
    head_err: list[str] = []
    # the API also returns one entry per workflow task ({global: false, workflow, taskId}) with the same app/name:
    # only the global rows are exported, so every (app, task) series is unique (measured: 200 rows, 30 global)
    for r in (r for r in tasks if r.get("global")):
        succ = sum(int(m.get("totalSuccesses") or 0) for m in r.get("metrics") or [])
        errs = sum(int(m.get("totalErrors") or 0) for m in r.get("metrics") or [])
        labels = f'app="{_esc(r.get("app") or "")}",task="{_esc(r.get("name") or "")}"'
        out.append(f"itential_task_successes_total{{{labels}}} {succ}")
        head_err.append(f"itential_task_errors_total{{{labels}}} {errs}")
    head("itential_task_errors_total", "Failed task runs per application and task")
    out.extend(head_err)
    head("itential_application_running", "1 when the Platform application reports state RUNNING")
    for a in apps:
        out.append(f'itential_application_running{{app="{_esc(a["id"])}"}} {1 if a.get("state") == "RUNNING" else 0}')
    head("itential_adapter_online", "1 when the adapter is RUNNING and its connection ONLINE")
    for a in adapters:
        conn = (a.get("connection") or {}).get("state")
        ok = a.get("state") == "RUNNING" and conn in ("ONLINE", None)
        out.append(f'itential_adapter_online{{adapter="{_esc(a["id"])}"}} {1 if ok else 0}')
    head("itential_up", "1 when the last refresh of the Platform API succeeded")
    out.append(f"itential_up {up}")
    return "\n".join(out) + "\n"


class Platform:
    def __init__(self, url: str, user: str, password: str, ca: str | None) -> None:
        self.url = url.rstrip("/")
        self.user, self.password = user, password
        ctx = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def _req(self, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, headers={"Content-Type": "application/json"})
        with self.opener.open(req, timeout=60) as resp:
            text = resp.read().decode()
            # POST /login answers with the session token as a bare string (measured on 6.5.2), not a JSON document
            return json.loads(text) if resp.headers.get_content_type() == "application/json" else {"raw": text}

    def login(self) -> None:
        self._req("/login", {"username": self.user, "password": self.password})

    def paged(self, path: str) -> list:
        rows: list = []
        skip = 0
        while True:
            d = self._req(f"{path}?limit={PAGE}&skip={skip}")
            rows += d.get("results") or []
            skip += PAGE
            if skip >= int(d.get("total") or 0):
                return rows

    def collect(self) -> str:
        try:
            jobs = self.paged("/workflow_engine/jobs/metrics")
        except urllib.error.HTTPError as e:  # session expired: log in once and retry
            if e.code not in (401, 403):
                raise
            self.login()
            jobs = self.paged("/workflow_engine/jobs/metrics")
        tasks = self.paged("/workflow_engine/tasks/metrics")
        apps = self._req("/health/applications").get("results") or []
        adapters = self._req("/health/adapters").get("results") or []
        return render(jobs, tasks, apps, adapters, up=1)


STATE = {"text": render([], [], [], [], up=0), "ok": False}


def refresh_forever(p: Platform, interval: int) -> None:
    while True:
        try:
            STATE["text"] = p.collect()
            STATE["ok"] = True
        except Exception as e:  # noqa: BLE001 - the exporter must keep serving
            print(f"refresh failed: {e}", file=sys.stderr, flush=True)
            STATE["text"] = STATE["text"].rsplit("itential_up ", 1)[0] + "itential_up 0\n"
            STATE["ok"] = False
        time.sleep(interval)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?")[0] not in ("/metrics", "/", "/-/healthy"):
            self.send_response(404)
            self.end_headers()
            return
        body = STATE["text"].encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return


def main() -> None:
    p = Platform(os.environ["ITENTIAL_URL"], os.environ["ITENTIAL_USER"], os.environ["ITENTIAL_PASSWORD"], os.environ.get("ITENTIAL_CA"))
    p.login()
    threading.Thread(target=refresh_forever, args=(p, int(os.environ.get("INTERVAL", "60"))), daemon=True).start()
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "9120"))), Handler).serve_forever()


if __name__ == "__main__":
    main()
