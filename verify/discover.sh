#!/usr/bin/env bash
# Phase 0 discovery: READ-ONLY inventory of the Proxmox host, EVE-NG VM, NetBox VM and NetBox API.
# Writes verify/results/<UTC-timestamp>-discover.log. Never changes infrastructure. Never prints secrets.
# Requires .env at repo root (see .env.example): NETBOX_URL, NETBOX_TOKEN, EVE_HOST, EVE_USERNAME, EVE_PASSWORD.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "missing .env (copy .env.example)"; exit 1; }
set -a; . ./.env; set +a
: "${NETBOX_URL:?}" "${NETBOX_TOKEN:?}" "${EVE_HOST:?}" "${EVE_USERNAME:?}" "${EVE_PASSWORD:?}"
PVE_HOST=${PVE_HOST:-192.168.68.161}
NETBOX_VM=${NETBOX_VM:-192.168.68.110}
SSH="ssh -o BatchMode=yes -o ConnectTimeout=8"
ts=$(date -u +%Y%m%dT%H%M%SZ)
log="verify/results/${ts}-discover.log"
mkdir -p verify/results
exec > >(tee "$log") 2>&1
echo "# discovery ${ts} (read-only)"

section() { echo; echo "=================== $1 ==================="; }

section "proxmox host ${PVE_HOST}"
$SSH root@"$PVE_HOST" '
set +e
hostname -f; pveversion; uptime
lscpu | grep -E "Model name|^CPU\(s\)|Socket|NUMA node\(s\)"; free -g | head -2
echo "nested: $(grep -o -m1 -w "vmx\|svm" /proc/cpuinfo) kvm_intel.nested=$(cat /sys/module/kvm_intel/parameters/nested 2>/dev/null)"
echo "-- storage --"; pvesm status; vgs; lvs -o lv_name,lv_size,data_percent,pool_lv --noheadings
echo "-- storage.cfg --"; cat /etc/pve/storage.cfg
echo "-- isos --"; ls -la /var/lib/vz/template/iso; echo "-- staging /srv/images --"; ls -la /srv/images 2>&1 | head -20
echo "-- nics --"; ip -br link | grep -vE "^(tap|fwbr|fwln|veth)"; lspci | grep -i ethernet
echo "-- interfaces --"; cat /etc/network/interfaces
echo "-- vms --"; qm list; pct list
for id in $(qm list | awk "NR>1{print \$1}"); do echo "-- qm config $id --"; qm config $id | grep -E "^(name|cores|sockets|cpu|memory|net[0-9]|scsi[0-9]|virtio[0-9]|ide[0-9]|boot|onboot|agent|machine|ostype|template|ipconfig|ciuser|scsihw)"; done
echo "-- users/tokens --"; pveum user list --output-format json | python3 -c "import sys,json;[print(u[\"userid\"],\"enable=\",u.get(\"enable\")) for u in json.load(sys.stdin)]"; pveum user token list root@pam --output-format json
echo "-- firewall/sdn --"; pve-firewall status; ls /etc/pve/sdn 2>/dev/null || echo "no sdn"
echo "-- dns/time --"; grep nameserver /etc/resolv.conf; timedatectl | grep -E "Time zone|synchronized"
echo "-- selected packages --"; for p in libguestfs-tools git python3-proxmoxer ansible; do printf "%s: " $p; dpkg-query -W -f="\${Version}\n" $p 2>/dev/null || echo "not installed"; done
'

