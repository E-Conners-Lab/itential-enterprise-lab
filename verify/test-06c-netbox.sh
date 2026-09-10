#!/usr/bin/env bash
# Phase 6 verification: NetBox enrichment derived from the topology (PID S4e, ADR 0048).
# Intent: topology/enterprise.yaml through topology/derive.py. State: NetBox (REST), the devices through
# wf-show-all-v1 (Gateway 5 + Genie/TextFSM, one call for every device) and direct SSH (verify/devcmd.py,
# second source). Read-only on the devices; spends no provider tokens unless an S4e agent criterion runs.
set -uo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "missing .env"; exit 1; }
set -a; . ./.env; set +a
: "${NETBOX_URL:?}" "${NETBOX_TOKEN:?}" "${AUTOMATION_PASSWORD:?}" "${ITENTIAL_ADMIN_PASSWORD:?}"
ADMIN_USER=${ITENTIAL_ADMIN_USER:-admin@itential}
PY=.venv/bin/python
V=itential/versions.yaml
# Both are overridable so the production environment can be proved before the cut-over moves the DNS record
# (ADR 0055): IT_IP=<load balancer> IT_MCP_IP=<tools VM> verify/test-... . After the cut-over the defaults are
# the production addresses anyway, because the name follows the record.
IT_IP=${IT_IP:-$(${PY} -c "import yaml;print(yaml.safe_load(open('$V'))['vm']['ip'])")}
IT_HOST=itential.lab.internal
PLATFORM="https://${IT_HOST}"
CA=docs/lab-root-ca.crt
ts=$(date -u +%Y%m%dT%H%M%SZ)
fail=0; pass=0
ok()  { echo "PASS  $1"; pass=$((pass+1)); }
bad() { echo "FAIL  $1"; fail=$((fail+1)); }
# ONLY="S4e.1" runs a subset while iterating
check() { local name=$1; shift; if [ -n "${ONLY:-}" ] && ! echo " ${ONLY} " | grep -q " ${name%% *} "; then echo "SKIP  $name"; return; fi; if "$@" >/tmp/verify06c.$$ 2>&1; then ok "$name"; sed 's/^/      /' /tmp/verify06c.$$; else bad "$name"; sed 's/^/      /' /tmp/verify06c.$$ | head -24; fi; }
nb()  { curl -s -m 30 -H "Authorization: Token ${NETBOX_TOKEN}" "$@"; }
JAR=$(mktemp); WORK=$(mktemp -d); trap 'rm -rf "$JAR" "$WORK" /tmp/verify06c.$$' EXIT
iap() { curl -s -m 120 --cacert "$CA" --resolve "${IT_HOST}:443:${IT_IP}" -b "$JAR" -H "Content-Type: application/json" "$@"; }
iap_login() { iap -c "$JAR" -X POST "${PLATFORM}/login" -d "{\"username\":\"${ADMIN_USER}\",\"password\":\"${ITENTIAL_ADMIN_PASSWORD}\"}" -o /dev/null -w '%{http_code}' | grep -qx 200; }
start_job() { iap -X POST "${PLATFORM}/operations-manager/jobs/start" -d "{\"workflow\":\"$1\",\"options\":{\"type\":\"automation\",\"description\":\"verify ${ts}\",\"variables\":$2}}" | ${PY} -c 'import sys,json;d=json.load(sys.stdin).get("data");print(d.get("_id","") if isinstance(d,dict) else "")'; }
job_status() { iap "${PLATFORM}/operations-manager/jobs/$1" | ${PY} -c 'import sys,json;print(json.load(sys.stdin)["data"]["status"])'; }
wait_job() { local st; for _ in $(seq 1 48); do st=$(job_status "$1"); case "$st" in complete) return 0;; error|canceled|cancelled) echo "job $1 ${st}"; return 1;; esac; sleep 5; done; echo "job $1 timeout (${st})"; return 1; }
job_vars() { iap "${PLATFORM}/operations-manager/jobs/$1" | ${PY} -c 'import sys,json;print(json.dumps(json.load(sys.stdin)["data"].get("variables",{})))'; }
# agent sessions (as test-06): run_agent <name> <inputs json> -> session id; session_text; session_usage
tokens_in=0; tokens_out=0
agent_id() { iap "${PLATFORM}/agent-project-service/operable-agents" | ${PY} -c "import sys,json;a=[x for x in json.load(sys.stdin)['data']['items'] if x.get('name')=='$1'];print(a[0]['_id'] if a else '')"; }
run_agent() {
  local name=$1 inputs=$2 aid sid state
  aid=$(agent_id "$name"); [ -n "$aid" ] || { echo "agent ${name} not found"; return 1; }
  sid=$(iap -X POST "${PLATFORM}/agent-session-manager/sessions" -d "{\"agentDefinitionId\":\"${aid}\",\"inputs\":${inputs}}" | ${PY} -c 'import sys,json;print(json.load(sys.stdin).get("sessionId",""))')
  [ -n "$sid" ] || { echo "session start failed for ${name}"; return 1; }
  echo "$sid"
  for _ in $(seq 1 60); do
    state=$(iap "${PLATFORM}/agent-session-manager/sessions/${sid}" | ${PY} -c 'import sys,json;print((json.load(sys.stdin).get("status") or "").lower())')
    case "$state" in complete|completed) return 0;; failed|error|canceled|cancelled) echo "session ${sid} ${state}"; return 1;; esac
    sleep 5
  done
  echo "session ${sid} did not finish (${state})"; return 1
}
session_msgs() { iap "${PLATFORM}/agent-session-manager/sessions/$1/messages?limit=500&sortBy=eventId&sortOrder=asc"; }
session_text()  { session_msgs "$1" | ${PY} -c 'import sys,json;m=json.load(sys.stdin);m=sorted([x for x in m if x.get("type")=="inference-succeeded" and x.get("text")],key=lambda x:x.get("timestamp",0));print(m[-1]["text"] if m else "")'; }
session_usage() { session_msgs "$1" | ${PY} -c 'import sys,json;m=json.load(sys.stdin);u=[x["data"]["tokenUsage"] for x in m if x.get("type")=="inference-succeeded" and x.get("data",{}).get("tokenUsage")];print(sum(t.get("inputTokens",0) for t in u),sum(t.get("outputTokens",0) for t in u))'; }
# the totals live in a file: count_tokens runs inside command substitutions, where shell variables would be lost
count_tokens() { read -r i o <<<"$(session_usage "$1")"; echo "$i $o" >> "$WORK/tokens"; echo "tokens in=${i} out=${o}"; }
token_totals() { [ -s "$WORK/tokens" ] && awk '{i+=$1;o+=$2} END{print "in=" i " out=" o}' "$WORK/tokens" || echo "in=0 out=0"; }
# compliance plan (as test-06b): plan_id <name>; run_plan <id> -> waits, prints "<status> <batch>"
plan_id() { iap -X POST "${PLATFORM}/configuration_manager/search/compliance_plans" -d '{"name":"","options":{"start":0,"limit":100}}' | ${PY} -c "import sys,json;d=json.load(sys.stdin);print(next((p['id'] for p in d.get('plans',[]) if p.get('name')=='$1'),''))"; }
# run_plan <plan id> -> prints the batch id after the instance completes (as test-06b)
run_plan() {
  local inst batch st
  inst=$(iap -X POST "${PLATFORM}/configuration_manager/compliance_plans/run" -d "{\"planId\":\"$1\",\"options\":{}}" | ${PY} -c 'import sys,json;print(json.load(sys.stdin).get("instanceId",""))')
  [ -n "$inst" ] || { echo "plan run did not start"; return 1; }
  for _ in $(seq 1 60); do
    read -r st batch <<<"$(iap -X POST "${PLATFORM}/configuration_manager/search/compliance_plan_instances" -d "{\"searchParams\":{\"instanceId\":\"${inst}\"}}" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);p=d.get("plans") or [x for g in d.get("groups",[]) for x in g.get("plans",[])];print((p[0].get("jobStatus") or ""),(p[0].get("batchId") or "")) if p else print("","")')"
    [ "$st" = complete ] && { echo "$batch"; return 0; }; sleep 5
  done
  echo "plan instance ${inst} did not finish (${st})"; return 1
}
# batch_issues <batchId> -> one line per device: name errors warnings passes | issues (as test-06b)
batch_issues() { iap "${PLATFORM}/configuration_manager/compliance_reports/batch/$1" | ${PY} -c '
import sys,json
for r in json.load(sys.stdin):
    t=r.get("totals",{}); iss=[" ".join(w["value"] for w in i["spec"]["words"]) for i in r.get("issues",[])]
    print(r["deviceName"], t.get("errors",0), t.get("warnings",0), t.get("passes",0), "|", "; ".join(iss))'; }
# show_all <command> -> writes the parsed results of every device to $WORK/<slug>.json
# wf-show-all-v1 caps each device's parsed result at 1500 characters (sized for the local model, ADR 0046); a device
# whose result hit the cap (a JSON string, not a list) is re-read alone through wf-show-command-v1.
show_all() {
  local id slug dev; slug=$(echo "$1" | tr ' ' '_')
  id=$(start_job wf-show-all-v1 "{\"command\":\"$1\"}"); [ -n "$id" ] || { echo "wf-show-all-v1 did not start"; return 1; }
  wait_job "$id" || return 1; job_vars "$id" > "$WORK/${slug}.json"
  for dev in $(${PY} -c "import json;r=json.load(open('$WORK/${slug}.json'))['results'];print(' '.join(d for d,e in r.items() if isinstance(e.get('parsed'),str)))"); do
    id=$(start_job wf-show-command-v1 "{\"device\":\"${dev}\",\"command\":\"$1\"}"); [ -n "$id" ] || { echo "wf-show-command-v1 did not start for ${dev}"; return 1; }
    wait_job "$id" || return 1; job_vars "$id" > "$WORK/${slug}-${dev}.json"
  done
  echo "$id"
}
mkdir -p verify/results; exec > >(tee "verify/results/${ts}-06c-netbox.log") 2>&1
echo "# test-06c-netbox ${ts}"
[ -s "$CA" ] || { bad "$CA missing"; echo; echo "passed=0 failed=1"; exit 1; }
iap_login || { bad "login to ${PLATFORM} as ${ADMIN_USER}"; echo; echo "passed=0 failed=1"; exit 1; }
# --- budget guard (ADR 0049): this run starts Anthropic sessions; the week's spend comes from the platform meter ---
if [ "${ANTHROPIC_VERIFY:-}" != "force" ]; then
  verify/tokens.sh --check || { bad "Anthropic weekly budget spent (verify/tokens.sh); set ANTHROPIC_VERIFY=force to run anyway"; echo; echo "passed=0 failed=1"; exit 1; }
else
  echo "budget guard bypassed (ANTHROPIC_VERIFY=force)"
fi
${PY} topology/derive.py > "$WORK/intent.json" || { bad "topology/derive.py"; exit 1; }
nb "${NETBOX_URL}/api/dcim/interfaces/?limit=1000" > "$WORK/nb-interfaces.json"
nb "${NETBOX_URL}/api/ipam/ip-addresses/?limit=1000" > "$WORK/nb-addresses.json"
nb "${NETBOX_URL}/api/dcim/devices/?limit=100&status=active&platform=ios-xe&platform=eos" > "$WORK/nb-devices.json"

# --- S4e.1 interface addressing and descriptions: NetBox == intent == the devices ---------------------------
c1() {
  local id_brief id_desc
  id_brief=$(show_all "show ip interface brief") || { echo "$id_brief"; return 1; }
  id_desc=$(show_all "show interfaces description") || { echo "$id_desc"; return 1; }
  ${PY} verify/devcmd.py 10.100.0.146 "show ip interface brief" > "$WORK/direct-br1-wan01.txt" 2>/dev/null || { echo "direct SSH to br1-wan01 failed"; return 1; }
  ${PY} verify/devcmd.py 10.100.0.162 "show ip interface brief" > "$WORK/direct-dc1-leaf01.txt" 2>/dev/null || { echo "direct SSH to dc1-leaf01 failed"; return 1; }
  WORK="$WORK" ${PY} - <<'PY'
import json, os, re, sys
W = os.environ["WORK"]
intent = json.load(open(f"{W}/intent.json"))["interfaces"]
nb_if = json.load(open(f"{W}/nb-interfaces.json"))["results"]
nb_ip = json.load(open(f"{W}/nb-addresses.json"))["results"]
nb_dev = {d["name"] for d in json.load(open(f"{W}/nb-devices.json"))["results"]}
def results(slug):
    """wf-show-all-v1 results; a device whose parsed result hit the 1500-character cap was re-read alone."""
    out = json.load(open(f"{W}/{slug}.json"))["results"]
    for device, entry in out.items():
        if isinstance(entry.get("parsed"), str):
            single = f"{W}/{slug}-{device}.json"
            if not os.path.exists(single):
                raise SystemExit(f"{device}: capped result and no single-device read")
            entry["parsed"] = json.load(open(single))["parsed"]
    return out
brief = results("show_ip_interface_brief")
desc = results("show_interfaces_description")
errs = []
if nb_dev != set(intent):
    errs.append(f"NetBox active network devices {sorted(nb_dev)} != intent {sorted(intent)}")
nb_desc = {(i["device"]["name"], i["name"]): (i.get("description") or "") for i in nb_if}
nb_addr = {}
for a in nb_ip:
    o = a.get("assigned_object") or {}
    if a.get("assigned_object_type") == "dcim.interface" and o:
        nb_addr.setdefault((o["device"]["name"], o["name"]), set()).add(a["address"])
mgmt = {"GigabitEthernet1", "Management1"}
def dev_table(name):
    """{iface: bare ip} from the parsed show ip interface brief (Genie dict or TextFSM rows)."""
    p = brief.get(name, {}).get("parsed")
    rows = {}
    if isinstance(p, dict):
        for k, v in (p.get("interface") or {}).items():
            rows[k] = v.get("ip_address")
    elif isinstance(p, list):
        for r in p:
            rows[r.get("interface")] = r.get("ip_address")
    return {k: v for k, v in rows.items() if v and v != "unassigned" and k not in mgmt and not k.startswith("Vlan4097")}
EOS_ABBREV = {"Et": "Ethernet", "Lo": "Loopback", "Vl": "Vlan", "Po": "Port-Channel", "Ma": "Management", "Tu": "Tunnel", "Vx": "Vxlan"}
def expand(port):
    """EOS prints Et1 / Lo0 / Vl10 in 'show interfaces description'; the device's full names are compared."""
    for short, long in EOS_ABBREV.items():
        if port.startswith(short) and port[len(short):len(short) + 1].isdigit():
            return long + port[len(short):]
    return port
def dev_desc(name):
    p = desc.get(name, {}).get("parsed")
    rows = {}
    if isinstance(p, dict):
        for k, v in (p.get("interfaces") or {}).items():
            rows[k] = v.get("description") or ""
    elif isinstance(p, list):
        for r in p:
            rows[expand(r.get("port") or r.get("interface") or "")] = r.get("description") or ""
    return rows
checked = 0
for device, rows in intent.items():
    table, descriptions = dev_table(device), dev_desc(device)
    if not brief.get(device):
        errs.append(f"{device}: no wf-show-all-v1 result"); continue
    virtual = {r["virtual"].split("/")[0] for r in rows if r.get("virtual")}
    for r in rows:
        key = (device, r["name"])
        if key not in nb_desc:
            errs.append(f"{device}: interface {r['name']} missing in NetBox"); continue
        if nb_desc[key] != r["description"]:
            errs.append(f"{device} {r['name']}: NetBox description {nb_desc[key]!r} != intent {r['description']!r}")
        for k in ("address", "virtual"):
            if r.get(k) and r[k] not in nb_addr.get(key, set()):
                errs.append(f"{device} {r['name']}: {r[k]} not on the interface in NetBox ({sorted(nb_addr.get(key, []))})")
        if r["name"] not in descriptions:
            errs.append(f"{device} {r['name']}: no parsed 'show interfaces description' row (parser {desc.get(device, {}).get('parser')})")
        elif descriptions[r["name"]] != r["description"]:
            errs.append(f"{device} {r['name']}: device description {descriptions[r['name']]!r} != NetBox {r['description']!r}")
        checked += 1
    # the device's addressed interfaces are exactly NetBox's (bare ip; the shared virtual ones are exempt from the converse)
    nb_bare = {n: {a.split("/")[0] for a in addrs} for (d, n), addrs in nb_addr.items() if d == device and n not in mgmt}
    for iface, ip in table.items():
        bare = ip.split("/")[0]
        if bare not in nb_bare.get(iface, set()):
            errs.append(f"{device} {iface}: device has {ip}, NetBox has {sorted(nb_bare.get(iface, []))}")
        elif "/" in ip and ip not in nb_addr.get((device, iface), set()):
            errs.append(f"{device} {iface}: device mask {ip} differs from NetBox {sorted(nb_addr[(device, iface)])}")
    for iface, bares in nb_bare.items():
        for b in bares - virtual:
            if table.get(iface, "").split("/")[0] != b:
                errs.append(f"{device} {iface}: NetBox has {b}, device has {table.get(iface)}")
# second source: direct SSH on one device per vendor equals the workflow's parsed table
for device in ("br1-wan01", "dc1-leaf01"):
    direct = {}
    for line in open(f"{W}/direct-{device}.txt"):
        m = re.match(r"^(\S+)\s+(\d+\.\d+\.\d+\.\d+(?:/\d+)?)\s", line)
        if m and m.group(1) not in mgmt:
            direct[m.group(1)] = m.group(2)
    if direct != dev_table(device):
        errs.append(f"{device}: direct SSH {direct} != workflow {dev_table(device)}")
if errs:
    print("\n".join(errs[:30])); sys.exit(1)
print(f"{len(intent)} devices, {checked} intent interfaces equal in NetBox and on the devices (addresses + descriptions); direct SSH agrees on br1-wan01 and dc1-leaf01")
PY
}
check "S4e.1 every addressed interface of the 12 network devices is in NetBox with its address, VRF and 'to <peer>' description; the devices agree (wf-show-all-v1 + direct SSH)" c1

# --- S4e.2 VRFs and route targets, ASNs on sites, BGP neighbours in the device context == the device, FHRP group ------
c2() {
  nb "${NETBOX_URL}/api/ipam/vrfs/?limit=100" > "$WORK/nb-vrfs.json"
  nb "${NETBOX_URL}/api/ipam/asns/?limit=100" > "$WORK/nb-asns.json"
  nb "${NETBOX_URL}/api/dcim/sites/?limit=100" > "$WORK/nb-sites.json"
  nb "${NETBOX_URL}/api/ipam/fhrp-groups/?limit=100" > "$WORK/nb-fhrp.json"
  nb "${NETBOX_URL}/api/ipam/fhrp-group-assignments/?limit=100" > "$WORK/nb-fhrp-members.json"
  nb "${NETBOX_URL}/api/ipam/prefixes/?limit=200" > "$WORK/nb-prefixes.json"
  nb "${NETBOX_URL}/api/dcim/devices/?limit=100&status=active&platform=ios-xe&platform=eos&include=config_context" > "$WORK/nb-devices-ctx.json"
  # the device's BGP tables over direct SSH (second source): routers one command, fabric switches two
  local dev ip
  while read -r dev ip; do
    case "$dev" in
      *-wan0*|isp-core01) ${PY} verify/devcmd.py "$ip" "show ip bgp all summary" > "$WORK/bgp-${dev}.txt" 2>/dev/null || { echo "direct SSH to ${dev} failed"; return 1; } ;;
      dc1-spine*|dc1-leaf*) { ${PY} verify/devcmd.py "$ip" "show ip bgp summary vrf all"; echo "=== EVPN"; ${PY} verify/devcmd.py "$ip" "show bgp evpn summary"; } > "$WORK/bgp-${dev}.txt" 2>/dev/null || { echo "direct SSH to ${dev} failed"; return 1; } ;;
      *) : > "$WORK/bgp-${dev}.txt" ;;
    esac
  done < <(${PY} -c "import json;[print(d['name'], d['primary_ip4']['address'].split('/')[0]) for d in json.load(open('$WORK/nb-devices-ctx.json'))['results']]")
  WORK="$WORK" ${PY} - <<'PY'
