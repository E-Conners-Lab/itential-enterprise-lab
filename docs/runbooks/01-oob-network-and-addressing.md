# 01 — Out-of-band network and addressing

The lab's management network: one gateway VM, one flat `/24`, one resolver, and a rule that the
hypervisor's existing network configuration is never touched.

Everything later in the series lands on this network. k3s nodes, the Itential VMs, every EVE-NG node and
every observability agent get an address from `10.100.0.0/24` and reach the outside world through the same
gateway. Get this chapter wrong and the symptom shows up three chapters later as something that "sometimes
hangs".

---

## Before you start

### What must already be true

- Chapter 00 is done: `make bootstrap` green, `.env` created, `make discover` ran and you have read its
  output in `verify/results/`.
- Your hypervisor has **two bridges**: `vmbr0` on your home LAN (this is the one that already works, and
  the one this repo never edits), and `vmbr1` — an isolated bridge with **no host IP and no carrier**. If
  `vmbr1` does not exist, create it by hand in the hypervisor UI before you start: bridge, VLAN-aware,
  untagged, no address, no ports. That is the only manual network change in the whole series.
- One free static address on `${HOME_LAN}`, outside the router's DHCP pool, for the gateway VM. This is
  `${OOB_GW_LAN_IP}` in chapter 00's table.
- A **static route on your home router**: `10.100.0.0/14` via `${OOB_GW_LAN_IP}`, on the LAN interface.
  This is manual step 1b in `docs/manual-steps.md`. It is what lets any device on `${HOME_LAN}` reach the
  lab without a per-host route, and — more importantly — it is what makes the return path symmetric. See
  the troubleshooting entry below if your router cannot do static routes.

### `.env` values this chapter reads

| Key | What it is |
|---|---|
| `PROXMOX_VE_ENDPOINT` | `https://${PVE_HOST}:8006` |
| `PROXMOX_VE_API_TOKEN` | Written back by the host-prep play on first run — leave it empty the first time |
| `EVE_HOST`, `EVE_USERNAME`, `EVE_PASSWORD` | Your EVE-NG host. Start with the factory password; the play rotates it |
| `NETBOX_URL`, `NETBOX_TOKEN` | Your existing NetBox. The token play replaces this with a described, expiring one |

### The design, in four decisions

| Decision | ADR | Why it matters here |
|---|---|---|
| Everything the lab allocates comes from `10.100.0.0/14`; OOB is `10.100.0.0/24` | [0003](../adr/0003-lab-supernet-and-oob-prefix.md) | Chosen to overlap nothing already on the wire — not the home LAN, not EVE-NG's `nat0`, not Docker's bridges, not k3s's `10.42`/`10.43` defaults |
| A dedicated `oob-gw` VM routes and NATs; the hypervisor gets **no** address on `vmbr1` | [0004](../adr/0004-oob-gateway-vm.md) | The hypervisor's `/etc/network/interfaces` stays exactly as discovered. `verify/test-02-oob.sh` S1.7 diffs it against a snapshot on every run |
| The lab zone is `${LAB_DOMAIN}` | [0005](../adr/0005-lab-dns-domain.md) | `.local` is mDNS and stalls macOS and systemd-resolved resolvers; `.internal` is ICANN-reserved for exactly this |
| Every reply from the lab to `${HOME_LAN}` goes back the way it came | [0030](../adr/0030-symmetric-return-path.md) | The subtlest thing in this chapter, and the one that produced "the handshake completes and then it stalls" |

### The address plan

`docs/ip-plan.md` is the human-readable version; `topology/ipam.yaml` is the machine-readable one, and it
is the oracle — `tests/test_ipam.py` proves the two agree row for row, and the seed play writes `ipam.yaml`
into NetBox. Nothing gets an address that is not reserved in NetBox first ([ADR 0002](../adr/0002-two-sources-of-truth.md)).

