#!/usr/bin/env python3
"""Run one show command on a lab network device over SSH with the device-local 'automation'
account (AUTOMATION_PASSWORD from .env). Used by verify/ scripts; never prints the password.

  verify/devcmd.py <ip> "<command>"        # exit 0 and stdout = command output
"""

import os
import sys
import time

import paramiko

# Seconds to connect and to wait for output. vEOS under nested KVM (the clab dev topology) took 23 s to answer its
# first `show version | json` and 9-11 s for other JSON commands (measured 2026-09-16), past the old 20 s, so the
# default is 90; DEVCMD_TIMEOUT overrides it. A read-only show command waiting longer costs nothing.
DEFAULT_TIMEOUT = int(os.environ.get("DEVCMD_TIMEOUT", "90"))


def run(ip: str, command: str, user: str = "automation", timeout: int = DEFAULT_TIMEOUT) -> str:
    pw = os.environ.get("AUTOMATION_PASSWORD")
    if not pw:
        raise SystemExit("AUTOMATION_PASSWORD missing in .env")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, username=user, password=pw, look_for_keys=False, allow_agent=False, timeout=timeout, banner_timeout=timeout, disabled_algorithms=None)
    try:
        _, out, err = c.exec_command(command, timeout=timeout)
        # IOS XE / EOS terminate lines with CRLF; strip CR so shell checks can anchor on $NF
        return (out.read().decode(errors="replace") + err.read().decode(errors="replace")).replace("\r", "")
    finally:
        c.close()


def reload(ip: str, user: str = "automation", timeout: int = 20) -> None:
    """IOS XE `reload` needs an interactive confirm; use a shell channel and drop the session."""
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, username=user, password=os.environ["AUTOMATION_PASSWORD"], look_for_keys=False, allow_agent=False, timeout=timeout)
    try:
        sh = c.invoke_shell()
        sh.send("reload\n")
        time.sleep(2)
        sh.send("\n")
        time.sleep(2)
    finally:
        c.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    sys.stdout.write(run(sys.argv[1], sys.argv[2]))
