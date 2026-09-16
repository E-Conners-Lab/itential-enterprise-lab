#!/usr/bin/env bash
# Programmatic image staging on the Proxmox host (run from the workstation; it drives the host
# over SSH). Sources that allow it are scripted; PA-VM, Panorama and NIOS stay manual (portal
# EULA clicks, docs/manual-steps.md). Every file's SHA256 lands in /srv/images/MANIFEST.sha256.
#
#   images/fetch.sh microsoft   # Windows Server 2025 eval, Windows 11 Enterprise 25H2 eval, virtio-win
#   images/fetch.sh arista      # cEOS64-lab via eos-downloader (needs ARISTA_TOKEN in .env); not used by the clab dev topology
#   images/fetch.sh itential    # Itential images from the private ECR (SSO session here) -> tarballs on the host (ADR 0035)
#   images/fetch.sh itential-load  # staged tarballs -> the itential VM's Docker
#   images/fetch.sh c8000v      # the EVE-NG C8000v qcow2 -> /srv/images/c8000v under vrnetlab's filename (ADR 0063)
#   images/fetch.sh veos        # the EVE-NG vEOS-lab qcow2 -> /srv/images/veos under vrnetlab's version name (ADR 0063)
#   images/fetch.sh clab-load   # staged vEOS + C8000v qcow2 -> the clab VM's /srv/stage (clab-host.yml builds both)
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

