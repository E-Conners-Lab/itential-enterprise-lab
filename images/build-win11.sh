#!/usr/bin/env bash
# Build the EVE-NG Windows 11 client image unattended on the Proxmox host (ADR 0034):
# temporary VM 9902 (SeaBIOS, virtio-scsi) boots the staged eval ISO with an autounattend ISO
# and the virtio-win ISO, installs, applies the TPM/SecureBoot bypass, creates local user 'lab'
# (password WIN_LAB_PASSWORD from .env), enables RDP + OpenSSH + guest agent, powers off.
# The disk is then converted to /srv/images/win11/win-11-25h2.qcow2 and imported into EVE-NG
# folder win-11-25h2 (images/import-eve.sh). The temporary VM is destroyed at the end.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "missing .env"; exit 1; }
set -a; . ./.env; set +a
: "${WIN_LAB_PASSWORD:?WIN_LAB_PASSWORD missing in .env}"
PVE_HOST=${PVE_HOST:-192.168.68.161}
STAGING=${IMAGE_STAGING:-/srv/images}
VMID=${WIN_BUILD_VMID:-9902}
SSH="ssh -o BatchMode=yes -o ConnectTimeout=8 root@${PVE_HOST}"
iso=$($SSH "ls ${STAGING}/win11/*CLIENTENTERPRISEEVAL*.iso | head -1"); [ -n "$iso" ] || { echo "Windows 11 eval ISO not staged (images/fetch.sh microsoft)"; exit 1; }
vio=$($SSH "ls ${STAGING}/winserver/virtio-win-*.iso | head -1"); [ -n "$vio" ] || { echo "virtio-win ISO not staged"; exit 1; }
out="${STAGING}/win11/win-11-25h2.qcow2"
if $SSH "[ -s ${out} ]"; then echo "present: ${out} (delete it to rebuild)"; else
  echo "== autounattend ISO"
  sed "s/__WIN_LAB_PASSWORD__/${WIN_LAB_PASSWORD//\//\\/}/g" images/win11/autounattend.xml | $SSH "mkdir -p /tmp/win11-unattend && cat > /tmp/win11-unattend/autounattend.xml && genisoimage -quiet -J -r -o ${STAGING}/win11/autounattend.iso /tmp/win11-unattend && rm -rf /tmp/win11-unattend && ls -la ${STAGING}/win11/autounattend.iso"
  echo "== temporary build VM ${VMID} (SeaBIOS, virtio-scsi, 3 cdroms)"
  $SSH "export LC_ALL=C.UTF-8; qm status ${VMID} >/dev/null 2>&1 && { qm stop ${VMID} >/dev/null 2>&1 || true; qm destroy ${VMID} --purge >/dev/null; }
    cp ${iso} /var/lib/vz/template/iso/win11-eval.iso 2>/dev/null || true; cp ${vio} /var/lib/vz/template/iso/virtio-win.iso; cp ${STAGING}/win11/autounattend.iso /var/lib/vz/template/iso/win11-autounattend.iso
    qm create ${VMID} --name win11-golden-build --machine pc --bios seabios --ostype win11 --cores 4 --memory 8192 --cpu host --scsihw virtio-scsi-single --scsi0 local-lvm:64,discard=on --net0 virtio,bridge=vmbr1 --ide0 local:iso/win11-eval.iso,media=cdrom --ide1 local:iso/virtio-win.iso,media=cdrom --ide2 local:iso/win11-autounattend.iso,media=cdrom --boot order=ide0 --agent enabled=1 --vga std >/dev/null
    qm start ${VMID} && echo started"
  echo "== waiting for the unattended install to power the VM off (typically 15-30 min)"
  for i in $(seq 1 90); do sleep 30; st=$($SSH "LC_ALL=C.UTF-8 qm status ${VMID} 2>/dev/null | awk '{print \$2}'"); printf "  t+%sm status=%s\n" $((i/2)) "$st"; [ "$st" = stopped ] && break; done
  [ "$st" = stopped ] || { echo "install did not finish in 45 min; inspect VM ${VMID} on the console"; exit 1; }
  echo "== capture the disk"
  $SSH "qemu-img convert -p -f raw -O qcow2 /dev/pve/vm-${VMID}-disk-0 ${out} 2>&1 | tail -1; cd ${STAGING} && sha256sum win11/win-11-25h2.qcow2 >> MANIFEST.sha256 && sort -u -k2 MANIFEST.sha256 -o MANIFEST.sha256; ls -la ${out}"
  $SSH "export LC_ALL=C.UTF-8; qm destroy ${VMID} --purge >/dev/null && echo 'build VM destroyed'; rm -f /var/lib/vz/template/iso/win11-eval.iso /var/lib/vz/template/iso/win11-autounattend.iso"
fi
echo "== import into EVE-NG"
images/import-eve.sh win11 win-11-25h2.qcow2 win-11-25h2
