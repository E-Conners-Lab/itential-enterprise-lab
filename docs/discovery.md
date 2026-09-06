# Phase 0 — Discovery (read-only)

Captured 2026-09-06 by `make discover` (`verify/discover.sh`). Raw output is in
`verify/results/*-discover.log`. Nothing on the infrastructure was changed.

## 1. Proxmox host `homelab` (192.168.68.161)

| Item | Finding |
|---|---|
| Hardware | Dell PowerEdge R640, 2x Xeon Gold 6154 (72 threads, 2 NUMA nodes), 314 GB RAM (38 GB in use) |
| Software | Proxmox VE 9.2.11, kernel 7.0.2-6-pve, ifupdown2, PVE firewall disabled, no SDN |
| Nested virt | `vmx` present, `kvm_intel.nested=Y` (EVE-NG and Containerlab nested KVM work) |
| Storage | Single 1.92 TB SAS SSD (HGST, behind PERC H730P as RAID0). LVM: `pve-root` 96 GB (17 GB used), swap 8 GB, thin pool `local-lvm` 1.6 TB (3.4 % used). VG free: 16 GB |
| Storage content | `local` (dir): iso, vztmpl, backup, import. `local-lvm` (thin): images, rootdir. **No `snippets` content type** on any storage |
| ISOs present | `eve-pro-prod-bm-6.5.0-21-full.iso` (8.6 GB), `noble-cloud.img` (Ubuntu 24.04 cloud image, 625 MB) |
| Staging path | `/srv/images` does not exist yet |
| NICs | 4 onboard: `nic0`/`nic1` BCM5720 1 GbE, `nic2`/`nic3` BCM57416 10 GbE dual-media. Only `nic1` has link |
| `vmbr0` | `nic1`, 192.168.68.161/22, gw 192.168.68.1. **Home LAN, do not touch without asking** |
| `vmbr1` | `nic2`, VLAN-aware (VIDs 2-4094), no host IP, **no carrier** (nothing plugged into nic2). Created 2026-09-06 as the isolated lab bridge |
| VMs | 110 `netbox-prod` (4 vCPU, 8 GB, 64 GB, q35, cpu=host, agent flag on, cloud-init, `vmbr0`, onboot). 300 `eve-ng` (24 vCPU, 128 GB, 200 GB, q35, cpu=host, **only `net0` on `vmbr0`**, onboot). No LXC, no templates |
| Allocation today | 28 vCPU / 72 threads (0.39:1), 136 GB / 314 GB RAM, 264 GB thin-provisioned disk |
| Users / API | `root@pam` only. **No API tokens exist.** Root SSH with the workstation ed25519 key |
| Host packages | `libguestfs-tools`, `git`, `python3-proxmoxer`, `ansible` all **not installed** |
| DNS / time | resolver 8.8.8.8, TZ America/New_York, NTP synchronized |

## 2. EVE-NG VM 300 (192.168.68.240)

| Item | Finding |
|---|---|
| Software | EVE-NG **Pro 6.5.0-27**, Ubuntu 22.04.5, kernel 6.7.5-eveng-6-ksm+, EVE qemu 2.4.0, KSM enabled, UKSM unsupported |
| License | Pro, expires **2027-04-15** (`eve_expire`), single `admin` user (role admin, no expiry) |
| Resources | 24 vCPU, 125 GB RAM (12 GB used), 195 GB root (39 GB used, 148 GB free) |
| Nested virt | `vmx` visible in guest, `/dev/kvm` present |
| Networking | `eth0` -> `pnet0` 192.168.68.240/22. `pnet1`..`pnet9` bridges exist **with no member interface** because the VM has a single vNIC. `nat0` 172.29.129.254/24, `wg0` 172.29.130.254/24 (Pro WireGuard), `docker0` 172.17.0.1/16 |
| QEMU images | `c8000v-17.13.01a` (virtioa.qcow2, 2.0 GB), `veos-4.33.1.1F` (hda.qcow2 + cdrom.iso), `linux-ubuntu-24.04-server`, `linux-ubuntu-24.04-tools`. **No** PA-VM, Panorama, Windows Server, or Windows client images. No IOL, no Dynamips |
| Docker images | eve-wireshark, eve-gui-server, eve-desktop, eve-firefox, registry:2 |
| Labs | `Test.unl` only |
| REST API | Reachable at `https://192.168.68.240/api/`, login works |
| Tools in VM | qemu-img 6.2, virt-customize 1.46.1, python3 3.10, jq. **pip3 missing.** qemu-guest-agent active |
| **Security** | Web/API `admin` password is still the **factory default**. Rotate in Phase 2 and store in `.env` (Vault later) |

