# Manual steps

Every step that survives automation, with the reason it could not be automated.
The goal is that this list only ever contains vendor-login downloads and
licence activations. Each row names the phase that needs it and the manifest
entry that says exactly what to fetch.

| # | Step | Why it is manual | Phase | Reference |
|---|------|------------------|-------|-----------|
| 1 | ~~Confirm a free static address on the home LAN for `oob-gw`~~ Done 2026-09-06: 192.168.68.120 | Discovery did not record the home DHCP scope; only the owner knows the router | 2 | PID assumption A-19 |
| 1b | ~~Add a static route on the home router~~ Done 2026-09-06: 10.100.0.0/14 via 192.168.68.120, LAN interface. Workstations with a VPN client that captures 10/8 also run `sudo scripts/workstation-route.sh` | The home router is owned by the owner and is never touched by this repo | 2 | PID S1 criterion 8 |
| 1c | After every `tofu apply`, copy `tofu/*/terraform.tfstate` to the owner's backup location | No in-lab state backend until Phase 9 (ADR 0029) | 2+ | ADR 0029 |
| 1d | Log into the EVE-NG web UI with the rotated password from `.env` (`EVE_PASSWORD`), never `eve` | Password rotation is automated; the human just needs to know | 2 | PID S1 criterion 6 |
| 2 | Download PA-VM 11.1 KVM base image to `/srv/images/pa-vm/` (Customer Support Portal asks for a device registration / auth code at account setup); then `images/import-eve.sh pa-vm`, set `lab.firewalls: true`, `eve/build.py apply` + `push-configs` | Palo Alto support portal login and EULA | 4 (deferred, lab runs firewall-less) | manifest 2.1, ADR 0034 amendment |
| 3 | ~~Download C8000v 17.18.4~~ Not needed: the loaded 17.13.01a is used (ADR 0032) | Cisco CCO login and EULA | 4 | manifest 2.3 |
| 4 | ~~Download vEOS64-lab 4.35.6M + Aboot ISO~~ Not needed: the loaded 4.33.1.1F is used (ADR 0033). Later upgrades run `images/fetch.sh arista` with `ARISTA_TOKEN` in `.env` | Arista account login | 4 | manifest 2.4 |
| 5 | ~~Download Windows 11 Enterprise 25H2 eval ISO~~ Automated: `images/fetch.sh microsoft` (Evaluation Center links need no login) | eval terms accepted at install | 4 | manifest 2.8 |
| 6 | Confirm Itential software-repository credentials (Nexus or JFrog) and licence terms; download Platform 6.5.2, Gateway 5.5.2 and Gateway Manager 1.1.1 to `/srv/images/itential/` | Account-managed repository, no public trial | 5 | manifest 3.2, 3.3 |
| 7 | Create the ServiceNow PDI integration user; record the instance name in `.env` | PDI admin UI, personal developer account | 5 | manifest 3.4 |
| 8 | Log into the ServiceNow PDI interactively at least every 10 days (calendar reminder) | ServiceNow reclamation counts only interactive logins | 5+ | manifest 3.4 |
| 9 | Obtain the NIOS 9.0.8 resizable image and the 60-day evaluation through the Infoblox support portal; download to `/srv/images/nios/`; apply `set temp_license` on the console | Vendor portal login; the public eval form is broken; console-only command | 6 | manifest 2.6, ADR 0015 |
| 10 | ~~Download Windows Server 2025 eval ISO and virtio-win~~ Automated: `images/fetch.sh microsoft` | eval terms accepted at install | 7 | manifest 2.7 |
| 11 | Hold the Vault unseal keys and root token offline; unseal after any Vault restart | Auto-unseal is out of scope (ADR 0026) | 9 | PID S8 |
| 12 | Download Panorama 11.1 KVM image to `/srv/images/panorama/`; request an evaluation Panorama licence via the support portal | Vendor portal login; licence issued by Palo Alto | 10 | manifest 2.2, ADR 0011 |
| 13 | cEOS64-lab (newest 4.33.x): `images/fetch.sh arista` with `ARISTA_TOKEN` in `.env`; manual only if the token path fails | Arista account login | 11 | manifest 2.5, ADR 0033 |
| 14 | After every download, append `sha256sum` output to `/srv/images/MANIFEST.sha256` | The checksum is the only proof the right file landed; vendor hashes are often login-gated | 2+ | manifest §1 |
