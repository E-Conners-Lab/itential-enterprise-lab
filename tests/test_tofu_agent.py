"""Every Proxmox VM module bounds the guest-agent wait.

The Ubuntu template has no qemu-guest-agent until the host play installs it, and the bpg provider waits its full
default of 15 minutes for the agent on a first apply and on refreshes. tofu/platform-ha2 set a 2-minute timeout
after the Phase 2 build (lab-build-lessons); tofu/clab and tofu/itential were copied from a module without it,
and the clab VM's first apply sat for 15m21s on 2026-09-16. A new VM module must not repeat that.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TOFU = Path(__file__).resolve().parent.parent / "tofu"
# The modules that carry the bound. tofu/oob (oob-gw, templates) and tofu/platform (k3s nodes) predate the lesson and
# still use the provider default; they manage live VMs and change only with the owner's approval (follow-up).
BOUNDED = ("clab", "itential", "platform-ha2")
VM_MODULES = sorted(
    p
    for p in TOFU.rglob("*.tf")
    if ".terraform" not in p.parts and p.parent.name in BOUNDED and 'resource "proxmox_virtual_environment_vm"' in p.read_text()
)


def test_there_are_vm_modules_to_check() -> None:
    names = {p.parent.name for p in VM_MODULES}
    assert names == set(BOUNDED), names


@pytest.mark.parametrize("path", VM_MODULES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_agent_block_sets_a_short_timeout(path: Path) -> None:
    agents = re.findall(r"agent\s*\{(.*?)\}", path.read_text(), flags=re.S)
    assert agents, f"{path}: a VM resource with no agent block"
    for body in agents:
        if re.search(r"enabled\s*=\s*true", body):
            m = re.search(r'timeout\s*=\s*"(\d+)m"', body)
            assert m, f"{path}: agent enabled without a timeout - the provider waits 15 minutes"
            assert int(m.group(1)) <= 5, f"{path}: agent timeout {m.group(1)}m"
