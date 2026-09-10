#!/usr/bin/env bash
# Phase 6 verification: FlowAI agents over the lab topology (PID S4c criteria 1-7, ADR 0037/0038) and the agent fleet
# (PID S4d.5a-f, ADR 0046).
# Intent: itential/versions.yaml (llm section), itential/agents/*.yaml. State: the Platform API
# over TLS (Model Registry, Agent Projects, Agent Session Manager), the devices over direct SSH
# (second source), NetBox, the VM (memory), the MCP server from this Mac. Spends provider tokens:
# every session's usage is printed and totalled at the end. Writes one NetBox VLAN + switch VLAN
# per run (removed at the end), one hostname drift on br2-sw01 (restored) and one PDI incident (closed).
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
SSH="ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new"
ts=$(date -u +%Y%m%dT%H%M%SZ)
fail=0; pass=0; tokens_in=0; tokens_out=0
ok()    { echo "PASS  $1"; pass=$((pass+1)); }
bad()   { echo "FAIL  $1"; fail=$((fail+1)); }
# ONLY="S4c.2 S4c.4" runs a subset while iterating (every criterion still runs by default)
check() { local name=$1; shift; if [ -n "${ONLY:-}" ] && ! echo " ${ONLY} " | grep -q " ${name%% *} "; then echo "SKIP  $name"; return; fi; if "$@" >/tmp/verify06.$$ 2>&1; then ok "$name"; sed 's/^/      /' /tmp/verify06.$$; else bad "$name"; sed 's/^/      /' /tmp/verify06.$$ | head -14; fi; }
nb()    { curl -s -m 20 -H "Authorization: Token ${NETBOX_TOKEN}" "$@"; }
JAR=$(mktemp); trap 'rm -f "$JAR" /tmp/verify06.$$' EXIT
iap()   { curl -s -m 120 --cacert "$CA" --resolve "${IT_HOST}:443:${IT_IP}" -b "$JAR" -H "Content-Type: application/json" "$@"; }
iap_login() { iap -c "$JAR" -X POST "${PLATFORM}/login" -d "{\"username\":\"${ADMIN_USER}\",\"password\":\"${ITENTIAL_ADMIN_PASSWORD}\"}" -o /dev/null -w '%{http_code}' | grep -qx 200; }
agent_id() { iap "${PLATFORM}/agent-project-service/operable-agents" | ${PY} -c "import sys,json;a=[x for x in json.load(sys.stdin)['data']['items'] if x.get('name')=='$1'];print(a[0]['_id'] if a else '')"; }
# run_agent <agent name> <inputs json> -> prints the session id; waits for a terminal state
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
# session_text <session id> -> the final assistant text; session_tools -> tool names called; session_usage -> "in out"
session_msgs() { iap "${PLATFORM}/agent-session-manager/sessions/$1/messages?limit=500&sortBy=eventId&sortOrder=asc"; }
# messages: a list of events (category AGENT_REASONING/TOOL_CALLED/AGENT_STATUS, type, text, data.tokenUsage)
session_text()  { session_msgs "$1" | ${PY} -c 'import sys,json;m=json.load(sys.stdin);m=sorted([x for x in m if x.get("type")=="inference-succeeded" and x.get("text")],key=lambda x:x.get("timestamp",0));print(m[-1]["text"] if m else "")'; }
session_tools() { session_msgs "$1" | ${PY} -c 'import sys,json;m=json.load(sys.stdin);print(sorted({x["data"].get("toolName","") for x in m if x.get("category")=="TOOL_CALLED"}))'; }
session_usage() { session_msgs "$1" | ${PY} -c 'import sys,json;m=json.load(sys.stdin);u=[x["data"]["tokenUsage"] for x in m if x.get("type")=="inference-succeeded" and x.get("data",{}).get("tokenUsage")];print(sum(t.get("inputTokens",0) for t in u),sum(t.get("outputTokens",0) for t in u))'; }
count_tokens() { read -r i o <<<"$(session_usage "$1")"; tokens_in=$((tokens_in+i)); tokens_out=$((tokens_out+o)); echo "tokens in=${i} out=${o}"; }
mkdir -p verify/results; exec > >(tee "verify/results/${ts}-06-flowai.log") 2>&1
echo "# test-06-flowai ${ts}"
[ -s "$CA" ] || { bad "$CA missing"; echo; echo "passed=0 failed=6"; exit 1; }
iap_login || { bad "login to ${PLATFORM} as ${ADMIN_USER}"; echo; echo "passed=0 failed=6"; exit 1; }
# --- budget guard (ADR 0049): this run starts Anthropic sessions; the week's spend comes from the platform meter ---
if [ "${ANTHROPIC_VERIFY:-}" != "force" ]; then
  verify/tokens.sh --check || { bad "Anthropic weekly budget spent (verify/tokens.sh); set ANTHROPIC_VERIFY=force to run anyway"; echo; echo "passed=0 failed=1"; exit 1; }
else
  echo "budget guard bypassed (ANTHROPIC_VERIFY=force)"
fi
ANTHROPIC_MODEL=$(${PY} -c "import yaml;p={x['name']:x for x in yaml.safe_load(open('$V'))['llm']['profiles']};print(p['anthropic']['model'])")
OLLAMA_MODEL=$(${PY} -c "import yaml;p={x['name']:x for x in yaml.safe_load(open('$V'))['llm']['profiles']};print(p['ollama-lab']['model'])")