import json, os, re, sys
W = os.environ["WORK"]
intent = json.load(open(f"{W}/intent.json"))
errs = []
vrfs = {v["name"]: v for v in json.load(open(f"{W}/nb-vrfs.json"))["results"]}
if set(vrfs) < {"MGMT", "WAN", "PROD"}:
    errs.append(f"VRFs in NetBox: {sorted(vrfs)}")
else:
    rts = {t["name"] for t in vrfs["PROD"]["import_targets"]} & {t["name"] for t in vrfs["PROD"]["export_targets"]}
    if rts != {"50001:50001"}:
        errs.append(f"PROD route targets: {rts}")
prefixes = json.load(open(f"{W}/nb-prefixes.json"))["results"]
for want in ("10.101.1.0/24", "10.101.10.0/24"):
    p = [x for x in prefixes if x["prefix"] == want]
    if not p or not p[0]["vrf"] or p[0]["vrf"]["name"] != "PROD":
        errs.append(f"prefix {want} not in VRF PROD ({p[0]['vrf'] if p else 'missing'})")
asns = {a["asn"]: a for a in json.load(open(f"{W}/nb-asns.json"))["results"]}
if set(asns) != {a["asn"] for a in intent["asns"]}:
    errs.append(f"ASNs in NetBox {sorted(asns)} != intent {sorted(a['asn'] for a in intent['asns'])}")
