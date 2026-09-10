# 03 — NetBox as the source of truth

NetBox owns every network fact in this lab. Nothing receives an address that is not reserved in NetBox
first, Ansible's inventory comes from NetBox rather than from a file, and by the end of the series the
agents answer questions about the network by reading NetBox rather than by logging into a device.

This chapter is not a phase of its own — the NetBox work is spread across chapters 01, 02, 04 and 06. It is
gathered here because "where does the truth live" is one idea, and it is easier to hold once than to meet
five times.

---

## Before you start

### What must already be true

- A NetBox instance you can reach and an API token for it. This lab used an existing NetBox on
  `${NETBOX_HOST}`, running `netbox-docker`, and chapter 01 gave it a second leg on the OOB network at
  `10.100.0.64`.
- Chapter 01 is green — its S1.4 criterion is the first proof that NetBox and the repo agree.
- `NETBOX_URL` and `NETBOX_TOKEN` in `.env`. Every `make` target exports both, and also exports
  `NETBOX_API` because the inventory plugin reads that name.

### The two sources of truth

[ADR 0002](../adr/0002-two-sources-of-truth.md) draws the line and it is worth being strict about:

- **NetBox owns network facts** — sites, devices, VMs, interfaces, prefixes, addresses, VLANs, VRFs, racks,
  circuits. Automation reads intent *from* NetBox. It never edits NetBox to match what it found on a
  device.
- **The repo owns everything else** — code, documents, decisions, verification results. If it is not in the
  repo, it did not happen.
- **`topology/` YAML is the single input that populates both** the EVE-NG lab and the NetBox objects, so
  the two cannot drift from each other.

That last point is the load-bearing one. NetBox is not typed into by hand at any point in this series.

### The three layers, and which play writes each

| Layer | Written by | From | Covered in |
|---|---|---|---|
| IPAM skeleton — prefixes, ranges, every static address with its DNS name | `ansible/playbooks/netbox-seed.yml` | `topology/ipam.yaml` | chapter 01 |
| Virtual machines — every VM this repo builds, with its reserved address attached | `ansible/playbooks/netbox-vms.yml` | `topology/ipam.yaml` and the phase's own list | chapters 02, 05, 08 |
| The network — sites, devices, interfaces, cables, VLANs | `ansible/playbooks/netbox-topology.yml` | `topology/enterprise.yaml` | chapter 04 |
| The enrichment — interface addressing, VRFs and route targets, ASNs, BGP neighbours, racks, circuits, config contexts, journal entries | `ansible/playbooks/netbox-enrich.yml` | `topology/enterprise.yaml` via `topology/derive.py` | this chapter, and chapter 06 consumes it |

---

## The commands, in order

The seed and the VM registration run inside their own phases. The enrichment has its own target so you can
re-run it on its own:

```
make netbox-enrich
```

It also runs automatically as the second step of `make phase-network-topology`, right after
`ansible/playbooks/netbox-topology.yml` — the skeleton has to exist before it can be enriched.

To use NetBox as the Ansible inventory:

```
cd ansible && ansible-inventory -i inventory/netbox.yml --graph
```

`ansible/inventory/netbox.yml` is a `netbox.netbox.nb_inventory` configuration. It groups by tags, cluster,
platform and device role, filters to `status: active`, and composes `ansible_user` — `automation` for
network devices, `ubuntu` for the cloud-init VMs. Every later chapter's plays run with
`-i inventory/netbox.yml`, so **a host that is not in NetBox, or not active, does not get configured**, and
the play succeeds having done nothing.

### What the enrichment derives

`topology/derive.py` is the single derivation, and both consumers read it: `eve/build.py` renders the
device startup configurations from it, and `netbox-enrich.yml` writes NetBox from it. That is what keeps
the device and the record equal by construction rather than by reconciliation.

| It writes | Detail |
|---|---|
| Interface addressing | Every addressed interface with its address in the right VRF, the `to <peer>` description the device actually runs, `mgmt_only` on the management port, and virtual interfaces (loopbacks, SVIs, tunnels, port-channels) created where the seed made only physical ones |
| VRFs and route targets | `MGMT`, `WAN` and `PROD`, with prefixes and addresses moved into their VRF |
| FHRP groups | The anycast gateway and the transit virtual router as groups with the leaf SVIs as members, rather than as duplicated addresses |
| ASNs | Seven private ASNs assigned to their sites |
| BGP neighbours | In each device's **local config context** — name, address, remote AS, VRF, description — derived by the same rules the configuration templates use |
| Racks | One location and one 42U rack per site, every device placed top down by role order at a unique position |
| Circuits | A provider and four transit circuits; the edge port and the provider port are cabled to the terminations so a cable trace *crosses the circuit* |
| Config contexts | `lab` (domain, DNS, NTP, management gateway), one per site, one per platform (the automation account name, never its password), and the per-device local context |
| Journal entries | One per site on any run that changed NetBox, naming the git commit; and one on the switch when a governed VLAN workflow completes |

**No `netbox-bgp` plugin.** `netbox-docker` would need a custom image for it, so BGP neighbours live in the
device's config context instead. That is a deliberate trade, not an oversight.

---

## What "done" looks like

The enrichment play takes a few minutes across the twelve network devices. The number that matters is the
**second** run: `changed=0`. If a re-run reports changes, something is not idempotent and the record will
oscillate.

Concretely, after the enrichment NetBox can answer questions it could not answer before:

- "What address does a given branch edge port carry, in which VRF, and to which peer?" — the interface has
  the address, the VRF and the description.
- "Which ASN belongs to this site?" — it is on the site.
- "Which BGP neighbours does this device run?" — in its config context.
- "Where is this device racked?" — a location, a rack and a position.
- "What does the path to the provider look like?" — a cable trace from the edge port that crosses a circuit
  to the provider's port, with no direct cable left over.

