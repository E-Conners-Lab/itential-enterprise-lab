#!/usr/bin/env bash
# Phase 8 verification: the production Itential environment in Itential's HA2 shape (PID S11, ADR 0053).
# Intent: itential/ha2/versions.yaml (topology), itential/versions.yaml (images and the assets phases 5-7 built),
# topology/ipam.yaml, docs/resource-budget.md.
# State: the Proxmox API, NetBox, mongosh and redis-cli inside the containers over SSH, the Platform API on each
# node and through the load balancer, and Prometheus (the official dashboard's series). Second sources: the
# Proxmox API against the budget, each database's own view of its cluster, and a login on one node replayed
# against the other.
# Writes: nothing outside a Platform login and, with VERIFY_DRILLS=1, the three failover drills (which stop and
# start containers on the production VMs). No agent sessions: nothing here spends Anthropic tokens (ADR 0049).
set -uo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "missing .env"; exit 1; }
set -a; . ./.env; set +a
: "${PROXMOX_VE_ENDPOINT:?}" "${PROXMOX_VE_API_TOKEN:?}" "${NETBOX_URL:?}" "${NETBOX_TOKEN:?}" "${ITENTIAL_ADMIN_PASSWORD:?}"
PY=.venv/bin/python
CA=docs/lab-root-ca.crt
V=itential/ha2/versions.yaml
IV=itential/versions.yaml
# the production environment has no LDAP yet (the identity phase adds it): the local account of the oracle
ADMIN_USER=$(.venv/bin/python -c "import yaml;print(yaml.safe_load(open('itential/ha2/versions.yaml'))['platform']['admin_user'])")
SSH="ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"
ts=$(date -u +%Y%m%dT%H%M%SZ)
fail=0; pass=0; deferred=0
ok()    { echo "PASS  $1"; pass=$((pass+1)); }
bad()   { echo "FAIL  $1"; fail=$((fail+1)); }
defer() { echo "DEFER $1"; deferred=$((deferred+1)); }
skip()  { echo "SKIP  $1"; }
# ONLY="S11.2 S11.3" runs a subset while iterating
check() { local name=$1; shift; if [ -n "${ONLY:-}" ] && ! echo " ${ONLY} " | grep -q " ${name%% *} "; then skip "$name"; return; fi; if "$@" >/tmp/verify08.$$ 2>&1; then ok "$name"; sed 's/^/      /' /tmp/verify08.$$; else bad "$name"; sed 's/^/      /' /tmp/verify08.$$ | head -25; fi; }
pve()   { curl -sk -m 20 -H "Authorization: PVEAPIToken=${PROXMOX_VE_API_TOKEN}" "${PROXMOX_VE_ENDPOINT%/}/api2/json/$1"; }
nb()    { curl -s -m 20 -H "Authorization: Token ${NETBOX_TOKEN}" "$@"; }
web()   { curl -s -m 30 --cacert "$CA" "$@"; }
val()   { ${PY} -c "import yaml,sys;d=yaml.safe_load(open('$V'));print(eval(sys.argv[1],{'d':d}))" "$1"; }
host()  { val "next(v['ip'] for v in d['vms'] if v['name']=='$1')"; }
JAR=$(mktemp); trap 'rm -f "$JAR" "$JAR".* /tmp/verify08.$$ /tmp/verify08.*.$$' EXIT
mkdir -p verify/results; exec > >(tee "verify/results/${ts}-08-platform-ha2.log") 2>&1
echo "# test-08-platform-ha2 ${ts}"
[ -s "$CA" ] || { bad "$CA missing"; echo; echo "passed=0 failed=1"; exit 1; }

