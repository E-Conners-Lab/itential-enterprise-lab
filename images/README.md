# images/ — import scripts only

Vendor images are never committed (see `.gitignore`). They are staged by hand
on the Proxmox host at `/srv/images/` (vendor logins are the one manual step),
then a script here imports each one to the right place: an EVE-NG
`/opt/unetlab/addons/qemu/<folder>/` (with the permissions fix) or a Proxmox
template. Each import validates boot and writes a record to `verify/results/`.
The authoritative list of what to download is `docs/image-manifest.md`.