sites = {s["slug"]: {a["asn"] for a in s.get("asns", [])} for s in json.load(open(f"{W}/nb-sites.json"))["results"]}
for site, want in intent["asns_by_site"].items():
    if sites.get(site) != set(want):
        errs.append(f"site {site} ASNs {sorted(sites.get(site, []))} != {want}")
groups = {g["name"]: g for g in json.load(open(f"{W}/nb-fhrp.json"))["results"]}
members = json.load(open(f"{W}/nb-fhrp-members.json"))["results"]
for g in intent["fhrp_groups"]:
    nbg = groups.get(g["name"])
    if not nbg:
        errs.append(f"FHRP group {g['name']} missing"); continue
    addrs = {a["address"] for a in nbg.get("ip_addresses", [])}
    if g["address"] not in addrs:
        errs.append(f"FHRP group {g['name']}: address {g['address']} not on the group ({addrs})")
    have = {(m["interface"]["device"]["name"], m["interface"]["name"]) for m in members if m["group"]["id"] == nbg["id"]}
    want = {(m["device"], m["interface"]) for m in g["members"]}
    if have != want:
        errs.append(f"FHRP group {g['name']} members {sorted(have)} != {sorted(want)}")
devices = json.load(open(f"{W}/nb-devices-ctx.json"))["results"]
if devices and "config_context" not in devices[0]:
    errs.append("device list carries no config_context (include=config_context unsupported?)")
