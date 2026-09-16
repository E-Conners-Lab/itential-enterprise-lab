# 0063 — The dev stack returns as `itential-dev` for Copilot, with a Containerlab topology of its own; production stays read-only to Copilot

- **Status:** accepted (owner decisions, 2026-09-16); amended 2026-09-16 (the dev switches are vEOS-lab, see the amendment at the end)
- **Date:** 2026-09-16
- **Amends:** ADR 0053 (its Retirement section: VM 205 comes back, under new names), ADR 0035 (the placement: the VM is `itential-dev`, `mcp.lab.internal` is not its alias), ADR 0014/0033 (the cEOS version is exactly 4.33.1.1F), `docs/resource-budget.md` ceilings (RAM 280 -> 296 GB, disk 1.4 -> 1.5 TB)
- **Related:** ADR 0003 (the supernet and the second-OOB reservation), ADR 0030 (symmetric return path), ADR 0055 (the replay and its overlay), ADR 0060 (the Mac serves local inference), PID S4, S10, S11.8, S12 (amendment 1.30)

## Context

GitHub Copilot is to build Itential assets for this lab: workflows, templates, agents. It needs a Platform
it can write to, and devices it can run commands against, and the only Platform left after S11.8 retired
VM 205 (2026-09-10) is production. Production holds the stored adapter secrets, the nightly compliance and
backup schedules and the replayed assets every verify reads, and the owner's rule is that Copilot sees it
read-only.

A read-only view of production is harder than a role name suggests. Measured on production (6.5.2,
GET only, 2026-09-16):

- `GET /inventory_manager/v1/inventories/lab/nodes` returns every node's `AUTOMATION_PASSWORD` in clear, so
  `InventoryManager/inventory:read` is a credential read, not a metadata read. The adapter and integration
  documents do not leak their secrets.
- Several built-in "read" roles can write: `ConfigurationManager/apiread` runs compliance, adapter tasks and
  task instances; `LifecycleManager/apiread` and `/operator` cancel action executions; `JsonForms/apiread`
  and `/readonly`, `Jst/apiread` and `/readonly`, `Tags/readonly` and `PrebuiltsRepository/apiread` create
  and delete; `FormBuilder/apiread` preserves form data. There is no built-in read-only role for
  Configuration Manager or Lifecycle Manager at all.
- LDAP groups are auto-created as Platform groups on first login (`admins`, `builders`, `operators` exist
  with LDAP provenance and no roles), so a directory group is the natural handle for a service account.

