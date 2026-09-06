#!/usr/bin/env bash
# Phase 2 verification: OOB management network (PID S1 criteria 1-7) and NetBox hardening (PID S0).
# Read-only except for one scratch VM named verify-<ts> (cloned, booted, destroyed) and one
# NetBox journal entry. Every check reads intent from topology/ipam.yaml and state from at least
# two of: the Proxmox API, the EVE-NG API/SSH, the guest itself. Exit 1 if any check fails.
set -uo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "missing .env"; exit 1; }
set -a; . ./.env; set +a
: "${NETBOX_URL:?}" "${NETBOX_TOKEN:?}" "${EVE_HOST:?}" "${EVE_USERNAME:?}" "${EVE_PASSWORD:?}" "${PROXMOX_VE_ENDPOINT:?}"
PVE_HOST=${PVE_HOST:-192.168.68.161}
OOB_GW=${OOB_GW:-10.100.0.1}
OOB_GW_LAN=${OOB_GW_LAN:-192.168.68.120}
EVE_OOB=${EVE_OOB:-10.100.0.2}
NETBOX_OOB=${NETBOX_OOB:-10.100.0.64}
NETBOX_VM=${NETBOX_VM:-192.168.68.110}
SCRATCH_IP=${SCRATCH_IP:-10.100.0.239}
TEMPLATE_ID=${TEMPLATE_ID:-9000}
SCRATCH_ID=${SCRATCH_ID:-9990}
SSH="ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new"
ts=$(date -u +%Y%m%dT%H%M%SZ)
fail=0; pass=0
ok()   { echo "PASS  $1"; pass=$((pass+1)); }
bad()  { echo "FAIL  $1"; fail=$((fail+1)); }
check(){ local name=$1; shift; if "$@" >/tmp/verify02.$$ 2>&1; then ok "$name"; else bad "$name"; sed 's/^/      /' /tmp/verify02.$$ | head -8; fi; }
nb()   { curl -s -m 15 -H "Authorization: Token ${NETBOX_TOKEN}" "${NETBOX_URL}/api/$1"; }
pve()  { curl -sk -m 20 -H "Authorization: PVEAPIToken=${PROXMOX_VE_API_TOKEN:-missing}" "${PROXMOX_VE_ENDPOINT%/}/api2/json/$1"; }
echo "# test-02-oob ${ts}"

# --- S1.1 workstation reaches the OOB gateway via the static route -------------------------------
c1() { ping -c 2 -W 1 "$OOB_GW" >/dev/null && $SSH "ubuntu@${OOB_GW}" hostname | grep -qx oob-gw; }
check "S1.1 ping ${OOB_GW} and ssh oob-gw answers with hostname oob-gw" c1

# --- S1.2 EVE-NG pnet1 has eth1 as a member and answers on the OOB address ---------------------
c2() {
  $SSH "root@${EVE_HOST}" 'ip -br addr show pnet1 | grep -q "10.100.0.2/24" && bridge link show | grep -q "eth1: .* master pnet1"' \
  && $SSH "ubuntu@${OOB_GW}" ping -c 2 -W 1 "$EVE_OOB" >/dev/null
}
check "S1.2 EVE-NG pnet1 = eth1 + ${EVE_OOB}, reachable from oob-gw" c2

