#!/usr/bin/env bash
# Phase 6 verification: FlowAI agents over the lab topology (PID S4c criteria 1-6, ADR 0037).
# Intent: itential/versions.yaml (llm section), itential/agents/*.yaml. State: the Platform API
# over TLS (Model Registry, Agent Projects, Agent Session Manager), the devices over direct SSH
# (second source), NetBox, the VM (memory), the MCP server from this Mac. Spends provider tokens:
# every session's usage is printed and totalled at the end. Writes one NetBox VLAN + switch VLAN
# per run (removed at the end).
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
SSH="ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new"
ts=$(date -u +%Y%m%dT%H%M%SZ)
fail=0; pass=0; tokens_in=0; tokens_out=0
ok()    { echo "PASS  $1"; pass=$((pass+1)); }
bad()   { echo "FAIL  $1"; fail=$((fail+1)); }
check() { local name=$1; shift; if "$@" >/tmp/verify06.$$ 2>&1; then ok "$name"; sed 's/^/      /' /tmp/verify06.$$; else bad "$name"; sed 's/^/      /' /tmp/verify06.$$ | head -14; fi; }
nb()    { curl -s -m 20 -H "Authorization: Token ${NETBOX_TOKEN}" "$@"; }
JAR=$(mktemp); trap 'rm -f "$JAR" /tmp/verify06.$$' EXIT
iap()   { curl -s -m 120 --cacert "$CA" --resolve "${IT_HOST}:443:${IT_IP}" -b "$JAR" -H "Content-Type: application/json" "$@"; }
iap_login() { iap -c "$JAR" -X POST "${PLATFORM}/login" -d "{\"username\":\"${ADMIN_USER}\",\"password\":\"${ITENTIAL_ADMIN_PASSWORD}\"}" -o /dev/null -w '%{http_code}' | grep -qx 200; }
agent_id() { iap "${PLATFORM}/agent-project-service/operable-agents" | ${PY} -c "import sys,json;d=json.load(sys.stdin);a=[x for x in (d.get('data') or d.get('results') or d) if x.get('name')=='$1'];print(a[0]['_id'] if a else a[0]['id'] if a else '')"; }
# run_agent <agent name> <inputs json> -> prints the session id; waits for a terminal state
run_agent() {
  local name=$1 inputs=$2 aid sid state
  aid=$(agent_id "$name"); [ -n "$aid" ] || { echo "agent ${name} not found"; return 1; }
  sid=$(iap -X POST "${PLATFORM}/agent-session-manager/sessions" -d "{\"agentDefinitionId\":\"${aid}\",\"inputs\":${inputs}}" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);d=d.get("data",d);print(d.get("sessionId") or d.get("_id") or d.get("id") or "")')
  [ -n "$sid" ] || { echo "session start failed for ${name}"; return 1; }
  echo "$sid"
  for _ in $(seq 1 60); do
    state=$(iap "${PLATFORM}/agent-session-manager/sessions/${sid}" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);d=d.get("data",d);print((d.get("state") or d.get("status") or "").lower())')
    case "$state" in completed|complete|succeeded|success) return 0;; failed|error|canceled|cancelled) echo "session ${sid} ${state}"; return 1;; esac
    sleep 5
  done
  echo "session ${sid} did not finish (${state})"; return 1
}
# session_text <session id> -> the final assistant text; session_tools -> tool names called; session_usage -> "in out"
session_msgs() { iap "${PLATFORM}/agent-session-manager/sessions/$1/messages?limit=100&sortBy=eventId&sortOrder=asc"; }
session_text()  { session_msgs "$1" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);m=d.get("data") or d.get("messages") or d.get("results") or d;txt=[x for x in m if isinstance(x,dict) and (x.get("role")=="assistant" or x.get("type") in ("assistant","final","text"))];print(json.dumps(txt[-1])[:4000] if txt else json.dumps(m)[:4000])'; }
session_tools() { session_msgs "$1" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);s=json.dumps(d);import re;print(sorted(set(re.findall(r"\"(?:toolName|tool_name|name)\":\s*\"([A-Za-z0-9_.:-]+)\"",s))))'; }
session_usage() { session_msgs "$1" | ${PY} -c 'import sys,json,re;s=json.dumps(json.load(sys.stdin));i=sum(int(x) for x in re.findall(r"\"(?:input_tokens|inputTokens|prompt_tokens|promptTokens)\":\s*(\d+)",s));o=sum(int(x) for x in re.findall(r"\"(?:output_tokens|outputTokens|completion_tokens|completionTokens)\":\s*(\d+)",s));print(i,o)'; }
count_tokens() { read -r i o <<<"$(session_usage "$1")"; tokens_in=$((tokens_in+i)); tokens_out=$((tokens_out+o)); echo "tokens in=${i} out=${o}"; }
mkdir -p verify/results; exec > >(tee "verify/results/${ts}-06-flowai.log") 2>&1
echo "# test-06-flowai ${ts}"
[ -s "$CA" ] || { bad "$CA missing"; echo; echo "passed=0 failed=6"; exit 1; }
iap_login || { bad "login to ${PLATFORM} as ${ADMIN_USER}"; echo; echo "passed=0 failed=6"; exit 1; }
ANTHROPIC_MODEL=$(${PY} -c "import yaml;p={x['name']:x for x in yaml.safe_load(open('$V'))['llm']['profiles']};print(p['anthropic']['model'])")
OLLAMA_MODEL=$(${PY} -c "import yaml;p={x['name']:x for x in yaml.safe_load(open('$V'))['llm']['profiles']};print(p['ollama-lab']['model'])")

