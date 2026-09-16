# clab: the Containerlab dev topology

A small topology that the `itential-dev` stack (and GitHub Copilot through it) can use without touching the
EVE-NG lab or production (ADR 0063, PID S10.6-S10.12). It runs on VM 230, `clab` (10.100.0.224).

```
            iBGP AS 65010 (loopbacks)
 clab-rtr1 ------------------------- clab-rtr2        C8000v 17.13.01a (vrnetlab, nested KVM)
   Gi3 | eBGP 65010-65020                 | Gi3       OSPF area 0 on every link
   Eth1|                                  | Eth1
 clab-sw1 --------- trunk 10,20,99 ---- clab-sw2      vEOS-lab 4.33.1.1F (vrnetlab, nested KVM)
            iBGP AS 65020 (loopbacks), Vlan99 transit; VLAN 10 USERS / 20 SERVERS /27s announced in BGP
```

| File | What it is |
|---|---|
| `versions.yaml` | The oracle: VM, versions, mgmt network, nodes, links, VLANs, routing, the access allowlist. Everything else reads it; `tests/test_clab.py` holds it to the tofu module, `topology/ipam.yaml`, `docs/resource-budget.md` and `docs/image-manifest.md` |
| `dev.clab.yml.j2` | The containerlab topology. Holds no secret |
| `configs/c8000v.cfg.j2`, `configs/veos.cfg.j2` | Startup configs per kind. The device password comes only from `CLAB_AUTOMATION_PASSWORD` |
| `docker-user.sh.j2` | The DOCKER-USER allowlist for the mgmt bridge, installed with a systemd unit by `clab-host.yml` |

## Addressing and access

- **Mgmt:** Docker network `clab-dev-mgmt`, 10.100.2.0/24, gateway 10.100.2.1 on bridge `br-clab-dev`. It is
  routed, not port-forwarded: oob-gw (`oob-gw.yml --tags routes`) and itential-dev (`itential-host.yml`) carry
  a static route via 10.100.0.224, so the Mac and the dev stack reach every node on its own address. Return
  traffic is symmetric (ADR 0030): node -> clab -> oob-gw -> the home router for the Mac.
- **Allowlist:** only `access_allow` (itential-dev, clab itself, the home LAN) may open connections to the
  nodes. Everything else on the OOB segment, production `iag-01` included, is dropped. The topology sets
  containerlab's `external-access: false` so nothing adds an accept-all beside it.
- **In-band:** 10.100.3.0/24 (links, loopbacks, VLANs) never leaves the clab VM.
- **Not in NetBox:** the nodes are never registered. Production's inventory, observability and verify read
  every active NetBox device without a site filter, so a clab device there would leak into production.

## Build

`make clab-dev` runs the whole sequence. By hand:

1. `cd tofu/clab && tofu apply`: VM 230 with CPU type `host` (nested KVM)
2. `images/fetch.sh c8000v`: copies the EVE-NG image to `/srv/images/c8000v/` under the filename vrnetlab
   parses (checksum taken on both ends)
3. `images/fetch.sh veos`: copies the EVE-NG switches' vEOS-lab image to `/srv/images/veos/` under the
   version name vrnetlab parses (checksum taken on both ends). No Arista download: `images/fetch.sh arista`
   (Arista's container image) is for the phase 12 CI twin, not this topology
4. `ansible-playbook -i inventory/netbox.yml playbooks/clab-host.yml`: baseline, Docker, containerlab,
   mgmt network, allowlist, vrnetlab at the pinned commit
5. `images/fetch.sh clab-load`: staged images to the VM's `/srv/stage`, then `clab-host.yml` again to convert
   the vEOS qcow2 to vmdk and build `vrnetlab/arista_veos:4.33.1.1F` and `vrnetlab/cisco_c8000v:17.13.01a`
6. `ansible-playbook -i inventory/netbox.yml playbooks/clab-dev.yml`: generates and persists
   `CLAB_AUTOMATION_PASSWORD`, deploys, fixes the C8000v licence level (one reload), waits for OSPF and BGP
7. `verify/test-12a-clab-dev.sh`: S10.6-S10.12

## Known behaviour

- A C8000v first boot takes 10-20 minutes nested, a vEOS about 4; `clab-dev.yml` waits up to 30 minutes for a
  real login. vrnetlab types the vEOS startup config in over the console after boot, so the `automation` login
  can work a little before routing is configured; the convergence checks retry until it is.
- The switches are vEOS-lab VMs, not Arista's container image (ADR 0063 amendment 2026-09-16): the same image
  the EVE-NG lab switches run. Management is `Management1`, set by vrnetlab's bootstrap; the startup config
  never touches it.
- vrnetlab's image build sets licence level `network-premier`; the startup config asks for
  `network-advantage` (the EVE-NG lab's level), so every fresh deploy reloads each router once.
- No node keeps a vendor default password (PID success criterion 5). vrnetlab's C8000v and vEOS bootstraps
  both create `admin`/`admin` (containerlab's default credentials for both kinds); the startup configs set
  `admin`'s secret to `CLAB_AUTOMATION_PASSWORD`, the same as `automation`. vrnetlab applies the startup config
  after its bootstrap and never logs in again (`cisco/c8000v/docker/launch.py` and
  `arista/veos/docker/launch.py` at the pinned commit), and every play and verify here uses `automation`
  (`verify/devcmd.py`).
  `containerlab save`, which would log in with the kind defaults, is not used.
- `containerlab deploy --reconfigure` runs only when a rendered file changed or a node is down, because it
  reboots both routers.
