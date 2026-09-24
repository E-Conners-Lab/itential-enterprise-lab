#!/usr/bin/env bash
# Phase 5 verification: Itential Platform + Automation Gateway (PID S4 criteria 1-7) and the
# ServiceNow PDI adapter (S4b criteria 1-5). Intent: itential/versions.yaml, docs/image-manifest.md,
# topology/ipam.yaml. State: the Platform API over TLS, Gateway Manager, NetBox, the devices
# themselves (verify/devcmd.py, second source), the VM over SSH, the MCP server from this Mac,
# and the PDI's REST API. Writes: one NetBox VLAN + one switch VLAN per run (removed at the end),
# one change request in the PDI. S4b never passes silently: a sleeping PDI prints HIBERNATED.
#
# Against the dev stack (ADR 0063) only ONLY="S4.1 S4.7" is meaningful, run as
#   IT_HOST=itential-dev.lab.internal MCP_HOST=mcp-dev.lab.internal IT_IP=10.100.0.65 IT_MCP_IP=10.100.0.65 \
#   ONLY="S4.1 S4.7" verify/test-05-itential.sh
# S4.2-S4.4 use the EVE-NG devices and write NetBox, S4.6 measures the production Platform node, and dev has
# no ServiceNow, so every other criterion either fails there or reaches production. Dev's own checks are in
# verify/test-05b-dev-copilot.sh. With no overrides the names are production's, exactly as before.
set -uo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "missing .env"; exit 1; }
set -a; . ./.env; set +a
: "${NETBOX_URL:?}" "${NETBOX_TOKEN:?}" "${AUTOMATION_PASSWORD:?}" "${ITENTIAL_ADMIN_PASSWORD:?}"
ADMIN_USER=${ITENTIAL_ADMIN_USER:-admin@itential}
PY=.venv/bin/python
V=itential/versions.yaml
# The S11 cut-over (ADR 0053/0055) moved itential.lab.internal onto the load balancer, so the name is the
# address: no --resolve is forced any more and these run against whatever the record points at. IT_IP (and
# IT_MCP_IP, for the MCP server on its own VM) still pin a specific host when one is being proved directly.
IT_IP=${IT_IP:-}
# S4.6 measures the Platform host. That was VM 205 until S11.8 retired it (ADR 0053), so it is now the active
# production node - the first of the HA2 oracle's platform nodes, the one the load balancer and the gateway use.
IAP_IP=${IT_IP:-$(${PY} -c "import yaml;d=yaml.safe_load(open('itential/ha2/versions.yaml'));n=d['platform']['nodes'][0];print(next(v['ip'] for v in d['vms'] if v['name']==n))")}
MONGO_IP=$(${PY} -c "import yaml;d=yaml.safe_load(open('itential/ha2/versions.yaml'));print(next(v['ip'] for v in d['vms'] if v['role']=='mongodb'))")
# IT_HOST / MCP_HOST default to production's names; the dev stack passes its own (ADR 0063, header above)
IT_HOST=${IT_HOST:-itential.lab.internal}
MCP_HOST=${MCP_HOST:-mcp.lab.internal}
RESOLVE=${IT_IP:+--resolve ${IT_HOST}:443:${IT_IP}}
# the MCP server has its own VM in the production environment (ADR 0053): S4.7 holds the name to the
# HA2 oracle's tools VM, so a stale record on the retiring dev-stack is still caught
MCP_IP=${IT_MCP_IP:-$(${PY} -c "import yaml;d=yaml.safe_load(open('itential/ha2/versions.yaml'));print(next(v['ip'] for v in d['vms'] if v['role']=='tools'))")}
PLATFORM="https://${IT_HOST}"
CA=docs/lab-root-ca.crt
SSH="ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new"
ts=$(date -u +%Y%m%dT%H%M%SZ)
fail=0; pass=0; deferred=0; hibernated=0
ok()    { echo "PASS  $1"; pass=$((pass+1)); }
bad()   { echo "FAIL  $1"; fail=$((fail+1)); }
defer() { echo "DEFER $1"; deferred=$((deferred+1)); }
# ONLY="S4.4" runs a subset while iterating (every criterion still runs by default)
check() { local name=$1; shift; if [ -n "${ONLY:-}" ] && ! echo " ${ONLY} " | grep -q " ${name%% *} "; then echo "SKIP  $name"; return; fi; if "$@" >/tmp/verify05.$$ 2>&1; then ok "$name"; else bad "$name"; sed 's/^/      /' /tmp/verify05.$$ | head -12; fi; }
nb()    { curl -s -m 20 -H "Authorization: Token ${NETBOX_TOKEN}" "$@"; }
JAR=$(mktemp); trap 'rm -f "$JAR" /tmp/verify05.$$' EXIT
# Every call to the Platform goes through the lab CA and the real name: no -k anywhere.
iap()   { curl -s -m 60 --cacert "$CA" ${RESOLVE} -b "$JAR" -H "Content-Type: application/json" "$@"; }
iap_login() { iap -c "$JAR" -X POST "${PLATFORM}/login" -d "{\"username\":\"${ADMIN_USER}\",\"password\":\"${ITENTIAL_ADMIN_PASSWORD}\"}" -o /dev/null -w '%{http_code}' | grep -qx 200; }
# run_job <workflow> <variables-json> -> prints the job id; waits for a terminal status unless $3=nowait
run_job() {
  local wf=$1 vars=$2 mode=${3:-wait} id status
  local resp; resp=$(iap -X POST "${PLATFORM}/operations-manager/jobs/start" -d "{\"workflow\":\"${wf}\",\"options\":{\"type\":\"automation\",\"description\":\"verify ${ts}\",\"variables\":${vars}}}")
  id=$(echo "$resp" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);x=d.get("data");print(x.get("_id","") if isinstance(x,dict) else "")')
  [ -n "$id" ] || { echo "job start failed for ${wf}: $(echo "$resp" | head -c 300)"; return 1; }
  echo "$id"
  [ "$mode" = nowait ] && return 0
  for _ in $(seq 1 60); do
    status=$(iap "${PLATFORM}/operations-manager/jobs/${id}" | ${PY} -c 'import sys,json;print(json.load(sys.stdin)["data"]["status"])')
    case "$status" in complete) return 0;; error|canceled|cancelled) echo "job ${id} status ${status}"; return 1;; esac
    sleep 5
  done
  echo "job ${id} did not finish (last status ${status})"; return 1
}
job_vars() { iap "${PLATFORM}/operations-manager/jobs/$1" | ${PY} -c 'import sys,json;print(json.dumps(json.load(sys.stdin)["data"].get("variables",{})))'; }
job_status() { iap "${PLATFORM}/operations-manager/jobs/$1" | ${PY} -c 'import sys,json;print(json.load(sys.stdin)["data"]["status"])'; }
mkdir -p verify/results; exec > >(tee "verify/results/${ts}-05-itential.log") 2>&1
echo "# test-05-itential ${ts}"
PLATFORM_TAG=$(${PY} -c "import yaml;print(yaml.safe_load(open('$V'))['images']['platform']['tag'])")
PLATFORM_VER=${PLATFORM_TAG%%-*}
[ -s "$CA" ] || { bad "$CA missing"; echo; echo "passed=0 failed=12"; exit 1; }
iap_login || { bad "S4.1 login to ${PLATFORM} as ${ADMIN_USER} through the lab CA"; echo; echo "passed=0 failed=12"; exit 1; }

