# 0041 — The five IOS-XE routers booted as `hostname Router`; restored through the governed config push

- **Status:** accepted
- **Date:** 2026-09-07
- **Amends:** ADR 0034 (lab topology design: C8000v bootstrap), PID S3 (silent-failure eval E13)
- **Related:** ADR 0040

## Context

Measured 2026-09-07 while preparing S4d.1: running and startup configuration of dc1-wan01,
dc1-wan02, br1-wan01, br2-wan01 and isp-core01 all say `hostname Router`; the seven EOS switches
carry their names. `topology/configs/c8000v.j2` sets `hostname {{ name }}` on its first line.
`eve/build.py push-configs` wipes the node, boots it with the rendered file as the config.iso
bootstrap and, once the licence level answers, runs `write memory` and reloads. The bootstrap
skipped line one (every other line, BGP, IPsec, addresses, applied: verify 04 S3.4/S3.5 passed),
and `write memory` then saved the default name. Verify 04 reaches devices by address and never
compared the hostname, so PID E7/E11-style drift detection did not cover it.

**Second finding, same cause (2026-09-07, first run of the compliance plan):** none of the five
routers carries `ntp server vrf MGMT 10.100.0.1` either (line 17 of the rendered template); every
other baseline line is present. The plan's first run flagged exactly that line on the five IOS-XE
devices and nothing on the seven EOS devices. Restored the same way (five more approved pushes).
Root cause tracked in GitHub issue #18.

## Decision

- **Restore through the platform, not through EVE-NG.** Owner decision 2026-09-07 ("option 1"):
  `wf-config-push-v1` (ADR 0040) pushes `hostname <NetBox name>` and saves it, one router at a
  time, each behind a Work Center approval and verified over direct SSH. No wipe, no reboot, no
  BGP or IPsec churn. This is the first governed write of S4d and the same path the remediation
  agent uses later.
- **Root cause is a Phase 4 follow-up** (GitHub issue): confirm whether IOS-XE ignores the first
  line of `iosxe_config.txt` or the `hostname` command specifically, then fix the template (a
  leading comment line or `hostname` after `license boot level`) and add a hostname check to
  `verify/test-04-topology.sh` S3.2. Until then a rebuilt router would drift again and S4d.1 would
  report it.
- `verify/test-06b-platform.sh` S4d.1 fails, not skips, while any device's running hostname
  differs from its NetBox name outside the deliberate-drift window.

## Consequences

- The compliance plan's first clean run is evidence that the fix landed on all five routers.
- Rejected: `eve/build.py push-configs --only <routers>` (15 minutes of outage per router and
  the defect would recur); leaving the drift and redefining S4d.1 around it.
