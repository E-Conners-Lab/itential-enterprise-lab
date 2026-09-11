# Image manifest

Phase 1 deliverable. Every image the lab uses, with the version decision and
its evidence. **Versions were verified on 2026-09-06 by web research against
the vendor pages linked in each entry; nothing here comes from memory.** Facts
that could not be confirmed from a readable primary source are marked
**UNVERIFIED** and must be resolved by the phase that imports the image.
Login-gated downloads are done by the owner (`docs/manual-steps.md`), who fills
in the *actual filename / checksum* columns of the staging record at
`/srv/images/MANIFEST.sha256` on the Proxmox host; Phase 2 verifies checksums
before any import.

Conventions: **Min** is the vendor's published minimum; **Lab** is what this
repo allocates (and what `docs/resource-budget.md` counts). EVE-NG image
naming per <https://www.eve-ng.net/index.php/documentation/qemu-image-namings/>.

## 1. Summary

| Key | Product | Version | Runs on | Lab vCPU / RAM / disk | Licence clock | ADR |
|---|---|---|---|---|---|---|
| `pa-vm` | Palo Alto VM-Series (PAN-OS) | 11.1, latest KVM base image on the portal (>= 11.1.4-h7; 11.1.16-h1 is the current maintenance release) | EVE-NG | 4 / 8 GB / 60 GB | unlicensed mode, no expiry, ~1,230 sessions | 0010 |
| `panorama` | Palo Alto Panorama | 11.1, same maintenance release as `pa-vm` | Proxmox | 8 / 32 GB / 81 GB + 100 GB log disk | eval or 180-day device-management grace: **UNVERIFIED**, see entry | 0011 |
| `c8000v` | Cisco Catalyst 8000V (IOS XE) | **17.13.01a as loaded** (owner decision 2026-09-06, ADR 0032); upgrade target 17.18.4 | EVE-NG | 2 / 6 GB / 8 GB | Smart Licensing Using Policy, no registration needed, 10 Mbps default throughput (250 Mbps settable) | 0032 (0012 superseded) |
| `veos` | Arista vEOS-lab | **4.33.1.1F as loaded** (owner decision 2026-09-06, ADR 0033); upgrade target 4.35.6M | EVE-NG | 2 / 4 GB / 4 GB | free with arista.com account, no expiry | 0033 (0013 superseded) |
| `ceos` | Arista cEOS-lab | newest 4.33.x cEOS64-lab at Phase 11 (train parity with `veos`, ADR 0033) | Containerlab (`clab` VM) | ~1 GB RAM per node | free with arista.com account | 0014, 0033 |
| `nios` | Infoblox NIOS (vNIOS IB-V825) | 9.0.8 | Proxmox | 2 / 16 GB / 150 GB (resizable image) | temp licence **60 days** | 0015 |
| `winserver` | Windows Server 2025 Standard eval (Desktop Experience) | 2025 eval, build 26100 | Proxmox | 4 / 8 GB / 80 GB | **180 days**, activate within 10 days; rearm count **UNVERIFIED** | 0016 |
| `win11` | Windows 11 Enterprise eval | 25H2 | EVE-NG | 2 / 6 GB / 64 GB | **90 days** | 0017 |
| `ubuntu` | Ubuntu 24.04 LTS cloud image | noble, serial 20260826 (24.04.4 point release) | Proxmox template; EVE-NG | per VM | free; standard support to 2029-05-31 | 0018 |
| `alpine` | Alpine Linux | 3.24.1 | EVE-NG (virt ISO / cloud qcow2); containers | 1 / 512 MB / 2 GB | free; 3.24 supported to 2028-06-01 | 0019 |
| `virtio-win` | Fedora virtio-win drivers ISO | 0.1.302 | Windows installs | n/a | free | 0016 |
| `rocky` | Rocky Linux 9 GenericCloud image | 9.8 (20260525.0) | Proxmox template (Itential VMs only) | per VM | free | 0020 |
| `itential-platform` | Itential Platform | 6.5.2 (Extended Support line) | Proxmox, Rocky 9 | 8 / 32 GB / 250 GB (all-in-one with MongoDB + Redis) | account-managed; licence mechanism **UNVERIFIED** | 0020 |
| `itential-gateway` | Itential Gateway 5 + Gateway Manager | Gateway 5.5.2, Gateway Manager 1.1.1 | Proxmox, Rocky 9 | 4 / 8 GB / 40 GB | licensed via Gateway Manager | 0020 |
| k3s images | see section 4 | | k3s | | | 0021-0027 |

Staging layout on the host: `/srv/images/<key>/<original filename>` plus
`/srv/images/MANIFEST.sha256` (one line per file, produced by the owner with
`sha256sum` after download, committed *as a copy* under `images/manifest.sha256`
once verified).

## 2. Virtual machine images

### 2.1 `pa-vm` — Palo Alto Networks VM-Series firewall (KVM)