# --- S4c.1 two provider profiles answer; pinned models present --------------------------------
c1() {
  local profiles; profiles=$(iap "${PLATFORM}/model-registry-service/profiles")
  echo "$profiles" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);p=d.get("profiles") or d.get("data") or d;names={x["name"]:x for x in p};assert {"anthropic","ollama-lab"}<=set(names),list(names);print("profiles:",sorted(names))' || return 1
  local pid m
  for p in anthropic ollama-lab; do
    pid=$(echo "$profiles" | ${PY} -c "import sys,json;d=json.load(sys.stdin);p=d.get('profiles') or d.get('data') or d;print([x for x in p if x['name']=='$p'][0].get('_id') or [x for x in p if x['name']=='$p'][0].get('id'))")
    m=$([ "$p" = anthropic ] && echo "$ANTHROPIC_MODEL" || echo "$OLLAMA_MODEL")
    iap -X POST "${PLATFORM}/model-registry-service/providers/$([ "$p" = anthropic ] && echo anthropic || echo ollama)/fetch-models" -d "{\"profileId\":\"${pid}\"}" | ${PY} -c "import sys,json;d=json.load(sys.stdin);ms=d.get('models') or d.get('data') or d;ids=[x.get('id') or x.get('name') or x.get('modelId') for x in ms];assert '$m' in ids,('$m not in',ids[:20]);print('$p lists $m among',len(ids),'models')" || return 1
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
  sleep 5; iap -X POST "${PLATFORM}/operations-manager/jobs/${job}/tasks/${task}/finish" -d '{"taskData":{"finish_state":"success","variables":{}}}' -o /dev/null -w '%{http_code}' | grep -qx 200 && echo "approved job ${job}"
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
  echo "$txt" | grep -qiE "not (in|part of) the inventory|unknown (node|device)|cannot find|no such (node|device)|not found" || { echo "no refusal in: $(echo "$txt" | head -c 300)"; return 1; }
  ! ( echo "$tools" | grep -qiE "send.?command|send.?config" ) || { echo "gateway tool was called for an unknown node: ${tools}"; return 1; }
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
c6() {
  local url="http://mcp.lab.internal:8000/mcp" tools res
  tools=$(${PY} verify/mcpcall.py "$url" tools) || { echo "$tools"; return 1; }
  echo "$tools" | grep -qx get_agents || { echo "get_agents missing from MCP tools"; return 1; }
  res=$(${PY} verify/mcpcall.py "$url" call get_agents) || { echo "$res"; return 1; }
  echo "$res" | grep -q "lab-netops" || { echo "lab-netops not listed by MCP get_agents: $(echo "$res" | head -c 300)"; return 1; }
  res=$(${PY} verify/mcpcall.py "$url" call get_sessions '{"agent_name":"lab-netops"}') || { echo "$res"; return 1; }
  echo "$res" | grep -qiE "session|completed" || { echo "no sessions readable through MCP: $(echo "$res" | head -c 300)"; return 1; }
  echo "MCP lists lab-netops and its sessions from this Mac"
}
check "S4c.6 MCP from this Mac lists lab-netops and reads its sessions" c6

echo
echo "tokens spent this run: in=${tokens_in} out=${tokens_out} (Anthropic + local)"
echo "passed=${pass} failed=${fail}"
[ "$fail" = 0 ]
