#!/usr/bin/env bash
# Phase 5 verification: Itential Platform + Automation Gateway (PID S4 criteria 1-7) and the
# ServiceNow PDI adapter (S4b criteria 1-5). Intent: itential/versions.yaml, docs/image-manifest.md,
# topology/ipam.yaml. State: the Platform API over TLS, Gateway Manager, NetBox, the devices
# themselves (verify/devcmd.py, second source), the VM over SSH, the MCP server from this Mac,
# and the PDI's REST API. Writes: one NetBox VLAN + one switch VLAN per run (removed at the end),
# one change request in the PDI. S4b never passes silently: a sleeping PDI prints HIBERNATED.
set -uo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "missing .env"; exit 1; }
set -a; . ./.env; set +a
: "${NETBOX_URL:?}" "${NETBOX_TOKEN:?}" "${AUTOMATION_PASSWORD:?}" "${ITENTIAL_ADMIN_PASSWORD:?}"
PY=.venv/bin/python
V=itential/versions.yaml
IT_IP=$(${PY} -c "import yaml;print(yaml.safe_load(open('$V'))['vm']['ip'])")
IT_HOST=itential.lab.internal
MCP_HOST=mcp.lab.internal
PLATFORM="https://${IT_HOST}"
CA=docs/lab-root-ca.crt
SSH="ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new"
ts=$(date -u +%Y%m%dT%H%M%SZ)
fail=0; pass=0; deferred=0; hibernated=0
ok()    { echo "PASS  $1"; pass=$((pass+1)); }
bad()   { echo "FAIL  $1"; fail=$((fail+1)); }
defer() { echo "DEFER $1"; deferred=$((deferred+1)); }
check() { local name=$1; shift; if "$@" >/tmp/verify05.$$ 2>&1; then ok "$name"; else bad "$name"; sed 's/^/      /' /tmp/verify05.$$ | head -12; fi; }
nb()    { curl -s -m 20 -H "Authorization: Token ${NETBOX_TOKEN}" "$@"; }
JAR=$(mktemp); trap 'rm -f "$JAR" /tmp/verify05.$$' EXIT
# Every call to the Platform goes through the lab CA and the real name: no -k anywhere.
iap()   { curl -s -m 60 --cacert "$CA" --resolve "${IT_HOST}:443:${IT_IP}" -b "$JAR" -H "Content-Type: application/json" "$@"; }
iap_login() { iap -c "$JAR" -X POST "${PLATFORM}/login" -d "{\"username\":\"admin\",\"password\":\"${ITENTIAL_ADMIN_PASSWORD}\"}" -o /dev/null -w '%{http_code}' | grep -qx 200; }
# run_job <workflow> <variables-json> -> prints the job id; waits for a terminal status unless $3=nowait
run_job() {
  local wf=$1 vars=$2 mode=${3:-wait} id status
  id=$(iap -X POST "${PLATFORM}/operations-manager/jobs/start" -d "{\"workflow\":\"${wf}\",\"options\":{\"type\":\"automation\",\"description\":\"verify ${ts}\",\"variables\":${vars}}}" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);print(d.get("data",{}).get("_id") or d.get("_id") or "")')
  [ -n "$id" ] || { echo "job start failed for ${wf}"; return 1; }
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
iap_login || { bad "S4.1 login to ${PLATFORM} as admin through the lab CA"; echo; echo "passed=0 failed=12"; exit 1; }

# --- S4.1 TLS from the lab CA on 10.100.0.65; version equals the pin; both gateways registered ---
c1() {
  local san; san=$(openssl s_client -connect "${IT_IP}:443" -servername "$IT_HOST" -CAfile "$CA" </dev/null 2>/dev/null | openssl x509 -noout -ext subjectAltName 2>/dev/null)
  echo "$san" | grep -q "DNS:${IT_HOST}" || { echo "SAN missing ${IT_HOST}: ${san}"; return 1; }
  openssl s_client -connect "${IT_IP}:443" -servername "$IT_HOST" -CAfile "$CA" </dev/null 2>/dev/null | grep -q "Verify return code: 0" || { echo "chain does not verify against ${CA}"; return 1; }
  local ver; ver=$(iap "${PLATFORM}/health/server" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);print(d.get("release") or d.get("version") or d)')
  [ "$ver" = "$PLATFORM_VER" ] || { echo "platform reports ${ver}, versions.yaml pins ${PLATFORM_VER}"; return 1; }
  local conns; conns=$(iap "${PLATFORM}/gateway_manager/v1/connections")
  echo "$conns" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);c=d.get("data",d);assert any(v for v in c.values()),"no gateway5 connections"' || { echo "$conns" | head -c 300; return 1; }
  # Gateway 4 is reached through the built-in IAG adapter; it must be running.
  iap "${PLATFORM}/health/adapters" | ${PY} -c 'import sys,json;a=json.load(sys.stdin);a=a.get("results",a);r=[x for x in a if "gateway4" in (x.get("id") or x.get("_id") or "").lower() or "iag" in (x.get("id") or x.get("_id") or "").lower()];assert r,"no IAG4 adapter";assert all(x.get("state")=="RUNNING" for x in r),r' || return 1
}
check "S4.1 ${PLATFORM} serves a lab-CA cert for ${IT_HOST}, runs Platform ${PLATFORM_VER}, Gateway 5 connected, Gateway 4 adapter RUNNING" c1