LB=$(host iap-lb); IAP1=$(host iap-01); IAP2=$(host iap-02)
MONGOS=$(val "[v['name'] for v in d['vms'] if v['role']=='mongodb']" | tr -d "[]',")
REDISES=$(val "[v['name'] for v in d['vms'] if v['role']=='redis']" | tr -d "[]',")
RS=$(val "d['mongodb']['replica_set']"); MASTER_NAME=$(val "d['redis']['master_name']")
SERVICE=$(val "d['service_name']").$(val "d['domain']")
# the Platform API on one node (direct) or through the load balancer (SERVICE)
iap()   { curl -s -m 120 --cacert "$CA" -b "$JAR" -H "Content-Type: application/json" "$@"; }
login_at() { local url=$1 jar=$2; curl -s -m 60 --cacert "$CA" -c "$jar" -H "Content-Type: application/json" -X POST "${url}/login" -d "{\"username\":\"${ADMIN_USER}\",\"password\":\"${ITENTIAL_ADMIN_PASSWORD}\"}" -o /dev/null -w '%{http_code}'; }
# mongosh / redis-cli run inside the containers on their VMs (no client needed on the workstation)
mongosh_on() { $SSH "ubuntu@$(host "$1")" "sudo docker exec mongodb mongosh --quiet --tls --tlsCAFile /etc/mongo/tls/ca.crt --host $1.$(val "d['domain']") -u admin -p '${MONGO_ADMIN_PASSWORD:-}' --authenticationDatabase admin --eval \"$2\"" 2>/dev/null; }
redis_on()   { $SSH "ubuntu@$(host "$1")" "sudo docker exec redis redis-cli --user monitor --pass '${REDIS_MONITOR_PASSWORD:-}' --no-auth-warning $2" 2>/dev/null; }
sentinel_on() { $SSH "ubuntu@$(host "$1")" "sudo docker exec sentinel redis-cli -p $(val "d['redis']['sentinel_port']") --user sentineluser --pass '${REDIS_SENTINEL_PASSWORD:-}' --no-auth-warning $2" 2>/dev/null; }

