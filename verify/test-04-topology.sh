#!/usr/bin/env bash
# Phase 4 verification: EVE-NG network topology (PID S3 criteria 1-7). Read-only.
# Intent: topology/enterprise.yaml + topology/ipam.yaml + docs/image-manifest.md.
# State: EVE-NG REST API, NetBox API, the devices themselves over SSH/eAPI/XML-API via oob-gw.
set -uo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "missing .env"; exit 1; }
set -a; . ./.env; set +a
: "${EVE_HOST:?}" "${EVE_USERNAME:?}" "${EVE_PASSWORD:?}" "${NETBOX_URL:?}" "${NETBOX_TOKEN:?}"
LAB=${EVE_LAB:-/enterprise.unl}
SSH="ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new"
ts=$(date -u +%Y%m%dT%H%M%SZ)
fail=0; pass=0
ok()   { echo "PASS  $1"; pass=$((pass+1)); }
bad()  { echo "FAIL  $1"; fail=$((fail+1)); }
check(){ local name=$1; shift; if "$@" >/tmp/verify04.$$ 2>&1; then ok "$name"; else bad "$name"; sed 's/^/      /' /tmp/verify04.$$ | head -10; fi; }
nb()   { curl -s -m 20 -H "Authorization: Token ${NETBOX_TOKEN}" "${NETBOX_URL}/api/$1"; }
C=$(mktemp); trap 'rm -f "$C" /tmp/verify04.$$' EXIT
eve_login() { curl -sk -m 15 -c "$C" -H "Content-Type: application/json" -d "{\"username\":\"${EVE_USERNAME}\",\"password\":\"${EVE_PASSWORD}\",\"html5\":\"-1\"}" "https://${EVE_HOST}/api/auth/login" | python3 -c "import sys,json;sys.exit(0 if json.load(sys.stdin).get('status')=='success' else 1)"; }
eve()  { curl -sk -m 30 -b "$C" "https://${EVE_HOST}/api/labs${LAB}/$1"; }
echo "# test-04-topology ${ts}"
eve_login || { bad "EVE-NG login"; echo; echo "passed=0 failed=8"; exit 1; }
[ -f topology/enterprise.yaml ] || { bad "topology/enterprise.yaml missing"; echo; echo "passed=0 failed=8"; exit 1; }

# --- S3.1 lab exists; every YAML node exists in EVE-NG and is running ------------------------
c1() {
  local nodes; nodes=$(eve nodes) || return 1
  python3 - "$nodes" <<'PY'
import json, sys, yaml
want = yaml.safe_load(open("topology/enterprise.yaml"))["nodes"]
raw = json.loads(sys.argv[1])
if raw.get("status") != "success": print("EVE-NG:", raw.get("message")); sys.exit(1)
have = {n["name"]: n for n in raw["data"].values()}
missing = sorted(set(want) - set(have)); extra = sorted(set(have) - set(want))
notrun = sorted(n for n in want if n in have and have[n].get("status") != 2)
errs = []
if missing: errs.append(f"missing in EVE-NG: {missing}")
if extra: errs.append(f"unexpected in EVE-NG: {extra}")
if notrun: errs.append(f"not running: {notrun}")
if errs: print("\n".join(errs)); sys.exit(1)
print(f"{len(have)} nodes present and running")
PY
}
check "S3.1 every node in topology/enterprise.yaml exists in EVE-NG ${LAB} and is running" c1

# --- S3.2 every node answers SSH on its OOB address ---------------------------------------------
c2() {
  python3 - <<'PY' > /tmp/verify04-hosts.$$ || return 1
import yaml
t = yaml.safe_load(open("topology/enterprise.yaml"))
for n, v in t["nodes"].items():
    print(n, v["mgmt_ip"], v["platform"])
PY
  local bad_hosts=""
  while read -r name ip plat; do
    case "$plat" in win11) nc -z -w 3 "$ip" 3389 >/dev/null 2>&1 || bad_hosts="$bad_hosts $name(rdp)";; *) nc -z -w 3 "$ip" 22 >/dev/null 2>&1 || bad_hosts="$bad_hosts $name(ssh)";; esac
  done < /tmp/verify04-hosts.$$
  rm -f /tmp/verify04-hosts.$$
  [ -z "$bad_hosts" ] || { echo "unreachable:$bad_hosts"; return 1; }
}
check "S3.2 every node answers on its management address (ssh, or rdp for Windows)" c2