# --- S4.1 TLS from the lab CA on 10.100.0.65; version equals the pin; both gateways registered ---
c1() {
  local san; san=$(openssl s_client -connect "${IT_HOST}:443" -servername "$IT_HOST" -CAfile "$CA" </dev/null 2>/dev/null | openssl x509 -noout -ext subjectAltName 2>/dev/null)
  echo "$san" | grep -q "DNS:${IT_HOST}" || { echo "SAN missing ${IT_HOST}: ${san}"; return 1; }
  openssl s_client -connect "${IT_HOST}:443" -servername "$IT_HOST" -CAfile "$CA" </dev/null 2>/dev/null | grep -q "Verify return code: 0" || { echo "chain does not verify against ${CA}"; return 1; }
  local ver; ver=$(iap "${PLATFORM}/health/server" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);print(d.get("release") or d.get("version") or d)')
  [ "$ver" = "$PLATFORM_VER" ] || { echo "platform reports ${ver}, versions.yaml pins ${PLATFORM_VER}"; return 1; }
  local conns; conns=$(iap "${PLATFORM}/gateway_manager/v1/connections")
  echo "$conns" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);c=d.get("data",d);assert any(v for v in c.values()),"no gateway5 connections"' || { echo "$conns" | head -c 300; return 1; }
  # Gateway 4 is deferred (owner decision 2026-09-07); Gateway 5 is the registered gateway.
  iap "${PLATFORM}/gateway_manager/v1/gateways" | ${PY} -c 'import sys,json;g=json.load(sys.stdin)["results"];assert any(x["enabled"] for x in g),g'
}
check "S4.1 ${PLATFORM} serves a lab-CA cert for ${IT_HOST}, runs Platform ${PLATFORM_VER}, Gateway 5 registered and connected" c1