# --- S11.1 the eleven VMs exist with the budgeted size and their NetBox records ---------------------------
c1() {
  local node; node=$(pve nodes | ${PY} -c "import sys,json;print(json.load(sys.stdin)['data'][0]['node'])") || return 1
  pve "nodes/${node}/qemu" > /tmp/verify08.qemu.$$ || return 1
  nb "${NETBOX_URL}/api/virtualization/virtual-machines/?limit=200" > /tmp/verify08.nb.$$ || return 1
  ${PY} - /tmp/verify08.qemu.$$ /tmp/verify08.nb.$$ <<'PY' || return 1
import json, re, sys, yaml
ha2 = yaml.safe_load(open("itential/ha2/versions.yaml"))
budget = open("docs/resource-budget.md").read()
host = {v["name"]: v for v in json.load(open(sys.argv[1]))["data"]}
nbvms = {v["name"]: v for v in json.load(open(sys.argv[2]))["results"]}
errs = []
for v in ha2["vms"]:
    n = v["name"]
    h = host.get(n)
    if not h:
        errs.append(f"{n}: not on the Proxmox host"); continue
    if h["status"] != "running": errs.append(f"{n}: {h['status']}")
    got = (h["cpus"], round(h["maxmem"] / 2**30), round(h["maxdisk"] / 2**30))
    want = (v["cores"], v["memory_mb"] // 1024, v["disk_gb"])
    if got != want: errs.append(f"{n}: host {got} vs oracle {want}")
    if int(h["vmid"]) != v["vm_id"]: errs.append(f"{n}: vmid {h['vmid']} vs oracle {v['vm_id']}")
    m = re.search(r"\| `%s` \| 8 \| [^|]+ \| (\d+) \| (\d+) \| (\d+) \|" % re.escape(n), budget)
    if not m or (int(m.group(1)), int(m.group(2)), int(m.group(3))) != want:
        errs.append(f"{n}: budget row {m.groups() if m else None} vs oracle {want}")
    nbv = nbvms.get(n)
    if not nbv: errs.append(f"{n}: not registered in NetBox")
    elif (nbv.get("primary_ip4") or {}).get("address", "").split("/")[0] != v["ip"]:
        errs.append(f"{n}: NetBox address {(nbv.get('primary_ip4') or {}).get('address')} vs {v['ip']}")
if errs: print("\n".join(errs)); sys.exit(1)
print(f"{len(ha2['vms'])} VMs running with the budgeted vCPU/RAM/disk and their NetBox records")
PY
}
check "S11.1 the eleven production VMs run with the sizes of the oracle and the budget, and NetBox holds each one" c1

# --- S11.2 MongoDB replica set: one PRIMARY, two SECONDARY, auth required, TLS ------------------------------
c2() {
  # a ${VAR:?} expansion would exit the whole script (set -u), so missing secrets fail this check alone
  [ -n "${MONGO_ADMIN_PASSWORD:-}" ] || { echo "MONGO_ADMIN_PASSWORD missing from .env"; return 1; }
  local first; first=$(echo "$MONGOS" | awk '{print $1}')
  local st; st=$(mongosh_on "$first" "JSON.stringify(rs.status().members.map(m => [m.name, m.stateStr]))")
  echo "$st" | ${PY} -c '
import json, sys
raw = sys.stdin.read().strip()
if not raw.startswith("["): print("rs.status() failed:", raw[:200]); sys.exit(1)
members = json.loads(raw)
prim = [m for m in members if m[1] == "PRIMARY"]; sec = [m for m in members if m[1] == "SECONDARY"]
if len(members) != 3 or len(prim) != 1 or len(sec) != 2:
    print("replica set members:", members); sys.exit(1)
print("replica set:", ", ".join(f"{n} {s}" for n, s in members))' || return 1
  # authentication is enforced: an unauthenticated command must be refused
  local anon; anon=$($SSH "ubuntu@$(host "$first")" "sudo docker exec mongodb mongosh --quiet --tls --tlsCAFile /etc/mongo/tls/ca.crt --host ${first}.$(val "d['domain']") --eval 'db.adminCommand({listDatabases:1})'" 2>&1 | tr -d '\n')
  echo "$anon" | grep -qi "unauthorized\|requires authentication" || { echo "an unauthenticated client was not refused: ${anon:0:160}"; return 1; }
  echo "unauthenticated clients are refused"
  # the Platform's connection string names all three members and the replica set
  local url; url=$($SSH "ubuntu@${IAP1}" "sudo docker inspect platform --format '{{range .Config.Env}}{{println .}}{{end}}'" | sed -n 's/^ITENTIAL_MONGO_URL=//p')
  local n; n=$(echo "$url" | tr ',' '\n' | grep -c ":$(val "d['mongodb']['port']")")
  [ "$n" = 3 ] || { echo "the Platform's MongoDB URL names ${n} members: ${url//:*@/:***@}"; return 1; }
  echo "$url" | grep -q "replicaSet=${RS}" || { echo "the Platform's MongoDB URL has no replicaSet=${RS}"; return 1; }
  echo "the Platform connects to all three members of ${RS} with TLS and SCRAM"
}
check "S11.2 MongoDB is a three-member replica set (one PRIMARY, two SECONDARY) with authentication and TLS" c2

# --- S11.3 Redis: one master, two replicas, three Sentinels agreeing ----------------------------------------
c3() {
  [ -n "${REDIS_MONITOR_PASSWORD:-}" ] && [ -n "${REDIS_SENTINEL_PASSWORD:-}" ] || { echo "REDIS_MONITOR_PASSWORD / REDIS_SENTINEL_PASSWORD missing from .env"; return 1; }
  local masters=0 replicas=0 name role
  for name in $REDISES; do
    role=$(redis_on "$name" "info replication" | tr -d '\r' | awk -F: '/^role:/{print $2}')
    case "$role" in master) masters=$((masters+1));; slave) replicas=$((replicas+1));; *) echo "${name}: role '${role}'"; return 1;; esac
  done
  [ "$masters" = 1 ] && [ "$replicas" = 2 ] || { echo "roles: ${masters} master, ${replicas} replicas"; return 1; }
  echo "Redis: 1 master, 2 replicas"
  # every Sentinel monitors the master name and agrees on the same address
  local seen="" addr
  for name in $REDISES; do
    addr=$(sentinel_on "$name" "sentinel get-master-addr-by-name ${MASTER_NAME}" | tr -d '\r' | paste -sd: -)
    [ -n "$addr" ] || { echo "${name}: Sentinel does not know ${MASTER_NAME}"; return 1; }
    seen="${seen}${addr}\n"
  done
  [ "$(printf "$seen" | sort -u | wc -l | tr -d ' ')" = 1 ] || { echo "Sentinels disagree: $(printf "$seen" | tr '\n' ' ')"; return 1; }
  echo "three Sentinels agree the master of ${MASTER_NAME} is $(printf "$seen" | head -1)"
  # the default user is disabled: an unauthenticated PING must fail
  local anon; anon=$($SSH "ubuntu@$(host "$(echo "$REDISES" | awk '{print $1}')")" "sudo docker exec redis redis-cli ping" 2>&1 | tr -d '\r\n')
  echo "$anon" | grep -qi "NOAUTH\|denied" || { echo "an unauthenticated Redis client was not refused: ${anon:0:120}"; return 1; }
  echo "unauthenticated clients are refused (ACL users only)"
  # the Platform reaches Redis through Sentinel, not a fixed host
  $SSH "ubuntu@${IAP1}" "sudo docker inspect platform --format '{{range .Config.Env}}{{println .}}{{end}}'" | grep -q "^ITENTIAL_REDIS_SENTINELS=" || { echo "the Platform is not configured for Sentinel"; return 1; }
  echo "the Platform connects through Sentinel"
}
check "S11.3 Redis has one master, two replicas and three Sentinels that agree, with ACL users only" c3

