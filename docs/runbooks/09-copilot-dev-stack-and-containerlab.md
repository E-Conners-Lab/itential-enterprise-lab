# 09 — The Copilot dev stack and its Containerlab topology

A sandbox for building Itential automation with an AI assistant (GitHub Copilot in VS Code with the Itential
builder skills), kept apart from production by design. Two VMs:

- **`itential-dev`**: the chapter 05 stack rebuilt with a *dev overlay*. It has its own names, its own
  encryption key, a NetBox token that cannot write, a local model only, and an MCP server that runs as a
  service account instead of the administrator.
- **`clab`**: a Containerlab host running a four-node topology of real vendor images: two Cisco C8000v
  routers and two Arista vEOS-lab switches with OSPF, iBGP/eBGP and VLANs. The dev Platform automates these
  devices and never touches the EVE-NG lab.

One command builds both, and one command proves them. The step-by-step table below records the order the
Makefile runs, what each step does, and every trap the first build hit, so the next engineer does not.

> **Why a separate sandbox.** Production (chapter 08) is where assets run. An assistant that can create,
> import and delete assets must learn somewhere that is not production. [ADR 0063](../adr/0063-dev-stack-returns-for-copilot-with-a-containerlab-topology.md)
> records the decision and its amendments.

---

## Before you start

### What must already be true

- Chapters 01–08 green: the OOB network, NetBox, the EVE-NG lab and production.
- The C8000v and vEOS images are on the EVE-NG host under `/opt/unetlab/addons/qemu/` (chapter 04). The clab
  build copies **the same image files** the EVE-NG lab runs. No Arista account or token is needed.
- `aws sso login --profile ${ECR_PROFILE}` is current (chapter 05). The Itential images come from the
  private registry.
- The Mac Ollama service is serving the local model (`scripts/mac-ollama.sh`, chapter 06). The dev stack has
  **no** cloud model profile.
- No VPN on the workstation that captures 10.0.0.0/8, or every lab address resolves and then times out.
- **Budget.** `itential-dev` is 8 vCPU / 24 GB / 160 GB and `clab` is 8 vCPU / 20 GB / 60 GB. Check
  `docs/resource-budget.md` before building: RAM is the binding constraint.

### The safety model, before anything runs

The dev stack is built with the same plays as production, so the protections are explicit and tested:

| Protection | Where | What it stops |
|---|---|---|
| Production fingerprint | `make prod-snapshot MODE=save` before, `MODE=compare` after every step | Any change to production's adapters, integrations, workflows, agents, device groups, roles or NetBox counts going unnoticed |
| One overlay, asserted | `ansible/playbooks/vars/itential-dev.yml` (`dev_overlay: true`); the Platform plays refuse to run unless exactly one of the production target or the dev overlay is set | A dev run that silently falls back to production defaults |
| Read-only NetBox token | `make netbox-token-dev`: a user without `users.*` permissions and a `write_enabled=false` token; the plays assert it | The dev stack writing to the source of truth production reads |
| Separate names | `itential-dev.${LAB_DOMAIN}` and `mcp-dev.${LAB_DOMAIN}`, never `itential` or `mcp` | A client pointed at the wrong Platform by accident |
| Devices kept out of NetBox | The clab nodes are never registered | Every production consumer of `status=active` devices picking up a lab router |
| Monitoring exclusion | `observability.yaml` `excluded_vm_roles` | A torn-down sandbox paging someone |

---

## The commands, in order

Take the production fingerprint first. It is the proof that the build changed nothing it should not:

```
make prod-snapshot MODE=save
make plan-clab
make plan-itential
make dev-stack
make prod-snapshot MODE=compare
```

`make dev-stack` is `phase-itential` → `phase-flowai` → `verify-dev`, and `phase-itential` starts with
its two prerequisites. The full order:

