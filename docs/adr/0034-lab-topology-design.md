# 0034 — Lab topology: one EVE-NG lab, EVPN-VXLAN DC fabric, IPsec/GRE WAN, two branches; built from `topology/*.yaml`

- **Status:** accepted
- **Date:** 2026-09-06

## Context

PID S3 commits to a DC with an HA firewall pair and a two-spine/two-leaf
fabric, a simulated provider core, and two branches, all manageable over OOB
from Phase 2 and automatable by Itential from Phase 5. ADR 0002 requires one
YAML to drive both EVE-NG and NetBox. The images available are the ones on
EVE-NG today (ADR 0032, 0033), PA-VM 11.1 (manual download) and the Windows
11 eval ISO (staged). Every node's first interface is management on `pnet1`.

## Decision

- **One lab** `/enterprise.unl` on EVE-NG. Management is the `pnet1` cloud;
  every point-to-point link is a hidden EVE-NG bridge network created by the
  builder. Nodes carry the OOB address from `topology/ipam.yaml`.
- **Provider**: `isp-core01` (C8000v, AS 65000) hands each site a /30 from
  10.103.0.0/24 and a default route over eBGP.
- **DC1 WAN edge**: `dc1-wan01/02` (C8000v, AS 65100) with eBGP to the ISP,
  iBGP between them, GRE-over-IPsec tunnels to each branch (10.103.100.0/24).
- **DC1 firewalls**: `dc1-fw01/02` (PA-VM 11.1) active/passive: HA1 over
  management, HA2 on `eth1/3`; untrust `eth1/1`+`eth1/2` towards the WAN
  routers (10.101.2.0/24), trust `eth1/4` into the fabric (10.101.1.0/24).
- **DC1 fabric**: `dc1-spine01/02` (AS 65101) and `dc1-leaf01/02` (MLAG pair,
  AS 65102) run eBGP underlay on /31s from 10.101.255.0/24 with loopbacks in
  10.101.254.0/24, iBGP EVPN overlay with VTEPs in 10.101.253.0/24;
  `dc1-acc01` is an L2 access switch on an MLAG port-channel; server VLAN 10 =
  10.101.10.0/24 (`dc1-srv01`), firewall transit VLAN 100 = 10.101.1.0/24.
- **Branches** `br1`, `br2`: `brN-wan01` (C8000v, AS 6520N) with tunnels to
  both DC edges, `brN-fw01` (PA-VM, NAT + policy), `brN-sw01` (vEOS L2),
  `brN-pc01` (Windows 11) and `brN-host01` (Ubuntu). Each branch owns a /20
  from 10.102.0.0/16 (br1 10.102.16.0/20, br2 10.102.32.0/20); user VLAN 10 is
  the first /24 of it.
- **Endpoints**: Linux endpoints use the existing `linux-ubuntu-24.04-server`
  image at 1 GB (the Alpine entry in the budget is replaced; +1 GB total).
  Windows 11 clients are built once, unattended, on the Proxmox host from the
  staged ISO into an EVE-NG image (`win-11-25h2`); if that automation fails the
  clients are deferred to Phase 7 and the PID is amended.
- **Startup configs** are Jinja templates per platform rendered by the
  builder and uploaded through EVE-NG's config API (`config: 1`), so a node
  boots with management, AAA and its routing baseline.
- **NetBox** receives sites, device types, roles, devices, interfaces,
  cables, the in-band prefixes and every address from the same YAML before
  the EVE-NG lab is created.

## Consequences

- 21 nodes, 29 links, 106 GB inside EVE-NG's 115 GB ceiling
  (`docs/resource-budget.md` §3 amended for the Linux endpoints).
- The topology YAML is the oracle for both systems; `tests/test_topology.py`
  holds it to the IP plan and the budget, and `verify/test-04-topology.sh`
  compares NetBox, the EVE-NG API and the devices themselves.
- EVE-NG templates default C8000v to 4 vCPU / 8 GB and vEOS to 6 GB; the
  builder passes the budget's smaller values per node.

## Amendment 2026-09-06 — vendor-guided design changes and the firewall-less build

Approved in chat after a research pass over current vendor guidance (every claim
below was read on the cited page that day).

**Changed**