# --- S4.2 NetBox adapter: workflow device count == GET /api/dcim/devices/ --------------------------
c2() {
  local want got id; want=$(nb "${NETBOX_URL}/api/dcim/devices/?limit=1" | ${PY} -c 'import sys,json;print(json.load(sys.stdin)["count"])')
  id=$(run_job wf-netbox-device-count-v1 '{}') || { echo "$id"; return 1; }
  got=$(job_vars "${id##*$'\n'}" | ${PY} -c 'import sys,json;print(json.load(sys.stdin).get("device_count"))')
  [ "$got" = "$want" ] || { echo "workflow ${got} vs NetBox API ${want}"; return 1; }
  echo "device_count=${got}"
}
check "S4.2 wf-netbox-device-count-v1 through the NetBox adapter equals GET /api/dcim/devices/" c2

# --- S4.3 show version through IAG on one node per vendor equals the manifest and the device itself ---
c3() {
  local errs="" dev="${PY} verify/devcmd.py" id out direct
  # (device, expected version string from ADR 0032/0033, direct command)
  for row in "br1-wan01:10.100.0.146:17.13.01a:show version" "br1-sw01:10.100.0.165:4.33.1.1F:show version"; do
    IFS=: read -r name ip want cmd <<<"$row"
    id=$(run_job wf-show-version-v1 "{\"device\":\"${name}\"}") || { errs+="${name}: ${id}\n"; continue; }
    out=$(job_vars "${id##*$'\n'}" | ${PY} -c 'import sys,json;print(json.load(sys.stdin).get("output",""))')
    echo "$out" | grep -q "$want" || { errs+="${name}: IAG output lacks ${want}\n"; continue; }
    direct=$($dev "$ip" "$cmd" 2>/dev/null) || { errs+="${name}: direct ssh failed\n"; continue; }
    echo "$direct" | grep -q "$want" || errs+="${name}: device itself lacks ${want}\n"
  done
  [ -z "$errs" ] || { printf "%b" "$errs"; return 1; }
}
check "S4.3 wf-show-version-v1 via IAG: br1-wan01 = 17.13.01a, br1-sw01 = 4.33.1.1F, cross-checked over direct SSH" c3

