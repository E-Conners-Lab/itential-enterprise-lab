# 05 — The Itential dev stack

One Ubuntu VM, one Compose stack: Platform, MongoDB, Redis, Gateway 5, an MCP server and a directory. Then
adapters, a device inventory generated from NetBox, and three workflows that reserve a VLAN, get it
approved and push it to a real switch.

This is the first chapter that needs a commercial registry. It is also where the series stops being
infrastructure and starts being automation.

> **This VM is temporary by design.** Chapter 08 rebuilds the same Platform in a production HA shape,
> replays every asset onto it, moves `itential.${LAB_DOMAIN}` to a load balancer and retires this VM. Build
> it anyway: it is where every asset is developed, and the replay in chapter 08 has nothing to replay
> without it.

---

## Before you start

### What must already be true

- Chapters 01–04 green. In particular the twelve network devices are up: the workflows in this chapter
  configure them for real.
- **Access to Itential's container registry.** This is the blocking requirement of the whole series. You
  need an AWS profile with pull access to the private ECR — `${ECR_PROFILE}` in `.env` as
  `ECR_AWS_PROFILE`, against account `${ECR_ACCOUNT}`. There is no free substitute.
- `.env` carries `ITENTIAL_ADMIN_PASSWORD` and `AUTOMATION_PASSWORD`. `ITENTIAL_ENCRYPTION_KEY` and the
  database passwords are **generated on first run and written back to `.env`** before they are used.
- Optional: a ServiceNow PDI, as `SNOW_INSTANCE` (`${SNOW_INSTANCE}`) plus its credentials. Everything in
  the S4b criteria is gated on those being present.

### Why one VM and containers

[ADR 0020](../adr/0020-itential-platform-6-5-gateway-5-5-rocky-9.md) originally planned two Rocky 9 VMs
installed by Itential's deployer from an RPM repository, gated on repository credentials and licence terms
— the largest schedule risk in the plan. [ADR 0035](../adr/0035-itential-single-ubuntu-vm-dev-stack.md)
replaced it: the public `itential-dev-stack` Compose file with images from the private ECR, on one Ubuntu
VM with Docker CE. Itential's Ubuntu non-support applies to RPM installs; a container host's distribution
is irrelevant to the images.

The upstream `docker-compose.yml` is **vendored unchanged** at a pinned commit, with its sha256 recorded in
`itential/versions.yaml` and checked by `tests/test_itential.py`. Every lab change lives in
`itential/compose.override.yml`. Upstream binds its ports to loopback; the override publishes 443 for the
Platform (with a lab-CA certificate), 50051 for Gateway 5 and 8000 for MCP on the OOB address.

### Why there is an LDAP container

Gateway Manager only honours group membership for **AAA-provisioned** users. The built-in local `admin` is
never in a group — even with the membership written directly into MongoDB — so it cannot own gateway
resources. The stack therefore runs the upstream OpenLDAP and the gateway cluster is created as the LDAP
user `admin@itential`. This is not decoration; without it the gateway cluster cannot be created at all.

---

## The commands, in order

First, an SSO session for the registry — this expires, and everything image-related fails without it:

```
aws sso login --profile ${ECR_PROFILE}
```

Then:

```
make plan-itential
make phase-itential
```

| | Step | What it does |
|---|---|---|
| 1 | `ansible/playbooks/netbox-vms.yml` | Registers the VM in NetBox with its reserved address |
| 2 | `tofu apply` in `tofu/itential` | The VM: 8 vCPU / 24 GB / 160 GB, Ubuntu template, `vmbr1` only |
| 3 | `ansible/playbooks/oob-gw.yml --tags dns` | Re-renders the resolver so `itential.${LAB_DOMAIN}` and the `mcp` alias resolve |
| 4 | `ansible/playbooks/itential-host.yml` | Docker CE at a pinned version and held, the lab root CA trusted **system-wide on the VM**, the stack directory tree, and a cert-manager certificate for the Platform's name issued from `ClusterIssuer` `lab-ca` |
| 5 | `images/fetch.sh itential` | On the workstation: mints a 12-hour ECR token from the SSO profile, pulls each image **by digest**, `docker save`s it to `/srv/images/itential/` and records its sha256. Long-lived keys never reach the VM |
| 6 | `images/fetch.sh itential-load` | Streams those tarballs into the VM and loads them |
| 7 | `ansible/playbooks/itential.yml` | The stack itself — see below |

Step 7 is the substantial one:

