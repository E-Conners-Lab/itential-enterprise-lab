"""platform-describe (ADR 0080): the Gateway service that describes a workflow from the Platform's own copy, or a job
from its record. A fake Platform (http.server in a thread) answers /login, the Studio workflow list with a committed
document (and the whole list, for the agent's tool), one job route with a simulated run, and the script is run as the Gateway runs it: a subprocess, the
credential in the environment, one JSON line out."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "itential" / "workflows" / "platform_describe.py"
ROTATE = json.loads((ROOT / "itential" / "workflows" / "rotate-aws-vpn-key.json").read_text())
PASSWORD = "not-a-real-password-test-only"
CLIENT_ID, CLIENT_SECRET = "test-client-id", "not-a-real-client-secret-test-only"  # Itential Cloud's client credentials
JOB_ID = "6d43c80a53b14be58f8f8c8d"


def _job() -> dict:
    """A finished run of Rotate AWS VPN Key, as test_describe_job simulates one (the same shape a job record has)."""
    from test_describe_job import simulate

    return simulate(ROTATE, variables={"outcome": "rotated"})


JOB = _job()
# a document the Platform holds that build.py did not make (no step titles): it lists with its tasks and no steps
BARE = {"name": "Port Turn Up from Cisco NX-OS", "description": "Turns a port up. Vendor pack.",
        "tasks": {"workflow_start": {"name": "workflow_start"}, "a": {"name": "query", "summary": "q", "type": "operation"},
                  "workflow_end": {"name": "workflow_end"}},
        "transitions": {"workflow_start": {"a": {"state": "success", "type": "standard"}},
                        "a": {"workflow_end": {"state": "success", "type": "standard"}}}}


class FakePlatform(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *_: object) -> None:  # quiet
        pass

    def _send(self, status: int, body: object, cookie: str = "") -> None:
        data = json.dumps(body).encode() if not isinstance(body, str) else body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
        if self.headers.get("Content-Type") == "application/x-www-form-urlencoded":
            body = dict(urllib.parse.parse_qsl(raw))
        else:
            body = json.loads(raw or "{}")
        FakePlatform.seen.append(("POST", self.path, body))
        if self.path == "/login" and body == {"username": "svc-copilot", "password": PASSWORD}:
            self._send(200, "a-token", cookie="token=a-token; Path=/; HttpOnly")
        elif self.path == "/oauth/token" and body == {"grant_type": "client_credentials", "client_id": CLIENT_ID,
                                                      "client_secret": CLIENT_SECRET}:
            self._send(200, {"access_token": "a-bearer", "token_type": "bearer", "expires_in": 3600})
        else:
            self._send(401, {"message": "Invalid credentials"})

    def do_GET(self) -> None:  # noqa: N802
        FakePlatform.seen.append(("GET", self.path, self.headers.get("Cookie") or self.headers.get("Authorization")))
        if self.headers.get("Cookie") != "token=a-token" and self.headers.get("Authorization") != "Bearer a-bearer":
            return self._send(401, {"message": "no session"})
        if self.path.startswith("/automation-studio/workflows?"):
            if "equals[name]=" not in self.path:  # the whole list (ADR 0081): two documents, one without steps
                return self._send(200, {"items": [ROTATE, BARE], "total": 2})
            wanted = urllib.parse.unquote(self.path.split("equals[name]=", 1)[1])
            items = [ROTATE] if wanted == ROTATE["name"] else []
            return self._send(200, {"items": items, "total": len(items)})
        if self.path == "/operations-manager/jobs/" + JOB_ID:
            return self._send(200, {"data": JOB, "message": "found"})
        if self.path.startswith("/operations-manager/jobs/"):
            return self._send(404, {"message": "Job not found"})
        self._send(404, {"message": "no such route"})


@pytest.fixture(scope="module")
def platform():
    server = HTTPServer(("127.0.0.1", 0), FakePlatform)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def run(platform_url: str, *argv: str, env: dict | None = None) -> tuple[int, dict]:
    base = {"PATH": os.environ.get("PATH", ""), "PLATFORM_URL": platform_url, "PLATFORM_USER": "svc-copilot",
            "PLATFORM_PASSWORD": PASSWORD, "PLATFORM_CACERT": ""}
    base.update(env or {})
    r = subprocess.run([sys.executable, "-I", str(SCRIPT), *argv], capture_output=True, text=True, env=base)
    assert r.stderr == "", r.stderr
    lines = r.stdout.strip().splitlines()
    assert len(lines) == 1, "one JSON line out"
    return r.returncode, json.loads(lines[0])


def test_a_workflow_is_described_from_the_platforms_own_copy(platform: str) -> None:
    code, out = run(platform, "--action", "workflow", "--name", "Rotate AWS VPN Key")
    assert code == 0 and out["ok"] is True
    assert out["name"] == "Rotate AWS VPN Key" and out["tasks"] == 106 and out["steps"] == 12 and out["manual"] == 1
    assert out["markdown"].startswith("# Rotate AWS VPN Key\n")
    assert "as the Platform stores it" in out["markdown"] and "by platform-describe, as svc-copilot" in out["markdown"]
    assert out["html"].startswith("<h1>Rotate AWS VPN Key</h1>") and "<h3>" in out["html"]
    assert PASSWORD not in json.dumps(out)


def test_the_guide_is_the_pages_walk_through(platform: str) -> None:
    """The same document, the same words: only the provenance and the 'how it runs' lines differ from the page."""
    _, out = run(platform, "--action", "workflow", "--name", "Rotate AWS VPN Key")
    page = (ROOT / "docs" / "workflows" / "rotate-aws-vpn-key.md").read_text()
    walk = lambda text: text.split("## The walk-through")[1]  # noqa: E731
    assert walk(out["markdown"]) == walk(page)


def test_no_such_workflow_is_an_answer(platform: str) -> None:
    code, out = run(platform, "--action", "workflow", "--name", "No Such Workflow")
    assert code == 1 and out == {"ok": False, "message": "no workflow named 'No Such Workflow' on this Platform"}


def test_a_bad_name_never_reaches_the_platform(platform: str) -> None:
    FakePlatform.seen.clear()
    code, out = run(platform, "--action", "workflow", "--name", "x; drop")
    assert code == 1 and "1-80 characters" in out["message"]
    assert all(call[1] == "/login" for call in FakePlatform.seen), "only the login was sent"


def test_a_wrong_password_is_reported_without_the_password(platform: str) -> None:
    code, out = run(platform, "--action", "workflow", "--name", "Rotate AWS VPN Key", env={"PLATFORM_PASSWORD": "wrong"})
    assert code == 1 and out == {"ok": False, "message": "login as svc-copilot answered HTTP 401"}


def test_a_missing_credential_reads_nothing(platform: str) -> None:
    FakePlatform.seen.clear()
    code, out = run(platform, "--action", "workflow", "--name", "Rotate AWS VPN Key", env={"PLATFORM_PASSWORD": ""})
    assert code == 1 and out["message"].startswith("the runner has no PLATFORM_PASSWORD")
    assert FakePlatform.seen == []


def test_a_job_is_described_from_its_record_and_the_stored_workflow(platform: str) -> None:
    FakePlatform.seen.clear()
    code, out = run(platform, "--action", "job", "--id", JOB_ID)
    assert code == 0 and out["ok"] is True
    assert out["name"] == "Rotate AWS VPN Key, job 6d43c80a" and out["workflow"] == "Rotate AWS VPN Key" and out["job"] == JOB_ID
    assert out["status"] == "complete" and out["tasks"] == 106 and out["steps"] == 12 and out["ran_steps"] >= 1
    assert out["markdown"].startswith("# Rotate AWS VPN Key: job 6d43c80a\n") and "still matches" in out["markdown"]
    assert "Read from the Platform's job record at " in out["markdown"] and "as svc-copilot" in out["markdown"]
    assert out["brand"] == "the job, described" and '<img class="chart"' in out["html"]
    paths = [call[1] for call in FakePlatform.seen if call[0] == "GET"]
    assert paths == ["/operations-manager/jobs/" + JOB_ID, "/automation-studio/workflows?equals[name]=Rotate%20AWS%20VPN%20Key"]
    assert PASSWORD not in json.dumps(out)


def test_no_such_job_is_an_answer(platform: str) -> None:
    code, out = run(platform, "--action", "job", "--id", "0" * 24)
    assert code == 1 and out == {"ok": False, "message": "no job %s on this Platform" % ("0" * 24)}


def test_a_bad_job_id_never_reaches_the_platform(platform: str) -> None:
    FakePlatform.seen.clear()
    code, out = run(platform, "--action", "job", "--id", "abc; drop")
    assert code == 1 and "24 hexadecimal characters" in out["message"]
    assert all(call[1] == "/login" for call in FakePlatform.seen), "only the login was sent"


def test_the_list_is_every_stored_document_on_one_line(platform: str) -> None:
    """ADR 0081: the agent's first call when a request names a workflow loosely."""
    FakePlatform.seen.clear()
    code, out = run(platform, "--action", "list", "--which", "all")
    assert code == 0 and out["ok"] is True and out["count"] == 2
    assert out["summary"] == "2 workflows on this Platform, 1 of them stops for a person"
    rows = {r["name"]: r for r in out["workflows"]}
    assert rows["Rotate AWS VPN Key"] == {"name": "Rotate AWS VPN Key", "tasks": 106, "steps": 12, "manual": 1,
                                          "what": first_sentence(ROTATE["description"])}
    assert rows["Port Turn Up from Cisco NX-OS"]["steps"] == 0 and rows["Port Turn Up from Cisco NX-OS"]["tasks"] == 1
    assert out["markdown"].startswith("# The workflows on this Platform\n") and "| Rotate AWS VPN Key | 106 | 12 | 1 |" in out["markdown"]
    assert "by platform-describe, as svc-copilot" in out["markdown"]
    paths = [call[1] for call in FakePlatform.seen if call[0] == "GET"]
    assert paths == ["/automation-studio/workflows?limit=200"]