# --- S4.4 wf-branch-vlan-v1: reserve in NetBox + configure br1-sw01, approval task, idempotent, rollback ---
VLAN_NAME="verify-${ts,,}"
approve_pending_task() {
  # the workflow parks on a manual task; claim and finish it as admin (that is the approval)
  local job=$1 task
  for _ in $(seq 1 24); do
    task=$(iap "${PLATFORM}/operations-manager/jobs/${job}" | ${PY} -c 'import sys,json;d=json.load(sys.stdin)["data"];t=[k for k,v in d.get("tasks",{}).items() if v.get("type")=="manual" and v.get("status") in ("running","paused","incomplete")];print(t[0] if t else "")')
    [ -n "$task" ] && break; sleep 5
  done
  [ -n "$task" ] || { echo "no pending manual task on job ${job}"; return 1; }
  iap -X POST "${PLATFORM}/workflow_engine/tasks/${task}/claim" -d "{\"jobId\":\"${job}\"}" -o /dev/null
  iap -X POST "${PLATFORM}/workflow_engine/tasks/${task}/finish" -d "{\"jobId\":\"${job}\",\"variables\":{\"approved\":true}}" -o /dev/null
}
wait_job() { local id=$1 s; for _ in $(seq 1 60); do s=$(job_status "$id"); case "$s" in complete) return 0;; error|canceled|cancelled) echo "job ${id} ${s}"; return 1;; esac; sleep 5; done; echo "job ${id} timeout (${s})"; return 1; }
nb_vlan() { nb "${NETBOX_URL}/api/ipam/vlans/?site=br1&name=${VLAN_NAME}"; }
c4() {
  local id vid
  id=$(run_job wf-branch-vlan-v1 "{\"branch\":\"br1\",\"vlan_name\":\"${VLAN_NAME}\"}" nowait) || { echo "$id"; return 1; }
  approve_pending_task "$id" || return 1
  wait_job "$id" || return 1
  vid=$(nb_vlan | ${PY} -c 'import sys,json;d=json.load(sys.stdin);assert d["count"]==1,d["count"];v=d["results"][0];assert v["status"]["value"]=="active",v["status"];print(v["vid"])') || { echo "NetBox VLAN ${VLAN_NAME} not active in br1"; return 1; }
  ${PY} verify/devcmd.py 10.100.0.165 "show vlan ${vid}" | grep -q "$VLAN_NAME" || { echo "br1-sw01 has no VLAN ${vid} ${VLAN_NAME}"; return 1; }
  echo "run 1: VLAN ${vid} ${VLAN_NAME} in NetBox and on br1-sw01"
  # run 2 must be a no-op: same VID, still exactly one NetBox object, job reports no change
  id=$(run_job wf-branch-vlan-v1 "{\"branch\":\"br1\",\"vlan_name\":\"${VLAN_NAME}\"}" nowait) || { echo "$id"; return 1; }
  approve_pending_task "$id" || return 1
  wait_job "$id" || return 1
  job_vars "$id" | ${PY} -c 'import sys,json;v=json.load(sys.stdin);assert v.get("changed") is False, v' || { echo "run 2 was not a no-op"; return 1; }
  nb_vlan | ${PY} -c 'import sys,json;d=json.load(sys.stdin);assert d["count"]==1 and d["results"][0]["vid"]=='"$vid"',d' || { echo "run 2 changed NetBox"; return 1; }
  echo "run 2: no-op"
  # rollback: an unreachable switch makes the device step fail and the NetBox reservation must be removed
  id=$(run_job wf-branch-vlan-v1 "{\"branch\":\"br1\",\"vlan_name\":\"${VLAN_NAME}-rb\",\"switch_override\":\"10.100.0.199\"}" nowait) || { echo "$id"; return 1; }
  approve_pending_task "$id" || return 1
  wait_job "$id" >/dev/null 2>&1 && { echo "rollback run unexpectedly succeeded"; return 1; }
  nb "${NETBOX_URL}/api/ipam/vlans/?site=br1&name=${VLAN_NAME}-rb" | ${PY} -c 'import sys,json;assert json.load(sys.stdin)["count"]==0,"reservation survived the failure"' || return 1
  echo "run 3: device failure rolled the NetBox reservation back"
}
check "S4.4 wf-branch-vlan-v1: reserve + configure with approval; second run no-op; NetBox rollback on device failure" c4
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
  up=$($SSH "ubuntu@${IT_IP}" "cut -d. -f1 /proc/uptime") || { echo "ssh to ${IT_IP} failed"; return 1; }
  read -r total used <<<"$($SSH "ubuntu@${IT_IP}" "free -m | awk '/^Mem:/{print \$2, \$3}'")"
  pct=$((used * 100 / total))
  cache=$($SSH "ubuntu@${IT_IP}" "sudo docker exec mongodb mongosh --quiet --eval 'JSON.stringify(db.serverStatus().wiredTiger.cache)' 2>/dev/null" | ${PY} -c 'import sys,json;c=json.load(sys.stdin);print(round(int(c["bytes currently in the cache"])/2**20), "MB in cache of", round(int(c["maximum bytes configured"])/2**20), "MB max")' 2>/dev/null || echo "wiredTiger cache: n/a")
  echo "uptime ${up}s; RAM used ${used}/${total} MB (${pct}%); wiredTiger ${cache}"
  [ "$pct" -lt 80 ] || { echo "memory above 80%: apply the budget levers by PR"; return 1; }
  [ "$up" -ge 86400 ] || { echo "MEASURE-EARLY: uptime under 24 h, re-run after $(( (86400 - up) / 3600 )) h"; return 2; }
}
if c6 >/tmp/verify05.$$ 2>&1; then ok "S4.6 itential VM memory under 80 % after 24 h"; sed 's/^/      /' /tmp/verify05.$$
elif [ $? = 2 ]; then defer "S4.6 memory under 80 % now, but uptime is under 24 h"; sed 's/^/      /' /tmp/verify05.$$
else bad "S4.6 itential VM memory pressure"; sed 's/^/      /' /tmp/verify05.$$; fi

