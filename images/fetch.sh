#!/usr/bin/env bash
# Programmatic image staging on the Proxmox host (run from the workstation; it drives the host
# over SSH). Sources that allow it are scripted; PA-VM, Panorama and NIOS stay manual (portal
# EULA clicks, docs/manual-steps.md). Every file's SHA256 lands in /srv/images/MANIFEST.sha256.
#
#   images/fetch.sh microsoft   # Windows Server 2025 eval, Windows 11 Enterprise 25H2 eval, virtio-win
#   images/fetch.sh arista      # cEOS64-lab / vEOS64-lab via eos-downloader (needs ARISTA_TOKEN in .env)
#   images/fetch.sh itential    # Itential images from the private ECR onto the itential VM + tarballs on the host (ADR 0035)
#   images/fetch.sh all         # microsoft + arista (itential needs an SSO session, so it is explicit)
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
    curl -fsSL --retry 3 -o \"\$name.part\" \"\$final\" && mv \"\$name.part\" \"\$name\"
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

# Itential (ADR 0035): the VM pulls with a 12 h ECR token minted here (SSO profile or pasted
# temporary keys); the long-lived credential never leaves the workstation. Every ECR image is
# saved through this workstation into ${STAGING}/itential/ as the offline copy; when ECR is
# unreachable the tarball on the host is loaded into the VM instead. Public images (mongo,
# redis, itential-mcp) are pulled by Compose on the VM directly.
itential() {
  local V=itential/versions.yaml vm_ip registry
  vm_ip=$(.venv/bin/python -c "import yaml;print(yaml.safe_load(open('$V'))['vm']['ip'])")
  local VM="ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new ubuntu@${vm_ip}"
  $VM 'docker version --format "{{.Server.Version}}"' >/dev/null || { echo "docker not reachable on ${vm_ip} (run ansible/playbooks/itential-host.yml first)"; exit 1; }
  $SSH "mkdir -p ${STAGING}/itential"
  local ecr_pw=""
  ecr_login() {
    [ -n "$ecr_pw" ] && return 0
    if [ -n "${ECR_AWS_ACCESS_KEY_ID:-}" ]; then
      ecr_pw=$(AWS_ACCESS_KEY_ID="$ECR_AWS_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$ECR_AWS_SECRET_ACCESS_KEY" AWS_SESSION_TOKEN="${ECR_AWS_SESSION_TOKEN:-}" aws ecr get-login-password --region us-east-2)
    else
      ecr_pw=$(aws ecr get-login-password --region us-east-2 --profile "${ECR_AWS_PROFILE:?ECR_AWS_PROFILE or ECR_AWS_* keys missing in .env}") || { echo "ECR token failed: run 'aws sso login --profile ${ECR_AWS_PROFILE}'"; exit 1; }
    fi
    printf '%s' "$ecr_pw" | $VM "docker login --username AWS --password-stdin ${registry}" >/dev/null
  }
  while read -r name repo tag; do
    local image="${repo}:${tag}" tar="${name}-${tag}.tar"
    registry=${repo%%/*}
    if $VM "docker image inspect ${image} >/dev/null 2>&1"; then echo "present on vm: ${image}"
    elif $SSH "test -s ${STAGING}/itential/${tar}"; then
      echo "loading ${tar} from the host into the vm (offline path)"
      $SSH "cat ${STAGING}/itential/${tar}" | $VM "docker load" >/dev/null
    else
      ecr_login
      echo "pulling ${image}"
      $VM "docker pull -q ${image}" >/dev/null
    fi
    if ! $SSH "grep -q ' itential/${tar}\$' ${STAGING}/MANIFEST.sha256 2>/dev/null"; then
      echo "saving ${image} -> ${STAGING}/itential/${tar}"
      $VM "docker save ${image}" | $SSH "cat > ${STAGING}/itential/${tar}.part && mv ${STAGING}/itential/${tar}.part ${STAGING}/itential/${tar} && cd ${STAGING} && sha256sum itential/${tar} >> MANIFEST.sha256 && sort -u -k2 MANIFEST.sha256 -o MANIFEST.sha256 && tail -1 MANIFEST.sha256"
    fi
  done < <(.venv/bin/python -c "
import yaml
for n, i in yaml.safe_load(open('$V'))['images'].items():
    if '.ecr.' in i['repository']: print(n, i['repository'], i['tag'])")
  $VM "docker logout ${registry} >/dev/null 2>&1 || true; docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}' | grep -E 'ecr|itential'"
}

case "$what" in
  microsoft) microsoft ;;
  arista) arista ;;
  itential) itential ;;
  all) microsoft; arista ;;
  *) echo "usage: $0 {microsoft|arista|itential|all}"; exit 1 ;;
esac