Rebuilding the dev stack is not free either. Every asset play targets `platform_target | default('itential-host')`,
so once a dev VM carries that NetBox role an overlay-less `make phase-itential` builds it from production's
EVE-NG inventory; the secrets play regenerates `ITENTIAL_ENCRYPTION_KEY` in the shared `.env` whenever it is
not 64 characters, and production reads that same key; and every production consumer of NetBox devices
(`tasks/platform-assets.yml`, `platform.yml`, `observability.yml`'s five-plus-seven assert, verify S7.1) reads
`status=active` with no site filter. The dev stack has to be isolated by construction, not by care.

## Decision

1. **VM 205 returns as `itential-dev`** at 10.100.0.65, 8 vCPU / 24 GB / 160 GB, with the alias `mcp-dev`.
   It never takes the names `itential` or `mcp`: those are aliases of production's `iap-lb` (.71) and
   `tools-01` (.81), and `.mcp.json` and every phase 5-7 verify follow them. `topology/ipam.yaml` and
   `itential/versions.yaml` (`vm.name`, the new `dev:` block) are the oracles; a test fails if any hostname
   or alias appears twice.
2. **The isolation contract: a dev overlay plus guard asserts, with defaults that reproduce production.**
   `ansible/playbooks/vars/itential-dev.yml` sets `dev_overlay: true` and every dev-only choice; the shared
   task files take each new variable with `| default(<today's production behaviour>)`, so
   `vars/itential-prod.yml` and the replay change nothing. `itential.yml`, `platform.yml` and `flowai.yml`
   assert that exactly one overlay is passed, and `platform-ha2-replay.yml` refuses `dev_overlay`. The dev
   encryption key is its own `.env` key, `ITENTIAL_DEV_ENCRYPTION_KEY`, in both the lookup and the
   `lineinfile` regexp.
3. **A Containerlab host `clab`** (VM 230, 10.100.0.224, 8 vCPU / 16 GB / 60 GB, CPU type `host` for nested
   KVM) runs topology `dev`: two C8000v 17.13.01a (vrnetlab, built from the image EVE-NG already runs, ADR
   0032) and two cEOS **4.33.1.1F**, exact parity with the lab's vEOS (ADR 0033). If Arista no longer offers
   a cEOS build of 4.33.1.1F, the fallback is the newest 4.33.x, recorded in the manifest when used. The
   oracle is `clab/versions.yaml`.
4. **The dev inventory is the clab devices only, and they are not registered in NetBox.** NetBox gets the
   two prefixes and the two VMs; the nodes stay out, because the production inventory, the observability
   assert and S7.1 would all pick them up. Dev reads its inventory from `clab/versions.yaml`.
5. **Routed clab management, 10.100.2.0/24**, via static routes to `clab` on `oob-gw` and on `itential-dev`;
   in-band 10.100.3.0/24 stays inside the host. Reachability is limited by a DOCKER-USER allowlist on `clab`
   to `itential-dev`, the `clab` host and the home LAN 192.168.68.0/22 (`docs/ip-plan.md` 3.3).
6. **Dev reads production NetBox with a read-only token** (`NETBOX_DEV_RO_TOKEN`: a non-superuser with a
   view-only object permission and `write_enabled: false`). A dev workflow that writes NetBox fails with 403,
   by design. **ServiceNow is off on dev**: the PDI is shared with production's change history. **FlowAI on
   dev uses the `ollama-mac` profile only**, never the metered company key.
7. **`svc-copilot`, one LDAP account per environment with its own password** (`SVC_COPILOT_PROD_PASSWORD`,
   `SVC_COPILOT_DEV_PASSWORD`), added by a task file with `ldapadd`, never by editing the pinned upstream
   LDIF. On production it is in `copilot-readonly`: the built-in read roles measured as read-only, plus three
   custom roles, `copilot-cm-read`, `copilot-lcm-read` and `copilot-jst-read`, built from `get`/`search`/
   `export`-style methods, and **no InventoryManager role** and no membership of the inventory groups. On dev
   it is in `copilot-builders`, which holds every role except the administrative ones and is a member of the
   dev inventories and gateway.
8. **No production MCP server for Copilot.** `mcp-dev` on the dev stack runs as `svc-copilot`; the production
   MCP server keeps serving Claude Code and is not pointed at Copilot.
9. **Budget:** the RAM ceiling rises from 280 to **296 GB** and the thin disk allocation ceiling from 1,400 to
   **1,500 GB**; the 80 % data-usage alert stays the real disk guard. Lever 3 (Containerlab 16 -> 12 GB) no
   longer holds with two C8000v and is replaced by stopping the dev topology or stack when the RAM is needed.
10. **Dev verification is separate:** `make verify-dev` runs `verify/test-12a-clab-dev.sh` (S10.6-S10.12) and
    `verify/test-05b-dev-copilot.sh` (S12.1-S12.9) and is never part of `make verify`, which must not go red
    because a sandbox is torn down. `make phase-itential` and `make phase-flowai` pass the dev overlay; the
    new targets are `dev-stack`, `clab-dev`, `plan-clab`, `netbox-token-dev`, `prod-snapshot`, `verify-dev`
    and `copilot-prod`.
11. **Production was protected during the build by two things outside the plays:** a temporary Claude Code
    PreToolUse guard hook that refused any command naming a production host, address or make target outside
    a dev-build allowlist, and `verify/prod-snapshot.py`, a GET-only fingerprint of production (workflows and
    their `lastUpdated`, inventories and their groups, device groups, agents, profiles, integrations,
    adapters, role and group counts, NetBox device and VLAN counts) saved before the build and compared
    after it. The only allowed difference is the additions of `make copilot-prod`.

## Alternatives rejected

- **A second NetBox on the dev VM.** Write-safe, but about 3 GB of RAM at zero headroom, another component to
  seed and keep in step, and a dev stack that no longer sees the lab's real source of truth.
- **Containerlab management on an existing bridge** (`vmbr1`, the .225-.238 block). No route changes, but
  Docker's bridge and `br_netfilter` fight the Proxmox bridge, the devices land on the OOB segment next to
  production, and four nodes use up most of the clab block.
- **The Containerlab default 172.20.20.0/24 with NAT or port-forwards.** Nothing outside `clab` reaches a node
  on its own address, so neither the Mac nor Gateway 5 on the dev stack gets per-node SSH without a forward
  per node and port.
- **10.100.1.0/24 for the clab management prefix.** ADR 0003 reserves it for a second OOB VLAN; taking it
  would contradict an accepted decision for no gain.
- **Registering the clab devices in NetBox as `active`.** They would flow into production's inventory on the
  next replay and break the observability assert and S7.1. A site `clab-dev` with status `planned` would be
  ignored by those consumers; it is a later change with its own tests, not this one.
- **Built-in roles only on production.** No visibility of Configuration Manager or Lifecycle Manager at all,
  and several built-ins that carry "read" in their names can write.
- **A read-only production MCP server (`mcp-ro` on `tools-01`).** Another production container for a need the
  dev stack already meets.
- **Dev verification inside `make verify`.** It would fail whenever the sandbox is down, and a verify that is
  red for an expected reason teaches everyone to ignore red.

## Consequences

- RAM headroom is **0 GB** against the raised 296 GB ceiling (disk plan 1,455 of 1,500 GB). Measured use on
  2026-09-11 was 222 GB; the two new VMs add 40 GB, about 262 GB of 314. **The firewall track (`nios` plus
  `panorama`, 32 GB) now depends on the section 5 levers** in `docs/resource-budget.md`, including stopping the
  dev topology or stack while it runs.
- `make phase-itential` and `make phase-flowai`, and so `make up`, build the dev stack; a fresh clone still
  gets a Phase 5 stack at .65, now under the name it keeps.
- Dev workflows that write NetBox or ServiceNow fail by design; they import, and fail only at run time. That
  is the price of reading the real source of truth without a way to change it.
- **The sandbox is not monitored** (owner decision, 2026-09-16). `itential-dev` and `clab` must stay `active` in
  NetBox because `ansible/inventory/netbox.yml` only returns active VMs, so they cannot be `staged`. Instead
  `observability/observability.yaml` `zabbix.excluded_vm_roles` names their NetBox roles (`itential-host`,
  `clab-host`), and `observability.yml`'s Zabbix host list, verify S7.1 and `observability-hosts.yml` all honour
  it. A partial exception to ADR 0057's "monitoring follows the estate": the sandbox is stopped and rebuilt at
  will, and a production verify must not turn red because of it. No production VM role may be excluded (tested).
- **Production's OpenLDAP keeps its data in anonymous Docker volumes.** A `docker compose down` on `tools-01`
  loses `svc-copilot` and its group. `make copilot-prod` is idempotent and re-runnable, and belongs after the
  identity step of any production rebuild.
- Whether a role assignment on an LDAP-provenance group persists across logins, and the exact body of
  `POST /authorization/roles`, were not measured before this decision; the task file measures both. On dev the
  fallback is the database membership path `tasks/ldap-admin.yml` already uses, a `copilot-builders-local`
  group, and the task's `svc_effective_group` makes the dev inventories and gateway grant whichever group was
  used. On production `copilot-access.yml` disables the fallback: an unexpected measurement stops the play with
  a message instead of writing a `-local` group and a MongoDB membership, and S12.7/S12.8 accept only
  `copilot-readonly`.
- `copilot-builders` gets its roles resolved against the Platform's role list at the time the task runs.
  `itential.yml` runs it before the adapters and Integration Models exist, so `flowai.yml`, the last dev play,
  runs the same task again (idempotent) and the group ends up with their roles too.
- The dev plays refuse to run unless `NETBOX_TOKEN` equals a non-empty `NETBOX_DEV_RO_TOKEN`, so the read-only
  token is enforced by the plays, not only by the Makefile's `load_env_dev`. The dev NetBox permission covers
  every object type except `users.*`: view on tokens would expose legacy token keys, which are credentials.
- `make phase-itential` (and so `make up`) has `netbox-token-dev` and `clab-dev` as Make prerequisites. `make
  verify` (`verify/run.sh`) skips every script with `dev` as a word in its name after the test number; `make
  verify-dev` runs them.
- **What the production fingerprint does not cover.** `verify/prod-snapshot.py` records names, counts and
  `lastUpdated` stamps. It does not see adapter or integration properties (a changed base URL or credential
  reference), Operations Manager triggers, Golden Config trees and compliance plans, MOP command templates,
  Lifecycle Manager models, instances and actions, or which role ids a group holds (only how many). A change
  there passes the compare. `make prod-snapshot` runs `verify/snapshot-sanity.py` after `--save` and
  `--compare`, which fails when a required section (workflows, inventories, adapters, integrations, profiles,
  role total, groups, admin_group's role count, NetBox devices) came back empty, so an endpoint that silently
  answers nothing cannot make two empty fingerprints "equal". A compare after `make copilot-prod` needs
  `ALLOW=<allowlist>`, the one S12.8 derives from `itential/copilot/roles.yaml`.
- **Residuals that stay:**
  - dev and production share `ITENTIAL_ADMIN_PASSWORD`: it is the password of the dev stack's built-in `admin`
    as well as production's administrator.
  - Gateway 5 runner code on dev (a Copilot prototype's Python or Ansible service) has L3 reach to the OOB hosts,
    not only to the clab nodes; the DOCKER-USER allowlist protects the clab nodes from the lab, not the lab from
    the dev runner.
  - a dev inventory that already exists does not gain `copilot-builders` retroactively: Inventory Manager has no
    documented update endpoint for an inventory's groups, so the group is set only when the inventory is
    created.
- The leftover Kubernetes Certificate `itential/itential-platform` from VM 205's first life is left as it is;
  the dev stack uses its own namespace and certificate `itential-dev`, and production lives in `itential-ha2`.
- The guard hook is temporary: it protected the build, not the steady state. After the merge, the isolation
  is carried by the overlay, the asserts and their tests, and by `make prod-snapshot` before any later dev
  change that touches shared task files.

## Amendment 2026-09-16 — the dev switches are vEOS-lab 4.33.1.1F built with vrnetlab, not cEOS

Owner decision, 2026-09-16. Decision 3 named two cEOS 4.33.1.1F switches. cEOS was never downloaded
(`/srv/images/ceos` is empty) and `images/fetch.sh arista` needs an arista.com API token the owner cannot find.
The owner already holds vEOS-lab 4.33.1.1F: it is the image the EVE-NG lab switches run
(`/opt/unetlab/addons/qemu/veos-4.33.1.1F/hda.qcow2`, 610 MB, virtual 4.01 GiB; partition 1 is a 6 MB bootable
syslinux with Aboot embedded, partition 2 the 4 GB filesystem with `vEOS-lab.swi`, so it boots without the
`cdrom.iso` beside it).

**Changed**

- `clab-sw1` and `clab-sw2` are containerlab kind `arista_veos`, image `vrnetlab/arista_veos:4.33.1.1F`, built on
  `clab` by `arista/veos` of srl-labs/vrnetlab at the commit already pinned for the C8000v. `images/fetch.sh veos`
  copies the EVE-NG image to `/srv/images/veos/vEOS-lab-4.33.1.1F.qcow2` exactly as `c8000v` does (relayed
  through the workstation, sha256 on both ends before the MANIFEST line); `clab-load` relays it to `clab`, and
  `clab-host.yml` converts it to `vEOS-lab-4.33.1.1F.vmdk` (vrnetlab's vEOS build takes a vmdk and reads the
  version from its name) and builds the image. `clab/versions.yaml` `images.veos` is the oracle.
- Parity with the lab is now exact by construction: the same image file, not a different packaging of the same
  version. `tests/test_clab.py` holds `images.veos.version` equal to the `veos-<version>` image of every vEOS in
  `topology/enterprise.yaml`, and `verify/test-12a-clab-dev.sh` S10.8 checks model `vEOS-lab` and the version on
  the switches.
- Cost: each switch is a nested QEMU VM, about 2 GB of RAM (vrnetlab's default, `images.veos.ram_mb`) instead of
  ~1.5 GB for a cEOS container, and about 4 minutes to boot instead of seconds. `clab` stays 8 vCPU / 16 GB:
  2 x 4 GB C8000v + 2 x 2 GB vEOS is 12 GB of guest RAM.
- Management on the switches is `Management1`, owned by vrnetlab's bootstrap (10.0.0.15/24 behind QEMU user
  networking, stitched to the container's containerlab address); the startup config leaves it alone, as the
  C8000v config leaves `GigabitEthernet1`. The admin password override (PID success criterion 5) stays:
  containerlab's `arista_veos` kind starts vrnetlab with `admin`/`admin`.
- The **Amends** entry "ADR 0014/0033 (the cEOS version is exactly 4.33.1.1F)" no longer applies to the dev
  topology, which runs no cEOS. **cEOS remains the plan for the S10.1-S10.5 CI twin** (phase 12); `images/fetch.sh arista` stays in
  the script for it, unused by `make clab-dev`.

**Unchanged:** every isolation rule of this ADR (routed mgmt prefix, DOCKER-USER allowlist, no NetBox
registration, own device password, dev-only verification), the addressing, VLANs and routing of the topology.

## Amendment 2026-09-17 — the vEOS switches get 4 GB, `clab` 20 GB, the RAM ceiling 300 GB

- **Measured 2026-09-16:** at vrnetlab's 2 GB default both switches ran out of memory in a loop: about 70 OOM kills
  each (OpenConfig, ReloadCauseAgent), 48 MB free, a load average of 20 inside the switch, 1.7-1.8 host cores per
  switch container. It surfaced as SSH logins of 1-95 s and intermittent login failures in S10.7, S10.10 and
  S10.11, and as a Gateway 5 banner timeout; the timeout changes of PRs #51 and #53 treated those symptoms.
- **Decision (owner):** `images.veos.ram_mb` 4096, what the EVE-NG lab's vEOS gets (`topology/enterprise.yaml`).
  `clab` grows from 8 vCPU / 16 GB to 8 vCPU / 20 GB (4 x 4 GB of guest RAM plus the host), and the RAM ceiling
  in `docs/resource-budget.md` rises from 296 to 300 GB. The hypervisor had about 266 GB of 314 GB in use.
- The earlier amendment's cost line ("`clab` stays 8 vCPU / 16 GB: 2 x 4 GB C8000v + 2 x 2 GB vEOS is 12 GB of
  guest RAM") no longer holds. The timeouts stay: they cost nothing on a healthy device.