# --- S11.4 the active Platform node serves; the standby is built and parked ---------------------------------
# ADR 0055 decision 9: Gateway Manager holds one connection per gateway cluster, and the Platform distributes
# job, task and agent execution across every running node, so a second RUNNING node breaks anything that
# touches a device. The environment is therefore Active/Standby - Itential's own shape: iap-02 is built,
# configured and attached to the same databases, and its Platform container is parked until a failover.
c4() {
  local code jar=${JAR}.active
  # /health/server is session-authenticated; /login is the unauthenticated liveness endpoint
  code=$(curl -s -m 60 --cacert "$CA" --resolve "${SERVICE}:3443:${IAP1}" -c "$jar" -H "Content-Type: application/json" -X POST "https://${SERVICE}:3443/login" -d "{\"username\":\"${ADMIN_USER}\",\"password\":\"${ITENTIAL_ADMIN_PASSWORD}\"}" -o /dev/null -w '%{http_code}')
  [ "$code" = 200 ] || { echo "the active node ${IAP1}: login ${code}"; return 1; }
  code=$(curl -s -m 30 --cacert "$CA" --resolve "${SERVICE}:3443:${IAP1}" -b "$jar" "https://${SERVICE}:3443/health/server" -o /tmp/verify08.h.$$ -w '%{http_code}')
  [ "$code" = 200 ] || { echo "the active node ${IAP1}: /health/server ${code}"; return 1; }
  ${PY} - "$IAP1" "/tmp/verify08.h.$$" <<'PYNODE'
import json, sys
d = json.load(open(sys.argv[2]))
print(f"  active node {sys.argv[1]}: Platform {d['version']} up {int(d['uptime'])}s")
PYNODE
  # the standby is built and parked: the container exists, is not running, and its host is otherwise up
  local state
  state=$($SSH "ubuntu@${IAP2}" "sudo docker inspect platform --format '{{.State.Status}}'" 2>/dev/null)
  [ -n "$state" ] || { echo "the standby ${IAP2} has no platform container: it is not built"; return 1; }
  [ "$state" != running ] || { echo "the standby ${IAP2} is RUNNING; ADR 0055 decision 9 parks it until a failover"; return 1; }
  $SSH "ubuntu@${IAP2}" "sudo docker inspect node-exporter --format '{{.State.Status}}'" 2>/dev/null | grep -qx running \
    || { echo "the standby ${IAP2} is not otherwise up (node-exporter is not running)"; return 1; }
  # the load balancer serves the service name with the lab CA, and the name resolves to it after the cut-over
  code=$(web "https://${SERVICE}/login" -o /dev/null -w '%{http_code}')
  [ "$code" = 200 ] || { echo "the load balancer answers ${code} for https://${SERVICE}/login"; return 1; }
  local resolved; resolved=$(${PY} -c "import socket;print(socket.gethostbyname('${SERVICE}'))")
  [ "$resolved" = "$LB" ] || { echo "${SERVICE} resolves to ${resolved}, not the load balancer ${LB} (cut-over not applied)"; return 1; }
  echo "the active node serves; ${SERVICE} resolves to the load balancer and answers through it; the standby is built and parked (platform ${state})"
}
check "S11.4 the active Platform node serves through the load balancer and the standby is built and parked" c4

