#!/usr/bin/env bash
# Phase 6b verification: Platform coverage of the EVE-NG lab (PID S4d criteria 1-4 and 6; S4d.5 is in
# test-06-flowai.sh). Intent: itential/versions.yaml (golden_config), itential/golden-config/, NetBox.
# State: the Platform API over TLS (Configuration Manager, Operations Manager) and the devices over
# direct SSH (verify/devcmd.py, second source). Writes: a hostname change on one device per vendor
# through wf-config-push-v1 (approved here through the API, restored at the end). Each element is
# added as it is built; a criterion that is not built yet prints DEFER, never PASS.
set -uo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "missing .env"; exit 1; }
set -a; . ./.env; set +a
: "${NETBOX_URL:?}" "${NETBOX_TOKEN:?}" "${AUTOMATION_PASSWORD:?}" "${ITENTIAL_ADMIN_PASSWORD:?}"
ADMIN_USER=${ITENTIAL_ADMIN_USER:-admin@itential}
PY=.venv/bin/python
V=itential/versions.yaml
IT_IP=$(${PY} -c "import yaml;print(yaml.safe_load(open('$V'))['vm']['ip'])")
IT_HOST=itential.lab.internal
PLATFORM="https://${IT_HOST}"
CA=docs/lab-root-ca.crt
ts=$(date -u +%Y%m%dT%H%M%SZ); ts_lc=$(echo "$ts" | tr "A-Z" "a-z")
fail=0; pass=0; deferred=0
ok()    { echo "PASS  $1"; pass=$((pass+1)); }
bad()   { echo "FAIL  $1"; fail=$((fail+1)); }
defer() { echo "DEFER $1"; deferred=$((deferred+1)); }
# ONLY="S4d.1" runs a subset while iterating
check() { local name=$1; shift; if [ -n "${ONLY:-}" ] && ! echo " ${ONLY} " | grep -q " ${name%% *} "; then echo "SKIP  $name"; return; fi; if "$@" >/tmp/verify06b.$$ 2>&1; then ok "$name"; sed 's/^/      /' /tmp/verify06b.$$; else bad "$name"; sed 's/^/      /' /tmp/verify06b.$$ | head -20; fi; }
nb()    { curl -s -m 20 -H "Authorization: Token ${NETBOX_TOKEN}" "$@"; }
JAR=$(mktemp); trap 'rm -f "$JAR" /tmp/verify06b.$$' EXIT
iap()   { curl -s -m 180 --cacert "$CA" --resolve "${IT_HOST}:443:${IT_IP}" -b "$JAR" -H "Content-Type: application/json" "$@"; }
iap_login() { iap -c "$JAR" -X POST "${PLATFORM}/login" -d "{\"username\":\"${ADMIN_USER}\",\"password\":\"${ITENTIAL_ADMIN_PASSWORD}\"}" -o /dev/null -w '%{http_code}' | grep -qx 200; }
mkdir -p verify/results; exec > >(tee "verify/results/${ts}-06b-platform.log") 2>&1
echo "# test-06b-platform ${ts}"
[ -s "$CA" ] || { bad "$CA missing"; echo; echo "passed=0 failed=1"; exit 1; }
iap_login || { bad "login to ${PLATFORM} as ${ADMIN_USER}"; echo; echo "passed=0 failed=1"; exit 1; }