NEIGH = re.compile(r"(\d+\.\d+\.\d+\.\d+)\s+4\s+(\d+)\s")
def device_neighbors(name):
    """{(neighbor, remote_as, vrf, afi)} from the raw tables: section headers give VRF and AFI."""
    out, vrf, afi = set(), None, "ipv4"
    for line in open(f"{W}/bgp-{name}.txt"):
        if line.startswith("=== EVPN"):
            afi, vrf = "evpn", None
        elif line.startswith("For address family:"):
            vrf = "WAN" if "VPNv4" in line else None
        elif line.startswith("BGP summary information for VRF"):
            v = line.split()[-1]; vrf = None if v == "default" else v
        m = NEIGH.search(line)
        if m:
            out.add((m.group(1), int(m.group(2)), vrf, afi))
    return out
checked = 0
for d in devices:
    ctx = d.get("config_context") or {}
    local = d.get("local_context_data") or {}
    want_ctx = intent["contexts"]["devices"].get(d["name"])
    if want_ctx is None:
        errs.append(f"{d['name']}: no intent context"); continue
    if local != want_ctx:
        errs.append(f"{d['name']}: local context differs from intent")
    for key in ("domain", "dns", "ntp", "management_gateway"):
        if ctx.get(key) != intent["contexts"]["lab"][key]:
            errs.append(f"{d['name']}: rendered context {key}={ctx.get(key)!r} != lab {intent['contexts']['lab'][key]!r}")
    if ctx.get("automation_user") != "automation" or "password" in json.dumps(ctx).lower():
        errs.append(f"{d['name']}: platform context (automation_user) missing or a password leaked")
    if ctx.get("site") != d["site"]["slug"]:
        errs.append(f"{d['name']}: site context {ctx.get('site')} != {d['site']['slug']}")
    want = {(n["neighbor"], n["remote_as"], n["vrf"], n["afi"]) for n in (ctx.get("bgp") or {}).get("neighbors", [])}
    have = device_neighbors(d["name"])
    if want != have:
        errs.append(f"{d['name']}: context neighbours {sorted(want)} != device {sorted(have)}")
    checked += len(want)