# --- S11.5 everything phases 5-7 built exists on production -------------------------------------------------
# Every endpoint here is the one the phase 5-7 plays and verifies use, so a PASS means the same API that
# created the asset can see it; the device count is checked against NetBox rather than against a constant.
c5() {
  curl -s -m 60 --cacert "$CA" --resolve "${SERVICE}:443:${LB}" -c "$JAR" -H "Content-Type: application/json" -X POST "https://${SERVICE}/login" -d "{\"username\":\"${ADMIN_USER}\",\"password\":\"${ITENTIAL_ADMIN_PASSWORD}\"}" -o /dev/null -w '%{http_code}' | grep -qx 200 || { echo "login as ${ADMIN_USER} through the load balancer failed"; return 1; }
  P="https://${SERVICE}"
  api() { curl -s -m 120 --cacert "$CA" --resolve "${SERVICE}:443:${LB}" -b "$JAR" -H "Content-Type: application/json" "$@"; }
  local errs=0 have
  # every workflow itential/versions.yaml names (Automation Studio lists them all in one page)
  have=$(api "${P}/automation-studio/workflows?limit=200")
  local wf; for wf in $(${PY} -c "import yaml;print(' '.join(yaml.safe_load(open('$IV'))['workflows'].values()))"); do
    echo "$have" | grep -q "\"${wf}\"" || { echo "workflow ${wf} missing"; errs=1; }
  done
  # Golden Config trees and the compliance plan (Configuration Manager)
  have=$(api "${P}/configuration_manager/configs")
  local tree; for tree in $(${PY} -c "import yaml;print(' '.join(t['name'] for t in yaml.safe_load(open('$IV'))['golden_config']['trees'].values()))"); do
    echo "$have" | grep -q "\"${tree}\"" || { echo "golden config tree ${tree} missing"; errs=1; }
  done
  api -X POST "${P}/configuration_manager/search/compliance_plans" -d '{"name":"","options":{"start":0,"limit":100}}' \
    | grep -q "$(${PY} -c "import yaml;print(yaml.safe_load(open('$IV'))['golden_config']['plan'])")" || { echo "compliance plan missing"; errs=1; }
  # MOP command and analytic templates
  have=$(api "${P}/mop/listTemplates"; api "${P}/mop/listAnalyticTemplates")
  local mop; for mop in $(${PY} -c "import yaml;m=yaml.safe_load(open('$IV'))['mop']['templates'];print(' '.join(v for t in m.values() for v in t.values()))"); do
    echo "$have" | grep -q "\"${mop}\"" || { echo "MOP template ${mop} missing"; errs=1; }
  done
  # the Lifecycle Manager model, the form, the Integration Models and the agent project
  api "${P}/lifecycle-manager/resources?limit=100" | grep -q "$(${PY} -c "import yaml;print(yaml.safe_load(open('$IV'))['lcm']['model'])")" || { echo "LCM model missing"; errs=1; }
  api "${P}/json-forms/forms" | grep -q "$(${PY} -c "import yaml;print(yaml.safe_load(open('$IV'))['forms']['approval'])")" || { echo "approval form missing"; errs=1; }
  have=$(api "${P}/integrations?limit=100")
  local inst; for inst in $(${PY} -c "import yaml;d=yaml.safe_load(open('$IV'))['integrations']['models'];print(' '.join(m['instance'] for m in d.values()))"); do
    echo "$have" | grep -q "\"${inst}\"" || { echo "integration ${inst} missing"; errs=1; }
  done
  api "${P}/agent-project-service/projects?limit=50" | grep -q "$(${PY} -c "import yaml;print(yaml.safe_load(open('$IV'))['agents']['project'])")" || { echo "agent project missing"; errs=1; }
  # both Inventory Manager inventories
  have=$(api "${P}/inventory_manager/v1/inventories")
  local invy; for invy in $(${PY} -c "import yaml;s=yaml.safe_load(open('$IV'))['stack'];print(s['inventory'], s['host_inventory'])"); do
    echo "$have" | grep -q "\"${invy}\"" || { echo "inventory ${invy} missing"; errs=1; }
  done
  # a live device call through Gateway 5, not a cached read: it is the only thing that catches the netsdk
  # store holding the server's musl pex path while the execution lands on the glibc runner (ADR 0055)
  api -X POST "${P}/gateway_manager/v1/services/run" -d '{"serviceName":"send-command","clusterId":"lab","params":{"commands":["show version"]},"inventory":[{"inventory":"lab","nodeNames":["br1-sw01"]}]}' \
    | ${PY} -c 'import sys,json;d=json.load(sys.stdin);r=(d.get("result") or {}).get("results") or [];assert r and r[0].get("success"),json.dumps(d)[:300]' \
    || { echo "a live send-command through Gateway 5 failed"; errs=1; }
  # the devices Configuration Manager sees come through the Device Broker from the inventory, which comes from
  # NetBox: the second source is NetBox's own count of active network devices with a management address
  local devs want
  devs=$(api -X POST "${P}/configuration_manager/devices" -d '{"options":{"start":0,"limit":200}}' | ${PY} -c 'import sys,json;print(json.load(sys.stdin).get("total",0))')
  want=$(nb "${NETBOX_URL%/}/api/dcim/devices/?limit=0&status=active&has_primary_ip=true&platform=ios-xe&platform=eos" | ${PY} -c 'import sys,json;print(json.load(sys.stdin)["count"])')
  [ "${devs:-0}" -eq "${want:-0}" ] || { echo "Configuration Manager sees ${devs} devices, NetBox has ${want}"; errs=1; }
  [ "$errs" = 0 ] || return 1
  echo "every workflow, Golden Config tree, compliance plan, MOP template, LCM model, form, integration, agent project and inventory of phases 5-7 exists on production; ${devs} devices through the broker"
}
check "S11.5 everything phases 5-7 built exists on production (workflows, Golden Config, MOP, LCM, integrations, agents, inventories)" c5