- Generates and **persists** the encryption key and every password to `.env` *before* using them.
- Builds the Gateway 5 runner image on the VM from the pinned gateway and Python images.
- Writes the stack `.env` and brings Compose up with profiles `platform`, `gateway5`, `mcp` and `ldap`, and
  **removes containers belonging to profiles that are now disabled**.
- Waits for `/health/status` — up to five minutes on a first boot.
- Creates `admin_group` holding all 173 roles (read page by page), provisions `admin@itential`, creates the
  gateway cluster and waits for Gateway 5 to connect, then registers the runner in the cluster store.
- Clones the adapters at pinned tags into the custom services mount, installs their dependencies with `npm`
  **as uid 1001 inside a throw-away platform-image container**, and reloads the Platform.
- Creates the adapter instances, generates the Inventory Manager inventory from NetBox, and imports the
  workflows.

---

## What "done" looks like

Expect the better part of an hour on a first run, dominated by the ECR pulls (the Platform image alone is
around 750 MB), the runner image build, and the Platform's first boot. Subsequent runs are minutes and are
idempotent.

- `https://itential.${LAB_DOMAIN}` serves a certificate issued by the lab CA and logs you in as
  `admin@itential`.
- Gateway Manager shows the cluster `lab` **connected**, with the runner registered.
- Inventory Manager holds the inventory `lab` with twelve nodes, generated from NetBox's active devices
  with a management address — IOS XE mapped to `cisco_ios`, EOS to `arista_eos`.
- Three workflows exist in Automation Studio and run green:

| Workflow | What it proves |
|---|---|
| `wf-netbox-device-count-v1` | The NetBox adapter works: its count equals NetBox's own API |
| `wf-show-version-v1` | Gateway 5's native `send-command` reaches a real router and a real switch |
| `wf-branch-vlan-v1` | The whole governed path: pick the next free VID in NetBox, reserve it, get it approved, push it to the switch, mark it active — and on a device failure, delete the reservation and end the job in error |

- The MCP server answers streamable HTTP at `mcp.${LAB_DOMAIN}:8000/mcp` and `get_health` returns the
  Platform version.

---

## Verification

`verify/test-05-itential.sh` is this chapter's script. `verify/devcmd.py` is the second source — every
device answer the Platform gives is cross-checked over direct SSH.

```
verify/test-05-itential.sh
```

| Criterion | What a PASS means |
|---|---|
| S4.1 | The Platform serves a lab-CA certificate for its own name, runs the pinned version, and Gateway 5 is registered and connected |
| S4.2 | The device-count workflow through the NetBox adapter equals NetBox's API |
| S4.3 | The show-version workflow via the gateway returns the running versions of a router and a switch, **cross-checked over direct SSH** |
| S4.4 | The VLAN workflow reserves and configures with approval; a second run is a no-op; a forced device failure rolls the NetBox reservation back |
| S4.5 | The licence state is recorded in the manifest |
| S4.6 | Memory on the Platform host is under 80 % **after 24 hours of uptime**. Under 24 hours it *defers* rather than passing — an early measurement is not a measurement |
| S4.7 | The MCP server answers from the workstation and `.mcp.json` matches |
| S4b.1 – S4b.5 | The ServiceNow adapter is `RUNNING`; a change-managed VLAN walks New → Scheduled → Implement → Review → Closed around the device work; the PDI rebuild record exists; the instance name is in `.env` and the manifest with no password in the repo; the PDI has had an interactive login within 10 days |

The script writes one NetBox VLAN and one switch VLAN per run and removes both at the end, plus one change
request in the PDI. **A hibernated PDI prints `HIBERNATED` and never passes silently.**

Two environment overrides are useful, and chapter 08 relies on them: `IT_IP` pins the Platform to a
specific host regardless of DNS, and `ONLY="S4.4"` runs a single criterion while iterating.

---

## Troubleshooting

**Every image operation fails with an authorization error.** The ECR token lasts 12 hours and the SSO
session behind it expires sooner than you expect. `aws sso login --profile ${ECR_PROFILE}`, then re-run.
The token is minted on the workstation deliberately; if you find yourself putting AWS keys on the VM,
stop — `images/fetch.sh` exists so that never happens.

**`images/fetch.sh itential-load` hangs partway through the image list.** Fixed, but worth knowing why: the
streamed `ssh` in the load loop was also consuming the loop's own stdin, so the loop ran out of items and
sat waiting. Any `ssh` inside a `while read` loop needs its stdin redirected.

**The Platform never becomes healthy and `/health` looks like it redirects.** Wait on `/health/status`, not
`/health` — the latter redirects to the login page, so a naive check "succeeds" against a Platform that is
not up. First boot legitimately takes up to five minutes.

