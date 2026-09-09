# 0047 — The Ubuntu hosts join Gateway 5 in their own inventory; password login for the automation user

- **Status:** accepted
- **Date:** 2026-09-08
- **Related:** ADR 0035 (Gateway 5), ADR 0039 (Configuration Manager through the InventoryBroker), PID S4d.6

## Context

PID S4d.6 wants the three Ubuntu hosts of the topology (`dc1-srv01`, `br1-host01`, `br2-host01`, NetBox platform
`ubuntu-24-04`, roles server and client) as inventory nodes with reachability and uptime checks; the PA-VM
firewalls stay deferred until the image is staged. Measured on 2026-09-08:

- Gateway 5's netsdk node model takes `itential_host`, `itential_port`, `itential_driver` (netmiko),
  `itential_platform`, `itential_user`, `itential_password`, `itential_become`, `itential_become_password`
  and `itential_driver_options`; there is no key attribute. Netmiko's `linux` platform runs shell commands.
- The hosts accept only public-key login for `automation` although the account has a password: the cloud
  image's `/etc/ssh/sshd_config.d/60-cloudimg-settings.conf` sets `PasswordAuthentication no` and sorts before
  EVE's `60-eve.conf` that sets it to yes, and sshd keeps the first value.
- The InventoryBroker adapter publishes exactly the inventories in its `inventories` list to Device Broker;
  every device it publishes is backed up nightly by `wf-backup-all-v1` and offered to Golden Config.

## Decision

- **A second inventory `lab-hosts`** (`stack.host_inventory`) built by `itential.yml` from NetBox (active
  devices of the `hosts.netbox_platforms` platforms with the server and client roles), nodes with
  `itential_platform linux`, the automation account and its password, `createBrokerActions false`, and the
  InventoryBroker's list untouched (`[lab]`): Configuration Manager, the compliance plan and the backups
  never see a host. The play proves Gateway 5 reaches one host with the read-only probe (`uptime -p`,
  `hostname`).
- **Password login for `automation` only**, enabled by `lab-endpoints.yml` with the drop-in
  `10-lab-automation.conf` (`Match User automation` / `PasswordAuthentication yes`), which sorts before the
  cloud image's file. Every other user keeps key-only login. The password is the same lab automation
  account the network devices use (`AUTOMATION_PASSWORD`), moved to a secrets manager in Phase 10.
- **Verification** S4d.6 in `verify/test-06b-platform.sh`: the NetBox host set equals the `lab-hosts` nodes;
  Gateway 5 `send-command` returns the hostname and `uptime -p` of every host; direct SSH with the same
  account (`verify/devcmd.py`) returns the same hostname and an uptime of the same day; the hosts are absent
  from Configuration Manager's device list. Firewalls print DEFER until the PA-VM image is staged.

## Consequences

- `device-ops` and the diagnostics agent can read hosts later by naming the `lab-hosts` inventory in the
  send-command selector; no agent gets that in this phase.
- Rejected: hosts in the `lab` inventory (nightly backups and compliance would run `get-config` on Linux);
  key-based login through `itential_driver_options` (the runner would need the private key mounted, a
  Phase 10 secrets concern); a Gateway 5 python or Ansible service for the probe (the native service is
  enough for reachability and uptime).