def test_which_names_the_workflow_or_the_job_for_the_agents_tool(platform: str) -> None:
    """ADR 0081: Describe for an Agent passes one argument whatever the action; the old ones still work."""
    _, by_name = run(platform, "--action", "workflow", "--name", "Rotate AWS VPN Key")
    _, by_which = run(platform, "--action", "workflow", "--which", "Rotate AWS VPN Key")
    assert by_which["ok"] and by_which["tasks"] == by_name["tasks"] == 106
    _, job = run(platform, "--action", "job", "--which", JOB_ID)
    assert job["ok"] and job["name"] == "Rotate AWS VPN Key, job 6d43c80a"
    code, out = run(platform, "--action", "job", "--which", "all")
    assert code == 1 and "24 hexadecimal characters" in out["message"]


def first_sentence(text: str) -> str:
    return text.split(". ", 1)[0].rstrip(".") + "."


def test_only_get_and_the_login_post_can_leave_the_script() -> None:
    spec = importlib.util.spec_from_file_location("pd", SCRIPT)
    pd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pd)
    api = pd.Platform("https://platform.invalid", "svc-copilot", PASSWORD, "", 1.0)
    for method, path in (("PUT", "/automation-studio/workflows"), ("POST", "/workflows/start"), ("DELETE", "/x")):
        with pytest.raises(RuntimeError, match="refused"):
            api._request(method, path)