| | Step | What it does |
|---|---|---|
| 1 | `make netbox-token-dev` | The view-only NetBox user and token, written to `.env` as `NETBOX_DEV_RO_TOKEN` before use. Proves GET 200 and POST 403 |
| 2 | `ansible/playbooks/netbox-vms.yml` | The `itential-dev` and `clab` VM records (no clab devices) |
| 3 | `tofu apply` in `tofu/clab` | VM 230: 8 vCPU / 20 GB / 60 GB, CPU type `host` for nested KVM |
| 4 | `ansible/playbooks/oob-gw.yml --tags routes` | A route for the clab management prefix via the clab VM |
| 5 | `images/fetch.sh c8000v`, `images/fetch.sh veos` | Copies the EVE-NG images through the workstation, sha256 checked on both ends |
| 6 | `ansible/playbooks/clab-host.yml` | Docker, containerlab and vrnetlab at pinned versions; converts vEOS to vmdk and builds both images; the DOCKER-USER allowlist |
| 7 | `images/fetch.sh clab-load`, then `clab-host.yml` again | Relays the staged images to the VM and builds what is missing |
| 8 | `ansible/playbooks/clab-dev.yml` | Generates the device password (to `.env` first), renders the topology and configs from `clab/versions.yaml`, deploys, reloads the routers once for the licence level, then waits for OSPF FULL, BGP Established **and the switch prefixes on rtr2** |
| 9 | `CLAB_DEV_ONLY=1 verify/test-12a-clab-dev.sh` | The clab verify, with the `itential-dev` half deferred, because that VM is not built yet |
| 10 | `netbox-vms.yml`, `tofu apply` in `tofu/itential`, `oob-gw.yml --tags dns` | The dev VM and its names |
| 11 | `ansible/playbooks/itential-host.yml` with the dev overlay | Docker, the lab-CA certificate for `itential-dev`, the route to the clab prefix |
| 12 | `images/fetch.sh itential`, `images/fetch.sh itential-load` | The Platform, Gateway and MCP images |
| 13 | `ansible/playbooks/itential.yml` with the dev overlay | The stack, LDAP, the gateway cluster, the inventory of the four clab nodes, and **svc-copilot** in the `copilot-builders` group |
| 14 | `itential.yml`, `ansible/playbooks/platform.yml`, `ansible/playbooks/flowai.yml` with the dev overlay | Workflows, Golden Config, MOP, LCM, the `ollama-mac` model profile and the `-local` agents |
| 15 | `make verify-dev` | The full clab verify and the dev Copilot verify |

Every step is idempotent. A re-run skips the clab deploy when nothing changed, so a green re-run takes about
20 minutes, most of it the Platform plays. The first build's end-to-end time was not measured cleanly (it
spanned several fixes); allow at least an hour and a half, dominated by image builds and the vEOS boot.

### Afterwards: the service account on production (optional, owner-approved)

To let the assistant **read and document** production, `make copilot-prod` creates `svc-copilot` there in
`copilot-readonly`: 34 built-in read roles and three custom read-only roles from `itential/copilot/roles.yaml`,
**no** Inventory Manager role (an inventory read returns device passwords), and no fallback group. It is
the only step in this chapter that writes to production, so run it on its own, then:

```
PROD=1 verify/test-05b-dev-copilot.sh
make prod-snapshot MODE=compare ALLOW=<allowlist>
```

S12.8 derives the allowlist from the role oracle; the compare must show only the Copilot additions.

---

## What "done" looks like

- `https://itential-dev.${LAB_DOMAIN}` serves a lab-CA certificate, runs Platform 6.5.2, and its Gateway 5
  cluster is connected.
- The dev inventory holds exactly `clab-rtr1`, `clab-rtr2`, `clab-sw1` and `clab-sw2`, and no EVE-NG name.
- `svc-copilot` logs in, belongs to `copilot-builders`, can create and delete an asset, and runs
  `show version` on a clab switch through Gateway 5.
- `mcp-dev.${LAB_DOMAIN}:8000/mcp` answers from the workstation as `svc-copilot`. `get_health` works;
  `describe_inventory` and `get_devices` are hidden.
- The clab topology: four OSPF adjacencies FULL, three BGP sessions Established, VLANs 10 and 20 active and
  trunked, and rtr2 carrying both switch /27s from BGP.
