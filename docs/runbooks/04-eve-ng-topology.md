# 04 — The EVE-NG topology

Twenty-one nodes and twenty-nine links described in one YAML file, built into EVE-NG, seeded into NetBox
from the same file, and booted with a startup configuration that already has management, AAA and a routing
baseline.

This is the network the rest of the series automates. Twelve of the nodes are real network operating
systems — Cisco IOS XE and Arista EOS — so everything chapter 05 onward does is done against devices that
behave like devices.

---

## Before you start

### What must already be true

- Chapters 01 and 02 are green. The nodes get their management addresses from the OOB network and their
  DHCP reservations from the gateway.
- The vendor images are **on EVE-NG**, in the folder names EVE-NG expects. `eve/build.py plan` fails loudly
  and names any image it cannot find; that is the whole point of running `plan` first.
- The Ubuntu golden image for the Linux endpoints exists (manual step 15). It is a `qcow2` prepared by hand
  from the cloud image with an `automation` user, passwordless sudo, the owner's public key, and netplan
  DHCP. There is no build script for it yet — a rebuild repeats those steps by hand.
- `.env` carries `AUTOMATION_PASSWORD`. The rendered configurations contain no placeholders — the password
  and the PAN-OS hash come from `.env`, and a rendered config with a placeholder in it is a build failure.

### The topology

| | Nodes | Role |
|---|---|---|
| Provider | `isp-core01` (C8000v, AS 65000) | Hands each site a `/30` from `10.103.0.0/24` and a default route over eBGP |
| DC WAN edge | `dc1-wan01`, `dc1-wan02` (C8000v, AS 65100) | eBGP to the ISP, iBGP between them, GRE-over-IPsec tunnels to each branch |
| DC firewalls | `dc1-fw01`, `dc1-fw02` (PA-VM) | Active/passive. **Not built in this lab** — see the firewall-less build below |
| DC fabric | `dc1-spine01/02` (AS 65101), `dc1-leaf01/02` (MLAG pair, AS 65102), `dc1-acc01` | eBGP underlay on `/31`s, eBGP EVPN overlay with the spines as route servers, tenant VRF `PROD` with L3VNI 50001 |
| Branches | `brN-wan01` (C8000v), `brN-fw01` (PA-VM), `brN-sw01` (vEOS), `brN-pc01` (Windows 11), `brN-host01` (Ubuntu) | Each branch owns a `/20` from `10.102.0.0/16`; user VLAN 10 is its first `/24` |

Every node's **first** interface is management on the `pnet1` cloud, which is how it gets an OOB address.
Every point-to-point link is an EVE-NG bridge network the builder creates.

Design, alternatives and the vendor guidance behind each choice:
[ADR 0034](../adr/0034-lab-topology-design.md).

### The firewall-less build

`lab.firewalls: false` in `topology/enterprise.yaml`. The PA-VM image needs a support-portal download and
an auth code, so this lab was built without the firewalls and four links are marked `bypass: true` —
routed `/30`s from `10.101.3.0/24` between each DC WAN edge and a leaf, and a VLAN 10 access port from each
branch router to its switch, with the router as the VLAN 10 gateway.

Flipping the flag and re-running `apply` plus `push-configs` removes the bypass bridges, builds the
firewall nodes and renders their peers. **No addresses change** — the bypass eBGP is the same pattern the
firewalls use. While the flag is false, NetBox keeps the firewall devices as `planned`, and
`verify/test-04-topology.sh` defers the firewall criteria and checks the bypass path instead.

---

## The commands, in order

```
make phase-network-topology
```

| | Step | What it does |
|---|---|---|
| 1 | `ansible/playbooks/netbox-topology.yml` | Sites, device types, roles, devices, interfaces, cables, in-band prefixes and addresses — from `topology/enterprise.yaml`, before EVE-NG is touched |
| 2 | `ansible/playbooks/netbox-enrich.yml` | The enrichment of chapter 03 |
| 3 | `eve/build.py plan` | **Read-only.** What would be created, and a loud failure on any image EVE-NG does not have |
| 4 | `eve/build.py apply` | Creates the lab, the bridge networks, the nodes, the links, and uploads each node's rendered startup configuration. Idempotent — existing nodes and networks are matched by name and left alone |
| 5 | `eve/build.py start` | Starts the nodes. `--waves` starts the firewalls last, in two waves 300 s apart, for the PA-VM boot storm |
| 6 | `eve/build.py export` | Writes `topology/generated/eve-nodes.yaml`: EVE-NG node ids and management MACs |
| 7 | `ansible/playbooks/oob-gw.yml` | Re-runs so the gateway's DHCP reservations are rendered from that MAC table |
| 8 | `ansible/playbooks/lab-endpoints.yml` | The Linux endpoints: hostname from NetBox, the data port on `ens4`, MTU 1400, and the automation account |