# --- S4.2 NetBox adapter: workflow device count == GET /api/dcim/devices/ --------------------------
c2() {
  local want got id; want=$(nb "${NETBOX_URL}/api/dcim/devices/?limit=1" | ${PY} -c 'import sys,json;print(json.load(sys.stdin)["count"])')
  id=$(run_job "Count Devices in NetBox" '{}') || { echo "$id"; return 1; }
  # S4f (ADR 0054): through the Integration Model the task output is the HTTP response object, so the
  # parsed payload is `body` where the adapter wrapped it in `response` (measured on 6.5.2).
  got=$(job_vars "${id##*$'\n'}" | ${PY} -c 'import sys,json;print(((json.load(sys.stdin).get("devices") or {}).get("body") or {}).get("count"))')
  [ "$got" = "$want" ] || { echo "workflow ${got} vs NetBox API ${want}"; return 1; }
  echo "device_count=${got}"
}
check "S4.2 Count Devices in NetBox through the lab-netbox integration returns NetBox's count == GET /api/dcim/devices/" c2

# --- S4.3 show version through IAG on one node per vendor equals the manifest and the device itself ---
c3() {
  local errs="" id out direct dev_name dev_ip want cmd
  # (device, expected version string from ADR 0032/0033, direct command)
  for row in "br1-wan01:10.100.0.146:17.13.01a:show version" "br1-sw01:10.100.0.165:4.33.1.1F:show version"; do
    IFS=: read -r dev_name dev_ip want cmd <<<"$row"
    id=$(run_job "Get Device Software Version" "{\"device\":\"${dev_name}\"}") || { errs+="${dev_name}: ${id}\n"; continue; }
    out=$(job_vars "${id##*$'\n'}" | ${PY} -c 'import sys,json;r=(json.load(sys.stdin).get("show_version") or {}).get("result",{}).get("results",[{}]);print(r[0].get("output","") if r and r[0].get("success") else "")')
    echo "$out" | grep -q "$want" || { errs+="${dev_name}: IAG output lacks ${want}\n"; continue; }
    direct=$(${PY} verify/devcmd.py "$dev_ip" "$cmd" 2>/dev/null) || { errs+="${dev_name}: direct ssh failed\n"; continue; }
    echo "$direct" | grep -q "$want" || errs+="${dev_name}: device itself lacks ${want}\n"
    echo "${dev_name}: ${want} via Gateway 5 and over direct SSH"
  done
  [ -z "$errs" ] || { printf "%b" "$errs"; return 1; }
}
check "S4.3 Get Device Software Version via IAG: br1-wan01 = 17.13.01a, br1-sw01 = 4.33.1.1F, cross-checked over direct SSH" c3

