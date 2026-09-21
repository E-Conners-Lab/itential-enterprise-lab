# 10 — The Cisco NX-OS asset pack and a source-of-truth device import

Itential publishes an asset pack per vendor platform in [`itential/assets`](https://github.com/itential/assets).
The Cisco IOS pack is the mature one: software upgrade, port turn-up, golden-configuration compliance and
inventory management, wired to Inventory Manager and Gateway 5. The Cisco NX-OS pack was two folders and a
README that still described Gateway 4.

This chapter converts NX-OS to the IOS shape, imports it, and then proves the part that matters — **NetBox
decides which devices exist, and one workflow turns that into an Itential inventory**. No Nexus hardware is
involved, and none is needed.

> **Where the pack lives.** The asset pack itself — `Cisco/NX-OS/`, its build and its checks — is its own
> repository, [`E-Conners-Lab/itential-nxos-assets`](https://github.com/E-Conners-Lab/itential-nxos-assets),
> so it can be reviewed and contributed without this lab. This chapter keeps the lab side: the NetBox mock
> devices, the Platform steps, and the traps.

What you get:

- A Studio project with **19 components in five folders** — Software Upgrade, Port Turn Up, Golden
  Configuration, Inventory Management, Command Template Runner.
- **Three golden configuration trees** for device type `cisco-nx` — literal, Jinja2-with-regex, and a lab baseline.
- **Two mock Nexus devices in NetBox**, created by a play, removable by the same play.
- An **executed** source-of-truth import: NetBox → `dcim_devices_list` → Jinja render → Inventory Manager.

> **Why the devices are fictional.** There is no Nexus in this lab and no `n9kv` image in EVE-NG or
> Containerlab. The devices exist only as NetBox records, tagged `nxos-mock`, so the inventory workflow has
> something real to select. They are deliberately **not** in `topology/enterprise.yaml`: that file drives
> `topology/derive.py` and the EVE-NG builder, and these nodes have no EVE-NG counterpart. This proves the
> onboarding path, not device connectivity — nothing can SSH to them.

---

## Before you start

### What must already be true

- Chapter 03 green: NetBox is the source of truth and reachable at `10.100.0.64:8080` from the Platform.
- Chapter 08 green: production Platform 6.5.2 is running.
- `.env` carries `NETBOX_URL` and both tokens. The seeding play writes, so it needs `NETBOX_TOKEN`; the
  Platform only ever reads, so its integration gets `NETBOX_DEV_RO_TOKEN`.
- **An authorization group you belong to, carrying `inventory:read` and `inventory:update`.** In this lab
  that is `admin_group`. The asset ships `admins`; that group exists here (from LDAP) but does not give
  the admin user those roles, so on its own it returns `403`. List both, or only yours.
- Free addresses in `10.100.0.0/24` for the mock devices. The play uses `.150` and `.151` and does **not**
  allocate — check first.

### What this chapter does not do

It does not run Software Upgrade: that needs a second NX-OS image staged on the switch, and the install
reloads it. Everything else can be run against a real Nexus borrowed from a Cisco DevNet sandbox — see
*Optional: a real Nexus from Cisco DevNet* below. Without one, the assets import and validate and the
inventory path runs against the mock devices.

---

## The commands, in order

Most steps are Platform UI, because the Platform has no import API for a Studio project. Each row says what
it does and what it costs if you skip it.

| # | Step | What it does | If you get it wrong |
|---|---|---|---|
| 1 | `make netbox-nxos` | Creates platform `cisco-nxos`, device type Nexus 9000v, two devices with `mgmt0` and `primary_ip4`, tagged `nxos-mock` | Nothing to import later; the workflow returns zero devices |
| 2 | **Admin Essentials → Import → Integration Model**, upload the repo's `NetBox/OpenAPIs/netbox-latest.json` | Registers model `NetBox:latest` (329 operations) | `No config found for Adapter: NetBox:latest` |
| 3 | Create an integration instance from that model | Gives the model somewhere to send requests | Same error as above |
| 4 | **Studio → Projects → Import** `Cisco/NX-OS/Studio Projects/Cisco NX-OS.project.json` from the pack repo | The 19 assets | — |
| 5 | **Configuration Manager → Search (🔍) → Golden Configurations → Import** ×3 | The three `cisco-nx` trees | Looking for Import on the Golden Configurations page itself, which offers only **Create**. Import is in the 🔍 Collection window |
| 6 | Create an inventory named `nxos` with a group you belong to | The workflow populates an existing inventory rather than creating one | See the `403` trap below |
| 7 | Run **Create & Update Inventory from NetBox** with `inventoryName: nxos` | NetBox → Inventory Manager | — |

### Step 3 in detail — the integration instance

| Setting | Value |
|---|---|
| Instance name | `netbox-latest` |
| protocol / host / port | `http` · `10.100.0.64` · `8080` |
| `base_path` | **empty** — every path in the spec already begins `/api` |
| `tokenAuth` | `Token ${NETBOX_TOKEN}` — the **whole header value**, not the bare token |

Three traps in that one table:

- **The instance cannot be called `NetBox`** if a classic NetBox adapter already exists. Integrations are
  virtual adapters and share the namespace. The workflow's `adapter_id` must match whatever you do call it.
- **`tokenAuth` is an `apiKey` in the `Authorization` header**, so Itential sends the field verbatim as the
  entire header. NetBox 4.7 accepts `Token <token>` and `Bearer <token>` for a v2 token; the bare token
  alone gets a `403` with no useful message.
- **Use the Platform's address for NetBox, not the workstation's.** Both may answer from your Mac; only one
  is routable from the Platform.

### Optional: a real Nexus from Cisco DevNet

A reservable DevNet sandbox with an NX-OS 9000v (any CML-based lab that includes one) gives a real switch
for the rest of the pack. The sandbox is reached over its own VPN from your workstation, and the Platform's
Gateway runner reaches it through a tunnel to the Gateway host. Everything below is temporary test
scaffolding: none of it belongs in the contribution.

| # | Step | Why |
|---|---|---|
| 1 | `sudo openconnect --no-dtls --script <split-script> <sandbox-vpn-host>:<port>` | `--no-dtls` survives Wi-Fi blips; the split script routes only the sandbox subnet and leaves DNS alone (see the traps) |
| 2 | `ssh -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -R 127.0.0.1:2222:${DEVNET_SWITCH}:22 ubuntu@iag-01`, running a small forwarder that listens on the Docker bridge address `172.18.0.1:2222` | The runner container sits on the `itential_default` bridge, not the host network, so a loopback-only tunnel is invisible to it |
| 3 | Create inventory `nxos-devnet` with `createBrokerActions: true` and `defaultClusterId: lab` | The broker actions are how Configuration Manager and MOP reach the node |
| 4 | Add node `devnet-n9k`: `itential_host 172.18.0.1`, `itential_port 2222`, `itential_platform cisco_nxos`, and **`cluster_id: lab`** | Without `cluster_id` the device never appears in Configuration Manager |
| 5 | Add `nxos-devnet` to the `InventoryBroker` adapter's `inventories` | The broker publishes only the inventories it lists; the lab play sets exactly `["lab"]` |
| 6 | Set the placeholders on the Platform copy only: `clusterId: lab` on Port Turn Up's **Send Config** and Run Compliance's **Get treeId**; `groups` and `defaultClusterId` on **Create a new inventory** | The repo keeps its documented placeholders |
| 7 | Bind `devnet-n9k` to each tree's `base` node | Look the tree ids up by name after every re-import — they change when the file content changes |

Prove the path before running anything: Configuration Manager's `isAlive` for `devnet-n9k` must return
`true` in a few seconds. A 60-second `false` is the netmiko connection timeout — the tunnel or VPN is down.

---

## What "done" looks like

- NetBox holds **2** devices on platform `cisco-nxos`, status `active`, each with a `primary_ip4`.
- The Studio project lists **19 components across 5 folders**. Count them — see the trap below.
- **3** golden config trees exist with `device_type: cisco-nx`, unbound to any device.
- Inventory `nxos` holds **2 nodes**, and its `updated_by` is `Pronghorn` — the engine wrote them, not you.
- The job ends `complete` with an empty `error` array, in about **3 seconds**.

---

## Verification

NetBox, using the workflow's exact filter:

```bash
curl -s -H "Authorization: Token ${NETBOX_TOKEN}" \
  "http://10.100.0.64:8080/api/dcim/devices/?platform=cisco-nxos&status=active" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["count"], [r["name"] for r in d["results"]])'
```

Expect `2 ['dc1-nxos01', 'dc1-nxos02']`. A count of `0` means step 1 did not run; a count of `2` with a
missing `primary_ip4` means the workflow will skip the device silently.

The asset pack ships its own checks. They read only local files, so they run anywhere:

```bash
git clone https://github.com/E-Conners-Lab/itential-nxos-assets && cd itential-nxos-assets
./build/check.sh
```

All six must pass. `baseline_check.py` is the interesting one: it re-runs the structural checks against the
*upstream* Cisco NX-OS and Cisco IOS projects and reports how many faults were **introduced** by the
conversion versus **inherited**. That number must be zero.

On the Platform, verify by **counting components**, never by the absence of an error:

```
describe_project "Cisco NX-OS"     -> 19 components
get_golden_config_trees            -> 3 trees, device_type cisco-nx
get_inventories                    -> nxos, nodeCount 2, updated_by Pronghorn
```

---

## Troubleshooting

Every entry below stopped this build.

**The Studio project imports "successfully" but is missing a component.** The importer drops a JSON Form it
dislikes and reports nothing — the project appears, just smaller. Two independent causes were found in one
form, each sufficient alone: a `version` that is not the string `"2020.1"` (it is the JsonForms schema
draft, not an asset revision), and any struct item of `type: boolean`. Across five published Itential
projects, form items are only ever `string` or `number`; a form in the same project containing `number`
items imported fine, which isolated it to `boolean`. Count components after every import.

**A fix does not take effect on re-import.** A plain re-import does **not** replace an existing project's
component set. Delete the project first, then import. Two debugging cycles were lost to this: the fix was
correct and looked like it had failed.

Golden configuration trees behave the same way. Re-importing a tree whose name already exists leaves the
old lines in place, and the UI says nothing: the trees keep their original `created` date and line count.
Delete the three trees first, then import. Compare line counts afterwards (Simple 28, Jinja2 24, Lab 56);
the tree list alone cannot tell an old tree from a new one.

**`No config found for Adapter: NetBox:latest`.** The task addresses an *Integration Model* by
`title:version`, not an adapter instance. A classic NetBox adapter, however healthy, does not satisfy it.
Import the model (step 2) and create an instance from it (step 3).

**`Could not parse parameter value string as JSON Object or JSON Array`.** The `NetBox:latest` model declares
`platform` and `status` as **array** parameters and Itential enforces declared types. They must be literal
arrays — `["cisco-nxos"]`, not `"cisco-nxos"`. Worse, `["$var.x"]` neither errors nor resolves: the task
completes and sends the literal text, returning nothing.

**`403 — User must be a member of at least one of the assigned groups with roles: inventory:read and
inventory:update`.** The shipped asset hardcodes `groups: ["admins"]`. Use a group that exists and that you
belong to. Creating the inventory yourself beforehand avoids the problem entirely: the workflow only calls
`createInventory` when the lookup **errors**, so an inventory that already exists bypasses that task and its
two wrong values, `groups` and `defaultClusterId`.

**`ERROR: Ansible requires blocking IO on stdin/stdout/stderr`.** The agent shell hands Ansible
non-blocking file handles. Redirect to a file — `make netbox-nxos > /tmp/nxos-seed.log 2>&1` — which gives
it regular-file descriptors, and read the log afterwards.

**Warning: `nodes should be of type array but referenced task output is of type object`.** Benign, and
inherited from the upstream IOS asset: the Jinja task declares `castDataType: "object"` while rendering a
JSON array. Rendering the template locally against live NetBox data confirms it produces a real array. Left
alone rather than changing upstream logic that works.

**The workflow returns zero devices although NetBox has them.** The Jinja `platform_map` translates NetBox
platform *slugs* to netmiko platforms, and an unmapped slug is skipped silently. Upstream maps
`cisco-ios`, `cisco-ios-xe`, `arista-eos`, `paloalto-panos` — none of which match this lab's actual slugs
(`ios-xe`, `eos`, `panos`). The NX-OS pack accepts `cisco-nxos`, `nxos` and `nx-os`; add yours if it differs.

**`Failed to update Config. No config parser found for the given device type.`** Run Compliance fails at
the compliance task. Configuration Manager's NX-OS parser is called `cisco-nx`; the trees shipped
`cisco-nxos`, which is the NetBox platform slug, not a parser name. List the parsers with
`GET /configuration_manager/configurations/parser` before choosing a tree's device type.

**A compliant NX-OS switch still fails `username admin role network-admin`, `ssh login-attempts 3` and the
`version` line.** Matching is whole-line. NX-OS prints `username admin password 5 <hash> role
network-admin`, hides `ssh login-attempts 3` as a default, and appends `Bios:version` to the version line.
The trees now use `username admin password 5 {/\S+/} role network-admin` and
`version {/10\.[45]\([0-9]+\)/} {/Bios:version.*/}`, and drop the default. A regex word may span several
words. Proven with a throwaway probe tree bound to the real switch: one run, one line per candidate.

**`Adapter:undefined failed to invoke getDevice` for a node you just added.** Configuration Manager has not
published it. Check the node carries `cluster_id` and that its inventory is in the `InventoryBroker`
adapter's `inventories` list, then `POST /configuration_manager/devices/refresh`.

**Port Turn Up: `Invalid inventory filter schema: /nodes/0/nodeNames/0 must NOT have fewer than 1
characters`.** Upstream built the filter by splitting the device name on `::`, which only works when the
Device Broker prepends inventory names. This lab's broker does not. The Inventory Object template now reads
the device record's `_inventory_name` and `_original_node_name`, falling back to the split. Inherited from
Cisco IOS.

**`Cannot start job from draft workflow` — `Transformation tasks must reference an existing
transformation`.** Command Template Runner's error-message task pointed at a transformation id the
project never shipped. Fixed in the build; `verify.py` now rejects any dangling `tr_id`. Inherited from
upstream NX-OS. In Studio, re-selecting the transformation also **clears its input mapping** — reset
`templateName` to `$var.job.templateName` afterwards.

**A job is refused with `metadata.error: ["suppressSuccessMessage", "suppressFailureMessage"]`.** Software
Upgrade and Command Template Runner require both flags. The Platform rebuilds a workflow's input schema on
import from the `$var.job` references in its tasks, so neither dropping them from `required` nor a
`default` survives an import. Pass both as booleans. The Upgrade Form cannot, which leaves Software
Upgrade unstartable from its form as shipped.

**Claude Code, `lab.internal` names or the Platform stop answering once the DevNet VPN is up.** The
sandbox VPN installs itself as the default DNS resolver. Replace openconnect's default script with one that
adds only the sandbox route and never touches DNS (step 1 above).

**`isAlive` goes from `true` to a 60-second `false` and stays there.** The VPN or the tunnel dropped, and
the Gateway host kept the tunnel's far end open: the listeners still accept connections and lead nowhere.
End the orphaned `sshd` session and its forwarder on the Gateway host, then restart the tunnel. The usual
root cause here was the workstation's Wi-Fi changing address — a *Private Wi-Fi address* that differs per
network, or auto-joining a guest network — which also breaks every service that expects the workstation's
reserved address.

---

## Tested versions

| Component | Version |
|---|---|
| Itential Platform | 6.5.2 |
| NetBox | 4.7.0 |
| NetBox Integration Model | `NetBox:latest` — the repo's curated spec, 329 of 1194 upstream operations |
| NetBox API token | v2 format (`nbt_<key>.<secret>`), read-only (`write_enabled=false`) |
| `netbox.netbox` Ansible collection | 3.22.0 |
| Source project | `itential/assets` Cisco NX-OS, `_id` `66d0d1ba21161b4df27174c2`, last upstream update 2025-01-24 |
| Mock devices | Nexus 9000v, 2 × NetBox record only, no image, no connectivity |
| Golden config trees | 3, `deviceType: cisco-nx` |
| Real-switch test | Cisco DevNet sandbox Nexus 9000v, NX-OS 10.4(2), netmiko `cisco_nxos`, 2026-09-21 |
| Itential Gateway | 5.5.2 |