- `make prod-snapshot MODE=compare` prints `production matches the baseline`.

---

## Verification

Two scripts, both outside `make verify`, so a torn-down sandbox never turns the production suite red:

```
make verify-dev
```

`verify/test-12a-clab-dev.sh`, the Containerlab topology:

| Criterion | What a PASS means |
|---|---|
| S10.6 | The clab VM equals the oracle and its NetBox record, `/dev/kvm` is present, and containerlab is the pinned version |
| S10.7 | Four nodes run with their oracle addresses and accept SSH from the workstation **and from `itential-dev`** |
| S10.8 | vEOS equals the EVE-NG lab's version; the C8000v runs 17.13.01a at network-advantage |
| S10.9 | Every OSPF adjacency on the oracle's links is FULL on both ends |
| S10.10 | Both iBGP pairs and the eBGP session are Established on both ends, and rtr2 has the switch VLAN /27s |
| S10.11 | VLANs 10 and 20 are active on both switches and allowed on the trunk |
| S10.12 | `oob-gw` and `itential-dev` route the clab prefix; the allowlist equals the oracle and drops everyone else; clab RAM is under its budget line |

`verify/test-05b-dev-copilot.sh`, the dev Platform and the service account:

| Criterion | What a PASS means |
|---|---|
| S12.1 | `itential-dev` matches its budget; the dev names resolve to it and the production names still resolve to production |
| S12.2 | Lab-CA certificate, Platform 6.5.2, Gateway 5 connected |
| S12.3 | Inventory and Configuration Manager hold exactly the clab nodes |
| S12.4 | The dev NetBox token reads (200), cannot write (403), and NetBox records it `write_enabled=false` |
| S12.5 | The only model profile is `ollama-mac` and every agent is a `-local` twin |
| S12.6 | `svc-copilot` is a builder on dev: it creates and deletes a probe asset and runs a device command through Gateway 5 |
| S12.7, S12.8 | Production only (`PROD=1`, after `make copilot-prod`): the account is read-only there, and the fingerprint differs only by the allowlist |
| S12.9 | `mcp-dev` answers from the workstation and hides the inventory tools |

`verify/devcmd.py` is the independent source for every device answer. `DEVCMD_TIMEOUT` overrides its
150-second limit.

---

## Handing over to VS Code

The server side is complete when `make verify-dev` is green. Two things measured against the dev stack with
the Itential Copilot plugin (`skills/solution-arch-agent/pull-platform-data.py`, 2026-09-17) need handling on
the workstation:

1. **Python does not trust the lab CA by default** (`CERTIFICATE_VERIFY_FAILED`). Export
   `SSL_CERT_FILE` pointing at `docs/lab-root-ca.crt` in the shell VS Code starts from.
2. **Password login and the plugin's scripts disagree.** `AUTH_METHOD=local` logs in with `POST /login`, and
   the Platform accepts that token **as a `?token=` query parameter** (200) but **rejects it as a Bearer header**
   (401). `pull-platform-data.py` always sends Bearer, so every pull fails with 401. Either use OAuth client
   credentials or change the script to send the token the way the plugin's own AGENTS.md describes for local
   auth.

Point the use-case `.env` at `https://itential-dev.${LAB_DOMAIN}` as `svc-copilot`
(`SVC_COPILOT_DEV_PASSWORD` in the lab `.env`), and register **only** `mcp-dev` as an MCP server. Production
is reached only for read-only documentation, and only after `make copilot-prod`.

---

## Troubleshooting

Every entry below stopped a real build.

**vEOS containers exit during boot with an MSR 0x345 assertion.** Nested KVM does not expose that
performance-monitoring MSR. The topology passes `QEMU_CPU=host,level=9,pmu=off` (`images.veos.cpu` in
`clab/versions.yaml`).