# --- S4.7 Claude Code on this Mac reaches mcp.lab.internal and runs a read-only tool -------------
c7() {
  local url="http://${MCP_HOST}:8000/mcp" tools res
  ${PY} -c "import socket;assert socket.gethostbyname('${MCP_HOST}')=='${IT_IP}','${MCP_HOST} resolves elsewhere'" || return 1
  tools=$(${PY} verify/mcpcall.py "$url" tools) || { echo "$tools"; return 1; }
  echo "$tools" | grep -qx get_health || { echo "get_health not in tools: $(echo "$tools" | tr '\n' ' ')"; return 1; }
  res=$(${PY} verify/mcpcall.py "$url" call get_health) || { echo "$res"; return 1; }
  echo "$res" | grep -q "$PLATFORM_VER" || { echo "get_health did not return platform ${PLATFORM_VER}: $(echo "$res" | head -c 300)"; return 1; }
  echo "$(echo "$tools" | wc -l | tr -d ' ') tools; get_health reports ${PLATFORM_VER}"
  ${PY} -c 'import json;c=json.load(open(".mcp.json"))["mcpServers"]["itential"];assert c["url"]=="'"$url"'",c'
}
check "S4.7 mcp.lab.internal answers streamable HTTP from this Mac; get_health returns Platform ${PLATFORM_VER}; .mcp.json matches" c7

# =============================== S4b ServiceNow PDI ==============================================
snow() { curl -s -m 30 -u "${SNOW_USER}:${SNOW_PASSWORD}" -H "Accept: application/json" "$@"; }
if [ -z "${SNOW_INSTANCE:-}" ] || [ -z "${SNOW_USER:-}" ] || [ -z "${SNOW_PASSWORD:-}" ]; then
  for c in "S4b.1 adapter-servicenow health" "S4b.2 change request lifecycle" "S4b.3 update set in servicenow/" "S4b.4 instance recorded" "S4b.5 interactive login age"; do bad "$c: SNOW_INSTANCE/SNOW_USER/SNOW_PASSWORD missing from .env"; done