# --- S4.4 Add Branch VLAN: reserve in NetBox + configure br1-sw01, approval task, idempotent, rollback ---
VLAN_NAME="verify-$(echo "$ts" | tr "A-Z" "a-z")"
approve_pending_task() {
  # the workflow parks on the JSON form task (4a, ADR 0044); finishing it with success and export.decision=approve is the
  # approval (what the Work Center card submits); a failure finish is a rejection and rolls the reservation back
  local job=$1 decision=${2:-success} task
  for _ in $(seq 1 24); do
    task=$(iap "${PLATFORM}/operations-manager/jobs/${job}" | ${PY} -c 'import sys,json;d=json.load(sys.stdin)["data"];t=[k for k,v in d.get("tasks",{}).items() if v.get("type")=="manual" and v.get("status")=="running"];print(t[0] if t else "")')
    [ -n "$task" ] && break; sleep 5
  done
  [ -n "$task" ] || { echo "no pending manual task on job ${job}"; return 1; }
  local vars='{}'; [ "$decision" = success ] && vars='{"export":{"decision":"approve"}}'
  iap -X POST "${PLATFORM}/operations-manager/jobs/${job}/tasks/${task}/finish" -d "{\"taskData\":{\"finish_state\":\"${decision}\",\"variables\":${vars}}}" -o /dev/null -w '%{http_code}' | grep -qx 200
}
wait_job() { local id=$1 s; for _ in $(seq 1 60); do s=$(job_status "$id"); case "$s" in complete) return 0;; error|canceled|cancelled) echo "job ${id} ${s}"; return 1;; esac; sleep 5; done; echo "job ${id} timeout (${s})"; return 1; }
nb_vlan() { nb "${NETBOX_URL}/api/ipam/vlans/?site=br1&name=${VLAN_NAME}"; }
c4() {
  local id vid
  id=$(run_job "Add Branch VLAN" "{\"branch\":\"br1\",\"vlan_name\":\"${VLAN_NAME}\",\"switch_override\":\"\",\"change_request\":false}" nowait) || { echo "$id"; return 1; }
  approve_pending_task "$id" || return 1
  wait_job "$id" || return 1
  vid=$(nb_vlan | ${PY} -c 'import sys,json;d=json.load(sys.stdin);assert d["count"]==1,d["count"];v=d["results"][0];assert v["status"]["value"]=="active",v["status"];print(v["vid"])') || { echo "NetBox VLAN ${VLAN_NAME} not active in br1"; return 1; }
  ${PY} verify/devcmd.py 10.100.0.165 "show vlan ${vid}" | grep -q "$VLAN_NAME" || { echo "br1-sw01 has no VLAN ${vid} ${VLAN_NAME}"; return 1; }
  echo "run 1: VLAN ${vid} ${VLAN_NAME} in NetBox and on br1-sw01"
  # run 2 must be a no-op: same VID, still exactly one NetBox object, job reports no change
  id=$(run_job "Add Branch VLAN" "{\"branch\":\"br1\",\"vlan_name\":\"${VLAN_NAME}\",\"switch_override\":\"\",\"change_request\":false}" nowait) || { echo "$id"; return 1; }
  wait_job "$id" || return 1
  job_vars "$id" | ${PY} -c 'import sys,json;v=json.load(sys.stdin);assert v.get("changed") is False, v' || { echo "run 2 was not a no-op"; return 1; }
  nb_vlan | ${PY} -c 'import sys,json;d=json.load(sys.stdin);assert d["count"]==1 and d["results"][0]["vid"]=='"$vid"',d' || { echo "run 2 changed NetBox"; return 1; }
  echo "run 2: no-op"
  # rollback: a switch that is not in the inventory makes the device step fail; the reservation must go
  id=$(run_job "Add Branch VLAN" "{\"branch\":\"br1\",\"vlan_name\":\"${VLAN_NAME}-rb\",\"switch_override\":\"no-such-switch\",\"change_request\":false}" nowait) || { echo "$id"; return 1; }
  approve_pending_task "$id" || return 1
  wait_job "$id" >/dev/null 2>&1 && { echo "rollback run unexpectedly succeeded"; return 1; }
  nb "${NETBOX_URL}/api/ipam/vlans/?site=br1&name=${VLAN_NAME}-rb" | ${PY} -c 'import sys,json;assert json.load(sys.stdin)["count"]==0,"reservation survived the failure"' || return 1
  echo "run 3: device failure rolled the NetBox reservation back"
}
check "S4.4 Add Branch VLAN: reserve + configure with approval; second run no-op; NetBox rollback on device failure" c4
# cleanup of the verify VLAN (NetBox object and the switch); never leaves lab state behind
vid=$(nb_vlan | ${PY} -c 'import sys,json;d=json.load(sys.stdin);print(d["results"][0]["vid"] if d["count"] else "")' 2>/dev/null)
if [ -n "$vid" ]; then
  nb -X DELETE "${NETBOX_URL}/api/ipam/vlans/$(nb_vlan | ${PY} -c 'import sys,json;print(json.load(sys.stdin)["results"][0]["id"])')/" -o /dev/null
  ${PY} verify/devcmd.py 10.100.0.165 "configure
no vlan ${vid}
end" >/dev/null 2>&1 || true
fi