# --- shared helpers: jobs, approvals, compliance plan runs -----------------------------------------
# start_job <workflow> <variables json> -> job id (no wait)
start_job() { iap -X POST "${PLATFORM}/operations-manager/jobs/start" -d "{\"workflow\":\"$1\",\"options\":{\"type\":\"automation\",\"description\":\"verify ${ts}\",\"variables\":$2}}" | ${PY} -c 'import sys,json;d=json.load(sys.stdin).get("data");print(d.get("_id","") if isinstance(d,dict) else "")'; }
job_status() { iap "${PLATFORM}/operations-manager/jobs/$1" | ${PY} -c 'import sys,json;print(json.load(sys.stdin)["data"]["status"])'; }
wait_job() { local st; for _ in $(seq 1 48); do st=$(job_status "$1"); case "$st" in complete) return 0;; error|canceled|cancelled) echo "job $1 ${st}"; return 1;; esac; sleep 5; done; echo "job $1 timeout (${st})"; return 1; }
# approve_task <job id> <task id>: the Work Center approval (ViewData) finished through the API, as test-06 does
approve_task() { local i; for i in $(seq 1 24); do if iap -X POST "${PLATFORM}/operations-manager/jobs/$1/tasks/$2/finish" -d '{"taskData":{"finish_state":"success","variables":{}}}' -o /dev/null -w '%{http_code}' | grep -qx 200; then return 0; fi; sleep 5; done; echo "approval of $1/$2 never accepted"; return 1; }
# push <device> <config lines> <reason>: wf-config-push-v1 with the approval; returns when the job completes
push() { local id; id=$(start_job wf-config-push-v1 "{\"device\":\"$1\",\"config\":\"$2\",\"reason\":\"$3\"}"); [ -n "$id" ] || { echo "wf-config-push-v1 did not start"; return 1; }; sleep 8; approve_task "$id" 2a || return 1; wait_job "$id" || return 1; echo "pushed to $1 (job ${id})"; }
plan_id() { iap -X POST "${PLATFORM}/configuration_manager/search/compliance_plans" -d '{"name":"","options":{"start":0,"limit":100}}' | ${PY} -c "import sys,json;d=json.load(sys.stdin);print(next((x['id'] for x in d.get('plans',[]) if x.get('name')=='$1'),''))"; }
# run_plan <plan id> -> prints the batch id after the instance completes
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
# batch_issues <batch id> -> one line per report: device errors warnings passes | issue lines
batch_issues() { iap "${PLATFORM}/configuration_manager/compliance_reports/batch/$1" | ${PY} -c '
import sys,json
for r in json.load(sys.stdin):
    t=r.get("totals",{}); iss=[" ".join(w["value"] for w in i["spec"]["words"]) for i in r.get("issues",[])]
    print(r["deviceName"], t.get("errors",0), t.get("warnings",0), t.get("passes",0), "|", "; ".join(iss))'; }
running_hostname() { ${PY} verify/devcmd.py "$1" "show running-config | include ^hostname" 2>/dev/null | awk '/^hostname/{print $2}'; }

GC_TREES=$(${PY} -c "import yaml;g=yaml.safe_load(open('$V'))['golden_config'];print(' '.join(t['name'] for t in g['trees'].values()))")
PLAN_NAME=$(${PY} -c "import yaml;print(yaml.safe_load(open('$V'))['golden_config']['plan'])")
SCHED_NAME=$(${PY} -c "import yaml;print(yaml.safe_load(open('$V'))['golden_config']['schedule']['name'])")

# --- S4d.1 Golden Config: clean on 12; hostname drift on one device per vendor flagged, nothing else; restored ---
c1() {
  local trees devs groups plan batch bad_lines ip name
  # intent: every active NetBox network device; state: trees, groups, plan, devices attached
  local nbdevs=() row  # bash 3.2 on macOS: no mapfile
  while IFS= read -r row; do nbdevs+=("$row"); done < <(nb "${NETBOX_URL}/api/dcim/devices/?limit=0&status=active&has_primary_ip=true&platform=ios-xe&platform=eos" | ${PY} -c 'import sys,json;[print(d["name"],d["primary_ip4"]["address"].split("/")[0],d["site"]["slug"],d["role"]["slug"]) for d in json.load(sys.stdin)["results"]]')
  [ "${#nbdevs[@]}" = 12 ] || { echo "NetBox has ${#nbdevs[@]} network devices, wanted 12"; return 1; }
  # ADR 0041: every running hostname equals its NetBox name before the drift test starts
  bad_lines=""
  for row in "${nbdevs[@]}"; do read -r name ip _ <<<"$row"; [ "$(running_hostname "$ip")" = "$name" ] || bad_lines+="${name} runs as '$(running_hostname "$ip")' "; done
  [ -z "$bad_lines" ] || { echo "hostname drift before the test: ${bad_lines}"; return 1; }
  trees=$(iap "${PLATFORM}/configuration_manager/configs")
  for t in $GC_TREES; do echo "$trees" | grep -q "\"name\":\"${t}\"" || { echo "tree ${t} missing"; return 1; }; done
  devs=$(for t in $GC_TREES; do tid=$(echo "$trees" | ${PY} -c "import sys,json;print(next(x['id'] for x in json.load(sys.stdin) if x['name']=='${t}'))"); iap -X POST "${PLATFORM}/configuration_manager/devices/tree" -d "{\"treeId\":\"${tid}\",\"version\":\"initial\"}" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);d=d if isinstance(d,list) else d.get("devices") or d.get("list") or [];[print(x if isinstance(x,str) else x.get("name") or x.get("device")) for x in d]'; done | sort -u)
  [ "$(echo "$devs" | grep -c .)" = 12 ] || { echo "devices attached to the trees: $(echo "$devs" | tr '\n' ' ')"; return 1; }
  groups=$(iap "${PLATFORM}/configuration_manager/deviceGroups" | ${PY} -c 'import sys,json;print(" ".join(sorted(g["name"]+":"+str(len(g.get("devices",[]))) for g in json.load(sys.stdin))))')
  for g in site-dc1:7 site-br1:2 site-br2:2 site-wan:1 role-wan-edge:4 role-isp-core:1 role-spine:2 role-leaf:2 role-access:1 role-branch-switch:2; do echo " $groups " | grep -q " $g " || { echo "device group ${g%%:*} wrong: ${groups}"; return 1; }; done
  plan=$(plan_id "$PLAN_NAME"); [ -n "$plan" ] || { echo "compliance plan ${PLAN_NAME} missing"; return 1; }
  iap "${PLATFORM}/configuration_manager/compliance_plans/${plan}" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);n=d.get("nodes",[]);assert len(n)==12,("plan nodes",len(n));assert sorted(x for m in n for x in m["devices"])==sorted(sys.argv[1:]),n' ${nbdevs[@]/ */} || return 1
  iap "${PLATFORM}/operations-manager/triggers?equalsField=type&equals=schedule&limit=50" | ${PY} -c "import sys,json;d=json.load(sys.stdin)['data'];t=[x for x in d if x['name']=='${SCHED_NAME}'];assert t and t[0]['enabled'],('schedule trigger',[x['name'] for x in d]);print('schedule:',t[0]['name'],'every',t[0].get('repeatFrequency'),t[0].get('repeatUnit'),'first run',t[0].get('firstRunAt'))" || return 1
  # 1) clean run on all 12
  batch=$(run_plan "$plan") || { echo "$batch"; return 1; }
  batch_issues "$batch" > /tmp/verify06b.issues.$$; local n_reports; n_reports=$(grep -c . /tmp/verify06b.issues.$$)
  [ "$n_reports" = 12 ] || { echo "run 1: ${n_reports} reports, wanted 12"; cat /tmp/verify06b.issues.$$; return 1; }
  awk '$2+$3>0' /tmp/verify06b.issues.$$ | grep -q . && { echo "run 1 not clean:"; awk '$2+$3>0' /tmp/verify06b.issues.$$; return 1; }
  echo "run 1: 12 reports, 0 errors, 0 warnings (passes: $(awk '{s+=$4} END{print s}' /tmp/verify06b.issues.$$))"
  # 2) deliberate drift on one device per vendor through the governed push
  push br1-wan01 "hostname verify-${ts_lc}" "verify ${ts} S4d.1 deliberate drift" || return 1
  push br1-sw01 "hostname verify-${ts_lc}" "verify ${ts} S4d.1 deliberate drift" || return 1
  [ "$(running_hostname 10.100.0.146)" = "verify-${ts_lc}" ] && [ "$(running_hostname 10.100.0.165)" = "verify-${ts_lc}" ] || { echo "drift did not land on the devices (direct SSH)"; return 1; }
  batch=$(run_plan "$plan") || { echo "$batch"; return 1; }
  batch_issues "$batch" > /tmp/verify06b.issues.$$
  local flagged; flagged=$(awk '$2+$3>0{print $1}' /tmp/verify06b.issues.$$ | sort | tr '\n' ' ')
  [ "$flagged" = "br1-sw01 br1-wan01 " ] || { echo "run 2 flagged '${flagged}', wanted exactly br1-sw01 br1-wan01"; cat /tmp/verify06b.issues.$$; return 1; }
  grep -E '^(br1-wan01|br1-sw01) ' /tmp/verify06b.issues.$$ | grep -q "hostname" || { echo "run 2 issues do not name the hostname line"; cat /tmp/verify06b.issues.$$; return 1; }
  echo "run 2: exactly br1-wan01 and br1-sw01 flagged:"; grep -E '^(br1-wan01|br1-sw01) ' /tmp/verify06b.issues.$$
  # 3) restore through the same path and prove clean again
  push br1-wan01 "hostname br1-wan01" "verify ${ts} S4d.1 restore" || return 1
  push br1-sw01 "hostname br1-sw01" "verify ${ts} S4d.1 restore" || return 1
  [ "$(running_hostname 10.100.0.146)" = br1-wan01 ] && [ "$(running_hostname 10.100.0.165)" = br1-sw01 ] || { echo "restore did not land (direct SSH)"; return 1; }
  batch=$(run_plan "$plan") || { echo "$batch"; return 1; }
  batch_issues "$batch" > /tmp/verify06b.issues.$$
  awk '$2+$3>0' /tmp/verify06b.issues.$$ | grep -q . && { echo "run 3 not clean:"; awk '$2+$3>0' /tmp/verify06b.issues.$$; return 1; }
  echo "run 3: clean again on 12 devices; hostnames restored and confirmed over direct SSH"
  # the nightly path: wf-compliance-run-v1 (what the schedule trigger starts) finds the plan and starts a run
  local job inst
  job=$(start_job wf-compliance-run-v1 "{}"); [ -n "$job" ] || { echo "wf-compliance-run-v1 did not start"; return 1; }
  wait_job "$job" || return 1
  inst=$(iap "${PLATFORM}/operations-manager/jobs/${job}" | ${PY} -c 'import sys,json;v=json.load(sys.stdin)["data"].get("variables",{});assert v.get("plan_id")=="'"$plan"'",v.get("plan_id");print((v.get("run") or {}).get("instanceId",""))') || { echo "wf-compliance-run-v1 did not resolve the plan id ${plan}"; return 1; }
  [ -n "$inst" ] || { echo "wf-compliance-run-v1 started no plan instance"; return 1; }
  echo "run 4: wf-compliance-run-v1 (the schedule trigger's target) found plan ${plan} and started instance ${inst}"
  rm -f /tmp/verify06b.issues.$$
}
check "S4d.1 Golden Config: plan clean on 12 devices; hostname drift on br1-wan01 and br1-sw01 flagged with no false positive; restored through wf-config-push-v1" c1
# restore hostnames if the drift step left them behind (a failed run must not leave the lab drifted)
for row in "br1-wan01 10.100.0.146" "br1-sw01 10.100.0.165"; do read -r name ip <<<"$row"; [ "$(running_hostname "$ip")" = "$name" ] || { echo "restoring ${name} after a failed run"; push "$name" "hostname ${name}" "verify ${ts} cleanup" >/dev/null 2>&1 || echo "WARN ${name} still drifted; fix with wf-config-push-v1"; }; done

defer "S4d.2 command templates + nightly backups (element 2, not built yet)"
defer "S4d.3 Lifecycle Manager + JSON Forms (element 3, not built yet)"
defer "S4d.4 Integration Models (element 4, not built yet)"
defer "S4d.6 hosts and firewalls (element 6, not built yet)"

echo
echo "passed=${pass} failed=${fail} deferred=${deferred}"
[ "$fail" = 0 ]