if errs:
    print("\n".join(errs[:30])); sys.exit(1)
print(f"VRFs MGMT/WAN/PROD (PROD RT 50001:50001), {len(asns)} ASNs on their sites, FHRP group {[g['name'] for g in intent['fhrp_groups']]} with its members and address, {checked} BGP neighbours in the device contexts equal the devices' own tables over direct SSH ({len(devices)} devices)")
PY
}
check "S4e.2 VRFs with route targets, prefixes in PROD, the 7 ASNs on their sites, BGP neighbours and RDs in each device's config context equal to the device (direct SSH), the transit virtual router as an FHRP group" c2

# --- S4e.3 one location and one rack per site; every device racked at its derived position -------------------------
c3() {
  nb "${NETBOX_URL}/api/dcim/locations/?limit=100" > "$WORK/nb-locations.json"
  nb "${NETBOX_URL}/api/dcim/racks/?limit=100" > "$WORK/nb-racks.json"
  nb "${NETBOX_URL}/api/dcim/devices/?limit=100" > "$WORK/nb-all-devices.json"
  WORK="$WORK" ${PY} - <<'PY'
import json, os, sys
W = os.environ["WORK"]
intent = json.load(open(f"{W}/intent.json"))["racks"]
locations = {(loc["site"]["slug"], loc["name"]) for loc in json.load(open(f"{W}/nb-locations.json"))["results"]}
racks = {r["name"]: r for r in json.load(open(f"{W}/nb-racks.json"))["results"]}
devices = {d["name"]: d for d in json.load(open(f"{W}/nb-all-devices.json"))["results"]}
errs = []
for r in intent:
    if (r["site"], r["location"]) not in locations:
        errs.append(f"location {r['location']} missing at {r['site']}")
    nbr = racks.get(r["rack"])
    if not nbr:
        errs.append(f"rack {r['rack']} missing"); continue
    if nbr["site"]["slug"] != r["site"] or (nbr.get("location") or {}).get("name") != r["location"] or nbr["u_height"] != r["u_height"]:
        errs.append(f"rack {r['rack']}: site/location/height differ ({nbr['site']['slug']}, {(nbr.get('location') or {}).get('name')}, {nbr['u_height']})")
    seen = {}
    for dev in r["devices"]:
        d = devices.get(dev["name"])
        if not d:
            errs.append(f"{dev['name']}: not in NetBox"); continue
        if not d.get("rack") or d["rack"]["name"] != r["rack"] or d.get("position") != dev["position"] or (d.get("face") or {}).get("value") != dev["face"]:
            errs.append(f"{dev['name']}: rack {(d.get('rack') or {}).get('name')} U{d.get('position')} {(d.get('face') or {}).get('value')} != {r['rack']} U{dev['position']} {dev['face']}")
        if d.get("position") in seen:
            errs.append(f"{r['rack']}: U{d['position']} holds {seen[d['position']]} and {dev['name']}")
        seen[d.get("position")] = dev["name"]
if errs:
    print("\n".join(errs[:30])); sys.exit(1)
print(f"{len(intent)} locations and racks, {sum(len(r['devices']) for r in intent)} devices at their positions ({', '.join(r['rack'] + ': ' + str(len(r['devices'])) + 'U' for r in intent)})")
PY
}
check "S4e.3 one location and one rack per site, every device racked at its derived position (role order top down, unique U)" c3