# --- S4.5 licence state recorded -------------------------------------------------------------
c5() { sed -n '/^### 3.5 Itential/,/^### 3.4/p' docs/image-manifest.md | grep -qi "licen[cs]e.*2026-09-07" && ! sed -n '/^### 3.5 Itential/,/^### 3.4/p' docs/image-manifest.md | grep -q UNVERIFIED; }
check "S4.5 licence state (none required, owner decision 2026-09-07) recorded in manifest 3.5" c5

# --- S4.6 memory pressure on the VM after 24 h ------------------------------------------------
c6() {
  local up used total pct cache
  up=$($SSH "ubuntu@${IAP_IP}" "cut -d. -f1 /proc/uptime") || { echo "ssh to the Platform node ${IAP_IP} failed"; return 1; }
  read -r total used <<<"$($SSH "ubuntu@${IAP_IP}" "free -m | awk '/^Mem:/{print \$2, \$3}'")"
  pct=$((used * 100 / total))
  # the database is its own replica set now, so its cache is read from a member rather than from the Platform host
  cache=$($SSH "ubuntu@${MONGO_IP}" "sudo docker exec mongodb mongosh --quiet --tls --tlsCAFile /etc/mongo/tls/ca.crt --host \$(hostname -f) -u admin -p '${MONGO_ADMIN_PASSWORD:-}' --authenticationDatabase admin --eval 'const c=db.serverStatus().wiredTiger.cache; print(Math.round(c[\"bytes currently in the cache\"]/1048576)+\" MB in cache of \"+Math.round(c[\"maximum bytes configured\"]/1048576)+\" MB max\")' 2>/dev/null" || echo "n/a")
  echo "Platform node ${IAP_IP}: uptime ${up}s; RAM used ${used}/${total} MB (${pct}%); MongoDB wiredTiger ${cache}"
  [ "$pct" -lt 80 ] || { echo "memory above 80%: apply the budget levers by PR"; return 1; }
  [ "$up" -ge 86400 ] || { echo "MEASURE-EARLY: uptime under 24 h, re-run after $(( (86400 - up) / 3600 )) h"; return 2; }
}
if c6 >/tmp/verify05.$$ 2>&1; then ok "S4.6 the Platform node memory under 80 % after 24 h"; sed 's/^/      /' /tmp/verify05.$$
elif [ $? = 2 ]; then defer "S4.6 memory under 80 % now, but uptime is under 24 h"; sed 's/^/      /' /tmp/verify05.$$
else bad "S4.6 Platform node memory pressure"; sed 's/^/      /' /tmp/verify05.$$; fi

# --- S4.7 Claude Code on this Mac reaches mcp.lab.internal and runs a read-only tool -------------
c7() {
  local url="http://${MCP_HOST}:8000/mcp" tools res
  ${PY} -c "import socket;assert socket.gethostbyname('${MCP_HOST}')=='${MCP_IP}','${MCP_HOST} resolves elsewhere'" || return 1
  tools=$(${PY} verify/mcpcall.py "$url" tools) || { echo "$tools"; return 1; }
  echo "$tools" | grep -qx get_health || { echo "get_health not in tools: $(echo "$tools" | tr '\n' ' ')"; return 1; }
  # ADR 0039 amendment: the tools that return node attributes (itential_password) are hidden from MCP clients
  for hidden in describe_inventory get_devices; do echo "$tools" | grep -qx "$hidden" && { echo "${hidden} is exposed to MCP clients (returns node credentials)"; return 1; }; done
  res=$(${PY} verify/mcpcall.py "$url" call get_health) || { echo "$res"; return 1; }
  echo "$res" | grep -q "$PLATFORM_VER" || { echo "get_health did not return platform ${PLATFORM_VER}: $(echo "$res" | head -c 300)"; return 1; }
  echo "$(echo "$tools" | wc -l | tr -d ' ') tools; get_health reports ${PLATFORM_VER}"
  # .mcp.json points Claude Code at production's MCP by design; against mcp-dev there is nothing to match
  [ "$MCP_HOST" != mcp.lab.internal ] || ${PY} -c 'import json;c=json.load(open(".mcp.json"))["mcpServers"]["itential"];assert c["url"]=="'"$url"'",c'
}
check "S4.7 mcp.lab.internal answers streamable HTTP from this Mac; get_health returns Platform ${PLATFORM_VER}; .mcp.json matches" c7