## 3. NetBox VM 110 (192.168.68.110)

| Item | Finding |
|---|---|
| Guest | Ubuntu 24.04.4, 4 vCPU, 8 GB, 61 GB disk (4.4 GB used). Root SSH with workstation key |
| Stack | Docker 29.8.0, Compose v5.5.1, **netbox-docker 5.1.0** at `/opt/netbox-docker` |
| Containers | `netboxcommunity/netbox:v4.7-5.1.0`, `postgres:18-alpine`, `valkey/valkey:9.1-alpine` x2, all healthy. Published port 8080, **HTTP only** |
| NetBox | **4.7.0**, Django 6.1, Python 3.14.4, **no plugins**. `API_TOKEN_PEPPER` is set (v2 tokens work) |
| Data | Every object type has **0 objects** (fresh install). 1 user (`admin`, superuser). 1 API token (id 4, v2, no expiry, no description) |
| Gaps | qemu-guest-agent **not running** in the guest (Proxmox has the agent flag on, so `qm guest` calls time out). No cron, **no backups** |

## 4. k3s

No cluster exists. The previous 3-node cluster (192.168.68.101-103) died with the
old SSD. The workstation still has a stale kubeconfig pointing at .101.

## 5. Workstation and GitHub

- macOS workstation with `tofu` 1.12.6, `ansible-core` 2.21, `ansible-lint` 26.8, `yamllint` 1.38, `gitleaks` 8.30.1, `pre-commit` 4.6, `gh`, `jq`. All pre-commit hooks and `tofu validate` pass locally.
- `gh` is authenticated as **E-Conners-Lab**, which is a **user account, not an organization**. Repo created private, `main` protected (PR required, 1 approving review, required checks `tofu`/`ansible-lint`/`yamllint`/`gitleaks`/`links`, linear history, no force-push, conversation resolution). Merge methods: squash and rebase only, branches auto-deleted on merge.
- Existing local repos that this project can borrow from: `Proxmox_Kubernetes` (k3s + Cilium + MetalLB + Longhorn + Vault runbooks), `Loop_Engineering` (NetBox seeding, v2 token handling, Containerlab campus), `ccie-brain/k8s` (Kustomize + Vault agent injection patterns).

## 6. Assumptions I would otherwise have to make

Each one is a decision the PID (Phase 1) or a later PR should settle. Nothing below has been acted on.

