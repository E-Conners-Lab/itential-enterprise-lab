#!/usr/bin/env bash
# Phase 6b verification: Platform coverage of the EVE-NG lab (PID S4d criteria 1-4 and 6; S4d.5 is in
# test-06-flowai.sh). Intent: itential/versions.yaml (golden_config, mop, lcm, forms), itential/golden-config/,
# itential/command-templates/, itential/lcm/, itential/forms/, NetBox.
# State: the Platform API over TLS (Configuration Manager, Operations Manager) and the devices over
# direct SSH (verify/devcmd.py, second source). Writes: a hostname change on one device per vendor
# through Push Configuration with Approval (approved here through the API, restored at the end) and one branch VLAN on br2
# created and removed through the Lifecycle Manager actions (S4d.3). S4d.4 runs two lab-netops sessions (Anthropic tokens,
# printed). Each element is added as it is built; a criterion that is not built yet prints DEFER, never PASS.
set -uo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "missing .env"; exit 1; }
set -a; . ./.env; set +a
: "${NETBOX_URL:?}" "${NETBOX_TOKEN:?}" "${AUTOMATION_PASSWORD:?}" "${ITENTIAL_ADMIN_PASSWORD:?}" "${SNOW_INSTANCE:?}" "${SNOW_USER:?}" "${SNOW_PASSWORD:?}"
ADMIN_USER=${ITENTIAL_ADMIN_USER:-admin@itential}
PY=.venv/bin/python
V=itential/versions.yaml
# The S11 cut-over (ADR 0053/0055) moved itential.lab.internal onto the load balancer, so the name is the
# address: no --resolve is forced any more and these run against whatever the record points at. IT_IP (and
# IT_MCP_IP, for the MCP server on its own VM) still pin a specific host when one is being proved directly.
IT_IP=${IT_IP:-}
RESOLVE=${IT_IP:+--resolve itential.lab.internal:443:${IT_IP}}
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
iap()   { curl -s -m 180 --cacert "$CA" ${RESOLVE} -b "$JAR" -H "Content-Type: application/json" "$@"; }
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
# push <device> <config lines> <reason>: Push Configuration with Approval with the approval; returns when the job completes
push() { local id; id=$(start_job "Push Configuration with Approval" "{\"device\":\"$1\",\"config\":\"$2\",\"reason\":\"$3\"}"); [ -n "$id" ] || { echo "Push Configuration with Approval did not start"; return 1; }; sleep 8; approve_task "$id" 2a || return 1; wait_job "$id" || return 1; echo "pushed to $1 (job ${id})"; }
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
  # the nightly path: Run Nightly Compliance Check (what the schedule trigger starts) finds the plan and starts a run
  local job inst
  job=$(start_job "Run Nightly Compliance Check" "{}"); [ -n "$job" ] || { echo "Run Nightly Compliance Check did not start"; return 1; }
  wait_job "$job" || return 1
  inst=$(iap "${PLATFORM}/operations-manager/jobs/${job}" | ${PY} -c 'import sys,json;v=json.load(sys.stdin)["data"].get("variables",{});assert v.get("plan_id")=="'"$plan"'",v.get("plan_id");print((v.get("run") or {}).get("instanceId",""))') || { echo "Run Nightly Compliance Check did not resolve the plan id ${plan}"; return 1; }
  [ -n "$inst" ] || { echo "Run Nightly Compliance Check started no plan instance"; return 1; }
  echo "run 4: Run Nightly Compliance Check (the schedule trigger's target) found plan ${plan} and started instance ${inst}"
  rm -f /tmp/verify06b.issues.$$
}
check "S4d.1 Golden Config: plan clean on 12 devices; hostname drift on br1-wan01 and br1-sw01 flagged with no false positive; restored through Push Configuration with Approval" c1
# restore hostnames if the drift step left them behind (a failed run must not leave the lab drifted)
for row in "br1-wan01 10.100.0.146" "br1-sw01 10.100.0.165"; do read -r name ip <<<"$row"; [ "$(running_hostname "$ip")" = "$name" ] || { echo "restoring ${name} after a failed run"; push "$name" "hostname ${name}" "verify ${ts} cleanup" >/dev/null 2>&1 || echo "WARN ${name} still drifted; fix with Push Configuration with Approval"; }; done