# =============================== S4b ServiceNow PDI ==============================================
snow() { curl -s -m 30 -u "${SNOW_USER}:${SNOW_PASSWORD}" -H "Accept: application/json" "$@"; }
if [ -z "${SNOW_INSTANCE:-}" ] || [ -z "${SNOW_USER:-}" ] || [ -z "${SNOW_PASSWORD:-}" ]; then
  for c in "S4b.1 servicenow-api integration tools" "S4b.2 change request lifecycle" "S4b.3 update set in servicenow/" "S4b.4 instance recorded" "S4b.5 interactive login age"; do bad "$c: SNOW_INSTANCE/SNOW_USER/SNOW_PASSWORD missing from .env"; done
else
  SNOW_URL="https://${SNOW_INSTANCE}.service-now.com"
  code=$(snow -o /dev/null -w '%{http_code}' "${SNOW_URL}/api/now/table/sys_properties?sysparm_limit=1")
  if [ "$code" != 200 ]; then
    echo "HIBERNATED  PDI ${SNOW_INSTANCE} answered HTTP ${code}: wake it at developer.servicenow.com and re-run (S4b.1-S4b.5 not evaluated, not passed)"
    hibernated=5
  else
    # --- S4b.1 the Platform's path to the PDI ---
    # Was "adapter-servicenow RUNNING in /health/adapters" until 2026-09-11. S4f (ADR 0054 decision 3)
    # removed that adapter entirely - itential/versions.yaml says so and
    # tests/test_integrations.py::test_the_servicenow_adapter_is_gone_and_the_required_three_remain
    # asserts it - so this criterion had been asserting the opposite of S4f.6 in
    # verify/test-06d-integrations.sh ("adapter-servicenow is gone"). Both could not pass; this one
    # only kept passing while the adapter lingered on a Platform the play had not replayed yet.
    # The S4f replacement is the Integration Model: the PDI is reached through servicenow-api, and an
    # operation the lab uses is an authorized tool. NOT an integration health check - an integration is
    # a virtual adapter and reports STOPPED while every operation works (runbook 06).
    c8() {
      local refs got
      refs=$(${PY} -c "
import json, yaml
v = yaml.safe_load(open('itential/versions.yaml'))['integrations']['models']['servicenow']
ops = ['listIncidents', 'updateIncident']
print(json.dumps({'referenceIds': [f\"integration:{v['title']}%3A{v['version']}:{v['instance']}:{o}\" for o in ops],
                  'queryOptions': {'limit': 20}}))") || return 1
      got=$(iap -X POST "${PLATFORM}/tools/bulk" -d "$refs") || return 1
      echo "$got" | ${PY} -c '
import sys, json
d = json.load(sys.stdin)
d = d.get("data") or d.get("results") or []
assert d, "tools/bulk returned nothing for the servicenow-api operations"
unauth = [t["referenceId"].split(":")[-1] for t in d if not t.get("authorized", True)]
assert not unauth, f"not authorized: {unauth}"
print(f"  servicenow-api: {len(d)} operations are authorized tools")' || return 1
      # and the adapter really is gone, so this can never drift back to contradicting S4f.6
      iap "${PLATFORM}/health/adapters" | ${PY} -c '
import sys, json
a = json.load(sys.stdin); a = a.get("results", a)
r = [x for x in a if "servicenow" in (x.get("id") or x.get("_id") or "").lower()]
assert not r, f"adapter-servicenow is back; S4f (ADR 0054) removed it: {r}"
print("  adapter-servicenow absent, as S4f requires")'
    }
    check "S4b.1 the PDI is reached through the servicenow-api Integration Model (authorized tools), and adapter-servicenow is gone" c8
    # --- S4b.2 change request opened, work-noted with the NetBox reservation, closed ---
    c9() {
      local id chg sid
      id=$(run_job "Add Branch VLAN" "{\"branch\":\"br2\",\"vlan_name\":\"${VLAN_NAME}-snow\",\"switch_override\":\"\",\"change_request\":true}" nowait) || { echo "$id"; return 1; }
      approve_pending_task "$id" || return 1
      wait_job "$id" || return 1
      local vars; vars=$(job_vars "$id")
      chg=$(echo "$vars" | ${PY} -c 'import sys,json;print(json.load(sys.stdin).get("change_number",""))')
      [ -n "$chg" ] || { echo "workflow returned no change_number"; return 1; }
      # every state ServiceNow reported back to the workflow (sys_audit is admin-only on the PDI)
      echo "$vars" | ${PY} -c 'import sys,json;v=json.load(sys.stdin);st=[v.get("change_state_"+k) for k in ("scheduled","implement","review","closed")];assert st==["Scheduled","Implement","Review","Closed"],st;print("transitions:", " -> ".join(["New"]+st))' || return 1
      # second source: the change itself, read back from the PDI
      snow "${SNOW_URL}/api/now/table/change_request?sysparm_query=number=${chg}&sysparm_fields=number,state,work_notes,close_code&sysparm_display_value=true" | ${PY} -c 'import sys,json;r=json.load(sys.stdin)["result"];assert r and r[0]["state"]=="Closed",r;assert "netbox" in (r[0].get("work_notes") or "").lower(),"no NetBox work note";print("PDI:",r[0]["number"],r[0]["state"],r[0]["close_code"])' || { echo "change ${chg} not closed with a NetBox work note"; return 1; }
      # cleanup of the br2 verify VLAN
      local vinfo; vinfo=$(nb "${NETBOX_URL}/api/ipam/vlans/?site=br2&name=${VLAN_NAME}-snow" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);print(d["results"][0]["id"],d["results"][0]["vid"]) if d["count"] else print("")')
      if [ -n "$vinfo" ]; then nb -X DELETE "${NETBOX_URL}/api/ipam/vlans/${vinfo%% *}/" -o /dev/null; ${PY} verify/devcmd.py 10.100.0.166 "configure
no vlan ${vinfo##* }
end" >/dev/null 2>&1 || true; fi
    }
    check "S4b.2 Add Branch VLAN with change_request=true opens a standard change, work-notes the NetBox reservation, walks New->Scheduled->Implement->Review->Closed" c9
    # --- S4b.3 update set exported ---
    # No PDI customisation exists (stock template, stock group, one user): servicenow/README.md is the
    # rebuild record (PID 1.5) and the two stock records it names must be present on the instance.
    c10() {
      [ -s servicenow/README.md ] || { echo "servicenow/README.md missing"; return 1; }
      snow "${SNOW_URL}/api/sn_chg_rest/change/standard/template?sysparm_query=sys_id=b1c8d15147810200e90d87e8dee490f7" | ${PY} -c 'import sys,json;r=json.load(sys.stdin)["result"];assert r and r[0]["sys_name"]["value"]=="Change VLAN on a Cisco switchport",r' || { echo "standard change template missing on the PDI"; return 1; }
      snow "${SNOW_URL}/api/now/table/sys_user_group/287ebd7da9fe198100f92cc8d1d2154e?sysparm_fields=name" | ${PY} -c 'import sys,json;assert json.load(sys.stdin)["result"]["name"]=="Network"' || { echo "assignment group Network missing on the PDI"; return 1; }
    }
    check "S4b.3 servicenow/README.md is the PDI rebuild record; the stock VLAN change template and the Network group exist on the instance" c10
    # --- S4b.4 instance name + release family recorded, never the password ---
    c11() { grep -q "^SNOW_INSTANCE=${SNOW_INSTANCE}" .env && grep -q "${SNOW_INSTANCE}.service-now.com" docs/image-manifest.md && grep -Eq "Release family.*(Zurich|Australia)" docs/image-manifest.md && ! grep -rqF "${SNOW_PASSWORD}" docs itential servicenow ansible verify/results; }
    check "S4b.4 SNOW_INSTANCE in .env and the manifest with its release family; password nowhere in the repo" c11
    # --- S4b.5 interactive login age ---
    c12() {
      local last days; last=$(snow "${SNOW_URL}/api/now/table/sys_user?sysparm_query=user_name=admin&sysparm_fields=last_login_time" | ${PY} -c 'import sys,json;print(json.load(sys.stdin)["result"][0]["last_login_time"])')
      days=$(${PY} -c "from datetime import datetime,timezone;d=datetime.strptime('${last}','%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc);print((datetime.now(timezone.utc)-d).days)")
      echo "last interactive admin login ${last} UTC (${days} days ago)"
      [ "$days" -ge 7 ] && echo "WARN  log into the PDI soon (reclaimed after 10 days without an interactive login)"
      [ "$days" -le 10 ]
    }
    check "S4b.5 PDI admin interactive login within 10 days" c12
  fi
fi

echo
echo "passed=${pass} failed=${fail} deferred=${deferred} hibernated=${hibernated}"
[ "$fail" = 0 ] && [ "$hibernated" = 0 ]