else
  SNOW_URL="https://${SNOW_INSTANCE}.service-now.com"
  code=$(snow -o /dev/null -w '%{http_code}' "${SNOW_URL}/api/now/table/sys_properties?sysparm_limit=1")
  if [ "$code" != 200 ]; then
    echo "HIBERNATED  PDI ${SNOW_INSTANCE} answered HTTP ${code}: wake it at developer.servicenow.com and re-run (S4b.1-S4b.5 not evaluated, not passed)"
    hibernated=5
  else
    # --- S4b.1 adapter health green ---
    c8() { iap "${PLATFORM}/health/adapters" | ${PY} -c 'import sys,json;a=json.load(sys.stdin);a=a.get("results",a);r=[x for x in a if "servicenow" in (x.get("id") or x.get("_id") or "").lower()];assert r,"no servicenow adapter";assert all(x.get("state")=="RUNNING" for x in r),r'; }
    check "S4b.1 adapter-servicenow RUNNING in /health/adapters" c8
    # --- S4b.2 change request opened, work-noted with the NetBox reservation, closed ---
    c9() {
      local id chg sid
      id=$(run_job wf-branch-vlan-v1 "{\"branch\":\"br2\",\"vlan_name\":\"${VLAN_NAME}-snow\",\"change_request\":true}" nowait) || { echo "$id"; return 1; }
      approve_pending_task "$id" || return 1
      wait_job "$id" || return 1
      chg=$(job_vars "$id" | ${PY} -c 'import sys,json;print(json.load(sys.stdin).get("change_number",""))')
      [ -n "$chg" ] || { echo "workflow returned no change_number"; return 1; }
      sid=$(snow "${SNOW_URL}/api/now/table/change_request?sysparm_query=number=${chg}&sysparm_fields=sys_id,state" | ${PY} -c 'import sys,json;r=json.load(sys.stdin)["result"];assert r and r[0]["state"] in ("3","closed","Closed"),r;print(r[0]["sys_id"])') || { echo "change ${chg} not closed"; return 1; }
      snow "${SNOW_URL}/api/now/table/sys_journal_field?sysparm_query=element_id=${sid}^element=work_notes&sysparm_fields=value" | ${PY} -c 'import sys,json;r=json.load(sys.stdin)["result"];assert any("netbox" in x["value"].lower() for x in r),r' || { echo "no NetBox work note on ${chg}"; return 1; }
      snow "${SNOW_URL}/api/now/table/sys_audit?sysparm_query=documentkey=${sid}^fieldname=state&sysparm_fields=newvalue" | ${PY} -c 'import sys,json;r=json.load(sys.stdin)["result"];assert len(r)>=3,f"{len(r)} state transitions"' || { echo "fewer than 3 state transitions on ${chg}"; return 1; }
      echo "change ${chg}: opened, work-noted, closed"
      # cleanup of the br2 verify VLAN
      local vinfo; vinfo=$(nb "${NETBOX_URL}/api/ipam/vlans/?site=br2&name=${VLAN_NAME}-snow" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);print(d["results"][0]["id"],d["results"][0]["vid"]) if d["count"] else print("")')
      if [ -n "$vinfo" ]; then nb -X DELETE "${NETBOX_URL}/api/ipam/vlans/${vinfo%% *}/" -o /dev/null; ${PY} verify/devcmd.py 10.100.0.166 "configure
no vlan ${vinfo##* }
end" >/dev/null 2>&1 || true; fi
    }
    check "S4b.2 wf-branch-vlan-v1 with change_request=true opens, work-notes and closes a change (3 state transitions)" c9
    # --- S4b.3 update set exported ---
    c10() { ls servicenow/*.xml >/dev/null 2>&1 && ${PY} -c 'import glob,xml.etree.ElementTree as E;[E.parse(f) for f in glob.glob("servicenow/*.xml")];assert any("sys_remote_update_set" in open(f).read() for f in glob.glob("servicenow/*.xml"))'; }
    check "S4b.3 Itential update set exported to servicenow/ (well-formed, contains sys_remote_update_set)" c10
    # --- S4b.4 instance name + release family recorded, never the password ---
    c11() { grep -q "^SNOW_INSTANCE=${SNOW_INSTANCE}" .env && grep -q "\`${SNOW_INSTANCE}\`" docs/image-manifest.md && grep -Eq "Release family.*(Zurich|Australia)" docs/image-manifest.md && ! grep -rq "${SNOW_PASSWORD}" docs itential servicenow ansible; }
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