**SSH logins to the switches take a minute, fail with `BadAuthenticationType`, or Gateway 5 reports
`Error reading SSH protocol banner`, and the switches are slow for no obvious reason.** Check memory
*inside* the switch before touching timeouts: `bash dmesg | grep -ci "out of memory"`. At vrnetlab's default
2 GB, vEOS 4.33 killed OpenConfig and ReloadCauseAgent about 70 times each, with a load average of 20 and
48 MB free. Give it **4 GB**, as the EVE-NG lab does. The generous timeouts (`verify/devcmd.py` 150 s, Gateway 5
`itential_driver_options.netmiko` banner/auth 150 s) stay, because the first banner after idle still takes
about 15 s, which is netmiko's default limit. Note that paramiko reports an authentication **timeout** as
`BadAuthenticationType`, so the error looks like a password problem when it is not.

**S10.10 fails with `% Subnet not in table` on a fresh deploy only.** The routers reload once to apply the
licence level. BGP sessions come up within 25 s of the reload, but rtr2 learns the switch /27s about 45 s
later. The deploy play now waits for the routes, not only the sessions. If you see this, the play is older
than that fix.

**`make clab-dev` fails S10.7 or S10.12 because `itential-dev` does not answer.** The clab build runs before
the dev VM exists. The Makefile runs that verify with `CLAB_DEV_ONLY=1`, which defers the `itential-dev`
half to `make verify-dev`. Deferral is opt-in and never depends on whether the VM happens to answer.

**`REMOTE HOST IDENTIFICATION HAS CHANGED` for the dev VM.** It was rebuilt at an address that existed
before. Verify the new fingerprint out of band (from `oob-gw` and from the console), then
`ssh-keygen -R` the address. Never disable host key checking to get past it.

**`ldapsearch` exits 1 with `Invalid general option name: ldif_wrap`.** OpenLDAP's `-o` options are
hyphenated: `ldif-wrap`, `nettimeout`.

**Ansible refuses to run from a script or agent with "requires blocking IO".** Ansible needs a blocking
stdout. Pipe it through `cat` (`make ... |& cat`).

**A template renders two commands onto one line.** Ansible templates run with `trim_blocks`, so a
line-start `{%-` also eats the previous newline. The clab templates use `{%` and `{#` at line start, and
`tests/test_clab.py` holds every rendered file to one command per line.

**A regex that works in Jinja fails in Ansible.** Ansible does not process escapes inside Jinja string
literals the way plain Jinja does: `'\\d'` fails, and `'\b'` is a word boundary. Use character classes
(`[0-9]`) instead of escapes; a test forbids the escaped forms in the clab plays.

**The deploy check says the address is wrong when it is right.** `containerlab inspect` reports
`address/prefix`; compare bare addresses.

**The clab VM resize applies but the topology is down afterwards.** A memory change reboots the VM.
The next `clab-dev.yml` run sees the nodes are not running and redeploys; the vEOS boot takes about 5
minutes.

**The OpenTofu provider hangs for minutes on refresh.** The QEMU guest agent timeout is stored in state; the
`tofu/clab` and `tofu/itential` modules set `agent { timeout = "2m" }`.

---

## Tested versions

| Component | Version |
|---|---|
| Itential Platform | 6.5.2 |
| Itential Gateway 5 | 5.5.2-amd64 (netsdk netmiko driver) |
| Itential MCP server | v0.14.0, running as `svc-copilot` |
| Local model | `gemma4:26b` on the Mac, the only dev profile (`ollama-mac`) |
| containerlab | 0.79.0 |
| vrnetlab | commit `3a34fa63d73843c871f248e2df2ffa5c038183ce` |
| Cisco C8000v | 17.13.01a, 4 GB, network-advantage |
| Arista vEOS-lab | 4.33.1.1F, 4 GB, `QEMU_CPU=host,level=9,pmu=off` |
| `clab` VM | 8 vCPU / 20 GB / 60 GB, Ubuntu 24.04, CPU type `host` |
| `itential-dev` VM | 8 vCPU / 24 GB / 160 GB, Ubuntu 24.04 |
| Itential Copilot plugin | `automateyournetwork/Itential_Copilot_Plugin` at `e9e9bab` (2026-09-15), for the handover notes |
