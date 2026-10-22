#!/usr/bin/env python3
"""The skills eval suite's scratch routers (topology/skills-lab.yaml, ADR 0084): bootstrap helpers for
ansible/playbooks/skills-lab-devices.yml and the Makefile. Never prints a password.

  scripts/skills-lab-device.py ensure-secrets                 # add missing SKILLS_LAB_* passwords to .env (mode 600)
  scripts/skills-lab-device.py wait-ssh <ip>                   # until the router accepts the automation login
  scripts/skills-lab-device.py accounts <ip>                  # restconf-reader + judge at privilege 1, NACM read rules
  scripts/skills-lab-device.py pins <ip>                       # HTTPS certificate and SSH host key SHA-256 (not secret)
  scripts/skills-lab-device.py check-readers <ip> <cert-sha256>  # each reader can GET and is refused a PATCH
  scripts/skills-lab-device.py baseline <ip>                   # write memory; copy running-config bootflash:baseline.cfg
  scripts/skills-lab-device.py creds <dest .env>               # the eval credentials into an untracked file, no echo

Passwords come from the environment (.env via make): SKILLS_LAB_AUTOMATION_PASSWORD (the automation user the scratch
day-0 renders), SKILLS_LAB_READER_PASSWORD (restconf-reader, used by the Gateway's restconf-get service) and
SKILLS_LAB_JUDGE_PASSWORD (judge, used only by the eval suite's independent judge). The routers are new and lab-only
on the OOB network, so the first SSH and TLS contact cannot be pinned; this tool records both fingerprints for the
pins the later steps enforce (the owner's no-trust-on-first-use rule applies from the first commit of the pins).
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import secrets
import socket
import ssl
import sys
import time
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
SECRET_VARS = (
    "SKILLS_LAB_AUTOMATION_PASSWORD",
    "SKILLS_LAB_READER_PASSWORD",
    "SKILLS_LAB_JUDGE_PASSWORD",
)
READERS = {
    "restconf-reader": "SKILLS_LAB_READER_PASSWORD",
    "judge": "SKILLS_LAB_JUDGE_PASSWORD",
}
TIMEOUT = 60
PROMPT = re.compile(r"[\r\n][\w.-]+(\(config[^)]*\))?[#>]\s*$")
HOSTNAME_PATH = "/restconf/data/Cisco-IOS-XE-native:native/hostname"


def env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(
            f"{name} missing in .env (make skills-lab-up generates the scratch passwords)"
        )
    return value


def ensure_secrets(env_file: Path = ENV_FILE) -> list[str]:
    """Append any missing scratch password to .env; return the names added. URL-safe characters only: IOS treats '?'
    as help and a space as an argument separator in `username ... secret 0 <password>`."""
    text = env_file.read_text() if env_file.exists() else ""
    present = {
        line.split("=", 1)[0]
        for line in text.splitlines()
        if "=" in line and not line.startswith("#")
    }
    added = [name for name in SECRET_VARS if name not in present]
    if added:
        lines = [
            "",
            "# Skills eval scratch lab (ADR 0084): generated once; never the production device password",
        ]
        lines += [f"{name}={secrets.token_urlsafe(24)}" for name in added]
        with env_file.open("a") as fh:
            fh.write("\n".join(lines) + "\n")
        env_file.chmod(0o600)
    return added


def _ssh(ip: str) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    # first contact with a freshly booted lab-only router; `pins` records the key for later pinning
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        ip,
        username="automation",
        password=env("SKILLS_LAB_AUTOMATION_PASSWORD"),
        look_for_keys=False,
        allow_agent=False,
        timeout=TIMEOUT,
        banner_timeout=TIMEOUT,
        auth_timeout=TIMEOUT,
    )
    return client


def _shell(client: paramiko.SSHClient, commands: list[tuple[str, str | None]]) -> str:
    """Run commands in one interactive shell; each item is (command, answer to a [confirm]/filename prompt or None).
    Returns the transcript with any password-looking secret already absent (callers never send one twice)."""
    chan = client.invoke_shell(width=250)
    out = ""

    def read_until_prompt(answer: str | None) -> str:
        # `copy` asks for a filename and, when the file exists, to confirm the overwrite: answer each new question
        buf = ""
        answered = 0
        deadline = time.time() + TIMEOUT
        while time.time() < deadline:
            if chan.recv_ready():
                buf += chan.recv(65535).decode(errors="replace")
                fresh = buf[answered:].rstrip("\n")
                if answer is not None and re.search(r"(\[confirm\]|\?\s*$)", fresh):
                    chan.send(answer + "\n")
                    answered = len(buf)
                    continue
                if PROMPT.search(buf):
                    return buf
            else:
                time.sleep(0.2)
        raise SystemExit("the router did not return to its prompt in time")

    out += read_until_prompt(None)
    chan.send("terminal length 0\n")
    out += read_until_prompt(None)
    for command, answer in commands:
        chan.send(command + "\n")
        out += read_until_prompt(answer)
    chan.close()
    return out


def wait_ssh(ip: str, minutes: int = 20) -> None:
    deadline = time.time() + minutes * 60
    last = ""
    while time.time() < deadline:
        try:
            _ssh(ip).close()
            return
        except (paramiko.SSHException, OSError) as err:
            last = type(err).__name__
            time.sleep(15)
    raise SystemExit(f"{ip}: no automation login after {minutes} minutes ({last})")


def accounts(ip: str) -> None:
    """The two read accounts at privilege 1 and Cisco's NACM read rules for privilege 1 (Cisco IOS XE 17.9.1
    and later): RESTCONF then lets them read and refuses writes."""
    client = _ssh(ip)
    try:
        lines = [("configure terminal", None)]
        lines += [
            (f"username {user} privilege 1 secret 0 {env(var)}", None)
            for user, var in READERS.items()
        ]
        lines += [
            ("end", None),
            (
                "request platform software yang-management nacm populate-read-rules privilege 1",
                None,
            ),
        ]
        transcript = _shell(client, lines)
    finally:
        client.close()
    for var in READERS.values():
        transcript = transcript.replace(env(var), "<redacted>")
    if re.search(r"% ?(Invalid|Incomplete|Ambiguous)", transcript):
        raise SystemExit(f"{ip}: the router refused a command:\n{transcript}")
    print(
        f"{ip}: restconf-reader and judge at privilege 1; NACM read rules for privilege 1"
    )


def pins(ip: str) -> dict[str, str]:
    """Fingerprints the later steps pin: SHA-256 of the HTTPS certificate (DER) and of the SSH host key."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with (
        socket.create_connection((ip, 443), timeout=TIMEOUT) as raw,
        context.wrap_socket(raw) as tls,
    ):
        der = tls.getpeercert(binary_form=True)
    transport = paramiko.Transport((ip, 22))
    try:
        transport.start_client(timeout=TIMEOUT)
        key = transport.get_remote_server_key()
    finally:
        transport.close()
    result = {
        "https_cert_sha256": hashlib.sha256(der).hexdigest(),
        "ssh_hostkey_sha256": base64.b64encode(hashlib.sha256(key.asbytes()).digest())
        .decode()
        .rstrip("="),
        "ssh_hostkey_type": key.get_name(),
    }
    print(json.dumps({"ip": ip, **result}))
    return result