Two things `make phase-network-topology` deliberately does **not** run:

```
.venv/bin/python eve/build.py push-configs      # re-render and re-push startup configs
.venv/bin/python eve/build.py status            # read-only
```

`push-configs` **wipes and restarts** the nodes it touches, so it is a considered action after a topology
or template change, never part of a routine build. Use `--only <node> <node>` to limit it.

---

## What "done" looks like

The first build is long — an hour or more of it is the nodes booting, and the C8000v and vEOS images are
not quick. `eve/build.py apply` itself is minutes; `start` returns as soon as the nodes are started, not
when they are usable. Give the routers several minutes before expecting SSH.

- Every node in the YAML exists in EVE-NG's `/enterprise.unl` lab and is running.
- Every node answers on its management address — SSH for the network devices and the Linux endpoints, RDP
  for the Windows clients.
- The ISP has four BGP peers. Each branch has two tunnels up. The leaves have two EVPN peers each and MLAG
  is active.
- `br1-host01` can reach `dc1-srv01` at `10.101.10.10`, through the branch edge, over the IPsec tunnel, to
  the DC edge, and into the fabric.
- NetBox's device, address and cable counts equal the YAML, and EVE-NG's link count does too.
- Summed node RAM inside EVE-NG stays within the 115 GB internal ceiling.

---

## Verification

`verify/test-04-topology.sh` is this chapter's script. `verify/devcmd.py` is the second source — it talks
to the devices over SSH directly, so nothing is checked only against the system that built it.

```
verify/test-04-topology.sh
```

| Criterion | What a PASS means |
|---|---|
| S3.1 | Every node in `topology/enterprise.yaml` exists in the EVE-NG lab and is running |
| S3.2 | Every node answers on its management address |
| S3.3 | NetBox devices, primary addresses and cable count equal the YAML; EVE-NG's link count equals the YAML |
| S3.4 | Routing state: the ISP's four peers, two tunnels per branch, two EVPN peers per leaf, MLAG active, firewalls in HA (or the bypass eBGP while they are deferred) |
| S3.5 | The end-to-end path from a branch client to the DC server actually carries traffic |
| S3.6 | `show version` on one node per vendor equals the manifest's running version |
| S3.7 | Summed node RAM in EVE-NG is within the internal ceiling |

With `lab.firewalls: false` the run is **7 passed, 1 deferred** — the deferral names the three firewall
checks explicitly rather than passing them silently. A deferral is not a pass; it is a promise recorded in
the log.

---

## Troubleshooting

**`eve/build.py plan` fails naming an image.** That is the intended behaviour, and it is worth reading the
name carefully: EVE-NG finds images by folder name, so `c8000v-17.13.01a` and `c8000v-17.13.1a` are
different images and only one of them exists. `apply --allow-missing` skips those nodes loudly if you want
to build the rest first.

**A node's links vanish after an `apply`.** Use **visible** bridge networks. EVE-NG Pro 6.5 does not
persist hidden ones, so a link built as hidden survives in memory and is gone after a restart — with no
error, and a topology that was correct ten minutes ago.

**A node's startup configuration does not take.** EVE-NG Pro uses config *sets*: the configuration is
uploaded against a config-set id (`cfsid`) and the node's `config` field is set to that id. Uploading a
config without wiring the set id leaves the node booting from nothing. This is what `push-configs` does
properly; it also uses `stopmode=3` when stopping a node so the wipe is clean.

**`apply` fails with a lock error.** EVE-NG holds a lock on a lab that is open in a browser session. Close
the lab in the web UI. The builder prints a hint when it sees this, because the API error itself does not
say so.

**Every `crypto` command is rejected on a C8000v and the tunnels never come up.** The 17.13.01a image boots
with an **empty licence level**, which hides the entire crypto command set — so the configuration applies
without error and the features simply are not there. The startup configuration sets
`license boot level network-advantage addon dna-advantage`. With the `config.iso` bootstrap the level is
active on first boot; `push-configs` checks it once a router answers on management and saves and reloads
only a router still showing an empty level. Throughput stays at the unlicensed 20 Mbps, which is plenty
here.

