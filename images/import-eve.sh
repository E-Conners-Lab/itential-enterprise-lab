#!/usr/bin/env bash
# Import a staged vendor image from the Proxmox host into an EVE-NG image folder, verifying the
# checksum recorded in /srv/images/MANIFEST.sha256 first, then fixing permissions (EVE-NG
# requirement). Idempotent: an image already present with the same checksum is left alone.
#
#   images/import-eve.sh pa-vm PA-VM-KVM-11.1.16-h1.qcow2 paloalto-11.1
#   images/import-eve.sh win11 win11.qcow2 win-11-25h2
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "missing .env"; exit 1; }
set -a; . ./.env; set +a
: "${EVE_HOST:?}"
PVE_HOST=${PVE_HOST:-192.168.68.161}
STAGING=${IMAGE_STAGING:-/srv/images}
key=${1:?key (manifest section, e.g. pa-vm)}; file=${2:?file name under ${STAGING}/<key>/}; folder=${3:?EVE-NG folder, e.g. paloalto-11.1}
case "$key" in pa-vm|panorama|c8000v|nios|win11|winserver|ubuntu|alpine) disk=virtioa.qcow2 ;; veos) disk=hda.qcow2 ;; *) echo "unknown key $key"; exit 1 ;; esac
SSH_PVE="ssh -o BatchMode=yes -o ConnectTimeout=8 root@${PVE_HOST}"
SSH_EVE="ssh -o BatchMode=yes -o ConnectTimeout=8 root@${EVE_HOST}"
echo "== verify ${key}/${file} against ${STAGING}/MANIFEST.sha256 on ${PVE_HOST}"
sum=$($SSH_PVE "cd ${STAGING} && grep ' ${key}/${file}\$' MANIFEST.sha256 | cut -d' ' -f1")
[ -n "$sum" ] || { echo "no MANIFEST.sha256 entry for ${key}/${file}; append it with: cd ${STAGING} && sha256sum ${key}/${file} >> MANIFEST.sha256"; exit 1; }
$SSH_PVE "cd ${STAGING} && echo '${sum}  ${key}/${file}' | sha256sum -c --quiet" && echo "checksum ok ${sum:0:16}..."
echo "== copy to EVE-NG ${EVE_HOST}:/opt/unetlab/addons/qemu/${folder}/${disk}"
if $SSH_EVE "[ -f /opt/unetlab/addons/qemu/${folder}/${disk} ] && sha256sum /opt/unetlab/addons/qemu/${folder}/${disk} | grep -q '^${sum}'"; then
  echo "present on EVE-NG with the same checksum; nothing to do"
else
  $SSH_EVE "mkdir -p /opt/unetlab/addons/qemu/${folder}"
  # host-to-host copy over the home LAN, resumable
  $SSH_PVE "rsync -a --partial --info=progress2 -e 'ssh -o StrictHostKeyChecking=accept-new' ${STAGING}/${key}/${file} root@${EVE_HOST}:/opt/unetlab/addons/qemu/${folder}/${disk}" 2>&1 | tail -2
  $SSH_EVE "sha256sum /opt/unetlab/addons/qemu/${folder}/${disk} | grep -q '^${sum}'" && echo "copied, checksum ok"
fi
$SSH_EVE "/opt/unetlab/wrappers/unl_wrapper -a fixpermissions >/dev/null 2>&1 && ls -la /opt/unetlab/addons/qemu/${folder}/"
echo "== record"
mkdir -p verify/results && printf '%s import-eve %s/%s -> %s/%s sha256=%s\n' "$(date -u +%Y%m%dT%H%M%SZ)" "$key" "$file" "$folder" "$disk" "$sum" >> verify/results/image-imports.log && tail -1 verify/results/image-imports.log