# --- S11.7 the official dashboard's Redis and MongoDB rows show the replica sets -----------------------------
c7() {
  local PROM=https://prometheus.lab.internal
  local n
  n=$(web "${PROM}/api/v1/query" --data-urlencode 'query=count(mongodb_rs_members_state)' | ${PY} -c 'import sys,json;r=json.load(sys.stdin)["data"]["result"];print(r[0]["value"][1] if r else 0)')
  [ "${n:-0}" -ge 3 ] || { echo "mongodb_rs_members_state has ${n} series, expected at least 3 (the dashboard's replica-set panels)"; return 1; }
  n=$(web "${PROM}/api/v1/query" --data-urlencode 'query=count(mongodb_rs_members_health == 1)' | ${PY} -c 'import sys,json;r=json.load(sys.stdin)["data"]["result"];print(r[0]["value"][1] if r else 0)')
  [ "${n:-0}" -ge 3 ] || { echo "only ${n} healthy replica-set members in Prometheus"; return 1; }
  n=$(web "${PROM}/api/v1/query" --data-urlencode 'query=count(redis_instance_info{job="redis_exporter",role="master"})' | ${PY} -c 'import sys,json;r=json.load(sys.stdin)["data"]["result"];print(r[0]["value"][1] if r else 0)')
  [ "${n:-0}" = 1 ] || { echo "redis_instance_info role=master has ${n} series, expected exactly 1"; return 1; }
  n=$(web "${PROM}/api/v1/query" --data-urlencode 'query=count(redis_connected_slaves > 0)' | ${PY} -c 'import sys,json;r=json.load(sys.stdin)["data"]["result"];print(r[0]["value"][1] if r else 0)')
  [ "${n:-0}" -ge 1 ] || { echo "no Redis instance reports connected replicas"; return 1; }
  # the Platform nodes' own metrics: both scraped, both up
  # both Platform nodes are scrape targets, but only the active one answers: the standby's Platform is parked
  # (ADR 0055 decision 9), so exactly one iap_exporter target is up and the dashboard's Platform row is its own
  n=$(web "${PROM}/api/v1/query" --data-urlencode 'query=count(up{job="iap_exporter"} == 1)' | ${PY} -c 'import sys,json;r=json.load(sys.stdin)["data"]["result"];print(r[0]["value"][1] if r else 0)')
  [ "${n:-0}" = 1 ] || { echo "iap_exporter targets up: ${n}, expected 1 (the active Platform node; the standby is parked)"; return 1; }
  echo "the official dashboard has three replica-set members, one Redis master with replicas, and the active Platform node"
}
check "S11.7 the official Itential dashboard's MongoDB and Redis rows show the production replica sets" c7