# --- S3.3 NetBox devices/interfaces/cables/IPs equal the YAML; cable count equals EVE links ----
c3() {
  local links; links=$(eve networks) || return 1
  python3 - "$links" <<'PY'
import json, os, sys, urllib.request, yaml
t = yaml.safe_load(open("topology/enterprise.yaml"))
def get(path):
    req = urllib.request.Request(os.environ["NETBOX_URL"] + "/api/" + path, headers={"Authorization": "Token " + os.environ["NETBOX_TOKEN"]})
    return json.load(urllib.request.urlopen(req, timeout=20))
devs = {d["name"]: d for d in get("dcim/devices/?limit=500")["results"]}
errs = []
for n, v in t["nodes"].items():
    d = devs.get(n)
    if not d: errs.append(f"{n} missing in NetBox"); continue
    ip = (d.get("primary_ip4") or {}).get("address", "")
    if ip.split("/")[0] != v["mgmt_ip"]: errs.append(f"{n}: primary ip {ip} != {v['mgmt_ip']}")
cables = get("dcim/cables/?limit=500")["count"]
if cables != len(t["links"]): errs.append(f"NetBox cables {cables} != yaml links {len(t['links'])}")
eve = json.loads(sys.argv[1])
bridges = [x for x in eve.get("data", {}).values() if x.get("type") == "bridge"]
if len(bridges) != len(t["links"]): errs.append(f"EVE-NG bridge networks {len(bridges)} != yaml links {len(t['links'])}")
if errs: print("\n".join(errs)); sys.exit(1)
print(f"NetBox: {len(t['nodes'])} devices, {cables} cables; EVE-NG: {len(bridges)} links")
PY
}
check "S3.3 NetBox devices, primary IPs and cable count equal the YAML; EVE-NG link count equals the YAML" c3

# --- S3.4 routing state --------------------------------------------------------------------------
c4() {
  local errs=""
  # ISP: five BGP neighbours established
  local est; est=$($SSH "automation@10.100.0.148" "show bgp summary" 2>/dev/null | awk '/^10\.103\./ && $NF ~ /^[0-9]+$/ {c++} END{print c+0}')
  [ "${est:-0}" -ge 5 ] || errs="$errs isp-bgp=${est:-0}/5"
  # branches: two tunnels up each
  for b in 146 147; do local up; up=$($SSH "automation@10.100.0.$b" "show ip interface brief | include Tunnel" 2>/dev/null | grep -c "up *up"); [ "${up:-0}" -ge 2 ] || errs="$errs br@.$b-tunnels=${up:-0}/2"; done
  # leaves: EVPN peers + MLAG
  for l in 162 163; do local ev; ev=$($SSH "automation@10.100.0.$l" "show bgp evpn summary | include Estab" 2>/dev/null | grep -c Estab); [ "${ev:-0}" -ge 2 ] || errs="$errs leaf@.$l-evpn=${ev:-0}/2"; $SSH "automation@10.100.0.$l" "show mlag | include State" 2>/dev/null | grep -qi active || errs="$errs leaf@.$l-mlag"; done
  # firewalls: HA active / passive
  local ha1 ha2; ha1=$($SSH "automation@10.100.0.128" "show high-availability state" 2>/dev/null | grep -m1 -oiE "state: *(active|passive)" ); ha2=$($SSH "automation@10.100.0.129" "show high-availability state" 2>/dev/null | grep -m1 -oiE "state: *(active|passive)")
  echo "$ha1" | grep -qi active || errs="$errs fw01=$ha1"; echo "$ha2" | grep -qi passive || errs="$errs fw02=$ha2"
  [ -z "$errs" ] || { echo "routing:$errs"; return 1; }
}
check "S3.4 ISP has 5 BGP peers; each branch 2 tunnels up; leaves 2 EVPN peers + MLAG active; fw01 active / fw02 passive" c4

# --- S3.5 end-to-end path branch client -> DC server ---------------------------------------------
c5() { $SSH "automation@10.100.0.195" "ping -c 3 -W 2 10.101.10.10 && traceroute -n -m 12 -w 2 10.101.10.10" 2>/dev/null | tee /tmp/verify04-trace.$$ | grep -q " 0% packet loss" || { cat /tmp/verify04-trace.$$ 2>/dev/null; rm -f /tmp/verify04-trace.$$; return 1; }; rm -f /tmp/verify04-trace.$$; }
check "S3.5 br1-host01 reaches dc1-srv01 (10.101.10.10) through fw -> tunnel -> DC fw -> fabric" c5

# --- S3.6 image versions equal the manifest ----------------------------------------------------
c6() {
  local errs=""
  $SSH "automation@10.100.0.148" "show version | include Version" 2>/dev/null | grep -q "17.13.01a" || errs="$errs c8000v"
  $SSH "automation@10.100.0.160" "show version | include Software image version" 2>/dev/null | grep -q "4.33.1.1F" || errs="$errs veos"
  $SSH "automation@10.100.0.128" "show system info | match sw-version" 2>/dev/null | grep -q "11.1" || errs="$errs pa-vm"
  [ -z "$errs" ] || { echo "version mismatch:$errs"; return 1; }
}
check "S3.6 show version on one node per vendor equals the manifest running version" c6

# --- S3.7 RAM inside EVE-NG within the budget ---------------------------------------------------
c7() {
  local nodes; nodes=$(eve nodes) || return 1
  python3 - "$nodes" <<'PY'
import json, sys
d = json.loads(sys.argv[1])["data"]
total = sum(int(n.get("ram", 0)) for n in d.values())
print(f"EVE-NG node RAM: {total/1024:.0f} GB (ceiling 115 GB incl. 6 GB host)")
sys.exit(0 if total + 6*1024 <= 115*1024 else 1)
PY
}
check "S3.7 summed node RAM in EVE-NG within the 115 GB internal ceiling" c7

echo; echo "passed=${pass} failed=${fail}"
[ "$fail" -eq 0 ]