def _restconf(
    ip: str,
    cert_sha256: str,
    user: str,
    password: str,
    method: str,
    body: str | None = None,
) -> tuple[int, bytes]:
    """One RESTCONF request on a connection whose certificate matched the pin before any credential was sent."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # verification is the pin below, not a CA
    conn = http.client.HTTPSConnection(ip, 443, timeout=TIMEOUT, context=context)
    try:
        conn.connect()
        presented = hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
        if presented != cert_sha256.lower():
            raise SystemExit(
                f"{ip}: HTTPS certificate does not match the pin; no credential sent"
            )
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        headers = {
            "Authorization": f"Basic {token}",
            "Accept": "application/yang-data+json",
        }
        if body is not None:
            headers["Content-Type"] = "application/yang-data+json"
        conn.request(method, HOSTNAME_PATH, body=body, headers=headers)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def check_readers(ip: str, cert_sha256: str) -> None:
    """Each reader reads the hostname, and a PATCH of that same hostname (a no-op even if NACM failed) is refused."""
    for user, var in READERS.items():
        password = env(var)
        status, payload = _restconf(ip, cert_sha256, user, password, "GET")
        if status != 200:
            raise SystemExit(
                f"{ip}: {user} could not read over RESTCONF (HTTP {status})"
            )
        hostname = json.loads(payload)["Cisco-IOS-XE-native:hostname"]
        body = json.dumps({"Cisco-IOS-XE-native:hostname": hostname})
        refused, _ = _restconf(ip, cert_sha256, user, password, "PATCH", body)
        if refused in (200, 204):
            raise SystemExit(
                f"{ip}: {user} was allowed to write over RESTCONF: NACM read rules are not in force"
            )
        print(f"{ip}: {user} reads (200) and is refused a write (HTTP {refused})")


def baseline(ip: str) -> None:
    """Save, then capture the running configuration as the baseline the eval runner restores before every run. Runs
    after `accounts`, so the baseline keeps the two read accounts (a replace to it never removes them)."""
    client = _ssh(ip)
    try:
        transcript = _shell(
            client,
            [
                ("write memory", None),
                ("copy running-config bootflash:baseline.cfg", ""),
                ("dir bootflash:baseline.cfg", None),
            ],
        )
    finally:
        client.close()
    if "baseline.cfg" not in transcript.split("dir bootflash:baseline.cfg", 1)[-1]:
        raise SystemExit(
            f"{ip}: bootflash:baseline.cfg is not on the router after the copy"
        )
    print(f"{ip}: running configuration saved and captured as bootflash:baseline.cfg")


def creds(dest: Path) -> None:
    """Write the eval credentials the workbench reads into an untracked .env (mode 600); values never printed."""
    if dest.resolve().is_relative_to(ROOT):
        raise SystemExit(
            "refusing to write eval credentials inside this public repository"
        )
    wanted = {
        "SKILLEVAL_JUDGE_USER": "judge",
        "SKILLEVAL_JUDGE_PASSWORD": env("SKILLS_LAB_JUDGE_PASSWORD"),
    }
    for name in (
        "SVC_EVAL_PROD_PASSWORD",
        "SVC_EVAL_APPROVER_PROD_PASSWORD",
    ):  # exist once the accounts are built
        if os.environ.get(name):
            wanted[name] = os.environ[name]
    lines = dest.read_text().splitlines() if dest.exists() else []
    kept = [line for line in lines if line.split("=", 1)[0] not in wanted]
    dest.write_text("\n".join(kept + [f"{k}={v}" for k, v in wanted.items()]) + "\n")
    dest.chmod(0o600)
    print(f"wrote {', '.join(wanted)} to {dest}")


def main(argv: list[str]) -> None:
    if not argv:
        raise SystemExit(__doc__)
    command, args = argv[0], argv[1:]
    if command == "ensure-secrets":
        added = ensure_secrets()
        print(
            f"added to .env: {', '.join(added)}"
            if added
            else "scratch passwords already in .env"
        )
    elif command == "wait-ssh" and len(args) == 1:
        wait_ssh(args[0])
    elif command == "accounts" and len(args) == 1:
        accounts(args[0])
    elif command == "pins" and len(args) == 1:
        pins(args[0])
    elif command == "check-readers" and len(args) == 2:
        check_readers(args[0], args[1])
    elif command == "baseline" and len(args) == 1:
        baseline(args[0])
    elif command == "creds" and len(args) == 1:
        creds(Path(args[0]))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