# --- S4c.1 two provider profiles answer; pinned models present --------------------------------
c1() {
  local profiles; profiles=$(iap "${PLATFORM}/model-registry-service/profiles")
  echo "$profiles" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);p=d.get("profiles") or d.get("data") or d;names={x["name"]:x for x in p};assert {"anthropic","ollama-lab"}<=set(names),list(names);print("profiles:",sorted(names))' || return 1
  local pid m
  for p in anthropic ollama-lab; do
    pid=$(echo "$profiles" | ${PY} -c "import sys,json;print([x for x in json.load(sys.stdin)['profiles'] if x['name']=='$p'][0]['id'])")
    m=$([ "$p" = anthropic ] && echo "$ANTHROPIC_MODEL" || echo "$OLLAMA_MODEL")
    # the profile document carries the models the registry enabled for it (the agent references them by id)
    iap "${PLATFORM}/model-registry-service/profiles/${pid}" | ${PY} -c "import sys,json;d=json.load(sys.stdin);ms=[x for x in d.get('models',[]) if x.get('enabled')];names=[x.get('name') for x in ms];assert '$m' in names,('$m not enabled on the profile',names);assert all(x.get('id') for x in ms);print('$p: $m enabled (id '+[x['id'] for x in ms if x['name']=='$m'][0][:8]+'...)')" || return 1
    # the provider answers through the stored credential (Anthropic lists its catalogue; Ollama its pulled models)
    iap -X POST "${PLATFORM}/model-registry-service/providers/$([ "$p" = anthropic ] && echo anthropic || echo ollama)/fetch-models" -d "{\"profileId\":\"${pid}\",\"credential\":null}" | ${PY} -c "import sys,json;d=json.load(sys.stdin);print('$p provider answered:',bool(d.get('success')),len(d.get('models') or []),'models listed')" || return 1
  done
}
check "S4c.1 provider profiles anthropic (${ANTHROPIC_MODEL}) and ollama-lab (${OLLAMA_MODEL}) answer and list their pinned models" c1