# --- S1.3 a throw-away clone of the template boots with the NetBox-reserved IP and reaches out --
c3() {
  [ -n "${PROXMOX_VE_API_TOKEN:-}" ] || { echo "PROXMOX_VE_API_TOKEN is empty in .env (created by the phase-2 host play)"; return 1; }
  local raw; raw=$(pve nodes); echo "$raw" | grep -q '"data"' || { echo "Proxmox API rejected the token: ${raw:0:120}"; return 1; }
  local node; node=$(echo "$raw" | python3 -c "import sys,json;print(json.load(sys.stdin)['data'][0]['node'])") || return 1
  nb "ipam/ip-addresses/?address=${SCRATCH_IP}/24" | python3 -c "import sys,json;d=json.load(sys.stdin);assert d['count']==1 and d['results'][0]['dns_name']=='verify-scratch.lab.internal', d" || return 1
  local api="${PROXMOX_VE_ENDPOINT%/}/api2/json/nodes/${node}/qemu"
  local H="Authorization: PVEAPIToken=${PROXMOX_VE_API_TOKEN}"
  curl -sk -m 20 -H "$H" -X POST "${api}/${TEMPLATE_ID}/clone" --data-urlencode "newid=${SCRATCH_ID}" --data-urlencode "name=verify-${ts}" --data-urlencode "full=0" >/dev/null || return 1
  sleep 5
  curl -sk -m 20 -H "$H" -X PUT "${api}/${SCRATCH_ID}/config" --data-urlencode "ipconfig0=ip=${SCRATCH_IP}/24,gw=${OOB_GW}" --data-urlencode "nameserver=${OOB_GW}" --data-urlencode "net0=virtio,bridge=vmbr1" >/dev/null || return 1
  curl -sk -m 20 -H "$H" -X POST "${api}/${SCRATCH_ID}/status/start" >/dev/null || return 1
  local i rc=1
  for i in $(seq 1 24); do sleep 5; if $SSH "ubuntu@${SCRATCH_IP}" 'hostname; getent hosts github.com >/dev/null && sudo apt-get -qq update >/dev/null' 2>/dev/null; then rc=0; break; fi; done
  curl -sk -m 20 -H "$H" -X POST "${api}/${SCRATCH_ID}/status/stop" >/dev/null; sleep 4
  curl -sk -m 30 -H "$H" -X DELETE "${api}/${SCRATCH_ID}?purge=1" >/dev/null
  return $rc
}
check "S1.3 scratch VM ${SCRATCH_ID} from template ${TEMPLATE_ID} boots on ${SCRATCH_IP}, resolves and apt-updates via oob-gw, then is destroyed" c3

# --- S1.4 NetBox holds the IP plan and tofu plan is clean ---------------------------------------
c4() {
  python3 - <<'PY' || return 1
import json, os, subprocess, sys, urllib.request, yaml
ipam = yaml.safe_load(open("topology/ipam.yaml"))
def get(path):
    req = urllib.request.Request(os.environ["NETBOX_URL"] + "/api/" + path, headers={"Authorization": "Token " + os.environ["NETBOX_TOKEN"]})
    return json.load(urllib.request.urlopen(req, timeout=15))
have_p = {r["prefix"] for r in get("ipam/prefixes/?limit=500")["results"]}
want_p = {r["prefix"] for r in ipam["prefixes"]}
have_a = {r["address"].split("/")[0]: r for r in get("ipam/ip-addresses/?limit=1000&parent=10.100.0.0/24")["results"]}
want_a = {r["address"]: r for r in ipam["addresses"]}
have_r = {(r["start_address"].split("/")[0], r["end_address"].split("/")[0]) for r in get("ipam/ip-ranges/?limit=100")["results"]}
want_r = {(r["start"], r["end"]) for r in ipam["ranges"]}
errs = []
if want_p - have_p: errs.append(f"prefixes missing in NetBox: {sorted(want_p - have_p)}")
if want_r - have_r: errs.append(f"ranges missing in NetBox: {sorted(want_r - have_r)}")
miss = [a for a in want_a if a not in have_a]
if miss: errs.append(f"addresses missing in NetBox: {miss}")
for a, row in want_a.items():
    if a in have_a and have_a[a]["dns_name"] != f"{row['hostname']}.{ipam['domain']}":
        errs.append(f"{a}: dns_name {have_a[a]['dns_name']!r} != {row['hostname']}.{ipam['domain']}")
if errs: print("\n".join(errs)); sys.exit(1)
print(f"netbox: {len(want_p)} prefixes, {len(want_r)} ranges, {len(want_a)} addresses match ipam.yaml")
PY
  ( cd tofu && tofu plan -input=false -detailed-exitcode -lock=false >/dev/null 2>&1 ); local rc=$?
  [ $rc -eq 0 ] || { echo "tofu plan exit $rc (0 = no changes)"; return 1; }
}
check "S1.4 NetBox == topology/ipam.yaml (prefixes, ranges, addresses, dns_name) and tofu plan has no changes" c4