# Not used by the clab dev topology since 2026-09-16: its switches are vEOS-lab copied from EVE-NG (`veos` below,
# ADR 0063 amendment), because no arista.com token is at hand. Kept for the S10.1-S10.5 cEOS CI twin later.
arista() { # eos-downloader (ardl) with the owner's arista.com token; runs in a venv on the host
  : "${ARISTA_TOKEN:?ARISTA_TOKEN missing in .env (arista.com profile > API token)}"
  # the version defaults to the dev switches' vEOS (clab/versions.yaml, exact parity with the lab); ARISTA_CEOS_VERSION overrides
  # declared apart from the assignment: `local ver=$(...)` returns local's status and hides a failed lookup
  local ver
  ver=${ARISTA_CEOS_VERSION:-$(clab_value images.veos.version)}
  [ -n "$ver" ] || { echo "no cEOS version: ARISTA_CEOS_VERSION unset and clab/versions.yaml images.veos.version empty"; exit 1; }
  $SSH "set -euo pipefail; mkdir -p ${STAGING}/ceos ${STAGING}/veos; [ -x /opt/ardl/bin/ardl ] || { python3 -m venv /opt/ardl && /opt/ardl/bin/pip install -q 'eos-downloader==0.16.0'; }
    cd ${STAGING}/ceos && ARISTA_TOKEN='${ARISTA_TOKEN}' /opt/ardl/bin/ardl get eos --version ${ver} --format cEOS64 --output ${STAGING}/ceos
    cd ${STAGING} && sha256sum ceos/* >> MANIFEST.sha256 && sort -u -k2 MANIFEST.sha256 -o MANIFEST.sha256 && ls -la ceos"
}

# Itential (ADR 0035). Staging runs where the SSO session is: this workstation pulls each ECR
# image (linux/amd64), verifies the digest against itential/versions.yaml and streams `docker save`
# into ${STAGING}/itential/ with its sha256 in MANIFEST.sha256 (the offline copy). The long-lived
# credential never leaves the workstation. `itential-load` pushes the staged tarballs into the VM's
# Docker (no ECR access needed on the VM). Public images (mongo, redis, itential-mcp) are pulled by
# Compose on the VM directly.
ecr_login() {
  local registry=$1
  if [ -n "${ECR_AWS_ACCESS_KEY_ID:-}" ]; then
    AWS_ACCESS_KEY_ID="$ECR_AWS_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$ECR_AWS_SECRET_ACCESS_KEY" AWS_SESSION_TOKEN="${ECR_AWS_SESSION_TOKEN:-}" \
      aws ecr get-login-password --region us-east-2
  else
    aws ecr get-login-password --region us-east-2 --profile "${ECR_AWS_PROFILE:?ECR_AWS_PROFILE or ECR_AWS_* keys missing in .env}" \
      || { echo "ECR token failed: run 'aws sso login --profile ${ECR_AWS_PROFILE}'"; exit 1; }
  fi | docker login --username AWS --password-stdin "$registry" >/dev/null
}
ecr_images() { # name repo tag digest, one per line
  .venv/bin/python -c "
import yaml
for n, i in yaml.safe_load(open('itential/versions.yaml'))['images'].items():
    if '.ecr.' in i['repository']: print(n, i['repository'], i['tag'], i['digest'])"
}
itential() {
  local logged_in=0
  $SSH "mkdir -p ${STAGING}/itential"
  while read -r name repo tag digest; do
    local image="${repo}:${tag}" tar="${name}-${tag}.tar"
    # </dev/null: an ssh inside a read loop would otherwise eat the loop's stdin
    if $SSH "grep -q ' itential/${tar}\$' ${STAGING}/MANIFEST.sha256 2>/dev/null && test -s ${STAGING}/itential/${tar}" </dev/null; then echo "present: itential/${tar}"; continue; fi
    if ! docker image inspect "$image" >/dev/null 2>&1; then
      [ $logged_in = 1 ] || { ecr_login "${repo%%/*}"; logged_in=1; }
      echo "pulling ${image}"
      docker pull --platform linux/amd64 -q "$image" >/dev/null
    fi
    local have; have=$(docker image inspect "$image" --format '{{index .RepoDigests 0}}' | cut -d@ -f2)
    [ "$have" = "$digest" ] || { echo "digest mismatch for ${image}: pulled ${have}, versions.yaml ${digest}"; exit 1; }
    echo "saving ${image} -> ${STAGING}/itential/${tar}"
    docker save "$image" | $SSH "cat > ${STAGING}/itential/${tar}.part && mv ${STAGING}/itential/${tar}.part ${STAGING}/itential/${tar} && cd ${STAGING} && sha256sum itential/${tar} >> MANIFEST.sha256 && sort -u -k2 MANIFEST.sha256 -o MANIFEST.sha256 && tail -1 MANIFEST.sha256"
  done < <(ecr_images)
  [ $logged_in = 1 ] && docker logout 497639811223.dkr.ecr.us-east-2.amazonaws.com >/dev/null 2>&1
  $SSH "ls -la ${STAGING}/itential/ && grep itential/ ${STAGING}/MANIFEST.sha256"
}
itential_load() { # staged tarballs -> the itential VM's Docker, through this workstation
  local vm_ip; vm_ip=$(.venv/bin/python -c "import yaml;print(yaml.safe_load(open('itential/versions.yaml'))['vm']['ip'])")
  local VM="ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new ubuntu@${vm_ip}"
  $VM 'docker version --format "{{.Server.Version}}"' >/dev/null || { echo "docker not reachable on ${vm_ip} (run ansible/playbooks/itential-host.yml first)"; exit 1; }
  while read -r name repo tag digest; do
    local image="${repo}:${tag}" tar="${name}-${tag}.tar"
    if $VM "docker image inspect ${image} >/dev/null 2>&1" </dev/null; then echo "present on vm: ${image}"; continue; fi
    $SSH "cd ${STAGING} && grep ' itential/${tar}\$' MANIFEST.sha256 | sha256sum -c --quiet" </dev/null || { echo "checksum failed for ${tar}"; exit 1; }
    echo "loading ${tar} into ${vm_ip}"
    $SSH "cat ${STAGING}/itential/${tar}" </dev/null | $VM "docker load" >/dev/null
  done < <(ecr_images)
  $VM "docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}' | grep ecr"
}

itential_load_ha2() { # staged tarballs -> the production VMs' Docker (PID S11, ADR 0053)
  # The Proxmox host has no address on the OOB network (ADR 0004), so the workstation relays, exactly as
  # itential-load does for the dev-stack. Each production VM gets only the image its role runs.
  local targets
  targets=$(.venv/bin/python -c 'import yaml
ha2 = yaml.safe_load(open("itential/ha2/versions.yaml"))
want = {"platform": "platform", "gateway": "gateway5"}
for v in ha2["vms"]:
    if v["role"] in want:
        print(v["ip"], want[v["role"]])')
  [ -n "$targets" ] || { echo "no platform/gateway VMs in itential/ha2/versions.yaml"; exit 1; }
  local vm_ip which name repo tag digest
  while read -r vm_ip which; do
    local VM="ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new ubuntu@${vm_ip}"
    $VM 'docker version --format "{{.Server.Version}}"' >/dev/null </dev/null || { echo "docker not reachable on ${vm_ip} (run ansible/playbooks/platform-ha2-hosts.yml first)"; exit 1; }
    while read -r name repo tag digest; do
      [ "$name" = "$which" ] || continue
      local image="${repo}:${tag}" tar="${name}-${tag}.tar"
      if $VM "docker image inspect ${image} >/dev/null 2>&1" </dev/null; then echo "present on ${vm_ip}: ${image}"; continue; fi
      $SSH "cd ${STAGING} && grep ' itential/${tar}$' MANIFEST.sha256 | sha256sum -c --quiet" </dev/null || { echo "checksum failed for ${tar}"; exit 1; }
      echo "loading ${tar} into ${vm_ip}"
      $SSH "cat ${STAGING}/itential/${tar}" </dev/null | $VM "docker load" >/dev/null
    done < <(ecr_images)
  done <<< "$targets"
}

# Containerlab dev topology (ADR 0063). clab/versions.yaml is the oracle for every name and version here.
clab_value() { # clab_value images.veos.version -> one scalar from clab/versions.yaml
  .venv/bin/python -c "import sys,yaml;v=yaml.safe_load(open('clab/versions.yaml'))
for k in sys.argv[1].split('.'): v=v[k]
print(v)" "$1"
}

# The C8000v image is the one the EVE-NG lab already runs (no Cisco download). The Proxmox host has no key for
# EVE-NG, so this workstation relays the bytes, and the checksum is taken on both ends: EVE-NG's copy and the
# staged copy must agree before the MANIFEST line is written. The staged name carries the version because
# vrnetlab's cisco/c8000v Makefile parses it out of the filename.
c8000v() {
  : "${EVE_HOST:?EVE_HOST missing in .env}"
  local src staged key name
  src=$(clab_value images.c8000v.source); src=${src#eve:}
  staged=$(clab_value images.c8000v.staged); name=$(basename "$staged"); key=c8000v
  local EVE="ssh -o BatchMode=yes -o ConnectTimeout=8 root@${EVE_HOST}"
  if $SSH "grep -q ' ${key}/${name}\$' ${STAGING}/MANIFEST.sha256 2>/dev/null && test -s ${STAGING}/${key}/${name}"; then echo "present: ${key}/${name}"; return 0; fi
  local want; want=$($EVE "sha256sum '${src}'" | cut -d' ' -f1)
  [ ${#want} -eq 64 ] || { echo "could not read ${src} on ${EVE_HOST}"; exit 1; }
  echo "copying ${EVE_HOST}:${src} -> ${STAGING}/${key}/${name}"
  $SSH "mkdir -p ${STAGING}/${key}"
  $EVE "cat '${src}'" | $SSH "cat > ${STAGING}/${key}/${name}.part"
  local got; got=$($SSH "sha256sum ${STAGING}/${key}/${name}.part" | cut -d' ' -f1)
  [ "$got" = "$want" ] || { $SSH "rm -f ${STAGING}/${key}/${name}.part"; echo "checksum mismatch: EVE-NG ${want}, staged ${got}"; exit 1; }
  $SSH "mv ${STAGING}/${key}/${name}.part ${STAGING}/${key}/${name} && cd ${STAGING} && sha256sum ${key}/${name} >> MANIFEST.sha256 && sort -u -k2 MANIFEST.sha256 -o MANIFEST.sha256 && grep ' ${key}/${name}\$' MANIFEST.sha256"
}

# The vEOS-lab image is the one the EVE-NG lab switches run (no Arista download, ADR 0063 amendment 2026-09-16),
# copied exactly as the C8000v is: relayed through this workstation, checksum taken on EVE-NG and on the staged
# copy, the MANIFEST line written only when both agree. The staged name carries the version for vrnetlab's
# arista/veos Makefile (clab-host.yml converts it to images.veos.vmdk there).
veos() {
  : "${EVE_HOST:?EVE_HOST missing in .env}"
  local src staged key name
  src=$(clab_value images.veos.source); src=${src#eve:}
  staged=$(clab_value images.veos.staged); name=$(basename "$staged"); key=veos
  local EVE="ssh -o BatchMode=yes -o ConnectTimeout=8 root@${EVE_HOST}"
  if $SSH "grep -q ' ${key}/${name}\$' ${STAGING}/MANIFEST.sha256 2>/dev/null && test -s ${STAGING}/${key}/${name}"; then echo "present: ${key}/${name}"; return 0; fi
  local want; want=$($EVE "sha256sum '${src}'" | cut -d' ' -f1)
  [ ${#want} -eq 64 ] || { echo "could not read ${src} on ${EVE_HOST}"; exit 1; }
  echo "copying ${EVE_HOST}:${src} -> ${STAGING}/${key}/${name}"
  $SSH "mkdir -p ${STAGING}/${key}"
  $EVE "cat '${src}'" | $SSH "cat > ${STAGING}/${key}/${name}.part"
  local got; got=$($SSH "sha256sum ${STAGING}/${key}/${name}.part" | cut -d' ' -f1)
  [ "$got" = "$want" ] || { $SSH "rm -f ${STAGING}/${key}/${name}.part"; echo "checksum mismatch: EVE-NG ${want}, staged ${got}"; exit 1; }
  $SSH "mv ${STAGING}/${key}/${name}.part ${STAGING}/${key}/${name} && cd ${STAGING} && sha256sum ${key}/${name} >> MANIFEST.sha256 && sort -u -k2 MANIFEST.sha256 -o MANIFEST.sha256 && grep ' ${key}/${name}\$' MANIFEST.sha256"
}

# Staged files -> the clab VM's /srv/stage, relayed through this workstation like itential-load (the Proxmox
# host has no address on the OOB network, ADR 0004). Each file is checked against MANIFEST.sha256 on the host
# before it leaves and against the same line on the VM after it lands. clab-host.yml then builds the vrnetlab
# vEOS and C8000v images from these.
clab_load() {
  local vm_ip veos_name c8000v_name
  vm_ip=$(clab_value vm.ip)
  veos_name=$(basename "$(clab_value images.veos.staged)")
  c8000v_name=$(basename "$(clab_value images.c8000v.staged)")
  local VM="ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new ubuntu@${vm_ip}"
  $VM 'test -d /srv/stage && test -w /srv/stage' || { echo "/srv/stage not writable on ${vm_ip} (run ansible/playbooks/clab-host.yml first)"; exit 1; }
  local rel line
  for rel in "veos/${veos_name}" "c8000v/${c8000v_name}"; do
    line=$($SSH "grep ' ${rel}\$' ${STAGING}/MANIFEST.sha256" </dev/null) || { echo "${rel} not in ${STAGING}/MANIFEST.sha256 (images/fetch.sh veos / c8000v first)"; exit 1; }
    if $VM "cd /srv/stage && echo '${line%% *}  $(basename "$rel")' | sha256sum -c --quiet" </dev/null 2>/dev/null; then echo "present on clab: $(basename "$rel")"; continue; fi
    $SSH "cd ${STAGING} && echo '${line}' | sha256sum -c --quiet" </dev/null || { echo "checksum failed on the host for ${rel}"; exit 1; }
    echo "relaying ${rel} -> ${vm_ip}:/srv/stage/"
    $SSH "cat ${STAGING}/${rel}" </dev/null | $VM "cat > /srv/stage/$(basename "$rel").part && mv /srv/stage/$(basename "$rel").part /srv/stage/$(basename "$rel")"
    $VM "cd /srv/stage && echo '${line%% *}  $(basename "$rel")' | sha256sum -c --quiet" </dev/null || { echo "checksum failed on clab for ${rel}"; exit 1; }
  done
  $VM "ls -la /srv/stage"
}

case "$what" in
  microsoft) microsoft ;;
  arista) arista ;;
  itential) itential ;;
  itential-load) itential_load ;;
  itential-load-ha2) itential_load_ha2 ;;
  c8000v) c8000v ;;
  veos) veos ;;
  clab-load) clab_load ;;
  all) microsoft; arista ;;
  *) echo "usage: $0 {microsoft|arista|itential|itential-load|itential-load-ha2|c8000v|veos|clab-load|all}"; exit 1 ;;
esac
