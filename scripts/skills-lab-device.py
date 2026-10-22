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
# spelling from Cisco's Programmability Command Reference, IOS XE 17.13.x:
# https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/prog/command/1713/b_1713_programmability_cr/programmability.html
NACM_POPULATE = "request platform software yang-management nacm populate-read-rules privilege 1"
NACM_ATTEMPTS = 8
NACM_RETRY_SECONDS = 15
PROBE_SUFFIX = "-nacm-check"


def env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(
            f"{name} missing in .env (make skills-lab-up generates the scratch passwords)"
        )
    return value


def _value(line: str) -> str:
    """The value of a `NAME=value  # comment` line as `. ./.env` sees it (empty when only a comment follows)."""
    value = line.split("=", 1)[1].strip()
    if value.startswith("#"):
        return ""
    value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
    return value.strip("'\"")


def write_private(path: Path, text: str) -> None:
    """Write a file that holds secrets: created at mode 600 (a temporary file then an atomic replace), so it is never
    readable by others, not even for a moment, and an existing file's looser mode does not survive."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def ensure_secrets(env_file: Path = ENV_FILE) -> list[str]:
    """Give every scratch password a value in .env; return the names generated. A `NAME=` line with no value (as
    .env.example ships) counts as missing and gets the value in place. URL-safe characters only: IOS treats '?' as
    help and a space as an argument separator in `username ... secret 0 <password>`."""
    lines = env_file.read_text().splitlines() if env_file.exists() else []
    has_value = {
        line.split("=", 1)[0].strip()
        for line in lines
        if "=" in line and not line.lstrip().startswith("#") and _value(line)
    }
    added = [name for name in SECRET_VARS if name not in has_value]
    if not added:
        return []
    fresh = {name: secrets.token_urlsafe(24) for name in added}
    out, placed = [], set()
    for line in lines:
        name = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if name in fresh and name not in placed:
            out.append(f"{name}={fresh[name]}")
            placed.add(name)
        elif name in fresh:
            continue  # a second empty assignment would override the value when .env is sourced
        else:
            out.append(line)
    missing = [n for n in added if n not in placed]
    if missing:
        out += ["", "# Skills eval scratch lab (ADR 0084): generated once; never the production device password"]
        out += [f"{name}={fresh[name]}" for name in missing]
    write_private(env_file, "\n".join(out) + "\n")
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
                if answer is None and "[confirm]" in fresh:
                    chan.send("\x03")  # do not let an unexpected question default to yes
                    raise SystemExit("the router asked for confirmation unexpectedly: " + fresh[-200:])
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
        lines += [("end", None)]
        transcript = _shell(client, lines)
    finally:
        client.close()
    for var in READERS.values():
        transcript = transcript.replace(env(var), "<redacted>")
    problem = command_refusal(transcript)
    if problem:
        raise SystemExit(f"{ip}: the router refused a command ({problem!r}):\n{transcript}")
    populate_read_rules(ip)
    print(
        f"{ip}: restconf-reader and judge at privilege 1; NACM read rules for privilege 1"
    )


def populate_read_rules(ip: str, attempts: int = NACM_ATTEMPTS, wait: float = NACM_RETRY_SECONDS) -> None:
    """Cisco's NACM read rules for privilege 1, retried: a router initialises NACM a few minutes after boot, and one
    that answers SSH before that refuses the first try (skl-r2, 2026-10-22: DMI-5-NACM_INIT at 13:00:18, after the
    refused populate at 13:00:04; skl-r1 and skl-r3 had initialised at 12:59; the retry loaded the rules)."""
    problem = None
    for attempt in range(1, attempts + 1):
        client = _ssh(ip)
        try:
            transcript = _shell(client, [(NACM_POPULATE, None)])
        finally:
            client.close()
        problem = command_refusal(transcript)
        if problem is None:
            return
        if attempt < attempts:
            time.sleep(wait)
    raise SystemExit(f"{ip}: the router refused the NACM read rules {attempts} times (last: {problem!r})")


def command_refusal(transcript: str) -> str | None:
    """The first sign that IOS XE did not accept a command: a line starting with '%', a line mentioning an error, or a
    [confirm] question left unanswered. None when the transcript is clean."""
    for line in transcript.splitlines():
        stripped = line.strip()
        if stripped.startswith("%") or re.search(r"\berror\b", stripped, re.IGNORECASE):
            return stripped
        if "[confirm]" in stripped:
            return stripped
    return None


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
    """Each reader reads the hostname and is refused a PATCH that would change it. IOS XE applies no NACM write check
    to a write that changes nothing (2026-10-22: a same-value PATCH by a privilege-1 reader got 204 while a real change
    got 403), so only a real change proves the rules; if the router takes it, the automation account puts the hostname
    back before the check fails."""
    for user, var in READERS.items():
        password = env(var)
        status, payload = _restconf(ip, cert_sha256, user, password, "GET")
        if status != 200:
            raise SystemExit(
                f"{ip}: {user} could not read over RESTCONF (HTTP {status})"
            )
        hostname = json.loads(payload)["Cisco-IOS-XE-native:hostname"]
        body = json.dumps({"Cisco-IOS-XE-native:hostname": hostname + PROBE_SUFFIX})
        refused, _ = _restconf(ip, cert_sha256, user, password, "PATCH", body)
        if refused in (200, 201, 204):
            raise SystemExit(
                f"{ip}: {user} was allowed to write over RESTCONF: NACM read rules are not in force"
                f" ({_put_hostname_back(ip, cert_sha256, hostname)})"
            )
        if refused != 403:
            # only NACM's access-denied (RFC 8040 4.2, HTTP 403) proves the read-only rules refused the write; a 400,
            # 401, 405 or 500 says nothing about them
            raise SystemExit(
                f"{ip}: {user}'s write got HTTP {refused}, not 403 (NACM access denied): read-only not proven"
            )
        print(f"{ip}: {user} reads (200) and is refused a write (HTTP {refused})")


def _put_hostname_back(ip: str, cert_sha256: str, hostname: str) -> str:
    body = json.dumps({"Cisco-IOS-XE-native:hostname": hostname})
    status, _ = _restconf(ip, cert_sha256, "automation", env("SKILLS_LAB_AUTOMATION_PASSWORD"), "PATCH", body)
    if status in (200, 204):
        return f"hostname put back to {hostname}"
    return f"putting the hostname back to {hostname} got HTTP {status}: fix it by hand"


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
    problem = baseline_problem(transcript)
    if problem:
        raise SystemExit(f"{ip}: baseline not captured: {problem}")
    print(f"{ip}: running configuration saved and captured as bootflash:baseline.cfg")


# The one warning the copy is expected to print (and then answer) when it overwrites an earlier baseline.
OVERWRITE_WARNING = "%Warning: There is a file already existing with this name"


def baseline_problem(transcript: str) -> str | None:
    """Why the save-and-capture did not succeed, or None. Each command's own output must prove it: write memory
    reports [OK]; the copy reports bytes copied and no error; the listing shows baseline.cfg with a size. (A
    listing error such as `%Error opening bootflash:baseline.cfg` also contains the file name, so a name match
    proves nothing.)"""
    try:
        _, rest = transcript.split("write memory", 1)
        write, rest = rest.split("copy running-config bootflash:baseline.cfg", 1)
        copied, listing = rest.split("dir bootflash:baseline.cfg", 1)
    except ValueError:
        return "the transcript is missing one of the three commands"
    if "[OK]" not in write:
        return "write memory did not report [OK]"
    for line in copied.splitlines():
        stripped = line.strip()
        if stripped.startswith(OVERWRITE_WARNING) or "[confirm]" in stripped:
            continue  # the overwrite question, answered by _shell
        if stripped.startswith("%") or re.search(r"\berror\b", stripped, re.IGNORECASE):
            return f"the copy was refused: {stripped}"
    if not re.search(r"\b\d+ bytes copied\b", copied):
        return "the copy did not report bytes copied"
    for line in listing.splitlines():
        if line.strip().startswith("%"):
            return f"the listing failed: {line.strip()}"
    size = re.search(r"^\s*\d+\s+-r\S*\s+(\d+)\s.*\bbaseline\.cfg\s*$", listing, re.MULTILINE)
    if not size or int(size.group(1)) == 0:
        return "bootflash:baseline.cfg is not listed with a size"
    return None


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
    write_private(dest, "\n".join(kept + [f"{k}={v}" for k, v in wanted.items()]) + "\n")
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
