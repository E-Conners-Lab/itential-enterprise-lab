#!/usr/bin/env bash
# Programmatic image staging on the Proxmox host (run from the workstation; it drives the host
# over SSH). Sources that allow it are scripted; PA-VM, Panorama and NIOS stay manual (portal
# EULA clicks, docs/manual-steps.md). Every file's SHA256 lands in /srv/images/MANIFEST.sha256.
#
#   images/fetch.sh microsoft   # Windows Server 2025 eval, Windows 11 Enterprise 25H2 eval, virtio-win
#   images/fetch.sh arista      # cEOS64-lab / vEOS64-lab via eos-downloader (needs ARISTA_TOKEN in .env)
#   images/fetch.sh all
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "missing .env"; exit 1; }
set -a; . ./.env; set +a
PVE_HOST=${PVE_HOST:-192.168.68.161}
STAGING=${IMAGE_STAGING:-/srv/images}
SSH="ssh -o BatchMode=yes -o ConnectTimeout=8 root@${PVE_HOST}"
what=${1:-all}

# Evaluation Center fwlink IDs are the stable handle; the static filename changes per refresh, so we
# keep whatever name the redirect resolves to and record its checksum. Verified 2026-09-06.
fetch_url() { # <key> <url> <name-hint>
  local key=$1 url=$2 hint=$3
  $SSH "set -euo pipefail; mkdir -p ${STAGING}/${key}; cd ${STAGING}/${key}
    final=\$(curl -sIL -o /dev/null -w '%{url_effective}' '${url}')
    name=\$(basename \"\${final%%\\?*}\")
    [ -n \"\$name\" ] || { echo 'could not resolve ${hint}'; exit 1; }
    if [ -s \"\$name\" ] && grep -q \" ${key}/\$name\$\" ${STAGING}/MANIFEST.sha256 2>/dev/null; then echo \"present: ${key}/\$name\"; exit 0; fi
    echo \"downloading ${hint} -> ${key}/\$name\"
    curl -fL --retry 3 -o \"\$name.part\" \"\$final\" && mv \"\$name.part\" \"\$name\"
    cd ${STAGING} && sha256sum \"${key}/\$name\" >> MANIFEST.sha256 && sort -u -k2 MANIFEST.sha256 -o MANIFEST.sha256
    echo \"done: \$(tail -1 MANIFEST.sha256 | cut -c1-16)... ${key}/\$name\""
}

microsoft() {
  fetch_url winserver 'https://go.microsoft.com/fwlink/?linkid=2345730&clcid=0x409&culture=en-us&country=us' "Windows Server 2025 eval ISO"
  fetch_url win11     'https://go.microsoft.com/fwlink/?linkid=2334167&clcid=0x409&culture=en-us&country=us' "Windows 11 Enterprise 25H2 eval ISO"
  fetch_url winserver 'https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/archive-virtio/virtio-win-0.1.302-1/virtio-win-0.1.302.iso' "virtio-win 0.1.302"
}

arista() { # eos-downloader (ardl) with the owner's arista.com token; runs in a venv on the host
  : "${ARISTA_TOKEN:?ARISTA_TOKEN missing in .env (arista.com profile > API token)}"
  local ver=${ARISTA_CEOS_VERSION:-4.33.10M}
  $SSH "set -euo pipefail; mkdir -p ${STAGING}/ceos ${STAGING}/veos; [ -x /opt/ardl/bin/ardl ] || { python3 -m venv /opt/ardl && /opt/ardl/bin/pip install -q 'eos-downloader==0.16.0'; }
    cd ${STAGING}/ceos && ARISTA_TOKEN='${ARISTA_TOKEN}' /opt/ardl/bin/ardl get eos --version ${ver} --format cEOS64 --output ${STAGING}/ceos
    cd ${STAGING} && sha256sum ceos/* >> MANIFEST.sha256 && sort -u -k2 MANIFEST.sha256 -o MANIFEST.sha256 && ls -la ceos"
}

case "$what" in
  microsoft) microsoft ;;
  arista) arista ;;
  all) microsoft; arista ;;
  *) echo "usage: $0 {microsoft|arista|all}"; exit 1 ;;
esac