**IOS XE rejects a route you are sure is valid.** Two specific ones: `ip route vrf MGMT 0.0.0.0/0` is
rejected — the mask form is required — and a BGP `address-family ipv4 vrf` needs an `rd` on the VRF first.

**A leaf receives zero EVPN prefixes and it looks broken.** It is not. Both leaves share AS 65102, so every
route a spine reflects (with `next-hop-unchanged`) carries 65102 in its AS path and the receiving leaf
drops it as a loop. The spines accept fourteen EVPN prefixes from each leaf, which is the direction that
matters, and with one MLAG pair sharing an anycast VTEP there is nothing for a leaf to learn from its peer.
A second leaf pair needs `allowas-in` on the leaves, or a distinct AS per pair.

**`show mlag` says dual-primary detection is "Disabled" although it is configured.** Both leaves carry
`dual-primary detection delay 5 action errdisable all-interfaces`, and `show mlag detail` confirms it. The
summary line reports the *runtime arming* state, and no explicit heartbeat peer address is set — the
heartbeat runs over the peer address and is alive with zero timeouts. Whether vEOS-lab arms detection
without a dedicated heartbeat peer is unconfirmed; the intent has not drifted.

**Windows 11 will not install, or installs and will not boot.** Three separate traps, in order:
25H2 setup refuses a BIOS/MBR target even with the `LabConfig` registry bypass, so the image is built under
OVMF with Secure Boot and a TPM 2.0 — the requirements are genuinely met rather than bypassed. On EVE-NG
the node runs QEMU with `q35` and OVMF as pflash, and **the firmware files ship inside the image folder**
because QEMU runs chrooted in the node directory and cannot see the host's. The disk is `hda.qcow2` on
SATA, because Windows was installed on AHCI and has no boot-start virtio driver. And EVE-NG's node `PUT`
does not persist `qemu_options`, so a node needing different options is **re-created**, not edited.

**A Windows client boots but has no network.** The NIC is `e1000`, deliberately: the image sent nothing at
all on `virtio-net` because it has no inbox driver for it. No error, no link — just silence.

**A Linux endpoint comes up with the wrong interface configured.** The golden image's netplan matches `en*`
for DHCP, and `networkd` applies the lexically first unit — so a catch-all silently wins over the specific
definition you just wrote. `lab-endpoints.yml` narrows the definition to `ens3`. Data ports run **MTU
1400**, because VXLAN plus IPsec has to fit inside the 1500-byte fabric and WAN links.

**A PAN-OS password hash cannot be generated.** Python 3.13 removed the `crypt` module. The build renders
the hash through `openssl` instead.

**The EVE-NG API returns something the builder does not expect.** Its list and empty shapes are not
consistent — an empty collection and a populated one come back differently, and interface data can arrive
as a list where a dict is documented. The builder tolerates both. If you extend it, assume neither.

---

## Tested versions

| Component | Version |
|---|---|
| EVE-NG | Pro 6.5, Ubuntu 20.04 base |
| Cisco C8000v | IOS XE **17.13.01a** (the image already loaded; [ADR 0032](../adr/0032-c8000v-stays-on-17-13-01a.md) supersedes the 17.18.4 plan) |
| Arista vEOS-lab | **4.33.1.1F** ([ADR 0033](../adr/0033-veos-stays-on-4-33-1-1f.md)) |
| Palo Alto PA-VM | 11.1 — **not built**, `lab.firewalls: false` |
| Windows 11 | 25H2 Enterprise evaluation, built unattended under OVMF + TPM 2.0, run on EVE-NG under QEMU 5.2.0 with `q35` and pflash OVMF, `e1000` NIC, SATA disk |
| Linux endpoints | Ubuntu 24.04 golden image, `linux-ubuntu-24.04-server`, 1 GB, MTU 1400 on the data port |
| Topology | 21 nodes, 29 links, 106 GB of a 115 GB internal ceiling |

`show version` on one node per vendor is compared against the manifest by S3.6 on every run, so the table
above cannot quietly stop being true.

---

**Previous:** [03 — NetBox as the source of truth](03-netbox-source-of-truth.md) · **Next:** [05 — The Itential dev stack](05-itential-dev-stack.md)