| Range | Block |
|---|---|
| `.1 – .15` | Infrastructure (`oob-gw` is `.1`, EVE-NG's OOB leg is `.2`) |
| `.16 – .31` | k3s nodes and the API VIP (chapter 02) |
| `.32 – .63` | MetalLB LoadBalancer pool (chapter 02) |
| `.64 – .95` | Service VMs (NetBox is `.64`; the production Itential VMs are `.71 – .81`) |
| `.128 – .191` | Network devices — firewalls, WAN edge routers, switches (chapter 04) |
| `.192 – .223` | Lab endpoints (chapter 04) |
| `.240 – .254` | DHCP pool, first boot and ZTP only |

These addresses are used literally throughout the series. They are private and invented in this repo, so
they are safe to copy as-is — unlike anything in chapter 00's fill-in table.

---

## The commands, in order

Look first. `make plan-oob` is read-only and shows you exactly what OpenTofu intends to create:

```
make plan-oob
```

You should see three resources to add: two templates (VM 9000 Ubuntu 24.04, VM 9001 Rocky 9) and the
gateway (VM 200). If it wants to *destroy* anything, stop and find out why before continuing.

Then build:

```
make phase-oob-network
```

That target is seven steps and the order is not negotiable:

| | Step | What it does |
|---|---|---|
| 1 | `ansible/playbooks/pve-host-prep.yml` | Additive host preparation: an API role, user and token (written straight into `.env`), the `snippets` content type, a 200 GB thin LV mounted at `/srv/images`, and the two cloud images downloaded and checksum-verified into `local:import/` |
| 2 | `tofu apply` in `tofu/oob` | The two cloud-init templates, then `oob-gw` — `net0` on `vmbr0` at `${OOB_GW_LAN_IP}`, `net1` on `vmbr1` at `10.100.0.1` |
| 3 | `ansible/playbooks/oob-gw.yml` | The guest: `nftables` masquerade OOB → LAN, router sysctls, the return-path policy route, `unbound` as the lab resolver, `dnsmasq` for DHCP only, `chrony` as the lab's clock |
| 4 | `ansible/playbooks/eve-oob.yml` | Hot-plugs a second vNIC onto the EVE-NG VM and puts `10.100.0.2/24` on `pnet1`, with its own return-path rule persisted in the interfaces stanza |
| 5 | `ansible/playbooks/netbox-oob.yml` | NetBox gets an OOB leg, the guest agent, a nightly `pg_dump` backup, and the Docker-safe conntrack return path |
| 6 | `ansible/playbooks/netbox-seed.yml` | Seeds prefixes, ranges and every static address from `topology/ipam.yaml` into NetBox. Idempotent, and it deletes reservations the plan has released |
| 7 | `ansible/playbooks/netbox-token.yml`, `ansible/playbooks/eve-password.yml` | Replaces the undescribed non-expiring NetBox token with a described one that expires in a year, then rotates EVE-NG off its factory password |

`make phase-oob-network` finishes by running `verify/run.sh`.

Finally, on each workstation — this needs `sudo` and cannot be done from the repo (manual step 6b):

```
sudo scripts/workstation-route.sh
```

It adds a `10.100.0.0/14` route via `${HOME_ROUTER}` and, on macOS, writes `/etc/resolver/${LAB_DOMAIN}`
pointing at the gateway's LAN leg. It is idempotent and it ends by pinging `10.100.0.1`, so it tells you
whether it worked. On macOS the route does not survive a reboot; re-run it.

---

## What "done" looks like

The first run is dominated by two downloads — the Ubuntu and Rocky cloud images, roughly 1 GB together —
so its elapsed time is mostly your bandwidth. Everything after that is a few minutes: the host play is
seconds once the images are staged, `tofu apply` takes about a minute per template and under a minute for
the gateway clone, and each guest play is a minute or two. A re-run with nothing to change is under five
minutes end to end, almost all of it `verify/run.sh`.

Observable results:

- `ping 10.100.0.1` answers from your workstation.
- `ssh ubuntu@10.100.0.1 hostname` prints `oob-gw`.
- `dig @${OOB_GW_LAN_IP} netbox.${LAB_DOMAIN}` returns `10.100.0.64`, and on macOS a plain
  `dig netbox.${LAB_DOMAIN}` does too, because of the scoped resolver file.
- NetBox's IPAM shows `10.100.0.0/24` with every static from the plan, each with its DNS name.
- The hypervisor has a new `/srv/images` mount with at least 150 GB free, and a `TofuLab` role and user.
- Your hypervisor's `/etc/network/interfaces` is **byte-identical** to what it was before you started.

---

## Verification

```
verify/test-02-oob.sh
```

or `make verify` to run every phase's script. Eleven criteria, all of which must pass:

| Criterion | What a PASS means |
|---|---|
| S1.1 | The workstation route works and `oob-gw` answers SSH with its own hostname |
| S1.2 | EVE-NG's `pnet1` has `eth1` as a member and holds `10.100.0.2`, reachable from the gateway |
| S1.3 | A throw-away clone of the Ubuntu template boots on a DHCP address, **resolves a name and completes an `apt update` through the gateway**, then is destroyed. This is the one that proves NAT, DNS and DHCP all actually work rather than merely being configured |
| S1.4 | NetBox equals `topology/ipam.yaml` — prefixes, ranges, addresses and DNS names — and `tofu plan` reports no changes |
| S1.5 | `/srv/images` is a mounted LV with ≥ 150 GB free |
| S1.6 | The EVE-NG API **rejects** the factory password and accepts the one in `.env` |
| S1.7 | The `vmbr0`/`nic1` stanzas match `verify/fixtures/pve-interfaces-vmbr0.expected` exactly |
| S1.8 | Client access end to end: a name resolves via the gateway's LAN leg and NetBox answers over the route |
| S0.1 – S0.3 | NetBox answers on its OOB leg; a backup newer than 24 hours exists and the guest agent is running; every NetBox token has a description and an expiry |

A PASS on S1.3 and S1.8 together is the real "done" for this chapter: the first proves the lab can reach
out, the second proves you can reach in.

---

## Troubleshooting

**A VPN client swallows the whole lab.** Several corporate and consumer VPN clients install a route for
`10.0.0.0/8`, which contains `10.100.0.0/14`. Nothing errors — the lab simply stops answering while the VPN
is up, and starts again when it is down, which makes it look intermittent. `scripts/workstation-route.sh`
installs a more specific `/14`, which wins over the VPN's `/8`. If your VPN client also captures DNS, the
macOS `/etc/resolver/${LAB_DOMAIN}` file it writes keeps lab names resolving through the gateway.

**The route must point at `${HOME_ROUTER}`, not at the gateway VM.** This is the trap that costs the most
time, because it half works. Pointing your workstation's `10.100.0.0/14` route straight at the gateway's
LAN leg looks more direct and is faster to type. What happens is that the gateway sends every reply for
`${HOME_LAN}` back through the router ([ADR 0030](../adr/0030-symmetric-return-path.md)) — so your outbound
packets go workstation → gateway, and the replies come back workstation ← router. Your workstation accepts
them, so a `ping` works and a TCP handshake completes. Then the flow **stalls**: something in the path with
state — the router's firewall, a dual-homed host, conntrack on the gateway — sees only one half and drops
the other. The failure mode is a hang, not a refusal, and it is intermittent enough to blame on the
service you were talking to. Send the route via the router, which is what the script does by default:

```
LAB_NEXT_HOP=${HOME_ROUTER} sudo scripts/workstation-route.sh
```

If your home router genuinely cannot hold a static route, you can point per-workstation routes at
`${OOB_GW_LAN_IP}` — but then you must also remove the gateway's return-path policy route, or you have
built the asymmetry deliberately.

**`tofu apply` fails with "VM 200 already exists" after an interrupted run.** An interrupted `apply` can
leave a VM on the hypervisor that is not in the state file. Do not delete the VM. Import it:

```
cd tofu/oob && tofu import proxmox_virtual_environment_vm.oob_gw <node>/200
```

Then re-run. The same applies to the templates (`9000`, `9001`), which are `for_each` resources and import
as `'proxmox_virtual_environment_vm.template["ubuntu-2404"]'`.

**`tofu apply` fails saying the disk cannot shrink.** A clone cannot have a smaller disk than its template.
The gateway's disk is declared as 16 GB *because that is the template disk size* — if you change the cloud
image, change both.

**The EVE-NG vNIC appears but `pnet1` has no member.** EVE-NG's `pnet` bridges are built from the
interfaces present at boot, and the hot-plugged NIC arrives afterwards. The play brings `pnet1` up
explicitly and retries, because `ifup` on a bridge whose member has just appeared can race. If it still
fails, check that the new interface is called `eth1` inside EVE-NG (`ip -br link`): the play's guard greps
for `eth1:` specifically, and a host that enumerated it differently will fail that assertion rather than
silently configure the wrong bridge.

**Names resolve on the gateway but nothing else can resolve.** `unbound` owns port 53 on the gateway, and
two other things want it. `systemd-resolved`'s stub listener is disabled by a drop-in
(`DNSStubListener=no`), and `dnsmasq` — which is there for DHCP only — is pinned to `port=0`. If you add a
package that starts its own resolver, you will get a bind failure on port 53 in `journalctl -u unbound`,
and a resolver that is simply absent rather than broken. Check with `ss -lnup 'sport = :53'`.

**NetBox loses its Docker networking after a reboot.** Debian's `nftables.service` flushes *all* nftables
tables when it stops, including the ones Docker installed, and NetBox runs from containers. The play leaves
that unit **disabled** on NetBox and loads only the return-path table through a dedicated oneshot unit. If
you enable `nftables.service` on a Docker host to "tidy up", NetBox's containers lose their published ports
on the next stop — with no error anywhere except that nothing answers.

**The NetBox token play fails with a 403 straight after creating a token.** NetBox's newer token format
needs the credential composed as `<key>` plus its provided prefix — a v1-shaped token string will
authenticate as nothing and return 403 on every call. The play composes the v2 credential explicitly, and
it also has to pass an explicit user id when creating the token, because the API will not infer the owner.
If you rotate a token by hand, use the same shape.

**The EVE-NG password rotation locks you out.** It cannot, by design, but the ordering is worth knowing:
the play generates the new password once, **writes it to `.env` before using it**, sets it through the API,
and only then proves it with a fresh login. If the API call fails, the old password is still valid and the
play fails loudly with both values recorded. Never generate a password inline in the API call — a
regenerated value on a retry is a password nobody has.

**`ping` works but `traceroute` from a lab host shows an extra hop, or the path changes after a few
minutes.** ICMP redirects. A router that accepts them will learn a "better" next hop that bypasses the
policy route, and the symmetric return path quietly stops being symmetric. The gateway sets
`accept_redirects=0` and `send_redirects=0` and flushes the route cache; if you add another router to the
OOB segment, it needs the same.

**S1.7 fails.** Something changed the hypervisor's `/etc/network/interfaces`. That is the one file this
repo promises never to touch, so the diff in the verify output is telling you about a change made outside
it — usually a bridge edited in the hypervisor UI. Reconcile deliberately: either revert the change, or
re-snapshot `verify/fixtures/pve-interfaces-vmbr0.expected` in a commit that says why.

---

## Tested versions

| Component | Version |
|---|---|
| Proxmox VE | 9.2 |
| OpenTofu | 1.10.x, provider `bpg/proxmox` 0.112.0 |
| Ubuntu cloud image | 24.04 noble, serial `20260826` (template VM 9000) |
| Rocky Linux cloud image | 9.8 GenericCloud (template VM 9001) |
| `unbound` / `dnsmasq` / `chrony` / `nftables` | Ubuntu 24.04 archive versions |
| EVE-NG | Community, Ubuntu 20.04 base |
| NetBox | 4.x on the existing VM, Docker Compose |

The cloud image is pinned to a dated serial path on purpose: the `noble/current/` path moves, and a
template rebuilt from a different serial is a different template with the same name.

---

**Previous:** [00 — Prerequisites](00-prerequisites.md) · **Next:** [02 — k3s and platform services](02-k3s-platform-services.md)