1. **Reviews on a solo repo.** GitHub will not let the PR author approve their own PR. `enforce_admins` is off, so you (repo admin) can merge with the review requirement unmet. If you want a real second reviewer, add a second account as a collaborator.
2. **`vmbr1` uplink stays unplugged.** The OOB network works host-internally (Proxmox VMs plus EVE-NG `pnet1`) with no cable on `nic2`. A physical lab switch is optional and out of scope.
3. **OOB addressing.** No prefix chosen yet. Proposal for the IP plan: one /24 from RFC1918 space that does not overlap the home LAN /22, EVE's `nat0`/`wg0` /24s, or Docker's 172.17/172.18. Untagged on `vmbr1` (PVID 1) unless the PID decides on a VLAN.
4. **Proxmox API identity.** OpenTofu needs an API token. Plan: a dedicated `tofu@pve` user with a scoped token created over root SSH in Phase 2 (automatable), stored only in `.env`. Root password auth is not used.
5. **`snippets` content type** must be enabled on `local` for cloud-init custom user-data via the bpg provider. Host config change, Phase 2.
6. **EVE-NG second vNIC.** VM 300 needs `net1` on `vmbr1` for `pnet1`. Hot-plug should work without a reboot (q35, virtio, EVE's `/etc/network/interfaces` already defines `eth1`/`pnet1`). Phase 2, and a reboot fallback is documented if hot-plug fails.
7. **Image staging capacity.** `/srv/images` on `pve-root` has ~73 GB free. PAN-OS (~4 GB each), Panorama, C8000v, vEOS, Windows Server ISO (~5 GB), NIOS (~3 GB) fit, but a dedicated LV from the thin pool (for example 200 GB mounted at `/srv/images`) is safer. Decide in Phase 1.
8. **RAM budget.** EVE-NG alone holds 128 GB. With the 280 GB ceiling that leaves ~152 GB for every service VM and k3s. The PID must show this fits, or EVE's allocation gets trimmed (it uses 12 GB today).
9. **k3s placement.** A cluster is required for the Helm/Kustomize services. Assumed: a "platform" phase that builds a 3-node k3s cluster from the cloud-image template before the observability phase, reusing the `Proxmox_Kubernetes` runbook stack (Cilium, MetalLB, Longhorn).
10. **NetBox stays where it is** (docker-compose on VM 110) rather than moving to k3s. It gets an OOB leg, HTTPS via a reverse proxy when Keycloak SSO arrives, and a backup job.
11. **NetBox host guest agent** gets installed and enabled by Ansible in Phase 2 so Proxmox/OpenTofu can read guest IPs.
12. **EVE admin password** is rotated in Phase 2 and moved to `.env`; the discovery log flags the default.
13. **DNS domain.** EVE already uses `lab.local`; the IP plan should pick one lab domain (served by the DDI service) and every service gets a record there.
14. **Existing EVE images** (`c8000v-17.13.01a`, `veos-4.33.1.1F`) are candidates, not decisions. The image manifest will confirm whether they are current and API-stable.
15. **Workstation SSH key** (`elliot@mac-mini` ed25519, already trusted on the host, EVE-NG and VM 110) is the automation identity until Vault exists.
16. **Single SSD.** There is still no RAID1. Everything built here can be rebuilt from this repo plus staged images, which is the mitigation until a second drive arrives.
17. **Sizing of the old netbox-prod VM** (4 vCPU / 8 GB) is kept; the resource budget counts it.
18. **NetBox token hygiene.** The one existing v2 token has no description and no expiry. Phase 2 mints a described, scoped automation token and records the step in `docs/manual-steps.md` if it cannot be scripted.

## 7. Amendment 2026-09-06 (Phase 2 applied)

Changes made by `phase-2/oob-network`, so §1-3 above now differ in these points:

- Proxmox: `local` storage content is `iso,vztmpl,backup,import,snippets`; thin LV
  `pve/images` (200 GB) mounted at `/srv/images`; user `tofu@pve` with role `TofuLab` and
  token `tofu`; VMs 200 `oob-gw` (running), 9000 `tpl-ubuntu-2404` and 9001 `tpl-rocky-9`
  (templates). VM 300 and VM 110 each gained `net1` on `vmbr1` by hot-plug. `vmbr0`,
  `nic1` and `/etc/network/interfaces` unchanged (verified by `verify/test-02-oob.sh` S1.7).
- EVE-NG: `pnet1` = `eth1`, static 10.100.0.2/24. Admin password rotated (value in `.env`).
- NetBox: `oob0` at 10.100.0.64/24 with a route to 10.100.0.0/14 only; `qemu-guest-agent`
  running; nightly backup to `/var/backups/netbox/`; one API token (id 6, described,
  expires 2027-09-06); IPAM seeded from `topology/ipam.yaml`.
- Home router: static route 10.100.0.0/14 via 192.168.68.120 (owner). Router DHCP pool is
  192.168.68.131-192.168.71.250.