# --- S11.8 the dev-stack is retired ---------------------------------------------------------------------------
# Owner-approved and done on 2026-09-10. Every trace of VM 205 is gone: the machine, its NetBox records, its
# Zabbix host, its DNS name and its address reservation - and the plan no longer lists .65 at all.
c8() {
  local errs=0 dev_ip=10.100.0.65
  pve "cluster/resources?type=vm" | ${PY} -c "import sys,json;d=json.load(sys.stdin)['data'];assert not [x for x in d if x.get('vmid')==205],'VM 205 still exists on the host'" \
    || { echo "VM 205 still exists in Proxmox"; errs=1; }
  nb "${NETBOX_URL%/}/api/virtualization/virtual-machines/?name=itential" | grep -q '"count":0' || { echo "NetBox still holds the itential VM"; errs=1; }
  nb "${NETBOX_URL%/}/api/ipam/ip-addresses/?address=${dev_ip}" | grep -q '"count":0' || { echo "NetBox still holds ${dev_ip}"; errs=1; }
  ${PY} -c "import yaml;d=yaml.safe_load(open('topology/ipam.yaml'));assert not [a for a in d['addresses'] if a['address']=='${dev_ip}'],'ipam still lists ${dev_ip}'" \
    || { echo "topology/ipam.yaml still reserves ${dev_ip}"; errs=1; }
  local r; for r in itential-dev itential; do
    local got; got=$(${PY} -c "import socket
try: print(socket.gethostbyname('${r}.lab.internal'))
except Exception: print('')" )
    [ "$got" = "$dev_ip" ] && { echo "${r}.lab.internal still resolves to ${dev_ip}"; errs=1; }
  done
  ping -c 1 -W 2000 "$dev_ip" >/dev/null 2>&1 && { echo "${dev_ip} still answers"; errs=1; }
  [ "$errs" = 0 ] || return 1
  echo "VM 205 is gone from Proxmox, NetBox, Zabbix, DNS and the IP plan; ${dev_ip} released and silent"
}
check "S11.8 the dev-stack VM 205 is retired and every trace of it removed" c8

# --- S11.6 failover drills (disruptive; VERIFY_DRILLS=1 only) -----------------------------------------------
if [ "${VERIFY_DRILLS:-0}" = 1 ]; then
  # A drill takes a database or a node away, and the Platform reconnects when it comes back - but its
  # adapters do not always survive the gap: after the Redis failover all four came up DEAD, so admin@itential
  # (the LDAP adapter) could not log in until the Platform was restarted. Every drill therefore ends by
  # waiting for the directory account to log in again, and restarts the active Platform once if it does not.
  # Anything else would leave the environment worse than it found it and blame the next check for it.
  settle() {
    local i code
    for i in $(seq 1 24); do
      code=$(curl -s -m 20 --cacert "$CA" --resolve "${SERVICE}:3443:${IAP1}" -H "Content-Type: application/json" -X POST "https://${SERVICE}:3443/login" -d "{\"username\":\"${ADMIN_USER}\",\"password\":\"${ITENTIAL_ADMIN_PASSWORD}\"}" -o /dev/null -w '%{http_code}')
      [ "$code" = 200 ] && return 0
      [ "$i" = 8 ] && $SSH "ubuntu@${IAP1}" "cd /opt/itential && sudo docker compose restart platform" >/dev/null 2>&1
      sleep 10
    done
    echo "after the drill ${ADMIN_USER} still cannot log in (the adapters may be DEAD): check /health/adapters on ${IAP1}"
    return 1
  }
  # 6a: stop one Platform node; the load balancer keeps serving
  # This is the failover the standby exists for (ADR 0055 decision 9): bring the standby up, take the active
  # node away, and the load balancer keeps serving from the standby. Restored to Active/Standby at the end.
  d6a() {
    $SSH "ubuntu@${IAP2}" "cd /opt/itential && sudo docker compose start platform" >/dev/null || return 1
    local i code ok=0
    for i in $(seq 1 30); do
      sleep 5
      code=$(curl -s -m 20 --cacert "$CA" --resolve "${SERVICE}:3443:${IAP2}" "https://${SERVICE}:3443/login" -o /dev/null -w '%{http_code}')
      [ "$code" = 200 ] && break
    done
    [ "$code" = 200 ] || { echo "the standby did not come up within 150 s"; return 1; }
    $SSH "ubuntu@${IAP1}" "sudo docker stop platform" >/dev/null || return 1
    for i in $(seq 1 12); do
      code=$(curl -s -m 20 --cacert "$CA" --resolve "${SERVICE}:443:${LB}" "https://${SERVICE}/login" -o /dev/null -w '%{http_code}')
      [ "$code" = 200 ] && { ok=$((ok+1)); }
      sleep 5
    done
    $SSH "ubuntu@${IAP1}" "sudo docker start platform" >/dev/null
    for i in $(seq 1 30); do
      sleep 5
      code=$(curl -s -m 20 --cacert "$CA" --resolve "${SERVICE}:3443:${IAP1}" "https://${SERVICE}:3443/login" -o /dev/null -w '%{http_code}')
      [ "$code" = 200 ] && break
    done
    # back to Active/Standby: the standby is parked again
    $SSH "ubuntu@${IAP2}" "cd /opt/itential && sudo docker compose stop platform" >/dev/null
    [ "$ok" -ge 10 ] || { echo "the service answered 200 only ${ok} of 12 times while the active node was down"; return 1; }
    settle || return 1
    echo "standby started, active node stopped: the load balancer kept serving (${ok}/12 probes 200); active node restarted and the standby parked again"
  }
  check "S11.6a drill: the standby takes over when the active Platform node is stopped, and is parked again after" d6a

  # 6b: stop the MongoDB primary; a new primary is elected and the Platform keeps serving
  d6b() {
    local first prim vm
    first=$(echo "$MONGOS" | awk '{print $1}')
    prim=$(mongosh_on "$first" "rs.status().members.filter(m => m.stateStr == 'PRIMARY')[0].name" | tr -d '"' | cut -d. -f1)
    [ -n "$prim" ] || { echo "no primary before the drill"; return 1; }
    echo "primary before: ${prim}"
    $SSH "ubuntu@$(host "$prim")" "sudo docker stop mongodb" >/dev/null || return 1
    local t0 i newp=""
    t0=$(date +%s)
    for i in $(seq 1 20); do
      sleep 3
      for vm in $MONGOS; do
        [ "$vm" = "$prim" ] && continue
        newp=$(mongosh_on "$vm" "try { rs.status().members.filter(m => m.stateStr == 'PRIMARY')[0].name } catch (e) { '' }" | tr -d '"' | cut -d. -f1)
        [ -n "$newp" ] && [ "$newp" != "$prim" ] && break 2
      done
      newp=""
    done
    local dt=$(( $(date +%s) - t0 ))
    $SSH "ubuntu@$(host "$prim")" "sudo docker start mongodb" >/dev/null
    [ -n "$newp" ] && [ "$dt" -le 30 ] || { echo "no new primary within 30 s (after ${dt}s: '${newp}')"; return 1; }
    local code; code=$(curl -s -m 30 --cacert "$CA" --resolve "${SERVICE}:443:${LB}" "https://${SERVICE}/login" -o /dev/null -w '%{http_code}')
    [ "$code" = 200 ] || { echo "the Platform answers ${code} after the election"; return 1; }
    settle || return 1
    echo "${prim} stopped: ${newp} became PRIMARY after ${dt}s and the Platform kept serving; ${prim} restarted"
  }
  check "S11.6b drill: stopping the MongoDB primary elects a new one within 30 s and the Platform keeps serving" d6b

  # 6c: stop the Redis master; Sentinel promotes a replica
  d6c() {
    local before after vm dt t0
    before=$(sentinel_on "$(echo "$REDISES" | awk '{print $1}')" "sentinel get-master-addr-by-name ${MASTER_NAME}" | tr -d '\r' | head -1)
    [ -n "$before" ] || { echo "Sentinel does not know the master"; return 1; }
    vm=$(val "next(v['name'] for v in d['vms'] if v['ip']=='${before}')" 2>/dev/null) || vm=""
    [ -n "$vm" ] || { echo "no VM in the oracle has the master address ${before}"; return 1; }
    echo "master before: ${vm} (${before})"
    $SSH "ubuntu@${before}" "sudo docker stop redis" >/dev/null || return 1
    t0=$(date +%s); after=""
    for _ in $(seq 1 30); do
      sleep 3
      after=$(sentinel_on "$(echo "$REDISES" | tr ' ' '\n' | grep -v "^${vm}$" | head -1)" "sentinel get-master-addr-by-name ${MASTER_NAME}" | tr -d '\r' | head -1)
      [ -n "$after" ] && [ "$after" != "$before" ] && break
      after=""
    done
    dt=$(( $(date +%s) - t0 ))
    $SSH "ubuntu@${before}" "sudo docker start redis" >/dev/null
    [ -n "$after" ] || { echo "Sentinel did not promote a replica within ${dt}s"; return 1; }
    local code; code=$(curl -s -m 30 --cacert "$CA" --resolve "${SERVICE}:443:${LB}" "https://${SERVICE}/login" -o /dev/null -w '%{http_code}')
    [ "$code" = 200 ] || { echo "the Platform answers ${code} after the failover"; return 1; }
    settle || return 1
    echo "${vm} stopped: Sentinel promoted ${after} after ${dt}s and the Platform kept serving; ${vm} restarted"
  }
  check "S11.6c drill: stopping the Redis master promotes a replica through Sentinel and the Platform keeps serving" d6c
else
  skip "S11.6 failover drills (set VERIFY_DRILLS=1 to run; they stop a Platform node, the MongoDB primary and the Redis master in turn)"
fi

echo; echo "passed=${pass} failed=${fail} deferred=${deferred}"
[ "$fail" -eq 0 ]