`make netbox-enrich` writes one journal entry per site naming the commit that ran, so NetBox carries its
own history of who reconciled it and when.

---

## Verification

`verify/test-06c-netbox.sh` is this chapter's script, with `verify/devcmd.py` as the independent second
source — it reads the devices directly over SSH and compares, so a claim that NetBox is right is never
checked against NetBox.

```
verify/test-06c-netbox.sh
```

| Criterion | What a PASS means |
|---|---|
| S4e.1 | Every addressed interface of the twelve network devices is in NetBox with its address, VRF and `to <peer>` description — **and the devices agree** |
| S4e.2 | VRFs with route targets, prefixes in `PROD`, the seven ASNs on their sites, and the BGP neighbours and RDs in each device's config context equal to what the device runs |
| S4e.3 | One location and one rack per site, every device racked at its derived position |
| S4e.4 | Provider circuits with terminations at their sites; the cable trace from each edge port crosses the circuit; no direct provider cable remains |
| S4e.5 | Journal entries: every site carries enrichment entries naming a real commit, and a switch carries the lifecycle create and delete entries |
| S4e.6 | The play re-runs with `changed=0`, and the source-of-truth agent answers address, VRF, peer, ASN, rack, circuit and context questions **equal to the NetBox API** |

Chapter 01's `verify/test-02-oob.sh` also covers NetBox itself: S1.4 (NetBox equals `topology/ipam.yaml`),
S0.1 (it answers on its OOB leg), S0.2 (a backup newer than 24 hours and the guest agent running) and S0.3
(every token has a description and an expiry).

---

## Troubleshooting

**A play runs against zero hosts and reports success.** The inventory is NetBox, and it filters on
`status: active`. A device or VM that is `planned`, `offline` or simply absent is not in the inventory, and
Ansible has nothing to do rather than something to complain about. Check with
`ansible-inventory -i inventory/netbox.yml --graph` before assuming a play is broken.

**`NETBOX_API` is not set.** The inventory plugin reads `NETBOX_API`, not `NETBOX_URL`. Every `make` target
exports it from `.env`; running `ansible-playbook` directly from a shell that only has `NETBOX_URL` gives a
plugin error that reads like an authentication failure. Run via `make`, or export both.

**Every API call returns 403 with a freshly created token.** NetBox's current token format needs the
credential composed from the key together with its provided prefix; a v1-shaped string authenticates as
nobody and gets a 403 on everything, including `GET`. `ansible/playbooks/netbox-token.yml` composes the v2
credential explicitly and passes an explicit user id when creating the token, because the API will not
infer the owner.

**The enrichment play is never `changed=0`.** Three measured causes, all on NetBox 4.7.0:

- *A shared (anycast) address moves between two devices on every run.* The collection's `ip_address` lookup
  matches on the address alone, so an address assigned to two leaves is repeatedly "moved" from one to the
  other. The anycast VTEP, the anycast gateway and the virtual router go through the REST API with a
  per-device lookup and the `anycast` role instead.
- *A prefix is duplicated.* `netbox_prefix` with a VRF creates a second copy of a prefix that was seeded
  globally. The seed creates VRFs before prefixes and places each prefix in its VRF; the enrichment retires
  a leftover global copy or moves a lone one.
- *A cable is recreated every run.* `netbox_cable` cannot look a cable up by its termination on the next
  run, so circuit cables are created through the REST API directly.

**Config contexts exist but no device sees them.** A tag on a config context is an *assignment criterion*,
not a label. Tagging a context restricts it to objects carrying that tag — so a context tagged for
housekeeping becomes invisible to everything. The contexts here carry no tags. This one cost real time
because nothing errors: the context is there in the UI, and the rendered context is simply empty.

**A device will not take a rack position.** NetBox refuses a position for a device type with a 0U height.
The seed creates device types as 1U for exactly this reason. If you add a type by hand, give it a height.

**A journal entry never appears after a governed workflow.** The NetBox adapter at 1.0.10 has no journal
method, and its generic request helper splits the path and drops the trailing slash that NetBox's API
requires — so the call returns without creating anything and the workflow task reports complete. The
workflows post journal entries from Python on the Gateway 5 runner instead, with the URL and token from the
runner's environment (never from job variables, which would put the token in the job record).

**A verification comparison truncates.** The fleet-wide "show" workflow caps its result at 1500 characters
per device, so a device with a long output comes back short and the comparison fails on the missing tail,
not on a real difference. The verify re-reads that device through the single-device workflow. If you write
your own comparison, do the same.

**NetBox stops answering after a reboot.** See chapter 01: NetBox runs from containers, and Debian's
`nftables.service` flushes Docker's tables when it stops. That unit stays disabled on the NetBox host, and
the return-path rules load from a dedicated oneshot unit that touches only its own table.

---

## Tested versions

| Component | Version |
|---|---|
| NetBox | 4.7.0, `netbox-docker`, **no plugins** |
| `netbox.netbox` collection | Pinned in `requirements.yml` |
| `pynetbox` | From `requirements-dev.txt`, in `.venv` |
| Inventory plugin | `netbox.netbox.nb_inventory`, `group_names_raw: true`, `config_context: false` |
| NetBox adapter (chapter 05) | 1.0.10 — the version whose generic request helper drops the trailing slash |

Measured object counts before the enrichment, for scale: 5 sites, 21 devices, 87 interfaces, 33 cables, 19
prefixes, 47 addresses — and zero VRFs, ASNs, racks, circuits, contexts or journal entries. Everything in
the enrichment table above is what closed that gap.

---

**Previous:** [02 — k3s and platform services](02-k3s-platform-services.md) · **Next:** [04 — The EVE-NG topology](04-eve-ng-topology.md)