# --- S4e.4 provider circuits: terminations at the right sites, the edge port's cable trace crosses the circuit -----------
c4() {
  nb "${NETBOX_URL}/api/circuits/circuits/?limit=100" > "$WORK/nb-circuits.json"
  nb "${NETBOX_URL}/api/circuits/circuit-terminations/?limit=100" > "$WORK/nb-terminations.json"
  nb "${NETBOX_URL}/api/circuits/providers/?limit=100" > "$WORK/nb-providers.json"
  nb "${NETBOX_URL}/api/dcim/cables/?limit=500" > "$WORK/nb-cables.json"
  local dev ifc id
  while read -r dev ifc; do
    id=$(nb "${NETBOX_URL}/api/dcim/interfaces/?device=${dev}&name=${ifc}" | ${PY} -c 'import sys,json;print(json.load(sys.stdin)["results"][0]["id"])')
    nb "${NETBOX_URL}/api/dcim/interfaces/${id}/trace/" > "$WORK/trace-${dev}.json"
  done < <(${PY} -c "import json;[print(c['a']['device'], c['a']['interface']) for c in json.load(open('$WORK/intent.json'))['circuits']]")
  WORK="$WORK" ${PY} - <<'PY'
import json, os, sys
W = os.environ["WORK"]
intent = json.load(open(f"{W}/intent.json"))
circuits = {c["cid"]: c for c in json.load(open(f"{W}/nb-circuits.json"))["results"]}
terms = json.load(open(f"{W}/nb-terminations.json"))["results"]
providers = {p["name"] for p in json.load(open(f"{W}/nb-providers.json"))["results"]}
cables = json.load(open(f"{W}/nb-cables.json"))
errs = []
for c in intent["circuits"]:
    if c["provider"] not in providers:
        errs.append(f"provider {c['provider']} missing")
    nbc = circuits.get(c["cid"])
    if not nbc or nbc["provider"]["name"] != c["provider"] or nbc["type"]["slug"] != c["type"] or nbc["status"]["value"] != "active":
        errs.append(f"circuit {c['cid']} missing or wrong ({nbc and (nbc['provider']['name'], nbc['type']['slug'], nbc['status']['value'])})"); continue
    for side in ("a", "z"):
        t = [x for x in terms if x["circuit"]["cid"] == c["cid"] and x["term_side"] == side.upper()]
        if not t or t[0].get("termination_type") != "dcim.site" or (t[0].get("termination") or {}).get("slug") != c[side]["site"]:
            errs.append(f"{c['cid']} termination {side.upper()}: expected site {c[side]['site']}, got {t and (t[0].get('termination_type'), t[0].get('termination'))}")
    # the trace from the edge port: segments of [near ends, cable, far ends]; a termination end carries a
    # circuit-terminations URL; the last far end must be the provider port
    path = json.load(open(f"{W}/trace-{c['a']['device']}.json"))
    ends = [e for seg in path for part in seg if isinstance(part, list) for e in part]
    if not any("circuit-terminations" in (e.get("url") or "") for e in ends):
        errs.append(f"{c['cid']}: the trace from {c['a']['device']} {c['a']['interface']} crosses no circuit termination")
    last = path[-1][-1][0] if path and isinstance(path[-1][-1], list) and path[-1][-1] else {}
    far = ((last.get("device") or {}).get("name"), last.get("name"))
    if far != (c["z"]["device"], c["z"]["interface"]):
        errs.append(f"{c['cid']}: the trace ends at {far}, expected {(c['z']['device'], c['z']['interface'])}")
direct = [cb for cb in cables["results"] if all(t.get("object_type") == "dcim.interface" for t in cb["a_terminations"] + cb["b_terminations"])
          and {t["object"]["device"]["name"] for t in cb["a_terminations"] + cb["b_terminations"]} & {"isp-core01"}]
if direct:
    errs.append(f"direct cables to isp-core01 still exist: {[cb['id'] for cb in direct]}")
if errs:
    print("\n".join(errs[:30])); sys.exit(1)
print(f"{len(intent['circuits'])} circuits of {sorted(providers)} with A/Z terminations at their sites; every edge port traces through its circuit to the isp-core01 port; {cables['count']} cables (no direct cable to the provider core)")
PY
}
check "S4e.4 provider circuits with terminations at their sites; the cable trace from each edge port crosses the circuit to the isp-core01 port; no direct provider cable remains" c4