section "eve-ng ${EVE_HOST}"
$SSH root@"$EVE_HOST" '
set +e
hostname; grep PRETTY /etc/os-release; uname -r; nproc; free -g | head -2; df -h / | tail -1
dpkg -l | grep -E "^ii\s+eve-ng" | awk "{print \$2, \$3}"
echo "nested: $(grep -o -m1 -w "vmx\|svm" /proc/cpuinfo) kvm=$(ls /dev/kvm 2>&1)"
echo "-- interfaces (physical + pnet) --"; ip -br addr | grep -E "^(lo|eth|pnet|nat0|wg0|docker0)"
echo "-- images --"; for d in /opt/unetlab/addons/qemu/*/; do echo "$d"; ls -l "$d" | awk "NR>1{print \"   \",\$5,\$9}"; done
echo "-- iol/dynamips --"; ls /opt/unetlab/addons/iol/bin 2>/dev/null | wc -l; ls /opt/unetlab/addons/dynamips 2>/dev/null | wc -l
echo "-- docker images --"; docker images --format "{{.Repository}}:{{.Tag}} {{.Size}}" 2>/dev/null
echo "-- labs --"; find /opt/unetlab/labs -name "*.unl"
echo "-- tools --"; for t in qemu-img virt-customize python3 pip3 jq; do printf "%s: " $t; command -v $t >/dev/null && ($t --version 2>&1 | head -1) || echo MISSING; done
echo "qemu-guest-agent: $(systemctl is-active qemu-guest-agent)"
'
echo "-- eve api (status, license expiry, node image lists) --"
C=$(mktemp)
curl -sk -c "$C" -H "Content-Type: application/json" \
  -d "{\"username\":\"${EVE_USERNAME}\",\"password\":\"${EVE_PASSWORD}\",\"html5\":\"-1\"}" \
  "https://${EVE_HOST}/api/auth/login" | python3 -c "import sys,json;d=json.load(sys.stdin);print('login:',d.get('status'),d.get('message'))"
curl -sk -b "$C" "https://${EVE_HOST}/api/status" | python3 -c "import sys,json;d=json.load(sys.stdin)['data'];print({k:d[k] for k in ('version','qemu_version','uksm','ksm','vCPU','memtotal','disk','diskavailable','qemu','docker','iol','dynamips')})"
curl -sk -b "$C" "https://${EVE_HOST}/api/auth" | python3 -c "import sys,json;d=json.load(sys.stdin);print('license expires:',d.get('eve_expire'),'| user:',d['data']['username'],d['data']['role'])"
for t in c8000v veos paloalto panorama win winserver linux; do printf "%s images: " $t; curl -sk -b "$C" "https://${EVE_HOST}/api/list/templates/$t" | python3 -c "import sys,json;d=json.load(sys.stdin);print(list(d['data']['options']['image']['list']) if isinstance(d.get('data'),dict) else d.get('message'))"; done
[ "${EVE_PASSWORD}" = "eve" ] && echo "SECURITY: EVE admin password is the factory default"
rm -f "$C"

section "netbox vm ${NETBOX_VM}"
$SSH root@"$NETBOX_VM" '
set +e
hostname; grep PRETTY /etc/os-release; nproc; free -g | head -2; df -h / | tail -1; ip -br addr | grep -E "^eth"
docker --version; docker compose version
docker ps --format "{{.Names}} {{.Image}} {{.Status}}"
echo "compose dir: /opt/netbox-docker  netbox-docker version: $(cat /opt/netbox-docker/VERSION 2>/dev/null)"
grep -q API_TOKEN_PEPPER /opt/netbox-docker/env/netbox.env && echo "API_TOKEN_PEPPER: set" || echo "API_TOKEN_PEPPER: MISSING"
echo "qemu-guest-agent: $(systemctl is-active qemu-guest-agent)"; echo "crontab: $(crontab -l 2>/dev/null | wc -l) lines"
'

section "netbox api ${NETBOX_URL}"
nb() { curl -s -m 15 "${NETBOX_URL}/api/$1" -H "Authorization: Token ${NETBOX_TOKEN}"; }
nb status/ | python3 -c "import sys,json;d=json.load(sys.stdin);print({k:d.get(k) for k in ('netbox-version','django-version','python-version','plugins')})"
for ep in dcim/sites dcim/regions dcim/manufacturers dcim/device-types dcim/device-roles dcim/platforms dcim/devices ipam/vrfs ipam/prefixes ipam/ip-ranges ipam/ip-addresses ipam/vlans ipam/vlan-groups ipam/asns virtualization/clusters virtualization/virtual-machines tenancy/tenants extras/tags extras/custom-fields extras/webhooks extras/event-rules users/users users/tokens; do
  printf "%-36s %s\n" "$ep" "$(nb "$ep/?limit=1&brief=1" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("count"))')"
done
nb "users/tokens/" | python3 -c "import sys,json;[print('token',t['id'],'user',t['user']['username'],'v'+str(t.get('version')),'expires',t.get('expires'),'desc',repr(t.get('description'))) for t in json.load(sys.stdin)['results']]"
echo "bogus token ->" "$(curl -s -o /dev/null -w '%{http_code}' "${NETBOX_URL}/api/dcim/sites/" -H 'Authorization: Token 0000')" "(expect 403)"

section "k3s"
echo "no k3s cluster (rebuild pending); stale kubeconfig on workstation: $(grep -m1 server: ~/.kube/config 2>/dev/null || echo none)"

section "workstation"
for t in tofu ansible ansible-lint yamllint gitleaks pre-commit gh jq; do printf "%-13s %s\n" $t "$(command -v $t >/dev/null && $t --version 2>&1 | head -1 | cut -c1-60 || echo MISSING)"; done
printf "%-13s %s\n" kubectl "$(kubectl version --client 2>/dev/null | head -1 || echo MISSING)"
printf "%-13s %s\n" helm "$(helm version --short 2>/dev/null || echo MISSING)"
gh auth status 2>&1 | grep -E "Logged in|scopes"
echo; echo "RESULT: discovery complete -> $log"