**The gateway cluster cannot be created, or is created and owns nothing.** You are logged in as the
built-in local `admin`. Gateway Manager honours group membership only for AAA-provisioned users, and the
built-in admin is never in a group — writing the membership straight into MongoDB does not help either
(observed). Log in as the LDAP user. This is the single most confusing failure in the chapter because
every API call succeeds; the resources just do not belong to anyone.

**An adapter installs but will not start.** Two ownership traps. `npm` must run as uid 1001 because that is
the uid the Platform reads the files as, and the adapter checkouts are owned by 1001 while being managed by
root, so `git` needs to be told to trust those directories. A mismatch gives you an adapter that is present
and permanently `STOPPED`.

**A workflow imports but the canvas is empty, or the import is rejected.** Platform 6.5's workflow
documents (`canvasVersion` 3) have engine conventions that are not documented for hand authoring. The
generator in `itential/workflows/build.py` records the ones learned the hard way:

- Import through **Automation Studio**'s import route; `workflow_builder/import` wants an encoded payload,
  `encodingVersion` must be **absent**, and tags must be objects, not strings.
- Task ids must be **hex**.
- Adapter tasks address the model by its **export** name and the instance by `adapter_id` — that is the
  engine's `validateAdapterMethod` contract, and getting it the other way round fails validation with a
  message that does not say so.
- **`$var` references resolve only at the top level of a task's inputs.** A `$var` nested inside a JSON
  object is passed through as the literal string `$var...`. Build nested JSON with `Tools.replace` and then
  `Tools.parse`.
- Task outputs are published as job variables; that is how a later task reads an earlier one.

**The NetBox adapter's `available-vlans` call returns nothing.** The adapter's generic request helper
splits the path and drops the trailing slash that NetBox's endpoint requires — so the call succeeds and
returns an empty result rather than erroring. The VLAN workflow picks the next free VID with a few lines of
Python run by Gateway 5 (`runCode`) instead. The same defect is why journal entries are posted from the
runner rather than through the adapter (chapter 03).

**A `runCode` task cannot import a package.** `runCode` runs on the **runner**, not on the gateway server,
so the package has to be in the runner image. That is why the runner is built from a Dockerfile in the repo
rather than pulled.

**Ansible fails with an unknown stdout callback.** The `yaml` stdout callback was removed in
`community.general` 12. Use the default callback with `callback_result_format` instead.

**An `itential` group appears in the inventory containing a host called `itential`.** Do not tag a NetBox VM
with a tag equal to its own name — `nb_inventory` builds a group from the tag and the collision confuses
host and group resolution.

**A ServiceNow change refuses to move state.** The change model requires an assignment group; assign the
Network group before attempting the state walk. Also note that on 2026 instances basic auth needs the
`snc_basic_auth_api_access` role on the integration user, and the adapter's instance configuration needs
the full SSL block its schema declares even when it is all defaults. The change steps use the adapter's
generic request against the raw REST APIs, reading `sys_id.value` and `state.display_value`.

**S4.6 defers rather than passing.** That is correct behaviour under 24 hours of uptime. The check exists
to catch a slow leak, and a measurement taken twenty minutes after a restart cannot see one. Re-run it the
next day.

---

## Tested versions

| Component | Version |
|---|---|
| Itential Platform | 6.5.2 (private ECR, pulled by digest) |
| Itential Gateway 5 | 5.5.2-amd64 |
| Itential Gateway 4 | 4.4.1 — staged, **not deployed** |
| Itential MCP server | `ghcr.io/itential/itential-mcp` v0.14.0, streamable HTTP |
| MongoDB | 7.0.40, wiredTiger cache capped at 4 GB |
| Redis | 7.4.11 |
| OpenLDAP | `osixia/openldap` 1.4.0 (upstream dev-stack) |
| etcd (runner store) | v3.5.21 — Gateway 5 requires etcd v3.5 |
| Docker CE | 29.8.0, pinned and held |
| `adapter-netbox` | v1.0.10 |
| `adapter-servicenow` | v3.0.11 |
| VM | 8 vCPU / 24 GB / 160 GB, Ubuntu 24.04 |
| Licence | **None required** for this lab (owner decision, recorded in the manifest and checked by S4.5) |

`itential/versions.yaml` is the oracle for all of it, and `tests/test_itential.py` holds it to the image
manifest, the resource budget, the IP plan and the OpenTofu module. Nothing is ever `latest`.

---

**Previous:** [04 — The EVE-NG topology](04-eve-ng-topology.md) · **Next:** [06 — Platform applications](06-platform-applications.md)