| | |
|---|---|
| Version | **PAN-OS 11.1** train; download the newest 11.1 "PAN-OS for VM-Series KVM Base Image" the portal lists, then upgrade in place to the current 11.1 maintenance release (11.1.16-h1 on 2026-08-10) |
| Why | 11.1 has the highest field adoption and is explicitly on Ubuntu 20.04 KVM in Palo Alto's hypervisor matrix (EVE-NG Pro is Ubuntu-based). 12.1 (supported to 2028-08) has **no** public report of running in EVE-NG, needs more RAM (delta **UNVERIFIED**) and its Panorama needs a 224 GB disk. 11.2 has ~12 % adoption. 11.1 Standard support ends **2027-05-03** (Extended 2027-08-31): accepted, with a later ADR to move to 12.1 once an EVE-NG report exists. Sources: [EOL summary](https://www.paloaltonetworks.com/services/support/end-of-life-announcements/end-of-life-summary), [hypervisor support](https://docs.paloaltonetworks.com/compatibility-matrix/reference/vm-series-firewalls/vms-series-hypervisor-support), [endoflife.date/panos](https://endoflife.date/panos) |
| Download | Support portal > Updates > Software Updates > filter "PAN-OS for VM-Series KVM Base Images" (login, owner does it) |
| Expected filename | `PA-VM-KVM-11.1.x.qcow2` (hotfix builds look like `PA-VM-KVM-11.1.4-h7.qcow2`), ~4-5 GB |
| Checksum | MD5 shown on the portal ("Show MD5"); SHA256 on the portal **UNVERIFIED**. Owner records `sha256sum` in the staging manifest |
| Licence / eval | Unlicensed VM-Series passes traffic with a cap of ~1,230 concurrent sessions and no content/threat updates ([source](https://docs.paloaltonetworks.com/vm-series/deployment/private-cloud/set-up-a-vm-series-firewall-on-an-esxi-server/install-a-vm-series-firewall-on-vmware-vsphere-hypervisor-esxi/perform-initial-configuration-on-the-vm-series-on-esxi)). No expiry. A 30-day VM-Series trial exists but the form rejects personal e-mail domains. Default `admin`/`admin`, password change forced at first login |
| Min / Lab | VM-100 minimum 2 vCPU / 6.5 GB / 60 GB (60 GB required while unlicensed). **Lab: 4 vCPU / 8192 MB / 60 GB** per EVE-NG's 11.0 table ([VM-Series system requirements](https://docs.paloaltonetworks.com/vm-series/11-1/vm-series-deployment/license-the-vm-series-firewall/vm-series-models/vm-series-system-requirements)) |
| EVE-NG folder | `/opt/unetlab/addons/qemu/paloalto-11.1.x/virtioa.qcow2`, then `unl_wrapper -a fixpermissions`. Template `paloalto` ships with EVE-NG ([how-to](https://www.eve-ng.net/index.php/documentation/howtos/howto-add-palo-alto/)) |
| Quirks | EVE-NG's own table uses QEMU 5.2.0 for 11.0 (set `qemu_version` in the node, do not leave the template default 2.12); boot cycles through `vm login:` -> `PA-HDF login:` -> `PA-VM login:` before it is usable; only e1000/virtio NICs; HA has no dedicated ports, so plan 6 NICs per DC firewall (mgmt, HA1 on mgmt, HA1-backup, HA2, two data); HA peers need identical CPU/NIC resources; first boot is CPU-heavy (the builder starts firewalls in waves, PID S3). First-boot duration figure **UNVERIFIED** |
| Itential | `adapter-panorama` (1.0.10, 2026-08-31, Node >= 20.19) is the only Palo Alto adapter; uses the XML/REST API key (`X-PAN-KEY`). Direct use against a standalone firewall **UNVERIFIED**; Phase 5 tests it, Phase 10 moves policy to Panorama ([repo](https://gitlab.com/itentialopensource/adapters/adapter-panorama)) |
| Verified | 2026-09-06 |

### 2.2 `panorama` — Palo Alto Networks Panorama (KVM)

| | |
|---|---|
| Version | **11.1**, same maintenance release as the firewalls (Panorama must be >= managed PAN-OS) |
| Why | Same train as `pa-vm`; 12.1.2+ requires a 224 GB system disk migration (`request system clone-system-disk`, irreversible) ([prerequisites](https://docs.paloaltonetworks.com/panorama/getting-started/set-up-panorama/set-up-the-panorama-virtual-appliance/setup-prerequisites-for-the-panorama-virtual-appliance)) |
| Download | Support portal > Updates > Software Updates > filter "Panorama Base Images" ([install on KVM](https://docs.paloaltonetworks.com/panorama/getting-started/set-up-panorama/set-up-the-panorama-virtual-appliance/install-the-panorama-virtual-appliance/install-panorama-on-kvm)) |
| Expected filename | `Panorama-KVM-11.1.x.qcow2` |
| Checksum | MD5 on portal; SHA256 **UNVERIFIED** |
| Licence / eval | Panorama needs a Support licence and a Device Management licence (25/100/1000 devices). Docs state a 180-day grace before commits fail "from the date of upgrade"; whether a *fresh* unlicensed install gets the same grace is **UNVERIFIED** and an older KB says devices cannot be managed until a licence exists. An evaluation Panorama can be requested through the Customer Support Portal (serial from a "Request for Software Evaluation Approved" e-mail); eval duration and device cap **UNVERIFIED**. **Action for the owner before Phase 10:** request an evaluation Panorama licence via the support portal / account team, or fund it with Software NGFW credits. Sources: [register and licence](https://docs.paloaltonetworks.com/panorama/10-2/panorama-admin/set-up-panorama/register-panorama-and-install-licenses), [eval activation](https://knowledgebase.paloaltonetworks.com/KCSArticleDetail?id=kA10g000000ClZjCAK) |
| Min / Lab | Current docs: Management Only mode 16 CPU / 64 GB / 81 GB; below that Panorama boots into Management Only or Maintenance mode. EVE-NG's table and community reports run 10.x-11.x at 8 vCPU / 16 GB (4 vCPU fails to boot). **Lab: 8 vCPU / 32 GB / 81 GB system + 100 GB log disk**, Management Only mode is acceptable (logs go to Loki). If it drops to Maintenance mode, raise to 16 vCPU / 64 GB by trimming EVE-NG (the budget shows where) |
| Placement | Proxmox VM (ADR 0006): `qm importdisk` the qcow2, virtio disk, `cpu host`. EVE-NG alternative: `panorama-11.1.x/virtioa.qcow2` + `virtiob.qcow2` (100 GB log disk) per [EVE-NG how-to](https://www.eve-ng.net/index.php/documentation/howtos/palo-panorama/) |
| Quirks | ~20 minutes to first CLI; admin/admin then forced password change; licence is tied to the VM UUID (keep the VM, snapshot the disk); EVE-NG forum reports of Panorama boot problems are why it runs on Proxmox |
| Verified | 2026-09-06 |

### 2.3 `c8000v` — Cisco Catalyst 8000V Edge Software (IOS XE)

| | |
|---|---|
| Version | **Running: 17.13.01a**, the image already on EVE-NG (ADR 0032, owner decision 2026-09-06). Upgrade target when wanted: 17.18.4 (Extended Maintenance; 17.18.1 GA 2025-08-08), reasoning kept below |
| Why | 17.13 is a Standard-support release whose software maintenance ended 2024-11-30. 17.15.x is EM but its maintenance ends 2027-03-30 and Cisco's EOL notice says migrate to 17.18.1+. Cisco's recommended EM releases (doc updated 2026-09-04): 17.18.4 and 17.15.6. 26.1.x is the new numbering with no C8000V feature delta. gNMI (Get/Set/Subscribe), NETCONF and RESTCONF are all documented for 17.18. Sources: [17.18 release notes](https://www.cisco.com/c/en/us/td/docs/routers/C8000V/Release-Notes/c8000v-releasenotes-17-18.html), [17.15 EOL](https://www.cisco.com/c/en/us/products/collateral/ios-nx-os-software/ios-xe-17/ios-xe-17-15-x-eol.html), [recommended releases](https://www.cisco.com/c/en/us/support/docs/switches/catalyst-9300-series-switches/214814-recommended-releases-for-catalyst-9200-9.html), [endoflife.date](https://endoflife.date/cisco-ios-xe). The software.cisco.com "suggested" star is login-gated: **UNVERIFIED** |
| Download | [software.cisco.com, Catalyst 8000V](https://software.cisco.com/download/home/286327102/type/282046477) (CCO login) |
| Expected filename | `c8000v-universalk9_8G_serial.17.18.04.qcow2` (**pattern extrapolated from** `c8000v-universalk9_8G_serial.17.15.01a.qcow2`; use the `_8G_serial` variant, not the non-serial or EFI files), ~1.8 GB |
| Checksum | MD5 and SHA512 shown on the download page |
| Licence | Smart Licensing Using Policy: no registration required, only a usage-report nag (`%SMART_LIC-6-REPORTING_REQUIRED`). Default throughput 10 Mbps; `platform hardware throughput level MB 250` works without an HSECK9 licence. "90-day eval" claims: **UNVERIFIED**, not needed ([licensing](https://www.cisco.com/c/en/us/td/docs/routers/C8000V/configure-licenses-throughput-c8000v.html)) |
| Min / Lab | Cisco KVM minimum 4 GB RAM for 1-4 vCPU; 8 GB recommended for feature-rich use. EVE-NG template: 2 vCPU / 6144 MB. **Lab: 2 vCPU / 6144 MB / 8 GB disk** ([KVM install](https://www.cisco.com/c/en/us/td/docs/routers/C8000V/Configuration/c8000v-installation-configuration-guide/install-cisco-catalyst-8000v-in-kvm-environment/installing-in-kvm-environments-overview.html)) |
| EVE-NG folder | `c8000v-17.18.04/virtioa.qcow2` ([how-to](https://www.eve-ng.net/index.php/documentation/howtos/catalyst-8000v/)) |
| Quirks | Choose the serial-console GRUB entry on first boot; day-0 config via `iosxe_config.txt` on a cdrom ISO (EVE-NG mounts the cdrom from the addons folder, not the node copy, so use a per-node bootstrap through the builder instead); run `fixpermissions` after copying |
| Itential | No open-source IOS XE adapter exists; device access is via Automation Gateway (netmiko/NETCONF) and the pre-built "Cisco - IOS - IAG" automation ([repo](https://gitlab.com/itentialopensource/pre-built-automations/cisco-ios-iag)) |
| Verified | 2026-09-06 |

### 2.4 `veos` — Arista vEOS-lab

| | |
|---|---|
| Version | **Running: 4.33.1.1F**, the image already on EVE-NG (ADR 0033, owner decision 2026-09-06). Upgrade target: EOS 4.35.6M (2026-08-18); reasoning kept below |
| Why | Newest train in M (fix-only) phase; 4.36 is still F-only. Same version as cEOS-lab so the Containerlab twin matches. EVE-NG's how-to lists 4.34.0F as tested, so 4.34.8M is the fallback. Sources: [Arista release notes feed](https://www.arista.com/en/support/release-notes), [EOS life-cycle policy](https://www.arista.com/en/support/product-documentation/eos-life-cycle-policy) |
| Download | [arista.com software download](https://www.arista.com/en/support/software-download) (free registered account) |
| Expected filename | `vEOS64-lab-4.35.6M.qcow2` (pattern from `vEOS64-lab-4.35.3F.qcow2`; exact name **UNVERIFIED**) + `Aboot-veos-serial-8.0.2.iso` (6 MB, MD5 `8d7e754efebca1930a93a2587ff7606c` per the GNS3 registry) |
| Checksum | MD5 and SHA512 files sit beside the image in the download folder (**partially UNVERIFIED**, page did not render) |
| Licence | Free for lab/simulation with an Arista account; no expiry. EULA text not readable in this session (**UNVERIFIED** restrictions) |
| Min / Lab | EVE-NG template 2 vCPU / 6144 MB; GNS3 runs it at 2048 MB. **Lab: 2 vCPU / 4096 MB / 4 GB** |
| EVE-NG folder | `veos-4.35.6M/virtioa.qcow2` + `cdrom.iso` (Aboot). The existing 4.33 folder uses `hda.qcow2`; the current EVE-NG how-to uses `virtioa` ([how-to](https://www.eve-ng.net/index.php/documentation/howtos/howto-add-arista-veos/)) |
| Quirks | "Very heavy nodes, need physical cores"; `zerotouch disable` on first boot (persistent) or ZTP loops; login `admin` no password; `-cpu host` recommended |
| Itential | No EOS adapter; "Arista - EOS - IAG" pre-built uses the CLI via IAG ([repo](https://gitlab.com/itentialopensource/pre-built-automations/arista-eos-iag)) |
| Verified | 2026-09-06 |

### 2.5 `ceos` — Arista cEOS-lab (Containerlab)

| | |
|---|---|
| Version | **4.35.6M**, 64-bit image |
| Why | Parity with `veos`; cEOS 4.32.0F+ auto-detects cgroups v1/v2 and supports up to 50 nodes/host |
| Download | Same portal, "cEOS Lab" folder |
| Expected filename | `cEOS64-lab-4.35.6M.tar.xz` (**UNVERIFIED** exact name); import with `docker import cEOS64-lab-4.35.6M.tar.xz ceos:4.35.6M` |
| Licence | As `veos` |
| Resources | ~1 GB RAM per node (Arista SE guidance 8 vCPU / 10 GB for 10+ nodes; exact figure **UNVERIFIED**). **Lab: 5 nodes, `clab` VM 8 vCPU / 24 GB** |
| Containerlab | kind `ceos`, creds admin/admin, gNMI 6030, NETCONF 830, `MGMT_INTF=eth0` ([kind docs](https://containerlab.dev/manual/kinds/ceos/)) |
| Verified | 2026-09-06 |

### 2.6 `nios` — Infoblox NIOS (vNIOS for KVM, IB-V825)

| | |
|---|---|
| Version | **NIOS 9.0.8** (WAPI v2.13.8) |
| Why | Qualified by Infoblox on **Proxmox VE 9.2.3** (the lab's hypervisor); 9.0.6/9.0.7 opened the LTS programme; fullest documentation. 9.1.x (WAPI 2.14, OAuth) exists but 9.1.1's release notes are login-gated. Sources: [Proxmox prerequisites](https://docs.infoblox.com/space/vniosproxmox/1861648475), [9.0.x docs](https://docs.infoblox.com/space/nios90/154608627/Infoblox+NIOS+9.0.x), [nios-swagger WAPI map](https://github.com/infobloxopen/nios-swagger) |
| Download | support.infoblox.com > Downloads > NIOS/vNIOS > 9.0.8 > **150 GB resizable** image (login). **The public evaluation sign-up page is broken today** (free-trial URL 404, eval form redirects); the owner must go through the support portal or account team. **UNVERIFIED** how a 60-day eval is granted to an individual account |
| Expected filename | `nios-9.0.8-<build>-<date>-resizable-150G.qcow2` (pattern from `nios-9.0.8-12345-00088-2025-09-10-08-37-44-fixed-500G.qcow2`) |
| Checksum | **UNVERIFIED** (portal login) |
| Licence / eval | `set temp_license` on the console: **expires in 60 days, then the software stops functioning** until a licence is installed. Whether a temp licence can be re-applied to the same instance is **UNVERIFIED**; the plan (ADR 0009) is to keep the pre-licence disk as a golden image and redeploy. Enable SNMP/e-mail expiry notifications before licensing ([set temp_license](https://docs.infoblox.com/space/nios90/220168268/set+temp_license)) |
| Min / Lab | IB-V825: **2 vCPU / 16 GB / 500 GB, or 150 GB-2.5 TB with the resizable image**. IB-V815 is not recommended as grid master. **Lab: 2 vCPU / 16 GB / 150 GB** ([X5 series specs](https://docs.infoblox.com/space/vniosspec/1488683067/vNIOS+for+KVM+X5+Series)) |
| Placement | Proxmox VM (ADR 0006): scsi0 virtio disk, virtio NICs, net0=MGMT, net1=LAN1 (a non-HA instance needs two interfaces), **cloud-init user-data must be attached before first power-on or it fails to boot** (needs `snippets`, assumption 5) ([Proxmox deploy guide](https://docs.infoblox.com/space/vniosproxmox/1861812246/Deploying+a+vNIOS+Instance+in+Proxmox+VE)) |
| EVE-NG folder | `infoblox-9.0.8/virtioa.qcow2`; EVE-NG only lists 8.x as supported and 9.x on EVE's QEMU is **UNVERIFIED**, another reason for Proxmox ([EVE-NG how-to](https://www.eve-ng.net/index.php/infoblox-ddi/)) |
| Itential | `adapter-infoblox` 2.0.10 (2026-08-31), basic auth, set `version` to `v2.13.8` (sample default is v2.7) ([repo](https://gitlab.com/itentialopensource/adapters/inventory/adapter-infoblox)) |
| Verified | 2026-09-06 |

### 2.7 `winserver` — Windows Server 2025 Standard evaluation

| | |
|---|---|
| Version | Windows Server 2025 eval, Standard, Desktop Experience (build 26100) |
| Why | Current eval; AD DS/DNS role for `ad.lab.internal`. 2022 is still offered as a fallback |
| Download | [Evaluation Center](https://www.microsoft.com/en-us/evalcenter/evaluate-windows-server-2025) (Microsoft account) |
| Expected filename | `26100.<build>.<date>_release_svc_refresh_SERVER_EVAL_x64FRE_en-us.iso` (~5 GB); current build **UNVERIFIED** |
| Checksum | Not published on the download page; owner records `sha256sum` |
| Licence / eval | **180 days**; must activate online within 10 days or it shuts down; at expiry it shuts down hourly. Rearm count conflicts between Microsoft answers (6 vs 1): **UNVERIFIED**, read `slmgr /dlv` after install and store it in `verify/results/`. Known bug: some 2025 Datacenter evals expire at ~50 days, only fix is redeploy ([source](https://learn.microsoft.com/en-us/answers/questions/5724072/windows-server-2025-evaluation-edition-prematurely)) |
| Min / Lab | 1.4 GHz x64 with SSE4.2/POPCNT, 2 GB (4 GB recommended), 32 GB disk. **Lab: 4 vCPU / 8 GB / 80 GB** on Proxmox with virtio-blk + virtio-net from `virtio-win` 0.1.302 ([requirements](https://learn.microsoft.com/en-us/windows-server/get-started/hardware-requirements)) |
| EVE-NG folder | `winserver-2025/virtioa.qcow2` if ever needed ([how-to](https://www.eve-ng.net/index.php/documentation/howtos/howto-create-own-windows-server-on-the-eve/)) |
| Quirks | Unattended install via `autounattend.xml` on a second ISO (Phase 7); `virtio-win` 0.1.302 is the current stable (2026-08-28), Proxmox's wiki still recommends 0.1.271 because of a vioscsi race in 0.1.285/0.1.292 under heavy load; the lab uses virtio-blk so 0.1.302 is chosen ([virtio-win](https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/), [Proxmox wiki](https://pve.proxmox.com/wiki/Windows_VirtIO_Drivers)) |
| Verified | 2026-09-06 |

### 2.8 `win11` — Windows 11 Enterprise evaluation

| | |
|---|---|
| Version | Windows 11 Enterprise 25H2 eval (x64) |
| Why | Branch client endpoints for domain-join and end-to-end tests |
| Download | [Evaluation Center](https://www.microsoft.com/en-us/evalcenter/evaluate-windows-11-enterprise) |
| Expected filename | `26200.<build>.<date>.25h2_ge_release_svc_refresh_CLIENTENTERPRISEEVAL_OEMRET_x64FRE_en-us.iso` (~7 GB); current build **UNVERIFIED** |
| Checksum | Microsoft publishes `Windows11EnterpriseHashValues.pdf` from the download page (SHA256) |
| Licence / eval | **90 days**; black desktop + hourly shutdown at expiry |
| Min / Lab | 2 cores, 4 GB, 64 GB, TPM 2.0 + Secure Boot. **Lab: 2 vCPU / 6144 MB / 64 GB** |
| EVE-NG folder | `win-11-25h2/hda.qcow2` (SATA) + `OVMF_CODE_4M.fd`/`OVMF_VARS_4M.fd`; node runs QEMU 5.2.0, q35, pflash OVMF, e1000 NIC (`topology/enterprise.yaml` `eve.*`); template default 4 vCPU / 8 GB ([how-to](https://www.eve-ng.net/index.php/documentation/howtos/howto-create-own-windows-host-on-the-eve/)) |
| Quirks | 25H2 setup refuses a BIOS/MBR disk even with the `LabConfig` bypass, so `images/build-win11.sh` installs under OVMF + Secure Boot + swtpm TPM 2.0 on Proxmox (built image sha256 `223991ef…` in `/srv/images/MANIFEST.sha256`); EVE-NG runs it without a TPM (not needed after install); 25H2 needs POPCNT/SSE4.2 (Xeon Gold 6154 has both) |
| Verified | 2026-09-06 |

### 2.9 `ubuntu` — Ubuntu 24.04 LTS cloud image

| | |
|---|---|
| Version | `noble-server-cloudimg-amd64.img`, serial **20260826** (24.04.4 point release; 24.04.5 not yet on releases.ubuntu.com) |
| Why | Every service VM, k3s node, `oob-gw`, `clab`, `ddi-fallback`; the host already has an older copy at `/var/lib/vz/template/iso/noble-cloud.img` which Phase 2 replaces with this serial |
| Download | <https://cloud-images.ubuntu.com/noble/20260826/noble-server-cloudimg-amd64.img> (pinned serial; `current` moves) (no login) |
| Checksum | `d0fe84bb5f80853425fa6be28e2c106f30104c3cfe8611933f2e65c9b63f0e30` from [SHA256SUMS](https://cloud-images.ubuntu.com/noble/current/SHA256SUMS) (serial 20260826; re-check at download, `current` moves) |
| Licence | Free; standard support to 2029-05-31 |
| Resources | Per VM, see the budget |
| EVE-NG folder | `linux-ubuntu-24.04-server` already present for endpoints; cloud-image recipe `linux-cloud-ubuntu-24.04/virtioa.qcow2` + cidata `cdrom.iso` |
| Verified | 2026-09-06 |

### 2.10 `alpine` — Alpine Linux

| | |
|---|---|
| Version | **3.24.1** (3.24 released 2026-06-09, end of support 2028-06-01) |
| Why | Tiny branch endpoints in EVE-NG and utility containers |
| Download | [alpine-virt-3.24.1-x86_64.iso](https://dl-cdn.alpinelinux.org/alpine/v3.24/releases/x86_64/) (66 MB) and cloud image `generic_alpine-3.24.1-x86_64-bios-cloudinit-r0.qcow2` from [cloud releases](https://dl-cdn.alpinelinux.org/alpine/v3.24/releases/cloud/) |
| Checksum | ISO SHA256 `e73a6241bd5f3c5c2d4d38c02cc52c378c0415a7c888bd292066bf36e0f41a39`; `.sha256`/`.sha512`/`.asc` published beside each file |
| Licence | Free |
| Resources | **Lab: 1 vCPU / 512 MB / 2 GB** |
| EVE-NG folder | `linux-alpine-3.24.1/virtioa.qcow2` |
| Container tag | `alpine:3.24.1` |
| Verified | 2026-09-06 |

## 3. Itential and ServiceNow

### 3.1 `rocky` — Rocky Linux 9 GenericCloud image

| | |
|---|---|
| Version | **Rocky 9.8**, `Rocky-9-GenericCloud-Base-9.8-20260525.0.x86_64.qcow2` |
| Why | Itential Platform and Gateway are supported only on RHEL/Rocky 8/9; Ubuntu is explicitly not supported for any Itential component ([system requirements](https://docs.itential.com/itential-platform/plan/system-requirements)). Only the two Itential VMs use it; everything else stays on Ubuntu |
| Download | <https://dl.rockylinux.org/pub/rocky/9/images/x86_64/Rocky-9-GenericCloud-Base-9.8-20260525.0.x86_64.qcow2> (no login) |
| Checksum | SHA256 `92c206cc6f790c61583247eefe87890f8828420662c17cacf247cec78ab4eec8` ([CHECKSUM](https://dl.rockylinux.org/pub/rocky/9/images/x86_64/Rocky-9-GenericCloud-Base-9.8-20260525.0.x86_64.qcow2.CHECKSUM)) |
| Licence | Free |
| Verified | 2026-09-06 |

### 3.2 `itential-platform` — Itential Platform 6

| | |
|---|---|
| Version | **6.5.2** (2026-09-02), the current Platform 6 maintenance release |
| Why | Platform 6 is the only Extended Support line (released 2025-02-27, end of maintenance 2027-08-27, end of support 2028-02-27). 2023.2 reaches end of support on 2026-10-02. Sources: [version lifecycle](https://docs.itential.com/docs/itential-version-lifecycle), [changelog](https://docs.itential.com/itential-platform/release-notes/changelog/2026/9/2) |
| Dependencies | Node.js 20.x, Python 3.11, **MongoDB 7.0** (8.0 is partial), **Redis 7.x** (Valkey 8 documented as drop-in). All installed by the deployer |
| Install | Ansible collection `itential.deployer` 4.2.0 (2026-08-11) with `platform_release: 6`, "all-in-one" architecture (Platform + MongoDB + Redis on one Rocky 9 VM), which Itential documents as dev/throw-away only ([deployer](https://github.com/itential/itential.deployer)). RPM `itential-platform-6.5.2.rpm` |
| Download | Itential software repository: Nexus (`registry.aws.itential.com`) or JFrog (`itential.jfrog.io`), credentials from the account representative ([FAQ](https://docs.itential.com/itential-platform/faqs)). Official container images exist in Itential's private ECR (bundle name from the account manager); Kubernetes/Helm is documented for EKS/AKS only, so the lab uses the RPM/deployer path |
| Licence | **UNVERIFIED.** No licence-file mechanism is documented; docs say "contact your account representative"; no free on-prem trial is advertised. **Action for the owner before Phase 5:** log into the Itential service desk/portal, confirm Nexus/JFrog credentials and whether a licence key is required and when it expires. This is the single largest schedule risk in the PID |
| Min / Lab | Itential's development sizing is 16 vCPU / 64 GB for Platform plus 16 / 128 GB for MongoDB plus 4 / 16 GB for Redis ([minimal architecture](https://docs.itential.com/itential-platform/6/plan/architecture/minimal)); no smaller minimum is published. **Lab: one all-in-one VM, 8 vCPU / 32 GB / 250 GB** (100 GB `/opt/itential`, 100 GB `/var/lib/mongo`, 50 GB root). Whether this runs acceptably is **UNVERIFIED** and Phase 5 measures it; the budget shows 16 GB of headroom to grow into |
| Adapters (npm `@itentialopensource/*`, source on GitLab, all republished 2026-08-31). **S4f (ADR 0054) removed `adapter-servicenow` from the lab: an Integration Model reaches the PDI now, and an npm adapter is installed only where the Platform itself requires one — `adapter-netbox`, because the InventoryBroker consumes an adapter (ADR 0039).** | ~~`adapter-servicenow` 3.0.11~~, `adapter-netbox` 1.0.10, `adapter-panorama` 1.0.10, `adapter-infoblox` 2.0.10, `adapter-zabbix` 1.0.10, `adapter-hashicorp_vault` 1.0.10, `adapter-git` / `adapter-gitlab` 1.0.10 (no Gitea adapter; `adapter-git` is the generic path), `adapter-cvp` 1.0.10 (CloudVision, not used). **No adapter exists for Cisco IOS XE, Arista eAPI, Grafana or Prometheus**: devices are reached through Gateway; Grafana/Prometheus are consumed via their HTTP APIs with the generic REST tooling ([adapter library](https://gitlab.com/itentialopensource/adapters)) |
| SSO | Platform user login supports **SAML** and **LDAP**, not OIDC ([LDAP](https://docs.itential.com/itential-platform/configure/auth/ldap/configure-ldap-authentication)). The lab uses LDAP to Active Directory (documented) and treats Keycloak-as-SAML-IdP as a stretch goal (**UNVERIFIED**) |
| Verified | 2026-09-06 |

### 3.3 `itential-gateway` — Itential Gateway 5 and Gateway Manager

| | |
|---|---|
| Version | **Gateway 5.5.2** (2026-09-02) with **Gateway Manager 1.1.1** |
| Why | Gateway 5 is the current line (single `iagctl` binary, Git-native content, mTLS, Vault KV v2 external secrets since 5.5.0); Gateway 4.4 is the legacy Python/Ansible line kept only for Configuration Manager features the lab does not need. Platform 6.5 features require Gateway 5.5 / Gateway Manager 1.1.1 ([feature comparison](https://docs.itential.com/itential-gateway/5/gateway-feature-comparison), [Gateway Manager](https://docs.itential.com/itential-gateway/gateway-manager-overview)) |
| Install | RPM `iagctl-5.5.2-amd64-server.rpm` from Nexus repo `automation-gateway5-releases` via the `itential.iag5` Ansible collection 1.1.1 ([rpm/deb install](https://docs.itential.com/itential-gateway/rpm-deb-install)); Gateway Manager RPM `app-gateway_manager-1.1.1.noarch.rpm` on the Platform VM. A DEB exists but Ubuntu is not in the support matrix, so the Gateway VM is Rocky 9 too |
| Licence | "Licensed access to Gateway Manager" ([requirements](https://docs.itential.com/itential-gateway/gateway-requirements)); same owner action as 3.2 |
| Min / Lab | Server 1 vCPU / 2 GB / 10 GB; runner 4 vCPU / 8 GB / 20 GB; etcd 2 vCPU / 8 GB. **Lab: one Rocky 9 VM with server + runner + embedded store, 4 vCPU / 8 GB / 40 GB**, plus Ansible collections `paloaltonetworks.panos`, `arista.eos`, `cisco.ios`, `infoblox.nios_modules`, `netbox.netbox` pinned in Phase 5 |
| Verified | 2026-09-06 |

### 3.5 Itential container images (owner-verified 2026-09-06, supersedes the RPM path in 3.2/3.3 for Phase 5)

The owner runs Itential's `itential-dev-stack` (Docker Compose) on a work laptop with images
pulled from Itential's private ECR. The lab will run the same stack on a lab VM, pulling with
the owner's ECR credentials, then `docker save` the images into `/srv/images/itential/` as the
offline rebuild copy. ADR 0020 is amended in the Phase 5 PR; the Rocky template stays unused.

Owner decision 2026-09-06: **pull the latest maintenance tag of each image at Phase 5 time**, not
the laptop's tags. Listed on 2026-09-07 with `aws ecr describe-images` under the owner's company
SSO profile (`ECR_AWS_PROFILE`; `ecr:ListImages` is denied, `DescribeImages` works). Exact tags and
digests are in `itential/versions.yaml` and the ADR 0020 amendment; tarballs in `/srv/images/itential/`.

| Image | Laptop 2026-09-06 | Expected latest (docs.itential.com, 2026-09-06) | Pinned (Phase 5) | Role |
|---|---|---|---|---|
| `497639811223.dkr.ecr.us-east-2.amazonaws.com/automation-platform-config-lcm-flowai` | `6.5.1` | `6.5.2` | `6.5.2` (pushed 2026-09-03, alias of `6.5.2-ecm-6.5.2-fai-1.1.0-gm-1.2.3-lcm-6.5.2-2`) | Itential Platform with the FlowAI bundle |
| `497639811223.dkr.ecr.us-east-2.amazonaws.com/automation-gateway5` | `5.5.1-amd64` | `5.5.2-amd64` | `5.5.2-amd64` (pushed 2026-09-02) | Gateway 5 (FlowAI agent tool-calling, device services) |
| `497639811223.dkr.ecr.us-east-2.amazonaws.com/automation-gateway` | `4.3.15` | newest `4.4.x` if present in ECR, else newest `4.3.x` | `4.4.1` (pushed 2026-09-02; 4.4.0 and 4.3.15 also present) | Gateway 4 (Golden Config / Configuration Manager). 4.3 receives no further security patches per Itential; 4.4 is the patched line. **Staged, not deployed**: owner chose Gateway 5 as the main gateway (2026-09-07, ADR 0035); enable the `gateway4` profile when Golden Config is wanted |
| `ghcr.io/itential/itential-mcp` | `v0.13.1` | `v0.14.0` (release 2026-08-13) | `v0.14.0` | MCP server, streamable HTTP on `mcp.lab.internal:8000` (S4.7); public image |
| `docker.io/ollama/ollama` | - | *withdrawn* | - | **No longer used (ADR 0060).** Local inference moved off the lab to the Mac Mini: no GPU here, and the container ran at 91% of a 6 GiB cap on a 7.8 GiB VM until llama-server crashed mid-verify. Ollama there is Homebrew-managed, so it is recorded rather than pinned: tested against ollama `0.23.3` serving model `qwen3:30b-a3b`. Reached as `ollama.lab.internal` (topology/ipam.yaml `home_lan.inference_host`) |
| `docker.io/osixia/openldap` | `1.4.0` | `1.4.0` (upstream dev-stack default) | `1.4.0` | OpenLDAP with the dev-stack LDIF: `admin@itential`, the login Gateway Manager needs (ADR 0035); replaced by AD in Phase 7 |
| `quay.io/coreos/etcd` | - | `v3.5.21` (quay.io, 2026-09-07; Gateway requires etcd v3.5) | `v3.5.21` | Shared store for the Gateway 5 cluster `lab` so a runner node can join (ADR 0038, Phase 6); Docker network only, no TLS until Phase 9 |
| `docker.io/library/python` | - | `3.12.14-slim-trixie` (Docker Hub, 2026-09-07) | `3.12.14-slim-trixie` | Base of the local `gateway5-runner` image: the pinned Gateway 5 `iagctl` binary on Debian/glibc + Python 3.12, because Cisco pyATS publishes manylinux wheels for Python <= 3.13 only and the stock gateway image is Alpine/Python 3.14 (ADR 0038). Parser packages pinned in `itential/versions.yaml`: `pyats==26.8`, `genie==26.8` (Cisco), `textfsm==2.1.0`, `ntc-templates==9.2.0` (Arista); one cached venv of ~790 MB on the runner volume |
| `ghcr.io/itential/job-metrics-exporter` | `latest` | pin a digest when Phase 8 scrapes it | deferred to Phase 8 | Prometheus exporter for jobs; never pin `latest` |

Do not pin: `automation-gateway5:5.1.0`, `automation-gateway:4.3.7`, `automation-platform-config-lcm:6`, `itential.jfrog.io/flow-ai-demo/itential_flowai:v0.1.4` (leftovers on the laptop).

**Licence: none required (owner decision 2026-09-07).** The running dev stack has no licence file,
key or licensing env var anywhere (compose, `.env`, platform volume, container filesystem, logs);
the owner confirmed on 2026-09-07 that the lab needs none and that ECR access through the company
AWS SSO is the sanctioned path. No expiry to monitor (S4.5); Zabbix watches only the TLS certificate
from Phase 8. Supporting images: MongoDB `7.0.40`, Redis `7.4.11` (Docker Hub, 2026-09-07), Docker CE
`29.8.0` on the VM (ADR 0035).

### 3.4 ServiceNow Personal Developer Instance (external, no image)

| | |
|---|---|
| Release | Current families: **Zurich** (2025) and **Australia** (GA 2026-05-05; PDI pools were wait-listed in July 2026). Request Zurich if Australia is unavailable; the Itential ServiceNow store app 3.1.13 is certified on Zurich ([release family FAQ](https://developer.servicenow.com/print_page.do?release=australia&category=now-platform&identifier=pdi_faq&module=guide)) |
| Instance | `dev409097.service-now.com` over HTTPS (owner's PDI, recorded 2026-09-07; `.env` `SNOW_INSTANCE=dev409097`, never the password). Release family: **Australia** |
| Hibernation | After roughly 6 hours idle; wake takes 3-20 minutes from the developer site |
| Reclamation (policy effective 2026-07-11) | Reclaimed when the PDI is >= 90 days old **and** has had no interactive login in the last 10 days. **Background jobs and API integrations do not count as activity**, so the keep-alive is a human login at least every 10 days (calendar reminder is a manual step) plus exporting the Itential-related update set to the repo ([reclamation rules](https://www.servicenow.com/community/developer-articles/servicenow-pdi-reclamation-rules-avoid-losing-access/ta-p/3572371)) |
| Adapter auth | Basic auth with a dedicated integration user (`itential.integration`, roles `itil`, `snc_platform_rest_api_access`, `rest_api_explorer` **and `snc_basic_auth_api_access`**: PDIs provisioned in 2026 ship with Basic Authentication Account Security, which answers 401 "User is not authenticated" to any basic-auth API call from a user without that role, even with the right password; observed 2026-09-07 on the Australia release). OAuth (`request_token`) is **UNVERIFIED** for this adapter |
| Verified | 2026-09-06 |

## 4. k3s container images and charts

All pinned; `latest` is never used. Release-age notes are why a chart default
was kept where the newest tag was less than three weeks old on 2026-09-06.

### 4.1 Platform (Phase 3) — ADR 0021

| Component | Version | Chart (repo, version) | Image:tag | Verified at | Notes |
|---|---|---|---|---|---|
| k3s | v1.36.4+k3s1 (2026-08-27, Kubernetes 1.36.4) | binary, `INSTALL_K3S_VERSION=v1.36.4+k3s1` | bundled containerd 2.3.4, etcd 3.6.14, CoreDNS 1.14.6 | [releases](https://github.com/k3s-io/k3s/releases) | Installed with `--flannel-backend=none --disable-network-policy --disable-kube-proxy --disable servicelb`; Traefik kept. The 1.36 minor is mature; the 1.36.4 build is 10 days old, fallback v1.35.8+k3s1 |
| Cilium | 1.20.1 (2026-08-18) | `cilium/cilium` 1.20.1 from <https://helm.cilium.io> | `quay.io/cilium/cilium:v1.20.1`, `quay.io/cilium/operator-generic:v1.20.1`, `quay.io/cilium/hubble-relay:v1.20.1` | [release](https://github.com/cilium/cilium/releases/tag/v1.20.1) | kube-proxy replacement, Hubble on; tested on k8s 1.33-1.36 |
| MetalLB | 0.16.1 (2026-05-27) | `metallb/metallb` 0.16.1 from <https://metallb.github.io/metallb> | `quay.io/metallb/controller:v0.16.1`, `quay.io/metallb/speaker:v0.16.1` (tags inferred from chart appVersion: **UNVERIFIED**) | [releases](https://github.com/metallb/metallb/releases) | L2 mode, pool 10.100.0.32-63 |
| Longhorn | 1.12.1 (2026-08-14) | `longhorn/longhorn` 1.12.1 from <https://charts.longhorn.io/index.yaml> | `longhornio/longhorn-manager:v1.12.1` | [releases](https://github.com/longhorn/longhorn/releases) | Nodes need `open-iscsi`, NFSv4 client, `cryptsetup`, `dmsetup`; default replica count 2 |
| cert-manager | 1.21.1 (2026-07-29) | `jetstack/cert-manager` v1.21.1 from <https://charts.jetstack.io> | `quay.io/jetstack/cert-manager-controller:v1.21.1` | [releases](https://github.com/cert-manager/cert-manager/releases) | Supports k8s 1.33-1.36 |
| Traefik (ingress) | 3.7.8 as bundled by k3s v1.36.4 (chart 40.x); upstream 3.7.13 | k3s `HelmChartConfig` override | `traefik:v3.7.8` (bundled) | [k3s release](https://github.com/k3s-io/k3s/releases/tag/v1.36.4%2Bk3s1) | ingress-nginx is **retired** (final 1.15.1, repo archived 2026-03-24) and is not used ([statement](https://www.kubernetes.io/blog/2026/01/29/ingress-nginx-statement/)) |
| kube-vip | 1.2.3 (2026-08-10) | static pod in `/var/lib/rancher/k3s/agent/pod-manifests/` | `ghcr.io/kube-vip/kube-vip:v1.2.3` (tag confirmed on ghcr) | [releases](https://github.com/kube-vip/kube-vip/releases) | ARP mode, control-plane VIP only (`svc_enable=false`, MetalLB owns Services); uses the local k3s kubeconfig so it does not depend on Cilium ([k3s notes](https://kube-vip.io/docs/usage/k3s/)) |
| Garage (S3 object store) | 2.4.0 (2026-09-06) | Kustomize, single node (`--single-node --default-bucket`) | `docker.io/dxflrs/garage:v2.4.0` (tag confirmed on Docker Hub) | [releases](https://git.deuxfleurs.fr/Deuxfleurs/garage/releases) | Backup target for CNPG, later Gitea/NetBox. **MinIO is not used**: `minio/minio` was archived 2026-04-25 and `minio/operator` on 2026-03-20 (ADR 0031) |
| CNPG Barman Cloud plugin | 0.15.0 (2026-09-03) | `kubectl apply` of the release manifest into `cnpg-system` | per release manifest | [installation](https://cloudnative-pg.io/plugin-barman-cloud/docs/installation/) | In-tree `barmanObjectStore` is deprecated since CNPG 1.26; the plugin needs cert-manager |
| CloudNativePG | 1.30.0 (2026-06-29) | `cnpg/cloudnative-pg` 0.29.0 from <https://cloudnative-pg.github.io/charts> | `ghcr.io/cloudnative-pg/cloudnative-pg:1.30.0`, operand `ghcr.io/cloudnative-pg/postgresql:18.6` (tags inferred: **UNVERIFIED**) | [releases](https://github.com/cloudnative-pg/cloudnative-pg/releases) | One `Cluster` per app: Zabbix, Keycloak, Gitea |
| PostgreSQL | 18.6 (2026-08-13) | via CNPG; netbox-docker keeps `postgres:18-alpine` | | [postgresql.org](https://www.postgresql.org/) | |

### 4.2 Identity and DDI containers (Phases 6-7) — ADR 0022, 0023

| Component | Version | Chart | Image:tag | Verified at | Notes |
|---|---|---|---|---|---|
| Keycloak | 26.7.3 (2026-08-31) | `codecentric/keycloakx` 7.3.1 from <https://codecentric.github.io/helm-charts> | `quay.io/keycloak/keycloak:26.7.3` | [releases](https://github.com/keycloak/keycloak/releases) | Bitnami is not used: versioned Bitnami images moved to the frozen `bitnamilegacy` namespace on 2025-08-28 ([issue](https://github.com/bitnami/charts/issues/35164)) |
| tac_plus-ng | pinned git commit of `MarcJHuber/event-driven-servers` (active, commits 2026-08-29) | Kustomize | built in-repo (`images/tacplus-ng/Dockerfile`, Alpine multi-stage), tag `tacplus-ng:<commit>` | [repo](https://github.com/MarcJHuber/event-driven-servers) | RFC 8907, TACACS+ over TLS 1.3, LDAP/AD backend. Community images are 1-2 years stale, so the lab builds its own |
| BIND 9 (on `ddi-fallback`, Docker Compose) | 9.20.27 (ESV; 9.18 EOL 2026-06-17) | n/a | `internetsystemsconsortium/bind9:9.20` **pinned by digest** at deploy (only branch tags exist) | [ISC](https://www.isc.org/bind/) | Secondary for `lab.internal` (ADR 0009) |
| Kea DHCP (on `ddi-fallback`, Docker Compose) | 3.2.0 (stable, EOL 2027-07); 3.0.4 LTS has no official image yet | n/a | `docker.cloudsmith.io/isc/docker/kea-dhcp4:3.2.0` | [ISC Kea](https://www.isc.org/kea/), [packages](https://kb.isc.org/docs/isc-kea-packages) | Host networking on the VM so DHCP broadcasts work without a relay |

### 4.3 Observability (Phase 7 after ADR 0050) — ADR 0024, ADR 0051

| Component | Version | Chart | Image:tag | Verified at | Notes |
|---|---|---|---|---|---|
| kube-prometheus-stack | chart 89.2.3 (2026-09-06) | `prometheus-community/kube-prometheus-stack` 89.2.3 from <https://prometheus-community.github.io/helm-charts> | `quay.io/prometheus-operator/prometheus-operator:v0.93.1`, `quay.io/prometheus/prometheus:v3.14.0-distroless`, `quay.io/prometheus/alertmanager:v0.34.0`, Grafana 13.2.1 via OCI subchart `oci://ghcr.io/grafana-community/helm-charts/grafana` (image tag `grafana/grafana:13.2.1` confirmed on Docker Hub 2026-09-09) | [releases](https://github.com/prometheus-community/helm-charts/releases) | 89.2.3 is a same-day subchart bump; 89.2.2 is the fallback. Add SNMP and blackbox exporters from the same repo |
| Loki | chart 7.3.0 (appVersion 3.6.12); binary 3.7.7 available | `grafana/loki` 7.3.0 from <https://grafana.github.io/helm-charts> | `grafana/loki:3.6.12` (chart default kept) | [releases](https://github.com/grafana/loki/releases) | `deploymentMode: SingleBinary`; syslog receiver via Alloy |
| Alloy | 1.19.2 (2026-08-26) | `grafana/alloy` 1.12.1 from <https://grafana.github.io/helm-charts> | `grafana/alloy:v1.19.2` (tag confirmed on Docker Hub 2026-09-09) | [releases](https://github.com/grafana/alloy/releases) | Promtail is EOL since 2026-03-02 and not used |
| gNMIc | 0.47.0 (2026-08-08) | Kustomize (no official chart) | `ghcr.io/openconfig/gnmic:0.47.0` | [releases](https://github.com/openconfig/gnmic/releases) | Prometheus output |
| Zabbix | 7.0.30 LTS (2026-08-25; full support to 2027-06-30) | `zabbix-community/zabbix` 7.1.0 from <https://zabbix-community.github.io/helm-zabbix> with image tags overridden | `zabbix/zabbix-server-pgsql:alpine-7.0.30`, `zabbix/zabbix-web-nginx-pgsql:alpine-7.0.30` | [life cycle](https://www.zabbix.com/life_cycle_and_release_policy) | Zabbix's own chart is agent/proxy only. 8.0 LTS is not GA. DB on CNPG |
| Zabbix agent 2 (every Ubuntu machine) | 7.0.30-1 (2026-08-25) | apt from <https://repo.zabbix.com/zabbix/7.0/ubuntu> (`zabbix-release_latest_7.0+ubuntu24.04_all.deb` / `+ubuntu22.04` for the EVE-NG host, SHA-256 pinned in `k8s/observability/versions.yaml`) | package `zabbix-agent2` 7.0.30-1 (confirmed in the pool 2026-09-09) | [repo](https://repo.zabbix.com/zabbix/7.0/ubuntu/pool/main/z/zabbix/) | Passive checks from the server pods (ADR 0051) |
| SNMP exporter | 0.30.1 | `prometheus-community/prometheus-snmp-exporter` 9.17.1 from <https://prometheus-community.github.io/helm-charts> | `quay.io/prometheus/snmp-exporter:v0.30.1` (tag confirmed on quay 2026-09-09) | [releases](https://github.com/prometheus/snmp_exporter/releases) | SNMPv3 passphrases from a Secret through `--config.expand-environment-variables` (ADR 0051) |
| Blackbox exporter | 0.28.0 | `prometheus-community/prometheus-blackbox-exporter` 11.18.0 from the same repo | `quay.io/prometheus/blackbox-exporter:v0.28.0` (tag confirmed on quay 2026-09-09) | [releases](https://github.com/prometheus/blackbox_exporter/releases) | ICMP to every device, HTTP to every UI through the lab CA |
| Grafana Zabbix plugin | 6.6.0 (2026-07-30, Grafana >= 11.6) | installed by Grafana at start (`plugins:` value) | `alexanderzobnin-zabbix-app` | [grafana.com](https://grafana.com/grafana/plugins/alexanderzobnin-zabbix-app/) | Datasource for the Expiries dashboard |
| Itential Platform exporter | in-repo `observability/itential-exporter/exporter.py` (stdlib) | plain manifests | `docker.io/library/python:3.12.14-slim-trixie` (the `runner_base` digest of `itential/versions.yaml`) | manifest 3.5 | The Platform has no Prometheus endpoint (measured 2026-09-09); the exporter reads `/workflow_engine/*/metrics` and `/health/*` (ADR 0051) |
| Itential Platform Monitoring dashboard | grafana.com 25527 revision 3 (2026-08-14, org Itential) | vendored `observability/grafana/dashboards/itential-platform-monitoring.json`, SHA-256 pinned in `k8s/observability/versions.yaml` | n/a | [grafana.com](https://grafana.com/grafana/dashboards/25527-itential-platform-monitoring/) | The only dashboard Itential publishes (ADR 0052); provisioned by the Grafana sidecar unchanged |
| node_exporter | v1.12.1 (2026-07-14) | the production VMs (ADR 0055): Platform nodes; the dev-stack profile retired with VM 205 | `docker.io/prom/node-exporter:v1.12.1` (Docker Hub 2026-09-10) | [releases](https://github.com/prometheus/node_exporter/releases) | On VM 205 for the dashboard's node panels (job `node_exporter`) |
| process_exporter | 0.8.7 (2025-04-21) | the production Platform nodes | `docker.io/ncabatoff/process-exporter:0.8.7` (Docker Hub 2026-09-10) | [releases](https://github.com/ncabatoff/process-exporter/releases) | Groups by the Pronghorn process titles (`itential/process-exporter.yml`) |
| redis_exporter | v1.91.1 (2026-09-07) | each of the three Redis servers | `docker.io/oliver006/redis_exporter:v1.91.1` (Docker Hub 2026-09-10) | [releases](https://github.com/oliver006/redis_exporter/releases) | job `redis_exporter` |
| mongodb_exporter | 0.53.0 (2026-08-20) | each of the three replica-set members | `docker.io/percona/mongodb_exporter:0.53.0` (Docker Hub 2026-09-10) | [releases](https://github.com/percona/mongodb_exporter/releases) | job `mongo_exporter`; the dev-stack MongoDB is standalone, so the replica-set panels stay empty |

### 4.4 Config, secrets, code (Phase 9) — ADR 0025, 0026, 0027

| Component | Version | Chart | Image:tag | Verified at | Notes |
|---|---|---|---|---|---|
| Oxidized | 0.37.0 (2026-05-20) | Kustomize (no chart exists) | `oxidized/oxidized:0.37.0` | [releases](https://github.com/ytti/oxidized/releases) | NetBox source, git output to Gitea |
| Vault | 2.0.4 (chart default; 2.1.0 is 5 days old) | `hashicorp/vault` 0.34.1 from <https://helm.releases.hashicorp.com> | `hashicorp/vault:2.0.4` | [releases](https://github.com/hashicorp/vault/releases) | BUSL 1.1: fine for a personal lab. OpenBao 2.6.2 (MPL-2.0, chart `openbao/openbao` 0.29.4) is the recorded alternative; Vault is kept because the Itential Vault adapter targets it (ADR 0026) |
| Gitea | 1.27.3 (2026-08-29) | `gitea-charts/gitea` 12.7.0 from <https://dl.gitea.com/charts/> with `image.tag: 1.27.3` | `docker.gitea.com/gitea:1.27.3` | [releases](https://github.com/go-gitea/gitea/releases) | External CNPG database, Gitea Actions runner on `clab` |

### 4.5 CI tier (Phase 11) — ADR 0014

| Component | Version | Install | Verified at |
|---|---|---|---|
| Containerlab | 0.79.0 (2026-08-21) | `bash -c "$(curl -sL https://get.containerlab.dev)" -- -v 0.79.0` on `clab` | [releases](https://github.com/srl-labs/containerlab/releases) |
| cEOS-lab | 4.35.6M | section 2.5 | |

### 4.6 Existing, unchanged

| Component | Version | Note |
|---|---|---|
| NetBox | 4.7.0 via netbox-docker 5.1.0 (`netboxcommunity/netbox:v4.7-5.1.0`, `postgres:18-alpine`, `valkey/valkey:9.1-alpine`) | Current as of 2026-09-06 (4.7.0 released 2026-09-02); watch for 4.7.1 |
| EVE-NG Pro | 6.5.0-27 | Current Pro is 7.2.0-4 (2026-09-06); an upgrade is out of scope for this PID and would be its own ADR |

## 5. Unverified items to close before use

| Item | Closed by |
|---|---|
| Exact filenames/checksums for PA-VM, Panorama, C8000v, vEOS, cEOS, NIOS, Windows ISOs | Owner at download; recorded in `/srv/images/MANIFEST.sha256` |
| Panorama unlicensed/eval behaviour on a fresh install | Owner's support-portal request before Phase 10 |
| Itential software-repository credentials and licence mechanism | Owner's Itential portal check before Phase 5 |
| Itential all-in-one sizing (8 vCPU / 32 GB) is workable | Phase 5 measurement; budget headroom reserved |
| NIOS eval grant path and temp-licence reuse | Owner's support-portal request before Phase 6 |
| Windows Server 2025 rearm count | `slmgr /dlv` in Phase 7 |
| MetalLB, Alloy, Grafana image tags | `helm template` / registry check in Phase 3 and 8 (kube-vip and CNPG postgresql:18.6 confirmed 2026-09-06) |
| PA-VM 11.1 RAM headroom and first-boot time in EVE-NG 6.5 | Phase 4 E3 drill |