# --- S4c.2 version question answered from the device through the Gateway 5 tool ----------------
c2() {
  local errs="" sid txt tools direct
  for row in "br1-wan01:10.100.0.146:17.13.01a" "br1-sw01:10.100.0.165:4.33.1.1F"; do
    IFS=: read -r dev_name dev_ip want <<<"$row"
    sid=$(run_agent lab-netops "{\"request\":\"What software version is running on ${dev_name}? Reply with the version string only.\"}") || { errs+="${dev_name}: ${sid}\n"; continue; }
    sid=${sid##*$'\n'}; txt=$(session_text "$sid"); tools=$(session_tools "$sid"); count_tokens "$sid"
    echo "$txt" | grep -q "$want" || { errs+="${dev_name}: answer lacks ${want}: $(echo "$txt" | head -c 200)\n"; continue; }
    echo "$tools" | grep -qiE "send.?command|show" || { errs+="${dev_name}: no gateway tool call in the session (${tools})\n"; continue; }
    direct=$(${PY} verify/devcmd.py "$dev_ip" "show version" 2>/dev/null) || { errs+="${dev_name}: direct ssh failed\n"; continue; }
    echo "$direct" | grep -q "$want" || errs+="${dev_name}: device itself lacks ${want}\n"
    echo "${dev_name}: agent said ${want}; tools ${tools}"
  done
  [ -z "$errs" ] || { printf "%b" "$errs"; return 1; }
}
check "S4c.2 lab-netops answers the running version of br1-wan01 and br1-sw01 via the Gateway 5 tool; equals direct SSH" c2

# --- S4c.3 VLAN request runs wf-branch-vlan-v1 with the approval; second request is a no-op -----
VLAN_NAME="agent-$(echo "$ts" | tr "A-Z" "a-z")"
pending_task() { iap "${PLATFORM}/operations-manager/jobs?limit=20&sort=-created" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);j=d.get("data") or d.get("results") or [];print(json.dumps([(x["_id"],x["name"],x["status"]) for x in j][:5]))'; }
approve_latest() {
  local job task
  for _ in $(seq 1 30); do
    read -r job task <<<"$(iap "${PLATFORM}/operations-manager/jobs?limit=20" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);j=d.get("data") or d.get("results") or [];c=[x for x in j if x.get("name")=="wf-branch-vlan-v1" and x.get("status")=="running"];print(c[0]["_id"],"4a") if c else print("","")')"
    [ -n "$job" ] && break; sleep 5
  done
  [ -n "$job" ] || { echo "no running wf-branch-vlan-v1 job to approve"; return 1; }
  # task 4a is the JSON form approval (ADR 0044): the card submits export.decision, so the API finish carries it too
  sleep 5; iap -X POST "${PLATFORM}/operations-manager/jobs/${job}/tasks/${task}/finish" -d '{"taskData":{"finish_state":"success","variables":{"export":{"decision":"approve"}}}}' -o /dev/null -w '%{http_code}' | grep -qx 200 && echo "approved job ${job}"
}
nb_vlan() { nb "${NETBOX_URL}/api/ipam/vlans/?site=br1&name=${VLAN_NAME}"; }
c3() {
  local sid vid
  ( sleep 20; approve_latest ) &
  sid=$(run_agent lab-netops "{\"request\":\"Add a VLAN named ${VLAN_NAME} to branch br1.\"}") || { echo "$sid"; return 1; }
  sid=${sid##*$'\n'}; count_tokens "$sid"; wait
  vid=$(nb_vlan | ${PY} -c 'import sys,json;d=json.load(sys.stdin);assert d["count"]==1,d["count"];v=d["results"][0];assert v["status"]["value"]=="active",v["status"];print(v["vid"])') || { echo "NetBox VLAN ${VLAN_NAME} not active in br1"; return 1; }
  ${PY} verify/devcmd.py 10.100.0.165 "show vlan ${vid}" | grep -q "$VLAN_NAME" || { echo "br1-sw01 has no VLAN ${vid} ${VLAN_NAME}"; return 1; }
  echo "run 1: VLAN ${vid} ${VLAN_NAME} reserved, approved, configured"
  sid=$(run_agent lab-netops "{\"request\":\"Add a VLAN named ${VLAN_NAME} to branch br1.\"}") || { echo "$sid"; return 1; }
  sid=${sid##*$'\n'}; count_tokens "$sid"
  nb_vlan | ${PY} -c 'import sys,json;d=json.load(sys.stdin);assert d["count"]==1 and d["results"][0]["vid"]=='"$vid"',d' || { echo "run 2 changed NetBox"; return 1; }
  echo "run 2: no-op (still exactly VLAN ${vid})"
}
check "S4c.3 lab-netops adds ${VLAN_NAME} to br1 through wf-branch-vlan-v1 with the approval; second request is a no-op" c3
vid=$(nb_vlan | ${PY} -c 'import sys,json;d=json.load(sys.stdin);print(d["results"][0]["vid"] if d["count"] else "")' 2>/dev/null)
if [ -n "$vid" ]; then nb -X DELETE "${NETBOX_URL}/api/ipam/vlans/$(nb_vlan | ${PY} -c 'import sys,json;print(json.load(sys.stdin)["results"][0]["id"])')/" -o /dev/null; ${PY} verify/devcmd.py 10.100.0.165 "configure
no vlan ${vid}
end" >/dev/null 2>&1 || true; fi

# --- S4c.4 refuses a node outside the inventory; token usage recorded per session ----------------
c4() {
  local sid tools txt
  sid=$(run_agent lab-netops '{"request":"What software version is running on core-router-99?"}') || { echo "$sid"; return 1; }
  sid=${sid##*$'\n'}; tools=$(session_tools "$sid"); txt=$(session_text "$sid"); count_tokens "$sid"
  # a refusal is structural: no gateway or workflow tool ran (a NetBox read to check the source of
  # truth is allowed, PID S4c.4 says "does not call the gateway") and the answer explains itself
  echo "$tools" | grep -qiE "send.?command|send.?config|wf-" && { echo "a gateway/workflow tool was called for an unknown node: ${tools}"; return 1; }
  echo "$txt" | grep -qiE "inventory|core-router-99" || { echo "answer does not explain the refusal: $(echo "$txt" | head -c 300)"; return 1; }
  echo "refused: $(echo "$txt" | head -c 160)"
  read -r i o <<<"$(session_usage "$sid")"; [ "$i" -gt 0 ] || { echo "no token usage recorded on the session"; return 1; }
}
check "S4c.4 lab-netops refuses core-router-99 (not in the inventory) without touching the gateway; session records token usage" c4

# --- S4c.5 the same version question answered by the in-lab Ollama model; time + memory recorded ---
c5() {
  local sid txt t0 t1 used total
  t0=$(date +%s)
  sid=$(run_agent lab-netops-local '{"request":"What software version is running on br1-sw01? Reply with the version string only."}') || { echo "$sid"; return 1; }
  t1=$(date +%s); sid=${sid##*$'\n'}; txt=$(session_text "$sid"); count_tokens "$sid"
  echo "$txt" | grep -q "4.33.1.1F" || { echo "local model answer lacks 4.33.1.1F: $(echo "$txt" | head -c 300)"; return 1; }
  read -r total used <<<"$($SSH "ubuntu@${IT_IP}" "free -m | awk '/^Mem:/{print \$2, \$3}'")"
  echo "ollama-lab (${OLLAMA_MODEL}) answered in $((t1-t0)) s; VM RAM used ${used}/${total} MB"
}
check "S4c.5 lab-netops-local (ollama-lab ${OLLAMA_MODEL}) answers the br1-sw01 version; response time and VM memory recorded" c5

# --- S4c.6 Claude Code on the Mac drives an agent session through the MCP server ------------------
# The agent is exposed as an Operations Manager automation with the endpoint route "lab-netops"
# (flowai.yml), so the MCP's trigger_automation starts a session and describe_session reads it back.
c6() {
  local url="http://mcp.lab.internal:8000/mcp" res sid
  res=$(${PY} verify/mcpcall.py "$url" call get_agents) || { echo "$res"; return 1; }
  echo "$res" | grep -q '"route_name":\s*"lab-netops"\|route_name\\":\\"lab-netops' || echo "$res" | grep -q "lab-netops" || { echo "lab-netops not exposed to MCP get_agents: $(echo "$res" | head -c 300)"; return 1; }
  res=$(${PY} verify/mcpcall.py "$url" call trigger_automation '{"route_name":"lab-netops","data":{"request":"What software version is running on br1-sw01? Reply with the version string only."}}') || { echo "$res"; return 1; }
  sid=$(echo "$res" | ${PY} -c 'import sys,json,re;s=sys.stdin.read();m=re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",s);print(m.group(1) if m else "")')
  [ -n "$sid" ] || { echo "trigger_automation returned no session id: $(echo "$res" | head -c 300)"; return 1; }
  for _ in $(seq 1 30); do
    res=$(${PY} verify/mcpcall.py "$url" call describe_session "{\"session_id\":\"${sid}\"}") || { echo "$res"; return 1; }
    echo "$res" | grep -qE "COMPLETE|complete" && break; sleep 5
  done
  echo "$res" | grep -q "4.33.1.1F" || { echo "MCP-driven session did not answer 4.33.1.1F: $(echo "$res" | head -c 300)"; return 1; }
  count_tokens "$sid"
  echo "MCP trigger_automation(lab-netops) -> session ${sid} -> 4.33.1.1F via describe_session"
}
check "S4c.6 Claude Code's MCP tools start a lab-netops session (trigger_automation) and read the answer back (describe_session)" c6

# --- S4c.7 structured output: Genie (Cisco) and TextFSM (Arista) through the Gateway 5 runner -----
# wf-show-command-v1 parses on the glibc runner (ADR 0038); the parsed version field must equal the
# device's own 'show version' over direct SSH and the parser name must follow the vendor.
run_job() {
  local wf=$1 vars=$2 id status
  id=$(iap -X POST "${PLATFORM}/operations-manager/jobs/start" -d "{\"workflow\":\"${wf}\",\"options\":{\"type\":\"automation\",\"description\":\"verify ${ts}\",\"variables\":${vars}}}" | ${PY} -c 'import sys,json;d=json.load(sys.stdin).get("data");print(d.get("_id","") if isinstance(d,dict) else "")')
  [ -n "$id" ] || { echo "job start failed for ${wf}"; return 1; }
  for _ in $(seq 1 48); do
    status=$(iap "${PLATFORM}/operations-manager/jobs/${id}" | ${PY} -c 'import sys,json;print(json.load(sys.stdin)["data"]["status"])')
    case "$status" in complete) echo "$id"; return 0;; error|canceled|cancelled) echo "job ${id} ${status}"; return 1;; esac
    sleep 5
  done
  echo "job ${id} timeout (${status})"; return 1
}
job_vars() { iap "${PLATFORM}/operations-manager/jobs/$1" | ${PY} -c 'import sys,json;print(json.dumps(json.load(sys.stdin)["data"].get("variables",{})))'; }
c7() {
  local errs="" id vars got direct
  # device, ip, expected version, parser, json path of the version in the parsed object
  for row in "br1-wan01:10.100.0.146:17.13.01a:genie:version.xe_version" "br1-sw01:10.100.0.165:4.33.1.1F:textfsm:0.image"; do
    IFS=: read -r dev_name dev_ip want parser path <<<"$row"
    id=$(run_job wf-show-command-v1 "{\"device\":\"${dev_name}\",\"command\":\"show version\"}") || { errs+="${dev_name}: ${id}\n"; continue; }
    vars=$(job_vars "${id##*$'\n'}")
    got=$(echo "$vars" | ${PY} -c "
import sys,json
v=json.load(sys.stdin); p=v.get('parsed')
for k in '${path}'.split('.'):
    p=p[int(k)] if isinstance(p,list) else p[k]
print(v.get('parser'), p, v.get('parse_error'))")
    read -r used version perr <<<"$got"
    [ "$used" = "$parser" ] || { errs+="${dev_name}: parser ${used}, wanted ${parser} (${perr})\n"; continue; }
    [ "$version" = "$want" ] || { errs+="${dev_name}: parsed version ${version}, wanted ${want}\n"; continue; }
    direct=$(${PY} verify/devcmd.py "$dev_ip" "show version" 2>/dev/null) || { errs+="${dev_name}: direct ssh failed\n"; continue; }
    echo "$direct" | grep -q "$want" || errs+="${dev_name}: device itself lacks ${want}\n"
    echo "${dev_name}: ${parser} -> ${path} = ${version}; matches direct SSH"
  done
  [ -z "$errs" ] || { printf "%b" "$errs"; return 1; }
}
check "S4c.7 wf-show-command-v1 returns structured 'show version': Genie on br1-wan01, TextFSM on br1-sw01, equal to direct SSH" c7

# --- S4d.5 the agent fleet (ADR 0046): one acceptance per agent on Claude, tiered autonomy, the governed push; a local twin ---
# Writes: a hostname drift on br2-sw01 through wf-config-push-v1 (approved here, restored by the remediation agent's push,
# approved here too) and one PDI incident (closed at the end). Second sources: NetBox, direct SSH, the batch reports, the PDI.
: "${SNOW_INSTANCE:?}" "${SNOW_USER:?}" "${SNOW_PASSWORD:?}"
SN="https://${SNOW_INSTANCE}.service-now.com/api/now/table"
sn() { curl -s -m 30 -u "${SNOW_USER}:${SNOW_PASSWORD}" -H 'Accept: application/json' -H 'Content-Type: application/json' "$@"; }
BR2_SW=10.100.0.166
DRIFT="verify-$(echo "$ts" | tr "A-Z" "a-z")"
INC_SYS=""; INC_NUM=""
start_job() { iap -X POST "${PLATFORM}/operations-manager/jobs/start" -d "{\"workflow\":\"$1\",\"options\":{\"type\":\"automation\",\"description\":\"verify ${ts}\",\"variables\":$2}}" | ${PY} -c 'import sys,json;d=json.load(sys.stdin).get("data");print(d.get("_id","") if isinstance(d,dict) else "")'; }
job_status() { iap "${PLATFORM}/operations-manager/jobs/$1" | ${PY} -c 'import sys,json;print(json.load(sys.stdin)["data"]["status"])'; }
wait_job() { local st; for _ in $(seq 1 48); do st=$(job_status "$1"); case "$st" in complete) return 0;; error|canceled|cancelled) echo "job $1 ${st}"; return 1;; esac; sleep 5; done; echo "job $1 timeout (${st})"; return 1; }
# approve_push [job]: the Work Center card (ViewData 2a) of a running wf-config-push-v1 job finished through the API
approve_push() { local job=${1:-} i; for i in $(seq 1 24); do [ -n "$job" ] || job=$(iap "${PLATFORM}/operations-manager/jobs?limit=30" | ${PY} -c 'import sys,json;j=[x for x in json.load(sys.stdin).get("data") or [] if x.get("name")=="wf-config-push-v1" and x.get("status")=="running"];print(j[0]["_id"] if j else "")'); if [ -n "$job" ] && iap -X POST "${PLATFORM}/operations-manager/jobs/${job}/tasks/2a/finish" -d '{"taskData":{"finish_state":"success","variables":{}}}' -o /dev/null -w '%{http_code}' | grep -qx 200; then echo "$job"; return 0; fi; sleep 5; done; echo "no wf-config-push-v1 card to approve"; return 1; }
push() { local id; id=$(start_job wf-config-push-v1 "{\"device\":\"$1\",\"config\":\"$2\",\"reason\":\"$3\"}"); [ -n "$id" ] || { echo "wf-config-push-v1 did not start"; return 1; }; sleep 8; approve_push "$id" >/dev/null || return 1; wait_job "$id" || return 1; echo "$id"; }
running_hostname() { ${PY} verify/devcmd.py "$1" "show running-config | include ^hostname" 2>/dev/null | awk '/^hostname/{print $2}'; }
plan_id() { iap -X POST "${PLATFORM}/configuration_manager/search/compliance_plans" -d '{"name":"","options":{"start":0,"limit":100}}' | ${PY} -c "import sys,json;d=json.load(sys.stdin);print(next((x['id'] for x in d.get('plans',[]) if x.get('name')=='$1'),''))"; }
run_plan() {
  local inst batch st
  inst=$(iap -X POST "${PLATFORM}/configuration_manager/compliance_plans/run" -d "{\"planId\":\"$1\",\"options\":{}}" | ${PY} -c 'import sys,json;print(json.load(sys.stdin).get("instanceId",""))')
  [ -n "$inst" ] || { echo "plan run did not start"; return 1; }
  for _ in $(seq 1 60); do
    read -r st batch <<<"$(iap -X POST "${PLATFORM}/configuration_manager/search/compliance_plan_instances" -d "{\"searchParams\":{\"instanceId\":\"${inst}\"}}" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);p=d.get("plans") or [];print((p[0].get("jobStatus") or ""),(p[0].get("batchId") or "")) if p else print("","")')"
    [ "$st" = complete ] && { echo "$batch"; return 0; }; sleep 5
  done
  echo "plan instance ${inst} did not finish (${st})"; return 1
}
batch_issues() { iap "${PLATFORM}/configuration_manager/compliance_reports/batch/$1" | ${PY} -c '
import sys,json
for r in json.load(sys.stdin):
    t=r.get("totals",{}); iss=[" ".join(w["value"] for w in i["spec"]["words"]) for i in r.get("issues",[])]
    print(r["deviceName"], t.get("errors",0), t.get("warnings",0), t.get("passes",0), "|", "; ".join(iss))'; }
latest_batch() { iap -X POST "${PLATFORM}/configuration_manager/search/compliance_plan_instances" -d '{"searchParams":{"planName":"lab-baseline"}}' | ${PY} -c 'import sys,json;p=[x for x in (json.load(sys.stdin).get("plans") or []) if x.get("jobStatus")=="complete"];p.sort(key=lambda x:x.get("startTime") or x.get("created") or "");print(p[-1]["batchId"] if p else "")'; }
# a) netbox-sot: a count from NetBox
c8() {
  local want sid txt tools
  want=$(nb "${NETBOX_URL}/api/dcim/devices/?site=dc1&status=active&limit=1" | ${PY} -c 'import sys,json;print(json.load(sys.stdin)["count"])')
  sid=$(run_agent netbox-sot '{"request":"How many active devices does NetBox have in site dc1? Reply with the number only."}') || { echo "$sid"; return 1; }
  sid=${sid##*$'\n'}; txt=$(session_text "$sid"); tools=$(session_tools "$sid"); count_tokens "$sid"
  echo "$tools" | grep -q "dcim_devices_list" || { echo "no NetBox integration tool call: ${tools}"; return 1; }
  echo "$txt" | grep -qw "$want" || { echo "answer '${txt}' lacks ${want} (NetBox count)"; return 1; }
  echo "netbox-sot: dc1 has ${want} active devices (NetBox agrees); tools ${tools}"
}
check "S4d.5a netbox-sot counts the active devices of dc1 through the NetBox integration; equals the NetBox API" c8
# b) device-ops: live state through the parsed show command, equals direct SSH
c9() {
  local sid txt tools direct
  sid=$(run_agent device-ops '{"request":"What software version is running on br2-sw01? Reply with the version string only."}') || { echo "$sid"; return 1; }
  sid=${sid##*$'\n'}; txt=$(session_text "$sid"); tools=$(session_tools "$sid"); count_tokens "$sid"
  echo "$tools" | grep -qiE "wf-show-command|send.?command" || { echo "no device read tool in the session: ${tools}"; return 1; }
  echo "$tools" | grep -qiE "send.?config|config-push" && { echo "device-ops used a write tool: ${tools}"; return 1; }
  direct=$(${PY} verify/devcmd.py "$BR2_SW" "show version" 2>/dev/null | ${PY} -c 'import sys,re;m=re.search(r"Software image version:\s*(\S+)",sys.stdin.read());print(m.group(1) if m else "")')
  [ -n "$direct" ] || { echo "direct SSH gave no version"; return 1; }
  echo "$txt" | grep -q "$direct" || { echo "answer '${txt}' lacks ${direct} (direct SSH)"; return 1; }
  echo "device-ops: br2-sw01 runs ${direct} (direct SSH agrees); tools ${tools}"
}
check "S4d.5b device-ops reports br2-sw01's software version through the show-command workflow; equals direct SSH" c9
# c) compliance: runs the plan and reports it clean; the batch reports read directly agree
c10() {
  local sid txt tools batch bad
  sid=$(run_agent compliance '{"request":"Run the lab-baseline compliance plan now and tell me which devices have errors or warnings. If none, reply exactly: all compliant."}') || { echo "$sid"; return 1; }
  sid=${sid##*$'\n'}; txt=$(session_text "$sid"); tools=$(session_tools "$sid"); count_tokens "$sid"
  echo "$tools" | grep -qi "compliance-report" || { echo "the summarising workflow was not used: ${tools}"; return 1; }
  echo "$tools" | grep -q "getJSONComplianceReportsByBatch\|searchCompliancePlanInstances" && { echo "the raw report tools were used (token cost): ${tools}"; return 1; }
  batch=$(latest_batch); [ -n "$batch" ] || { echo "no complete lab-baseline instance"; return 1; }
  bad=$(batch_issues "$batch" | awk '$2+$3>0{print $1}' | tr '\n' ' ')
  if [ -z "$bad" ]; then echo "$txt" | grep -qi "all compliant" || { echo "reports are clean but the agent said: $(echo "$txt" | head -c 200)"; return 1; }; echo "compliance: lab-baseline clean on $(batch_issues "$batch" | grep -c .) devices; agent said 'all compliant'; tools ${tools}"
  else for d in $bad; do echo "$txt" | grep -q "$d" || { echo "reports flag ${bad}but the agent said: $(echo "$txt" | head -c 200)"; return 1; }; done; echo "compliance: reports flag ${bad}; the agent named them; tools ${tools}"; fi
}
check "S4d.5c compliance runs lab-baseline and reports the result; the batch reports read directly agree" c10
# d) diagnostics: an incident about a deliberate hostname drift; the work note names the drift and proposes the fix
c11() {
  local job sid txt tools note
  job=$(push br2-sw01 "hostname ${DRIFT}" "verify ${ts} S4d.5 deliberate drift") || { echo "$job"; return 1; }
  [ "$(running_hostname "$BR2_SW")" = "$DRIFT" ] || { echo "drift did not land on br2-sw01 (direct SSH)"; return 1; }
  read -r INC_SYS INC_NUM <<<"$(sn -X POST "$SN/incident" -d "{\"short_description\":\"br2-sw01 answers with the wrong hostname\",\"description\":\"The branch switch br2-sw01 shows a hostname that is not its NetBox name. Please diagnose and propose a fix. (verify ${ts})\",\"category\":\"network\"}" | ${PY} -c 'import sys,json;d=json.load(sys.stdin)["result"];print(d["sys_id"],d["number"])')"
  [ -n "$INC_NUM" ] || { echo "the PDI did not create the incident"; return 1; }
  sid=$(run_agent diagnostics "{\"request\":\"Diagnose incident ${INC_NUM}.\"}") || { echo "$sid"; return 1; }
  sid=${sid##*$'\n'}; txt=$(session_text "$sid"); tools=$(session_tools "$sid"); count_tokens "$sid"
  echo "$tools" | grep -q "listIncidents\|getIncident" || { echo "the ticket was not read: ${tools}"; return 1; }
  echo "$tools" | grep -qiE "wf-show-command|send.?command" || { echo "the device was not read: ${tools}"; return 1; }
  echo "$tools" | grep -q "updateIncident" || { echo "no work note written: ${tools}"; return 1; }
  echo "$tools" | grep -qiE "config-push|send.?config" && { echo "diagnostics touched a write tool: ${tools}"; return 1; }
  note=$(sn "$SN/incident/${INC_SYS}?sysparm_fields=comments_and_work_notes&sysparm_display_value=true" | ${PY} -c 'import sys,json;print(json.load(sys.stdin)["result"]["comments_and_work_notes"])')
  echo "$note" | grep -q "Proposed fix" || { echo "no 'Proposed fix' note on ${INC_NUM}: $(echo "$note" | head -c 200)"; return 1; }
  echo "$note" | grep -qi "hostname br2-sw01" || { echo "the note does not propose 'hostname br2-sw01': $(echo "$note" | head -c 300)"; return 1; }
  echo "$note" | grep -q "$DRIFT" || echo "note: the observed hostname ${DRIFT} is not quoted (accepted)"
  echo "diagnostics: ${INC_NUM} got the note '$(echo "$note" | grep -o 'Proposed fix[^\n]*' | head -c 160)'; tools ${tools}; push job ${job} made the drift"
}
check "S4d.5d diagnostics reads a verify-created incident about br2-sw01, checks the device and writes one 'Proposed fix' work note" c11
# e) remediation: the drift shows in a fresh compliance run; the agent starts the governed push; the verify approves; SSH confirms
c12() {
  local plan batch sid txt tools job
  plan=$(plan_id lab-baseline); [ -n "$plan" ] || { echo "plan lab-baseline missing"; return 1; }
  if [ "$(running_hostname "$BR2_SW")" != "$DRIFT" ]; then push br2-sw01 "hostname ${DRIFT}" "verify ${ts} S4d.5 deliberate drift" >/dev/null || { echo "could not push the drift"; return 1; }; fi
  batch=$(run_plan "$plan") || { echo "$batch"; return 1; }
  batch_issues "$batch" | grep -q "^br2-sw01 [1-9]" || { echo "the fresh run does not flag br2-sw01"; batch_issues "$batch" | grep br2; return 1; }
  # the agent's push tool blocks on the Work Center card, so the card is approved while the session runs (a human would)
  local aid state i
  aid=$(agent_id remediation); [ -n "$aid" ] || { echo "agent remediation not found"; return 1; }
  sid=$(iap -X POST "${PLATFORM}/agent-session-manager/sessions" -d "{\"agentDefinitionId\":\"${aid}\",\"inputs\":{\"request\":\"The last lab-baseline compliance run flags br2-sw01. Fix its hostname drift; its NetBox name is br2-sw01.\"}}" | ${PY} -c 'import sys,json;print(json.load(sys.stdin).get("sessionId",""))')
  [ -n "$sid" ] || { echo "session start failed for remediation"; return 1; }
  job=""
  for i in $(seq 1 72); do
    [ -z "$job" ] && job=$(iap "${PLATFORM}/operations-manager/jobs?limit=30" | ${PY} -c 'import sys,json;j=[x for x in json.load(sys.stdin).get("data") or [] if x.get("name")=="wf-config-push-v1" and x.get("status")=="running"];print(j[0]["_id"] if j else "")')
    if [ -n "$job" ] && [ "${approved:-}" != "$job" ]; then iap -X POST "${PLATFORM}/operations-manager/jobs/${job}/tasks/2a/finish" -d '{"taskData":{"finish_state":"success","variables":{}}}' -o /dev/null -w '%{http_code}' | grep -qx 200 && approved=$job; fi
    state=$(iap "${PLATFORM}/agent-session-manager/sessions/${sid}" | ${PY} -c 'import sys,json;print((json.load(sys.stdin).get("status") or "").lower())')
    case "$state" in complete|completed) break;; failed|error|canceled|cancelled) echo "session ${sid} ${state}"; return 1;; esac
    sleep 5
  done
  [ "$state" = complete ] || [ "$state" = completed ] || { echo "session ${sid} did not finish (${state})"; return 1; }
  txt=$(session_text "$sid"); tools=$(session_tools "$sid"); count_tokens "$sid"
  echo "$tools" | grep -qi "compliance-report" || { echo "the report was not read: ${tools}"; return 1; }
  echo "$tools" | grep -qi "config-push" || { echo "remediation did not start wf-config-push-v1: ${tools}; said: $(echo "$txt" | head -c 200)"; return 1; }
  echo "$tools" | grep -qiE "send.?config" && { echo "remediation used send-config: ${tools}"; return 1; }
  [ -n "$job" ] || { echo "no wf-config-push-v1 job appeared"; return 1; }
  wait_job "$job" || return 1
  iap "${PLATFORM}/operations-manager/jobs/${job}" | ${PY} -c 'import sys,json;v=json.load(sys.stdin)["data"]["variables"];assert v.get("device")=="br2-sw01" and v.get("config","").strip()=="hostname br2-sw01",v' || { echo "the push job carried the wrong lines"; return 1; }
  [ "$(running_hostname "$BR2_SW")" = br2-sw01 ] || { echo "hostname not restored (direct SSH)"; return 1; }
  echo "remediation: proposed 'hostname br2-sw01', push job ${job} approved here, br2-sw01 restored (direct SSH); tools ${tools}"
}
check "S4d.5e remediation fixes the br2-sw01 hostname drift only through wf-config-push-v1; the card approved here; direct SSH confirms" c12
# f) a local twin: netbox-sot-local on the in-lab Ollama model; response time and VM memory recorded
c13() {
  local sid txt t0 t1 used total
  t0=$(date +%s)
  sid=$(run_agent netbox-sot-local '{"request":"Which site is the device br2-sw01 in? Reply with the site slug only."}') || { echo "$sid"; return 1; }
  t1=$(date +%s); sid=${sid##*$'\n'}; txt=$(session_text "$sid"); count_tokens "$sid"
  echo "$txt" | grep -qi "br2" || { echo "local twin answer lacks br2: $(echo "$txt" | head -c 200)"; return 1; }
  read -r total used <<<"$($SSH "ubuntu@${IT_IP}" "free -m | awk '/^Mem:/{print \$2, \$3}'")"
  echo "netbox-sot-local (${OLLAMA_MODEL}) answered in $((t1-t0)) s; VM RAM used ${used}/${total} MB"
}
check "S4d.5f netbox-sot-local (ollama-lab ${OLLAMA_MODEL}) answers br2-sw01's site; response time and VM memory recorded" c13
# g) the fleet-wide read: wf-show-all-v1 parses one command on every lab device in one call (direct SSH agrees on one device per
#    vendor); the local generalist answers an all-devices question from it with the device count NetBox confirms
c14() {
  local id vars n_nb sid txt tools t0 t1
  n_nb=$(nb "${NETBOX_URL}/api/dcim/devices/?limit=1&status=active&has_primary_ip=true&platform=ios-xe&platform=eos" | ${PY} -c 'import sys,json;print(json.load(sys.stdin)["count"])')
  id=$(run_job wf-show-all-v1 '{"command":"show version"}') || { echo "$id"; return 1; }
  vars=$(job_vars "${id##*$'\n'}")
  echo "$vars" | ${PY} -c "
import sys,json
v=json.load(sys.stdin); r=v.get('results') or {}
assert v.get('devices_checked')==${n_nb}, ('devices_checked', v.get('devices_checked'), 'NetBox', ${n_nb})
assert not v.get('parser_errors'), ('parser errors', v.get('parser_errors'))
p={n:e.get('parser') for n,e in r.items()}
assert p.get('br1-wan01')=='genie' and p.get('br1-sw01')=='textfsm', p
assert '17.13.01a' in json.dumps(r['br1-wan01']['parsed']) and '4.33.1.1F' in json.dumps(r['br1-sw01']['parsed']), 'versions missing from the parsed results'
print('wf-show-all-v1: %d devices, parsers %s' % (len(r), sorted(set(p.values()))))" || return 1
  ${PY} verify/devcmd.py 10.100.0.146 "show version" 2>/dev/null | grep -q "17.13.01a" && ${PY} verify/devcmd.py 10.100.0.165 "show version" 2>/dev/null | grep -q "4.33.1.1F" || { echo "direct SSH disagrees"; return 1; }
  t0=$(date +%s)
  sid=$(run_agent lab-netops-local '{"request":"Run show version on all devices with the show-all tool and reply with the number of devices that answered, as a number only."}') || { echo "$sid"; return 1; }
  t1=$(date +%s); sid=${sid##*$'\n'}; txt=$(session_text "$sid"); tools=$(session_tools "$sid"); count_tokens "$sid"
  echo "$tools" | grep -q "wf-show-all-v1" || { echo "the local generalist did not use wf-show-all-v1: ${tools}"; return 1; }
  echo "$txt" | grep -qw "$n_nb" || { echo "answer '$(echo "$txt" | head -c 200)' lacks ${n_nb}"; return 1; }
  echo "lab-netops-local counted ${n_nb} devices from one wf-show-all-v1 call in $((t1-t0)) s; tools ${tools}"
}
check "S4d.5g wf-show-all-v1 parses 'show version' on every lab device in one call (direct SSH agrees); lab-netops-local answers an all-devices question from it" c14

# cleanup: close the verify's incident; restore the hostname if a failed run left the drift behind
if [ -n "$INC_SYS" ]; then sn -X PATCH "$SN/incident/${INC_SYS}" -d '{"state":"7","close_code":"Solution provided","close_notes":"closed by verify/test-06-flowai.sh"}' -o /dev/null; echo "closed ${INC_NUM}"; fi
[ "$(running_hostname "$BR2_SW")" = br2-sw01 ] || { echo "restoring br2-sw01 after a failed run"; push br2-sw01 "hostname br2-sw01" "verify ${ts} cleanup" >/dev/null 2>&1 || echo "WARN br2-sw01 still drifted; fix with wf-config-push-v1"; }


echo
echo "tokens spent this run: in=${tokens_in} out=${tokens_out} (Anthropic + local)"
echo "passed=${pass} failed=${fail}"
[ "$fail" = 0 ]
