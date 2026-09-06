# Manual steps

Every step that survives automation, with the reason it could not be automated.
The goal is that this list only ever contains vendor-login downloads.

| # | Step | Why it is manual | Phase |
|---|------|------------------|-------|
| 1 | Download vendor images behind a login (PAN-OS, Panorama, C8000v, vEOS, Infoblox NIOS, Windows eval, Itential) to `/srv/images/` on the Proxmox host | Vendor portals require the account holder's login and EULA acceptance | 1+ |