# --- S4d.2 command templates (MOP) on one device per vendor; analytic pre/post; nightly backups = running config ---
MOP_NAMES=$(${PY} -c "import yaml;m=yaml.safe_load(open('$V'))['mop'];print(' '.join(v['command']+':'+v['analytic'] for v in m['templates'].values()))")
BACKUP_SCHED=$(${PY} -c "import yaml;print(yaml.safe_load(open('$V'))['mop']['backup_schedule']['name'])")
# normalise a configuration for comparison: no comment/header/timestamp lines, no trailing spaces
norm_cfg() { ${PY} -c '
import sys,re
skip=re.compile(r"^(!|> |Building configuration|Current configuration|% |\s*$)")  # "> cmd" = the EOS exec-channel echo
print("\n".join(l.rstrip() for l in sys.stdin.read().replace("\r","").split("\n") if not skip.match(l)))'; }
c2() {
  local tl al pre post an ok rules
  tl=$(iap "${PLATFORM}/mop/listTemplates"); al=$(iap "${PLATFORM}/mop/listAnalyticTemplates")
  for pair in $MOP_NAMES; do
    echo "$tl" | grep -q "\"name\":\"${pair%%:*}\"" || { echo "command template ${pair%%:*} missing"; return 1; }
    echo "$al" | grep -q "\"name\":\"${pair##*:}\"" || { echo "analytic template ${pair##*:} missing"; return 1; }
  done
  for row in "br1-wan01:lab-cisco-ios-checks:lab-cisco-ios-prepost" "br1-sw01:lab-arista-eos-checks:lab-arista-eos-prepost"; do
    IFS=: read -r dev ct at <<<"$row"
    pre=$(iap -X POST "${PLATFORM}/mop/RunCommandTemplate" -d "{\"template\":\"${ct}\",\"variables\":{},\"devices\":[\"${dev}\"]}")
    rules=$(echo "$pre" | ${PY} -c '
import sys,json
d=json.load(sys.stdin); assert d.get("result") is True, ("template failed", [(c["evaluated"], [(r["rule"], r.get("result")) for r in c["rules"]]) for c in d.get("commands_results",[]) if not c.get("result")])
n=0
for c in d["commands_results"]:
    for r in c["rules"]:
        assert isinstance(r.get("result"), bool) and r.get("eval") != "missing_parameters", (c["evaluated"], r); n+=1
print(n, "rules over", len(d["commands_results"]), "commands")') || { echo "${dev} ${ct}: ${rules}"; return 1; }
    echo "${dev}: ${ct} passed, ${rules}"
    post=$(iap -X POST "${PLATFORM}/mop/RunCommandTemplate" -d "{\"template\":\"${ct}\",\"variables\":{},\"devices\":[\"${dev}\"]}")
    an=$(iap -X POST "${PLATFORM}/mop/runAnalyticsTemplate" -d "{\"pre\":${pre},\"post\":${post},\"analytic_template_name\":\"${at}\",\"variables\":{}}")
    echo "$an" | ${PY} -c '
import sys,json;d=json.load(sys.stdin);d=d.get("analytic_result",d)
rules=[(pp["preRawCommand"],r["preRegex"],r.get("pass")) for pp in d.get("prepostCommands",[]) for r in pp.get("rules",[])]
assert rules and all(p is True for _,_,p in rules), rules
assert d.get("result", True) is not False and d.get("pass", True) is not False, {k:d.get(k) for k in ("result","pass")}
print(len(rules),"pre/post rules equal")' || { echo "${dev} ${at}: $(echo "$an" | head -c 400)"; return 1; }
    echo "${dev}: ${at} pre/post equal"
  done
  iap "${PLATFORM}/operations-manager/triggers?equalsField=type&equals=schedule&limit=50" | ${PY} -c "import sys,json;d=json.load(sys.stdin)['data'];t=[x for x in d if x['name']=='${BACKUP_SCHED}'];assert t and t[0]['enabled'],('backup schedule trigger',[x['name'] for x in d]);print('schedule:',t[0]['name'],'every',t[0].get('repeatFrequency'),t[0].get('repeatUnit'),'first run',t[0].get('firstRunAt'))" || return 1
  # one run of the backup workflow, then every device's newest backup equals its running config over direct SSH
  local t0 job n_ok=0 errs="" name ip
  t0=$(date -u +%Y-%m-%dT%H:%M:%S)
  job=$(start_job "Back Up All Device Configs" "{}"); [ -n "$job" ] || { echo "Back Up All Device Configs did not start"; return 1; }
  wait_job "$job" || return 1
  local nbrows=() row
  while IFS= read -r row; do nbrows+=("$row"); done < <(nb "${NETBOX_URL}/api/dcim/devices/?limit=0&status=active&has_primary_ip=true&platform=ios-xe&platform=eos" | ${PY} -c 'import sys,json;[print(d["name"],d["primary_ip4"]["address"].split("/")[0]) for d in json.load(sys.stdin)["results"]]')
  for row in "${nbrows[@]}"; do read -r name ip <<<"$row"
    iap -X POST "${PLATFORM}/configuration_manager/backups" -d "{\"options\":{\"filter\":{\"name\":\"${name}\"},\"start\":\"0\",\"limit\":1,\"sort\":{\"date\":-1},\"regex\":false}}" \
      | ${PY} -c "import sys,json;d=json.load(sys.stdin);b=(d.get('list') or [None])[0];assert b and b['name']=='${name}',d;assert b['date']>='${t0}',('newest backup older than the run',b['date']);print(b['id'] if 'id' in b else b['_id'])" > /tmp/verify06b.bid.$$ 2>&1 || { errs+="${name}: $(cat /tmp/verify06b.bid.$$ | tail -1)\n"; continue; }
    iap "${PLATFORM}/configuration_manager/backups/$(cat /tmp/verify06b.bid.$$)" | ${PY} -c 'import sys,json;print(json.load(sys.stdin).get("rawConfig",""))' | norm_cfg > /tmp/verify06b.bk.$$
    ${PY} verify/devcmd.py "$ip" "show running-config" | norm_cfg > /tmp/verify06b.run.$$ || { errs+="${name}: direct ssh failed\n"; continue; }
    if diff -q /tmp/verify06b.bk.$$ /tmp/verify06b.run.$$ >/dev/null; then n_ok=$((n_ok+1)); else errs+="${name}: backup differs from the running config: $(diff /tmp/verify06b.bk.$$ /tmp/verify06b.run.$$ | grep -c '^[<>]') lines\n"; fi
  done
  rm -f /tmp/verify06b.bid.$$ /tmp/verify06b.bk.$$ /tmp/verify06b.run.$$
  [ -z "$errs" ] || { printf "%b" "$errs"; return 1; }
  echo "backups: Back Up All Device Configs job ${job}; ${n_ok}/${#nbrows[@]} devices' newest backup equals the running config over direct SSH"
}
check "S4d.2 command templates on br1-wan01 and br1-sw01 with every rule evaluated; analytic pre/post green; nightly backup schedule and a run whose backups equal the running configs" c2

# --- S4d.3 Lifecycle Manager + JSON Forms (ADR 0043/0044): model, form, instances from NetBox; create -> approval form (Work Center
# shows branch, VLAN, switch, NetBox reservation) -> instance; delete -> the governed push; history; no-op when absent ---
LCM_MODEL=$(${PY} -c "import yaml;print(yaml.safe_load(open('$V'))['lcm']['model'])")
FORM_NAME=$(${PY} -c "import yaml;print(yaml.safe_load(open('$V'))['forms']['approval'])")
LCM_ACTIONS=$(${PY} -c "import yaml;d=yaml.safe_load(open('itential/lcm/${LCM_MODEL}.yaml'));print(' '.join(a['type']+':'+a['_id'] for a in d['actions']))" 2>/dev/null)
# approve_form <job> <task>: the JSON form approval (ShowJsonForm) finished through the API with decision approve (what the Work Center card submits)
approve_form() { local i; for i in $(seq 1 24); do if iap -X POST "${PLATFORM}/operations-manager/jobs/$1/tasks/$2/finish" -d '{"taskData":{"finish_state":"success","variables":{"export":{"decision":"approve"}}}}' -o /dev/null -w '%{http_code}' | grep -qx 200; then return 0; fi; sleep 5; done; echo "form approval of $1/$2 never accepted"; return 1; }
# reject_form <job> <task>: a failure finish with decision reject (rolls the reservation back; the job ends in error by design)
reject_form() { iap -X POST "${PLATFORM}/operations-manager/jobs/$1/tasks/$2/finish" -d '{"taskData":{"finish_state":"failure","variables":{"export":{"decision":"reject"}}}}' -o /dev/null -w '%{http_code}' | grep -qx 200; }
# a job in error is retryable on 6.5.2, so a parent childJob task and an LCM execution wait until the errored child is cancelled
cancel_jobs() { iap -X POST "${PLATFORM}/operations-manager/jobs/cancel" -d "{\"jobIds\":[$(printf '"%s",' "$@" | sed 's/,$//')]}" -o /dev/null -w '%{http_code}' | grep -qx 200; }
cancel_exec() { iap -X POST "${PLATFORM}/lifecycle-manager/action-executions/$1/cancel" -d '{}' | ${PY} -c 'import sys,json;d=json.load(sys.stdin);assert (d.get("data") or {}).get("status")=="canceled",d'; }
wait_status() { local st; for _ in $(seq 1 24); do st=$(job_status "$1"); [ "$st" = "$2" ] && return 0; sleep 5; done; echo "job $1 is ${st}, wanted $2"; return 1; }
# child_job <workflow name> <parent job id>: the running child started by the parent (LCM wraps every action in a resource:action job)
child_job() { local i id; for i in $(seq 1 24); do id=$(iap "${PLATFORM}/operations-manager/jobs?limit=40" | ${PY} -c "
import sys,json
for j in (json.load(sys.stdin).get('data') or []):
    if j.get('name')=='$1' and j.get('status')=='running' and '$2' in (j.get('ancestors') or []): print(j['_id']); break"); [ -n "$id" ] && { echo "$id"; return 0; }; sleep 5; done; echo "no running $1 under job $2"; return 1; }
lcm_model_id() { iap "${PLATFORM}/lifecycle-manager/resources?limit=100" | ${PY} -c "import sys,json;print(next((m['_id'] for m in json.load(sys.stdin)['data'] if m['name']=='$1'),''))"; }
# run_action <model id> <body> -> "execution id, wrapper job id, instance id"
run_action() { iap -X POST "${PLATFORM}/lifecycle-manager/resources/$1/run-action" -d "$2" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);x=d.get("data") or {};print(x.get("_id",""),x.get("jobId",""),x.get("instanceId",""))'; }
wait_exec() { local st; for _ in $(seq 1 48); do st=$(iap "${PLATFORM}/lifecycle-manager/action-executions/$1" | ${PY} -c 'import sys,json;print((json.load(sys.stdin).get("data") or {}).get("status",""))'); case "$st" in complete) return 0;; error|canceled) echo "execution $1 ${st}"; return 1;; esac; sleep 5; done; echo "execution $1 timeout (${st})"; return 1; }
wait_task() { for _ in $(seq 1 24); do iap "${PLATFORM}/operations-manager/jobs/$1" | ${PY} -c "import sys,json;t=json.load(sys.stdin)['data']['tasks'].get('$2',{});sys.exit(0 if t.get('status')=='running' else 1)" && return 0; sleep 5; done; echo "task $2 of job $1 never reached running"; return 1; }
# the id of the newest Push Configuration with Approval job (the list is newest first); a count inside a fixed window slides
# once the history is deep enough (the no-op job itself evicts an older push job, measured 20260908T130615Z)
push_jobs() { iap "${PLATFORM}/operations-manager/jobs?limit=60" | ${PY} -c 'import sys,json;p=[j["_id"] for j in json.load(sys.stdin).get("data") or [] if j.get("name")=="Push Configuration with Approval"];print(p[0] if p else "")'; }
LCM_NAME="lcm-${ts_lc}"; LCM_INST="br2-${LCM_NAME}"; BR2_SW=10.100.0.166
c3() {
  local model create_id delete_id pair
  model=$(lcm_model_id "$LCM_MODEL"); [ -n "$model" ] || { echo "resource model ${LCM_MODEL} missing"; return 1; }
  for pair in $LCM_ACTIONS; do case "${pair%%:*}" in create) create_id=${pair##*:};; delete) delete_id=${pair##*:};; esac; done
  [ -n "${create_id:-}" ] && [ -n "${delete_id:-}" ] || { echo "itential/lcm/${LCM_MODEL}.yaml lacks create/delete action ids"; return 1; }
  # every action runnable: the platform resolves the workflow names to the current imports
  iap -X POST "${PLATFORM}/lifecycle-manager/resources/${model}/actions/validate" -d "$(iap "${PLATFORM}/lifecycle-manager/resources/${model}" | ${PY} -c 'import sys,json;print(json.dumps({"actions":json.load(sys.stdin)["data"]["actions"]}))')" \
    | ${PY} -c 'import sys,json;d=json.load(sys.stdin)["data"];bad=[(a["action"]["name"],a["errors"]) for a in d if not a["runnable"]];assert not bad,bad;print("actions runnable:",", ".join(a["action"]["name"]+" -> workflow "+a["action"]["workflow"][:8] for a in d))' || return 1
  # the form carries the PID fields
  iap "${PLATFORM}/json-forms/forms" | ${PY} -c "import sys,json;f=[x for x in json.load(sys.stdin) if x['name']=='${FORM_NAME}'];assert f,'form ${FORM_NAME} missing';keys={i['customKey'] for i in f[0]['struct']['items']};need={'branch','vid','vlan_name','switch','netbox_vlan_id','decision'};assert need<=keys,(need-keys);print('form fields:',sorted(keys))" || return 1
  # one instance per NetBox branch VLAN (intent: NetBox; state: LCM)
  local nb_names inst_names missing
  nb_names=$(nb "${NETBOX_URL}/api/ipam/vlans/?limit=0&group=br1-user&group=br2-user" | ${PY} -c 'import sys,json;print("\n".join(sorted(v["site"]["slug"]+"-"+v["name"] for v in json.load(sys.stdin)["results"])))')
  inst_names=$(iap "${PLATFORM}/lifecycle-manager/resources/${model}/instances?limit=100" | ${PY} -c 'import sys,json;print("\n".join(sorted(i["name"] for i in json.load(sys.stdin)["data"])))')
  missing=$(comm -23 <(echo "$nb_names") <(echo "$inst_names"))
  [ -z "$missing" ] || { echo "NetBox branch VLANs without an instance: $(echo "$missing" | tr '\n' ' ')"; return 1; }
  echo "instances cover every NetBox branch VLAN: $(echo "$nb_names" | tr '\n' ' ')"
  # 1) create on br2 through the LCM action: the child job pauses on the form, Work Center shows the fields, approve, instance active
  local ex wrapper iid child wi vid nbid
  read -r ex wrapper iid <<<"$(run_action "$model" "{\"actionId\":\"${create_id}\",\"instanceName\":\"${LCM_INST}\",\"instanceDescription\":\"verify ${ts} S4d.3\",\"inputs\":{\"branch\":\"br2\",\"vlan_name\":\"${LCM_NAME}\",\"switch_override\":\"\",\"change_request\":false}}")"
  [ -n "$ex" ] && [ -n "$wrapper" ] || { echo "create action did not start"; return 1; }
  child=$(child_job "Add Branch VLAN" "$wrapper") || { echo "$child"; return 1; }
  wait_task "$child" 4a || return 1
  read -r vid nbid <<<"$(nb "${NETBOX_URL}/api/ipam/vlans/?site=br2&name=${LCM_NAME}" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);assert d["count"]==1,d["count"];v=d["results"][0];assert v["status"]["value"]=="reserved",v["status"];print(v["vid"],v["id"])')" || { echo "NetBox has no reserved VLAN ${LCM_NAME} in br2 while the approval waits"; return 1; }
  # Work Center: the pending item of the child job and the fields the approver sees (NetBox above is the second source)
  wi=$(iap -X POST "${PLATFORM}/work-center-service/work-items/search" -d '{"filters":[{"field":"name","operator":"contains","value":"ShowJsonForm"}],"sort":{"field":"createdAt","order":"desc"},"skip":0,"limit":20}' \
    | ${PY} -c "import sys,json;d=json.load(sys.stdin);d=d.get('data') if isinstance(d,dict) else [];assert isinstance(d,list),d;w=[x for x in d if (x.get('execution') or {}).get('id')=='${child}' and x.get('status')=='pending'];print(w[0]['id'] if w else '')")
  [ -n "$wi" ] || { echo "no pending Work Center item for job ${child}"; return 1; }
  iap "${PLATFORM}/work-center-service/work-items/${wi}/variables/incoming" | ${PY} -c "import sys,json;v=json.load(sys.stdin)['incoming'];assert v.get('form_id')=='${FORM_NAME}',v.get('form_id');d=v['instance_data'];assert d['branch']=='br2' and d['vid']==${vid} and d['vlan_name']=='${LCM_NAME}' and d['switch']=='br2-sw01' and d['netbox_vlan_id']==${nbid} and d['status']=='reserved',d;print('Work Center item',sys.argv[1],'shows',{k:d[k] for k in ('branch','vid','vlan_name','switch','netbox_vlan_id','status')})" "$wi" || return 1
  approve_form "$child" 4a || return 1
  wait_job "$child" || return 1; wait_exec "$ex" || return 1
  iap "${PLATFORM}/lifecycle-manager/resources/${model}/instances/${iid}" | ${PY} -c "import sys,json;i=json.load(sys.stdin)['data'];d=i['instanceData'];assert i['name']=='${LCM_INST}' and d['vid']==${vid} and d['status']=='active' and d['netbox_vlan_id']==${nbid} and i['lastAction']['type']=='create' and i['lastAction']['status']=='complete',i;print('instance',i['name'],d)" || return 1
  nb "${NETBOX_URL}/api/ipam/vlans/${nbid}/" | ${PY} -c 'import sys,json;assert json.load(sys.stdin)["status"]["value"]=="active"' || { echo "NetBox VLAN ${nbid} not active"; return 1; }
  ${PY} verify/devcmd.py "$BR2_SW" "show vlan ${vid}" | grep -q "$LCM_NAME" || { echo "br2-sw01 has no VLAN ${vid} ${LCM_NAME} (direct SSH)"; return 1; }
  echo "create: VLAN ${vid} ${LCM_NAME} active in NetBox (id ${nbid}) and on br2-sw01; instance ${LCM_INST} recorded (execution ${ex})"
  # 2a) a rejected push changes nothing: the delete workflow (started directly with the instance data) pushes only through
  #     Push Configuration with Approval; rejecting that card leaves the push job in error, the delete job waiting on it, NetBox and the
  #     switch untouched; cancelling both jobs is the terminal step (a job in error is retryable on 6.5.2)
  local djob push0 inst_json
  inst_json="{\"branch\":\"br2\",\"vid\":${vid},\"vlan_name\":\"${LCM_NAME}\",\"switch\":\"br2-sw01\",\"netbox_vlan_id\":${nbid},\"status\":\"active\"}"
  djob=$(start_job "Remove Branch VLAN" "{\"instance\":${inst_json}}"); [ -n "$djob" ] || { echo "Remove Branch VLAN did not start"; return 1; }
  push0=$(child_job "Push Configuration with Approval" "$djob") || { echo "$push0"; return 1; }
  sleep 5; iap -X POST "${PLATFORM}/operations-manager/jobs/${push0}/tasks/2a/finish" -d '{"taskData":{"finish_state":"failure","variables":{}}}' -o /dev/null -w '%{http_code}' | grep -qx 200 || { echo "could not reject the push card"; return 1; }
  wait_status "$push0" error || return 1
  [ "$(job_status "$djob")" = running ] || { echo "the delete job did not wait on the rejected push: $(job_status "$djob")"; return 1; }
  nb "${NETBOX_URL}/api/ipam/vlans/${nbid}/" | ${PY} -c 'import sys,json;assert json.load(sys.stdin)["status"]["value"]=="active"' || { echo "NetBox VLAN changed by a rejected delete"; return 1; }
  ${PY} verify/devcmd.py "$BR2_SW" "show vlan ${vid}" | grep -q "$LCM_NAME" || { echo "br2-sw01 lost VLAN ${vid} on a rejected delete (direct SSH)"; return 1; }
  cancel_jobs "$djob" "$push0" || { echo "cancel of ${djob} ${push0} refused"; return 1; }
  wait_status "$djob" canceled || return 1
  echo "rejected push: job ${push0} in error, delete job ${djob} waited, NetBox and br2-sw01 untouched, both cancelled"
  # 2) delete through the LCM action: the delete workflow reaches the switch only through Push Configuration with Approval and its approval card
  local ex2 wrapper2 child2 push
  read -r ex2 wrapper2 _ <<<"$(run_action "$model" "{\"actionId\":\"${delete_id}\",\"instance\":\"${iid}\",\"inputs\":{}}")"
  [ -n "$ex2" ] || { echo "delete action did not start"; return 1; }
  child2=$(child_job "Remove Branch VLAN" "$wrapper2") || { echo "$child2"; return 1; }
  push=$(child_job "Push Configuration with Approval" "$child2") || { echo "$push"; return 1; }
  approve_task "$push" 2a || return 1
  wait_job "$child2" || return 1; wait_exec "$ex2" || return 1
  iap "${PLATFORM}/operations-manager/jobs/${push}" | ${PY} -c 'import sys,json;d=json.load(sys.stdin)["data"];v=d["variables"];assert d["status"]=="complete" and v.get("changed") is True and "no vlan '"$vid"'" in v.get("config",""),(d["status"],v.get("config"))' || { echo "push job ${push} did not apply 'no vlan ${vid}'"; return 1; }
  nb "${NETBOX_URL}/api/ipam/vlans/?site=br2&name=${LCM_NAME}" | ${PY} -c 'import sys,json;assert json.load(sys.stdin)["count"]==0,"NetBox VLAN survived the delete"' || return 1
  ${PY} verify/devcmd.py "$BR2_SW" "show vlan ${vid}" | grep -q "not found" || { echo "br2-sw01 still has VLAN ${vid} (direct SSH)"; return 1; }
  iap "${PLATFORM}/lifecycle-manager/resources/${model}/instances?limit=100" | ${PY} -c "import sys,json;n=[i['name'] for i in json.load(sys.stdin)['data']];assert '${LCM_INST}' not in n,n" || { echo "instance ${LCM_INST} still listed after the delete"; return 1; }
  echo "delete: push job ${push} removed VLAN ${vid} from br2-sw01 after the approval; NetBox VLAN gone; instance ${LCM_INST} retired (execution ${ex2})"
  # the create and the delete each left a journal entry on the switch in NetBox (PID S4e.5, ADR 0048)
  local swid
  swid=$(nb "${NETBOX_URL}/api/dcim/devices/?name=br2-sw01" | ${PY} -c 'import sys,json;print(json.load(sys.stdin)["results"][0]["id"])')
  nb "${NETBOX_URL}/api/extras/journal-entries/?assigned_object_type=dcim.device&assigned_object_id=${swid}&limit=20" | ${PY} -c "
import sys,json
c=[e['comments'] for e in json.load(sys.stdin)['results']]
want=['Lifecycle Manager branch-vlan create: VLAN ${vid} (${LCM_NAME})','Lifecycle Manager branch-vlan delete: VLAN ${vid} (${LCM_NAME})']
missing=[w for w in want if not any(x.startswith(w) for x in c)]
assert not missing,('journal entries missing on br2-sw01',missing,c[:4])
print('journal: br2-sw01 carries the create and the delete entry for VLAN ${vid}')" || return 1
  # 3) history: the two executions on the instance, each with its job
  # (curl globs [ ] in a URL, so the instance filter is applied client-side on the newest executions)
  iap "${PLATFORM}/lifecycle-manager/action-executions?sort=startTime&order=1&limit=100" | ${PY} -c 'import sys,json;d=[x for x in json.load(sys.stdin)["data"] if x.get("instanceId")=="'"$iid"'"];h=[(x["actionType"],x["status"],bool(x.get("jobId"))) for x in d];assert h==[("create","complete",True),("delete","complete",True)],h;print("history:",[(x["actionName"],x["status"],x["jobId"]) for x in d])' || return 1
  # 4) no-op: the delete workflow on the retired VLAN touches nothing (changed=false, no push job started)
  local job before after
  before=$(push_jobs)
  job=$(start_job "Remove Branch VLAN" "{\"instance\":{\"branch\":\"br2\",\"vid\":${vid},\"vlan_name\":\"${LCM_NAME}\",\"switch\":\"br2-sw01\",\"netbox_vlan_id\":${nbid},\"status\":\"deleted\"}}"); [ -n "$job" ] || { echo "Remove Branch VLAN did not start"; return 1; }
  wait_job "$job" || return 1
  after=$(push_jobs)
  iap "${PLATFORM}/operations-manager/jobs/${job}" | ${PY} -c 'import sys,json;v=json.load(sys.stdin)["data"]["variables"];assert v.get("changed") is False,v' || { echo "second delete was not a no-op"; return 1; }
  [ "$before" = "$after" ] || { echo "the no-op delete started a push job (${after})"; return 1; }
  echo "no-op: Remove Branch VLAN on the retired VLAN changed nothing and started no push (job ${job})"
  # 5) a rejected create leaves nothing behind: the reservation is rolled back, the child job ends in error by design,
  #    the execution waits until it is cancelled (the terminal step), and the cancel retires the instance
  local ex3 wrapper3 iid3 child3 nbid3
  read -r ex3 wrapper3 iid3 <<<"$(run_action "$model" "{\"actionId\":\"${create_id}\",\"instanceName\":\"${LCM_INST}-rej\",\"instanceDescription\":\"verify ${ts} S4d.3 reject\",\"inputs\":{\"branch\":\"br2\",\"vlan_name\":\"${LCM_NAME}-rej\",\"switch_override\":\"\",\"change_request\":false}}")"
  [ -n "$ex3" ] || { echo "second create action did not start"; return 1; }
  child3=$(child_job "Add Branch VLAN" "$wrapper3") || { echo "$child3"; return 1; }
  wait_task "$child3" 4a || return 1
  nbid3=$(nb "${NETBOX_URL}/api/ipam/vlans/?site=br2&name=${LCM_NAME}-rej" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);assert d["count"]==1 and d["results"][0]["status"]["value"]=="reserved",d;print(d["results"][0]["id"])') || { echo "no reservation for ${LCM_NAME}-rej"; return 1; }
  reject_form "$child3" 4a || { echo "reject of ${child3}/4a refused"; return 1; }
  wait_status "$child3" error || return 1
  iap "${PLATFORM}/operations-manager/jobs/${child3}" | ${PY} -c 'import sys,json;v=json.load(sys.stdin)["data"]["variables"];assert v.get("rolled_back") is True and (v.get("approval") or {}).get("decision")=="reject",v' || { echo "rejected create did not roll back"; return 1; }
  nb "${NETBOX_URL}/api/ipam/vlans/?site=br2&name=${LCM_NAME}-rej" | ${PY} -c 'import sys,json;assert json.load(sys.stdin)["count"]==0,"reservation survived the reject"' || return 1
  [ "$(iap "${PLATFORM}/lifecycle-manager/action-executions/${ex3}" | ${PY} -c 'import sys,json;print(json.load(sys.stdin)["data"]["status"])')" = running ] || { echo "execution ${ex3} did not wait on the errored child"; return 1; }
  cancel_exec "$ex3" || { echo "cancel of execution ${ex3} refused"; return 1; }
  iap "${PLATFORM}/lifecycle-manager/resources/${model}/instances?limit=100" | ${PY} -c "import sys,json;n=[i['name'] for i in json.load(sys.stdin)['data']];assert '${LCM_INST}-rej' not in n,n" || { echo "instance ${LCM_INST}-rej survived the cancel"; return 1; }
  echo "rejected create: reservation ${nbid3} rolled back, child ${child3} in error, execution ${ex3} cancelled, instance retired"
}
check "S4d.3 Lifecycle Manager ${LCM_MODEL}: instances from NetBox; create -> JSON form approval (Work Center shows branch, VLAN, switch, NetBox reservation) -> instance; rejected push changes nothing; delete -> governed push; history; no-op; rejected create retired" c3
# cleanup if a failed run left the verify VLAN behind (same direct cleanup test-05/06 use; the LCM instance is retired by its delete action when possible)
lcm_cleanup() {
  local model row ex st name iid wrapper child2 push
  model=$(lcm_model_id "$LCM_MODEL"); [ -n "$model" ] || return 0
  # running executions of this run's instances: reject a pending form, then cancel (the cancel retires a create's instance)
  while read -r ex st name; do
    [ -n "$ex" ] || continue
    echo "cleanup: execution ${ex} (${name}) is ${st}: rejecting any pending form and cancelling"
    for j in $(iap "${PLATFORM}/operations-manager/jobs?limit=40" | ${PY} -c 'import sys,json;[print(j["_id"]) for j in json.load(sys.stdin).get("data") or [] if j.get("name")=="Add Branch VLAN" and j.get("status")=="running"]'); do reject_form "$j" 4a || true; done
    sleep 5; cancel_exec "$ex" >/dev/null 2>&1 || true
  done < <(iap "${PLATFORM}/lifecycle-manager/action-executions?sort=startTime&order=-1&limit=50" | ${PY} -c "import sys,json;[print(e['_id'],e['status'],e['instanceName']) for e in json.load(sys.stdin)['data'] if e.get('status')=='running' and (e.get('instanceName') or '').startswith('${LCM_INST}')]")
  # an instance left active: retire it through its delete action (push card approved here, as the checks do)
  iid=$(iap "${PLATFORM}/lifecycle-manager/resources/${model}/instances?limit=100" | ${PY} -c "import sys,json;print(next((i['_id'] for i in json.load(sys.stdin)['data'] if i['name']=='${LCM_INST}'),''))")
  [ -n "$iid" ] || return 0
  echo "cleanup: retiring instance ${LCM_INST} through its delete action"
  read -r ex wrapper _ <<<"$(run_action "$model" "{\"actionId\":\"$(echo "$LCM_ACTIONS" | tr ' ' '\n' | awk -F: '/^delete/{print $2}')\",\"instance\":\"${iid}\",\"inputs\":{}}")"
  child2=$(child_job "Remove Branch VLAN" "$wrapper" 2>/dev/null) && { push=$(child_job "Push Configuration with Approval" "$child2" 2>/dev/null) && approve_task "$push" 2a >/dev/null 2>&1; wait_job "$child2" >/dev/null 2>&1 || cancel_exec "$ex" >/dev/null 2>&1; }
}
lcm_cleanup
vid=$(nb "${NETBOX_URL}/api/ipam/vlans/?site=br2&name=${LCM_NAME}" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);print(d["results"][0]["vid"] if d["count"] else "")' 2>/dev/null)
if [ -n "$vid" ]; then echo "cleanup: removing leftover VLAN ${vid} ${LCM_NAME}"; nb -X DELETE "${NETBOX_URL}/api/ipam/vlans/$(nb "${NETBOX_URL}/api/ipam/vlans/?site=br2&name=${LCM_NAME}" | ${PY} -c 'import sys,json;print(json.load(sys.stdin)["results"][0]["id"])')/" -o /dev/null; ${PY} verify/devcmd.py "$BR2_SW" "configure
no vlan ${vid}
end" >/dev/null 2>&1 || true; fi


# --- S4d.4 Integration Models (ADR 0045): both models and integrations from the documents; every operation an authorized tool;
# lab-netops reads a NetBox device and a ServiceNow incident through the integration tools (the session names them, not the
# adapter methods); second sources: NetBox and the PDI over their own APIs. Spends Anthropic tokens (printed) ---
INT_MODELS=$(${PY} -c "import yaml;m=yaml.safe_load(open('$V'))['integrations']['models'];print(' '.join(f\"{k}:{v['title']}:{v['version']}:{v['instance']}:{','.join(v['operations'])}\" for k,v in m.items()))")
# agent session helpers (the same calls test-06 makes)
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
session_msgs()  { iap "${PLATFORM}/agent-session-manager/sessions/$1/messages?limit=500&sortBy=eventId&sortOrder=asc"; }
session_text()  { session_msgs "$1" | ${PY} -c 'import sys,json;m=json.load(sys.stdin);m=sorted([x for x in m if x.get("type")=="inference-succeeded" and x.get("text")],key=lambda x:x.get("timestamp",0));print(m[-1]["text"] if m else "")'; }
session_tools() { session_msgs "$1" | ${PY} -c 'import sys,json;m=json.load(sys.stdin);print(sorted({x["data"].get("toolName","") for x in m if x.get("category")=="TOOL_CALLED"}))'; }
session_usage() { session_msgs "$1" | ${PY} -c 'import sys,json;m=json.load(sys.stdin);u=[x["data"]["tokenUsage"] for x in m if x.get("type")=="inference-succeeded" and x.get("data",{}).get("tokenUsage")];print(sum(t.get("inputTokens",0) for t in u),sum(t.get("outputTokens",0) for t in u))'; }
c4() {
  local row key title ver inst ops tools_json errs=""
  # models and integrations as declared; every operation a tool the admin may use (roles re-synced by the play)
  for row in $INT_MODELS; do
    IFS=: read -r key title ver inst ops <<<"$row"
    iap "${PLATFORM}/integration-models/${title}:${ver}/export" | ${PY} -c "
import sys,json;d=json.load(sys.stdin);m=d.get('model',d);ops=sorted(o['operationId'] for p in m['paths'].values() for o in p.values())
assert ops==sorted('${ops}'.split(',')),ops;print('model ${title}:${ver}:',len(ops),'operations')" || return 1
    iap "${PLATFORM}/integrations/${inst}" | ${PY} -c "import sys,json;d=json.load(sys.stdin)['data'];p=d['properties']['properties'];assert d['model']=='@itential/adapter_${title}:${ver}' and d.get('virtual') is True,d['model'];print('integration ${inst}: server',p['server']['protocol'],p['server']['host'],p['server'].get('port'),'auth',list(p['authentication']))" || return 1
    tools_json=$(${PY} -c "import json;print(json.dumps({'referenceIds':['integration:${title}%3A${ver}:${inst}:'+o for o in '${ops}'.split(',')],'queryOptions':{'limit':100}}))")
    iap -X POST "${PLATFORM}/tools/bulk" -d "$tools_json" | ${PY} -c "import sys,json;d=json.load(sys.stdin)['data'];want='${ops}'.split(',');got={t['referenceId'].split(':')[-1]:t.get('authorized',True) for t in d};miss=[o for o in want if o not in got];den=[o for o,a in got.items() if a is False];assert not miss and not den,('missing',miss,'not authorized',den);print('tools ${inst}:',len(got),'authorized')" || return 1
  done
  # 1) a NetBox device through the integration (second source: NetBox itself)
  local sid txt tools site role
  read -r site role <<<"$(nb "${NETBOX_URL}/api/dcim/devices/?name=br1-sw01" | ${PY} -c 'import sys,json;d=json.load(sys.stdin)["results"][0];print(d["site"]["slug"],d["role"]["slug"])')"
  sid=$(run_agent lab-netops '{"request":"Using the NetBox integration, which site and device role does the device br1-sw01 have? Reply with the site slug and the role slug only."}') || { echo "$sid"; return 1; }
  sid=${sid##*$'\n'}; txt=$(session_text "$sid"); tools=$(session_tools "$sid"); read -r i o <<<"$(session_usage "$sid")"
  echo "$tools" | grep -q "dcim_devices_list\|dcim_devices_retrieve" || { echo "no NetBox integration tool in the session: ${tools}"; return 1; }
  echo "$tools" | grep -q "getDcimDevices\|Netbox" && { echo "the adapter was used instead of the integration: ${tools}"; return 1; }
  echo "$txt" | grep -qi "$site" && echo "$txt" | grep -qi "$role" || { echo "answer lacks ${site}/${role}: $(echo "$txt" | head -c 200)"; return 1; }
  echo "NetBox: br1-sw01 is ${site} / ${role} through ${tools}; tokens in=${i} out=${o}"
  # 2) a ServiceNow incident through the integration (second source: the PDI's own Table API)
  local want
  want=$(curl -s -m 30 -u "${SNOW_USER}:${SNOW_PASSWORD}" "https://${SNOW_INSTANCE}.service-now.com/api/now/table/incident?sysparm_query=number=INC0000060&sysparm_fields=short_description" -H 'Accept: application/json' | ${PY} -c 'import sys,json;print(json.load(sys.stdin)["result"][0]["short_description"])')
  [ -n "$want" ] || { echo "the PDI did not answer for INC0000060 (hibernated?)"; return 1; }
  sid=$(run_agent lab-netops '{"request":"Using the ServiceNow integration, what is the short description of incident INC0000060? Reply with the short description only."}') || { echo "$sid"; return 1; }
  sid=${sid##*$'\n'}; txt=$(session_text "$sid"); tools=$(session_tools "$sid"); read -r i2 o2 <<<"$(session_usage "$sid")"
  echo "$tools" | grep -q "listIncidents\|getIncident" || { echo "no ServiceNow integration tool in the session: ${tools}"; return 1; }
  echo "$tools" | grep -q "Servicenow\|genericAdapterRequest" && { echo "the adapter was used instead of the integration: ${tools}"; return 1; }
  echo "$txt" | grep -qi "$want" || { echo "answer lacks '${want}': $(echo "$txt" | head -c 200)"; return 1; }
  echo "ServiceNow: INC0000060 = '${want}' through ${tools}; tokens in=${i2} out=${o2}"
  echo "tokens spent by S4d.4: in=$((i+i2)) out=$((o+o2))"
}
check "S4d.4 Integration Models lab-netbox and lab-servicenow: instances netbox-api/servicenow-api, every operation an authorized tool; lab-netops reads br1-sw01 and INC0000060 through the integration tools (not the adapters)" c4


# --- S4d.6 hosts (ADR 0047): the Ubuntu hosts as lab-hosts inventory nodes, reachable through Gateway 5 with their uptime;
# absent from Configuration Manager; direct SSH as the same account is the second source. Firewalls stay deferred ---
HOST_INV=$(${PY} -c "import yaml;print(yaml.safe_load(open('$V'))['stack']['host_inventory'])")
HOST_PLATFORMS=$(${PY} -c "import yaml;print('&'.join('platform='+p for p in yaml.safe_load(open('$V'))['hosts']['netbox_platforms']))")
c6() {
  local nbrows=() row name ip inv_names nb_names missing out gw_host gw_up direct_host direct_up errs=""
  while IFS= read -r row; do nbrows+=("$row"); done < <(nb "${NETBOX_URL}/api/dcim/devices/?limit=0&status=active&has_primary_ip=true&${HOST_PLATFORMS}" | ${PY} -c 'import sys,json;[print(d["name"],d["primary_ip4"]["address"].split("/")[0]) for d in json.load(sys.stdin)["results"] if d["role"]["slug"] in ("server","client")]')
  [ "${#nbrows[@]}" = 3 ] || { echo "NetBox has ${#nbrows[@]} Ubuntu hosts, wanted 3"; return 1; }
  nb_names=$(printf '%s\n' "${nbrows[@]}" | awk '{print $1}' | sort)
  inv_names=$(iap "${PLATFORM}/inventory_manager/v1/inventories/${HOST_INV}/nodes" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);print("\n".join(sorted(n["name"] for n in d["result"]["data"])))')
  [ "$nb_names" = "$inv_names" ] || { echo "inventory ${HOST_INV} nodes ($(echo "$inv_names" | tr '\n' ' ')) differ from NetBox ($(echo "$nb_names" | tr '\n' ' '))"; return 1; }
  # never a Configuration Manager device (no backups, no compliance on hosts)
  iap -X POST "${PLATFORM}/configuration_manager/devices" -d '{"options":{"start":0,"limit":200}}' | ${PY} -c "import sys,json;names={d['name'] for d in json.load(sys.stdin)['list']};bad=names & set('''$nb_names'''.split());assert not bad,('hosts visible to Configuration Manager',bad);print('Configuration Manager sees',len(names),'devices, no host')" || return 1
  for row in "${nbrows[@]}"; do read -r name ip <<<"$row"
    out=$(iap -X POST "${PLATFORM}/gateway_manager/v1/services/run" -d "{\"serviceName\":\"send-command\",\"clusterId\":\"lab\",\"params\":{\"commands\":[\"uptime -p\",\"hostname\"]},\"inventory\":[{\"inventory\":\"${HOST_INV}\",\"nodeNames\":[\"${name}\"]}]}")
    read -r gw_up gw_host <<<"$(echo "$out" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);r=d["result"]["results"];assert all(x["success"] for x in r),r;o={x["command"]:x["output"].strip() for x in r};print(o["uptime -p"].replace(" ","_"),o["hostname"])' 2>/dev/null)" || { errs+="${name}: Gateway 5 send-command failed: $(echo "$out" | head -c 200)\n"; continue; }
    [ "$gw_host" = "$name" ] || { errs+="${name}: Gateway 5 saw hostname ${gw_host}\n"; continue; }
    direct_host=$(${PY} verify/devcmd.py "$ip" "hostname" 2>/dev/null | tr -d '\r' | tail -1); direct_up=$(${PY} verify/devcmd.py "$ip" "uptime -p" 2>/dev/null | tr -d '\r' | tail -1 | tr ' ' '_')
    [ "$direct_host" = "$name" ] || { errs+="${name}: direct SSH saw hostname '${direct_host}'\n"; continue; }
    # both uptimes report the same number of days (the minutes may tick between the two reads)
    [ "$(echo "$gw_up" | grep -o '[0-9]*_day' )" = "$(echo "$direct_up" | grep -o '[0-9]*_day')" ] || { errs+="${name}: uptime ${gw_up} (Gateway 5) vs ${direct_up} (direct SSH)\n"; continue; }
    echo "${name} (${ip}): reachable, $(echo "$gw_up" | tr '_' ' ') through Gateway 5; direct SSH agrees"
  done
  [ -z "$errs" ] || { printf "%b" "$errs"; return 1; }
}
check "S4d.6 the three Ubuntu hosts are ${HOST_INV} inventory nodes, reachable through Gateway 5 with their uptime (direct SSH agrees), and unknown to Configuration Manager" c6
defer "S4d.6 firewalls: PA-VM image not staged (S3.4 deferral)"


echo
echo "passed=${pass} failed=${fail} deferred=${deferred}"
[ "$fail" = 0 ]