# --- S4e.5 journal entries: one per site from the play's changing runs; create and delete entries on br2-sw01 (LCM) ----
c5() {
  nb "${NETBOX_URL}/api/extras/journal-entries/?limit=500" > "$WORK/nb-journal.json"
  nb "${NETBOX_URL}/api/dcim/sites/?limit=100" > "$WORK/nb-sites.json"
  nb "${NETBOX_URL}/api/dcim/devices/?name=br2-sw01" > "$WORK/nb-br2sw01.json"
  WORK="$WORK" ${PY} - <<'PY'
import json, os, re, subprocess, sys
W = os.environ["WORK"]
entries = json.load(open(f"{W}/nb-journal.json"))["results"]
sites = {s["id"]: s["slug"] for s in json.load(open(f"{W}/nb-sites.json"))["results"]}
sw = json.load(open(f"{W}/nb-br2sw01.json"))["results"][0]["id"]
errs = []
per_site = {}
for e in entries:
    if e["assigned_object_type"] == "dcim.site" and e["comments"].startswith("netbox-enrich.yml (git "):
        per_site.setdefault(sites.get(e["assigned_object_id"]), []).append(e)
for slug in sites.values():
    if not per_site.get(slug):
        errs.append(f"site {slug}: no netbox-enrich.yml journal entry")
shas = {m.group(1) for es in per_site.values() for e in es for m in [re.search(r"\(git ([0-9a-f]+)\)", e["comments"])] if m}
for sha in shas:
    if subprocess.run(["git", "cat-file", "-e", sha], capture_output=True).returncode != 0:
        errs.append(f"journal names commit {sha}, unknown to this repo")
switch = [e["comments"] for e in entries if e["assigned_object_type"] == "dcim.device" and e["assigned_object_id"] == sw]
for marker in ("Lifecycle Manager branch-vlan create: VLAN", "Lifecycle Manager branch-vlan delete: VLAN"):
    if not any(c.startswith(marker) for c in switch):
        errs.append(f"br2-sw01: no journal entry starting with {marker!r} (test-06b S4d.3 writes them through the LCM actions)")
if errs:
    print("\n".join(errs[:20])); sys.exit(1)
print(f"{sum(len(v) for v in per_site.values())} play entries over {len(per_site)} sites naming commits {sorted(shas)}; br2-sw01 carries {len(switch)} LCM entries (newest: {switch[0][:70]!r})")
PY
}
check "S4e.5 journal entries: every site carries netbox-enrich.yml entries naming a real commit; br2-sw01 carries the LCM create and delete entries" c5