# --- S1.5 /srv/images is a mounted LV with >= 150 GB free --------------------------------------
c5() { $SSH "root@${PVE_HOST}" 'findmnt -no SOURCE /srv/images | grep -q "^/dev/mapper/pve-images$" && [ "$(df -BG --output=avail /srv/images | tail -1 | tr -dc 0-9)" -ge 150 ]'; }
check "S1.5 /srv/images mounted from pve/images with >= 150 GB free" c5

# --- S1.6 EVE-NG rejects the factory password and accepts the one in .env ----------------------
eve_login() { curl -sk -m 15 -H "Content-Type: application/json" -d "{\"username\":\"${EVE_USERNAME}\",\"password\":\"$1\",\"html5\":\"-1\"}" "https://${EVE_HOST}/api/auth/login" | python3 -c "import sys,json;sys.exit(0 if json.load(sys.stdin).get('status')=='success' else 1)"; }
c6() { ! eve_login "eve" && eve_login "$EVE_PASSWORD"; }
check "S1.6 EVE-NG API rejects factory password 'eve' and accepts EVE_PASSWORD" c6

# --- S1.7 vmbr0 / nic1 stanzas are byte-identical to the discovery snapshot --------------------
c7() { $SSH "root@${PVE_HOST}" 'awk "/^iface nic1 inet manual/{f=1} f{print} /^\tbridge-fd 0/{if(f){exit}}" /etc/network/interfaces' | diff - verify/fixtures/pve-interfaces-vmbr0.expected; }
check "S1.7 vmbr0/nic1 stanzas unchanged vs verify/fixtures/pve-interfaces-vmbr0.expected" c7

# --- S1.8 client access: names resolve via the LAN leg, NetBox reachable by name over the route --
c7b() { dig +short +time=3 @"${OOB_GW_LAN}" netbox.lab.internal | grep -qx "${NETBOX_OOB}" && curl -s -m 10 -o /dev/null -w "%{http_code}" "http://${NETBOX_OOB}:8080/api/status/" | grep -qx 200; }
check "S1.8 dig @${OOB_GW_LAN} netbox.lab.internal = ${NETBOX_OOB} and NetBox answers over the route" c7b

# --- S0 NetBox hardening ------------------------------------------------------------------------
c8() { curl -s -m 10 -o /dev/null -w "%{http_code}" "http://${NETBOX_OOB}:8080/api/status/" | grep -qx 200; }
check "S0.1 NetBox answers on its OOB leg ${NETBOX_OOB}" c8
c9() { $SSH "root@${NETBOX_VM}" 'f=$(ls -t /var/backups/netbox/*.sql.gz 2>/dev/null | head -1); [ -n "$f" ] && [ $(( $(date +%s) - $(stat -c %Y "$f") )) -lt 86400 ] && systemctl is-active qemu-guest-agent >/dev/null'; }
check "S0.2 NetBox backup newer than 24 h exists and qemu-guest-agent is active" c9
c10() { nb "users/tokens/?limit=50" | python3 -c "
import sys,json; t=json.load(sys.stdin)['results']
mine=[x for x in t if x.get('description')]
assert mine, 'no described token'
assert all(x['expires'] for x in mine), [x['description'] for x in mine if not x['expires']]
assert not [x for x in t if not x.get('description')], 'undescribed token(s) still exist: %s' % [x['id'] for x in t if not x.get('description')]
print('tokens:', [(x['id'], x['description'], x['expires'][:10]) for x in mine])"; }
check "S0.3 every NetBox token has a description and an expiry (old token 4 deleted)" c10

echo; echo "passed=${pass} failed=${fail}"
rm -f /tmp/verify02.$$
[ "$fail" -eq 0 ]