- **DC edge to firewalls: routed links + eBGP, no HSRP.** Each C8000v gets a
  /30 to each firewall (10.101.2.0/24), eBGP with graceful-restart helper and
  BFD on the routers. Cisco's C8000V guide lists HSRP/VRRP as supported on
  bridge-domain interfaces but not BFD ([BDI on C8000V](https://www.cisco.com/c/en/us/td/docs/routers/C8000V/Configuration/c8000v-installation-configuration-guide/bdi_c8kv.html));
  Palo Alto documents BGP/OSPF with graceful restart on active/passive pairs
  and, for VM-Series, path monitoring as the failover trigger
  ([configure A/P HA](https://docs.paloaltonetworks.com/ngfw/administration/high-availability/set-up-activepassive-ha/configure-activepassive-ha/configure-activepassive-ha-pan-os),
  [VM-Series limits](https://docs.paloaltonetworks.com/vm-series/11-1/vm-series-deployment/set-up-a-vm-series-firewall-on-an-esxi-server/vm-series-on-esxi-system-requirements-and-limitations)).
- **WAN tunnels: IKEv2 with NGE ciphers and a front-door VRF.** Proposal
  `aes-gcm-256 / prf sha384 / group 19`, transform `esp-gcm 256`
  ([Cisco NGE](https://sec.cloudapps.cisco.com/security/center/resources/next_generation_cryptography),
  [IKEv2 on IOS XE 17](https://www.cisco.com/c/en/us/td/docs/routers/ios/config/17-x/sec-vpn/b-security-vpn/m_sec-cfg-ikev2-flex.html)).
  The ISP peering lives in VRF `WAN` (`rd <asn>:1`) so transport and overlay
  defaults cannot conflict, per the IWAN/FlexVPN transport pattern
  ([IWAN CVD](https://www.cisco.com/c/dam/en/us/td/docs/solutions/CVD/Sep2017/CVD-IWANDesign-SEP2017.pdf)).
  Tunnel encapsulation stays **GRE-over-IPsec** (`tunnel protection`), which is
  not affected by the crypto-map end-of-life
  ([white paper](https://www.cisco.com/c/en/us/products/collateral/ios-nx-os-software/ios-ipsec/white-paper-c11-744879.html));
  Cisco describes VTI as "an alternative to GRE"
  ([IPsec VTI](https://www.cisco.com/c/en/us/td/docs/routers/ios/config/17-x/sec-vpn/b-security-vpn/m_sec-ipsec-virt-tunnl-0.html)).
  `routing.wan_tunnel_mode: ipsec` in `topology/enterprise.yaml` switches every
  tunnel to a FlexVPN static VTI (`tunnel mode ipsec ipv4`) with no other change.
- **Fabric per Arista AVD L3LS.** eBGP underlay and eBGP EVPN overlay with the
  spines as route servers (`next-hop-unchanged`, `ebgp-multihop 3`), a tenant
  VRF `PROD` (L3VNI 50001) holding VLAN 10 (anycast gateway) and VLAN 100 as a
  routed transit with per-leaf addresses for eBGP to the firewalls, MLAG
  dual-primary detection on
  ([AVD input variables](https://avd.arista.com/4.7/roles/eos_designs/docs/input-variables.html),
  [AVD single-DC L3LS example](https://avd.arista.com/5.1/examples/single-dc-l3ls/index.html),
  [EOS MLAG](https://www.arista.com/en/um-eos/eos-multi-chassis-link-aggregation)).
  The earlier "firewall as fabric default gateway on VLAN 100" is Arista's
  L2LS pattern and needs every VLAN trunked to the firewall; VM-Series has no
  LACP pre-negotiation ([PAN-OS HA](https://docs.paloaltonetworks.com/pan-os/11-1/pan-os-admin/high-availability/ha-concepts/lacp-and-lldp-pre-negotiation-for-activepassive-ha)).
- **Firewall AS 65103** added; the firewall peers both leaves on VLAN 100
  (10.101.1.1) and both WAN edges on the /30s.

**Firewall-less build (`lab.firewalls: false`)**

- `topology/enterprise.yaml` marks four links `bypass: true`: routed /30s from
  10.101.3.0/24 between each DC WAN edge and a leaf (eBGP into VRF `PROD`), and
  a VLAN 10 access port from each branch router to its switch (the router is
  the VLAN 10 gateway with a DHCP pool). Firewall nodes, their ports and BGP
  neighbours are not rendered while the flag is false; NetBox keeps the
  firewall devices as `planned` and cables for the active links.
- Flipping the flag and running `eve/build.py apply` + `push-configs` removes
  the bypass bridges, builds the firewall nodes and renders their peers. No
  address changes: the bypass eBGP is the same pattern the firewalls use.
- `verify/test-04-topology.sh` defers the firewall checks while the flag is
  false and checks the bypass eBGP and the end-to-end path instead.

**Found while applying**

- The C8000v 17.13.01a image boots with an empty licence level, which hides
  every `crypto` command, so the startup config sets
  `license boot level network-advantage addon dna-advantage`. With the
  config.iso bootstrap the level is active on the first boot;
  `eve/build.py push-configs` checks it once each router answers on management
  and saves + reloads only a router that still shows an empty level. Throughput
  stays at the unlicensed 20 Mbps, enough for the lab.
- IOS XE rejects `ip route vrf MGMT 0.0.0.0/0`; the mask form is required. A
  BGP `address-family ipv4 vrf` needs an `rd` on the VRF.
- Windows 11 25H2 setup refuses a BIOS/MBR target even with the `LabConfig`
  bypass, so `images/build-win11.sh` builds under OVMF with Secure Boot and a
  TPM 2.0 (requirements genuinely met). On EVE-NG the node runs QEMU 5.2.0 with
  q35 and OVMF as pflash; the firmware files ship in the image folder because
  QEMU runs chrooted in the node directory, and the disk is `hda.qcow2` (SATA)
  because Windows was installed on AHCI and has no boot-start virtio driver.
  EVE-NG's node PUT does not persist `qemu_options`; nodes are re-created
  instead.
- Linux endpoints: the golden image's netplan matches `en*` for DHCP and
  networkd applies the lexically first unit, so the endpoint play narrows that
  definition to `ens3`; data ports run MTU 1400 (VXLAN + IPsec inside the
  1500-byte vEOS and WAN links, per AVD's vEOS guidance).