# --- S4e.6 idempotency, the agent on the enriched objects, Golden Config with the interface intent --------------------
COMPLIANCE_PLAN=$(${PY} -c "import yaml;print(yaml.safe_load(open('$V'))['golden_config']['plan'])")
c6() {
  # 1) a second run of the play changes nothing
  local out
  out=$(cd ansible && NETBOX_API="$NETBOX_URL" ansible-playbook playbooks/netbox-enrich.yml < /dev/null 2>&1) || { echo "$out" | grep -E 'ERROR|failed:' | head -5; return 1; }
  echo "$out" | grep -E '^localhost' | grep -q 'changed=0 ' || { echo "netbox-enrich.yml is not idempotent: $(echo "$out" | grep -E '^localhost')"; return 1; }
  echo "netbox-enrich.yml re-run: $(echo "$out" | grep -E '^localhost' | sed 's/  */ /g')"
  # 2) netbox-sot answers one question per object type; every answer is checked against the NetBox API
  local sid txt q want
  while IFS='|' read -r q want; do
    sid=$(run_agent netbox-sot "{\"request\":\"${q}\"}") || { echo "$sid"; return 1; }
    txt=$(session_text "${sid##*$'\n'}")
    for w in $want; do echo "$txt" | grep -qi -- "$w" || { echo "netbox-sot on '${q}': answer lacks '${w}': ${txt:0:300}"; return 1; }; done
    echo "netbox-sot: '${q}' -> ${txt:0:110} ($(count_tokens "${sid##*$'\n'}"))"
  done <<EOF
Which address does interface GigabitEthernet2 of br1-wan01 carry, in which VRF, and what does its description say?|$(nb "${NETBOX_URL}/api/ipam/ip-addresses/?device=br1-wan01&interface=GigabitEthernet2" | ${PY} -c 'import sys,json;r=json.load(sys.stdin)["results"][0];print(r["address"], r["vrf"]["name"])') isp-core01
Which autonomous system numbers are assigned to site br1?|$(nb "${NETBOX_URL}/api/ipam/asns/?site=br1" | ${PY} -c 'import sys,json;print(" ".join(str(a["asn"]) for a in json.load(sys.stdin)["results"]))')
In which rack and at which rack unit position is dc1-leaf01 installed?|$(nb "${NETBOX_URL}/api/dcim/devices/?name=dc1-leaf01" | ${PY} -c 'import sys,json;d=json.load(sys.stdin)["results"][0];print(d["rack"]["name"], int(d["position"]))')
Which circuit terminates at site br1 and who is its provider?|$(nb "${NETBOX_URL}/api/circuits/circuits/?site=br1" | ${PY} -c 'import sys,json;c=json.load(sys.stdin)["results"][0];print(c["cid"], c["provider"]["name"])')
What NTP server and domain does the rendered config context of br2-sw01 give?|$(nb "${NETBOX_URL}/api/dcim/devices/?name=br2-sw01" | ${PY} -c 'import sys,json;c=json.load(sys.stdin)["results"][0]["config_context"];print(c["ntp"], c["domain"])')
EOF
  # 3) the compliance plan is clean on every device with the interface intent in the leaves
  local pid batch
  pid=$(plan_id "$COMPLIANCE_PLAN"); [ -n "$pid" ] || { echo "compliance plan ${COMPLIANCE_PLAN} not found"; return 1; }
  batch=$(run_plan "$pid") || { echo "$batch"; return 1; }
  batch_issues "$batch" > "$WORK/issues.txt" || { echo "batch ${batch} reports unreadable"; return 1; }
  [ "$(wc -l < "$WORK/issues.txt")" -ge 12 ] || { echo "fewer than 12 device reports:"; cat "$WORK/issues.txt"; return 1; }
  awk '$2+$3>0' "$WORK/issues.txt" | grep -q . && { echo "issues with the interface intent in the tree:"; awk '$2+$3>0' "$WORK/issues.txt"; return 1; }
  echo "compliance ${COMPLIANCE_PLAN}: $(wc -l < "$WORK/issues.txt" | tr -d ' ') device reports, 0 errors, 0 warnings (passes: $(awk '{s+=$4} END{print s}' "$WORK/issues.txt")) with the interface intent rendered from NetBox"
}
check "S4e.6 the play re-runs with changed=0; netbox-sot answers address/VRF/peer, site ASNs, rack position, circuit and context questions equal to the NetBox API; the compliance plan stays clean with the interface intent in the device leaves" c6
echo
echo "tokens spent this run: $(token_totals) (Anthropic)"

echo
echo "passed=${pass} failed=${fail}"
[ "$fail" -eq 0 ]