CLIENT = {"PLATFORM_PASSWORD": "", "PLATFORM_CLIENT_ID": CLIENT_ID, "PLATFORM_CLIENT_SECRET": CLIENT_SECRET}


def test_client_credentials_get_a_bearer_token_instead_of_the_password(platform: str) -> None:
    FakePlatform.seen.clear()
    code, out = run(platform, "--action", "workflow", "--name", "Rotate AWS VPN Key", env=CLIENT)
    assert code == 0 and out["ok"] is True and out["name"] == "Rotate AWS VPN Key"
    posts = [(path, body) for method, path, body in FakePlatform.seen if method == "POST"]
    assert posts == [("/oauth/token", {"grant_type": "client_credentials", "client_id": CLIENT_ID,
                                       "client_secret": CLIENT_SECRET})], "one token request, never /login"
    assert {auth for method, _, auth in FakePlatform.seen if method == "GET"} == {"Bearer a-bearer"}


def test_a_wrong_client_secret_is_reported_without_the_secret(platform: str) -> None:
    code, out = run(platform, "--action", "list", "--which", "all", env={**CLIENT, "PLATFORM_CLIENT_SECRET": "wrong"})
    assert code == 1 and out == {"ok": False, "message": "the token request answered HTTP 401"}


def test_half_a_client_pair_reads_nothing(platform: str) -> None:
    FakePlatform.seen.clear()
    code, out = run(platform, "--action", "list", "--which", "all", env={**CLIENT, "PLATFORM_CLIENT_SECRET": ""})
    assert code == 1 and out["message"].startswith("the runner has no PLATFORM_CLIENT_SECRET")
    assert FakePlatform.seen == [], "the password is not a fallback once a client half is set"


def test_the_token_post_is_the_only_new_way_out() -> None:
    spec = importlib.util.spec_from_file_location("pd", SCRIPT)
    pd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pd)
    assert pd.LOGIN_POSTS == {"/login", "/oauth/token"}
    api = pd.Platform("https://platform.invalid", "svc-copilot", "", "", 1.0, CLIENT_ID, CLIENT_SECRET)
    for method, path in (("POST", "/oauth/revoke"), ("POST", "/operations-manager/jobs/start"), ("PUT", "/oauth/token")):
        with pytest.raises(RuntimeError, match="refused"):
            api._request(method, path)
