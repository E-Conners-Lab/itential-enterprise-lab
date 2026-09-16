#!/usr/bin/env bash
# PID S12 verification (ADR 0063): the Copilot dev stack on itential-dev, and svc-copilot's read-only access to
# production. Intent: itential/versions.yaml (vm, dev), clab/versions.yaml (the device set),
# itential/copilot/roles.yaml (the role oracle), topology/enterprise.yaml (names that must not appear on dev),
# docs/resource-budget.md. State: DNS, the dev Platform API over the lab CA, the dev VM over SSH, NetBox's API,
# Gateway 5 through the dev Platform, the dev MCP server from this Mac, and - with PROD=1 - production's Platform.
#
# Writes: on dev only, one probe workflow `copilot-probe-<ts>` created and deleted by svc-copilot, and one
# `show version` on a clab switch. NetBox: one POST that must be refused (403) and so writes nothing.
# Production (PROD=1 only): logins, GETs, the Configuration Manager device search (a POST that reads), one
# DELETE of a workflow name that does not exist, sent as svc-copilot, which must be refused - so nothing on
# production can change even if the role set were wrong - and verify/prod-snapshot.py --compare (GET only).
#
# S12.7 and S12.8 need `make copilot-prod` first, so they run only with PROD=1. Run by `make verify-dev`, never by
# `make verify`: a torn-down sandbox must not turn the production verify red.
set -uo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "missing .env"; exit 1; }
set -a; . ./.env; set +a
: "${NETBOX_URL:?}" "${NETBOX_TOKEN:?}" "${NETBOX_DEV_RO_TOKEN:?}" "${ITENTIAL_ADMIN_PASSWORD:?}" "${SVC_COPILOT_DEV_PASSWORD:?}"
PY=.venv/bin/python
V=itential/versions.yaml
HV=itential/ha2/versions.yaml
CLAB=clab/versions.yaml
ROLES=itential/copilot/roles.yaml
CA=docs/lab-root-ca.crt
val()  { ${PY} -c "import yaml,sys;d=yaml.safe_load(open(sys.argv[1]));print(eval(sys.argv[2],{'d':d}))" "$1" "$2"; }
ADMIN_USER=${ITENTIAL_ADMIN_USER:-admin@itential}
DEV_IP=$(val "$V" "d['vm']['ip']")
DEV_HOST="$(val "$V" "d['dev']['hostname']").lab.internal"
MCP_DEV_HOST="$(val "$V" "d['dev']['mcp_alias']").lab.internal"
SVC_USER=$(val "$V" "d['dev']['copilot']['user']")
DEV_GROUP=$(val "$ROLES" "d['dev']['group']")
PROD_GROUP=$(val "$ROLES" "d['prod']['group']")
# production's names stay on production (S11): the load balancer and the tools VM of the HA2 oracle
PROD_HOST="$(val "$HV" "d['service_name']").$(val "$HV" "d['domain']")"
PROD_LB_IP=$(val "$HV" "next(v['ip'] for v in d['vms'] if v['role']=='loadbalancer')")
PROD_MCP_IP=$(val "$HV" "next(v['ip'] for v in d['vms'] if v['role']=='tools')")
PLATFORM_TAG=$(val "$V" "d['images']['platform']['tag']")
PLATFORM_VER=${PLATFORM_TAG%%-*}
DEV="https://${DEV_HOST}"
# PROD_URL, never PROD: PROD=1 is the gate for S12.7/S12.8 below, and assigning the URL to PROD would close it
PROD_URL="https://${PROD_HOST}"
SSH="ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new"
ts=$(date -u +%Y%m%dT%H%M%SZ)
fail=0; pass=0
ok()   { echo "PASS  $1"; pass=$((pass+1)); }
bad()  { echo "FAIL  $1"; fail=$((fail+1)); }
skip() { echo "SKIP  $1"; }
# ONLY="S12.3 S12.6" runs a subset while iterating
check() { local name=$1; shift; if [ -n "${ONLY:-}" ] && ! echo " ${ONLY} " | grep -q " ${name%% *} "; then skip "$name"; return; fi; if "$@" >/tmp/verify05b.$$ 2>&1; then ok "$name"; sed 's/^/      /' /tmp/verify05b.$$; else bad "$name"; sed 's/^/      /' /tmp/verify05b.$$ | head -25; fi; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP" /tmp/verify05b.$$' EXIT
# every Platform call goes through the lab CA and the real name: no -k anywhere
api()   { local jar=$1; shift; curl -s -m 120 --cacert "$CA" -b "$jar" -H "Content-Type: application/json" "$@"; }
# the credentials reach python through its environment, never its argv (argv is visible in ps)
login() { local base=$1 user=$2 pw=$3 jar=$4; LOGIN_USER="$user" LOGIN_PW="$pw" ${PY} -c 'import json,os;print(json.dumps({"username":os.environ["LOGIN_USER"],"password":os.environ["LOGIN_PW"]}))' \
            | curl -s -m 60 --cacert "$CA" -c "$jar" -H "Content-Type: application/json" -X POST "${base}/login" -d @- -o /dev/null -w '%{http_code}' | grep -qx 200; }
code()  { local jar=$1; shift; api "$jar" -o /dev/null -w '%{http_code}' "$@"; }
nb()    { curl -s -m 20 -H "Accept: application/json" "$@"; }
# whoami group names, whichever shape the Platform returns them in
groups_of() { api "$1" "$2/whoami" | ${PY} -c 'import sys,json;g=json.load(sys.stdin).get("groups",[]);print(" ".join(sorted(x["name"] if isinstance(x,dict) else str(x) for x in g)))'; }
mkdir -p verify/results; exec > >(tee "verify/results/${ts}-05b-dev-copilot.log") 2>&1
echo "# test-05b-dev-copilot ${ts} (PROD=${PROD:-0})"
[ -s "$CA" ] || { bad "$CA missing"; echo; echo "passed=0 failed=1"; exit 1; }
DJAR=$TMP/dev-admin; SJAR=$TMP/dev-svc
login "$DEV" "$ADMIN_USER" "$ITENTIAL_ADMIN_PASSWORD" "$DJAR" || bad "login to ${DEV} as ${ADMIN_USER} through the lab CA (S12.2-S12.5 will fail too)"

# --- S12.1 the dev VM matches the budget; dev names on .65, production names untouched ---------------------
c1() {
  local errs=0 want got
  want=$(${PY} - <<'PY'
import re, yaml
vm = yaml.safe_load(open("itential/versions.yaml"))["vm"]
row = next(l for l in open("docs/resource-budget.md") if re.match(r"\| \d+ `" + re.escape(vm["name"]) + "`", l))
cells = [c.strip() for c in row.split("|")[1:-1]]
budget = (int(cells[3]), int(cells[4]), int(cells[5]))
oracle = (vm["cores"], vm["memory_mb"] // 1024, vm["disk_gb"])
assert budget == oracle, f"budget {budget} vs versions.yaml {oracle}"
print(*oracle)
PY
) || { echo "$want"; return 1; }
  read -r w_cores w_ram w_disk <<<"$want"
  got=$($SSH "ubuntu@${DEV_IP}" "nproc; awk '/^MemTotal:/{print \$2}' /proc/meminfo; lsblk -bdno SIZE /dev/\$(lsblk -no PKNAME \$(findmnt -no SOURCE /) | head -1)") || { echo "ssh ubuntu@${DEV_IP} failed"; return 1; }
  ${PY} - "$w_cores" "$w_ram" "$w_disk" $got <<'PY' || errs=1
import sys
w_cores, w_ram, w_disk, cores, mem_kb, disk_b = (int(x) for x in sys.argv[1:7])
assert cores == w_cores, f"nproc {cores}, budget {w_cores}"
# MemTotal is a little under the configured RAM (kernel reservations): within 10 %
assert 0.9 * w_ram <= mem_kb / 1024**2 <= w_ram, f"MemTotal {mem_kb / 1024**2:.1f} GiB, budget {w_ram}"
assert round(disk_b / 1024**3) == w_disk, f"root disk {disk_b / 1024**3:.0f} GiB, budget {w_disk}"
print(f"VM: {cores} vCPU, {mem_kb / 1024**2:.1f} GiB, {disk_b / 1024**3:.0f} GiB = budget {w_cores}/{w_ram}/{w_disk}")
PY
  ${PY} - "$DEV_HOST" "$MCP_DEV_HOST" "$DEV_IP" "$PROD_HOST" "$PROD_LB_IP" "mcp.lab.internal" "$PROD_MCP_IP" <<'PY' || errs=1
import socket, sys
dev, mcp_dev, dev_ip, prod, prod_ip, mcp, mcp_ip = sys.argv[1:8]
want = {dev: dev_ip, mcp_dev: dev_ip, prod: prod_ip, mcp: mcp_ip}
got = {n: socket.gethostbyname(n) for n in want}
assert got == want, f"resolved {got}, expected {want}"
print("DNS:", ", ".join(f"{n} -> {a}" for n, a in got.items()))
PY
  return $errs
}
check "S12.1 itential-dev matches the budget over SSH; itential-dev and mcp-dev resolve to .65, itential and mcp still to production" c1

# --- S12.2 lab-CA TLS for itential-dev, the pinned version, Gateway 5 connected -----------------------------
c2() {
  local san ver conns
  san=$(openssl s_client -connect "${DEV_HOST}:443" -servername "$DEV_HOST" -CAfile "$CA" </dev/null 2>/dev/null | openssl x509 -noout -ext subjectAltName 2>/dev/null)
  echo "$san" | grep -q "DNS:${DEV_HOST}" || { echo "SAN missing ${DEV_HOST}: ${san}"; return 1; }
  echo "$san" | grep -qE "DNS:(itential|mcp)\.lab\.internal" && { echo "the dev certificate names a production host: ${san}"; return 1; }
  openssl s_client -connect "${DEV_HOST}:443" -servername "$DEV_HOST" -CAfile "$CA" </dev/null 2>/dev/null | grep -q "Verify return code: 0" || { echo "chain does not verify against ${CA}"; return 1; }
  ver=$(api "$DJAR" "${DEV}/health/server" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);print(d.get("release") or d.get("version") or d)')
  [ "$ver" = "$PLATFORM_VER" ] || { echo "dev reports ${ver}, versions.yaml pins ${PLATFORM_VER}"; return 1; }
  conns=$(api "$DJAR" "${DEV}/gateway_manager/v1/connections")
  echo "$conns" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);c=d.get("data",d);assert any(v for v in c.values()),"no Gateway 5 connection"' || { echo "$conns" | head -c 300; return 1; }
  echo "${DEV_HOST}: lab-CA chain, Platform ${ver}, Gateway 5 connected"
}
check "S12.2 ${DEV_HOST} serves a lab-CA certificate, runs Platform ${PLATFORM_VER}, and its Gateway 5 is connected" c2

# --- S12.3 dev devices are exactly the clab oracle; no EVE-NG name anywhere on dev ------------------------
c3() {
  local inv_names cm_names dg_names
  # node documents carry device passwords: only names leave the pipe
  inv_names=$(api "$DJAR" "${DEV}/inventory_manager/v1/inventories" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);r=d.get("result",d);r=r.get("data",r) if isinstance(r,dict) else r;print(" ".join(i["name"] for i in r))') || return 1
  : > "$TMP/inv"
  local inv; for inv in $inv_names; do
    api "$DJAR" "${DEV}/inventory_manager/v1/inventories/${inv}/nodes" | ${PY} -c 'import sys,json;inv=sys.argv[1];[print(inv, n["name"]) for n in json.load(sys.stdin)["result"]["data"]]' "$inv" >> "$TMP/inv" || return 1
  done
  cm_names=$(api "$DJAR" -X POST "${DEV}/configuration_manager/devices" -d '{"options":{"start":0,"limit":200}}' | ${PY} -c 'import sys,json;d=json.load(sys.stdin);print(" ".join(sorted(x["name"] for x in d.get("list",d.get("results",[])))))') || return 1
  dg_names=$(api "$DJAR" "${DEV}/configuration_manager/deviceGroups" | ${PY} -c 'import sys,json;print(" ".join(d for g in json.load(sys.stdin) for d in g.get("devices",[])))') || return 1
  ${PY} - "$TMP/inv" "$cm_names" "$dg_names" "$(val "$V" "d['stack']['inventory']")" <<'PY'
import sys, yaml
inv_file, cm, dg, lab = sys.argv[1], set(sys.argv[2].split()), set(sys.argv[3].split()), sys.argv[4]
clab = {n["name"] for n in yaml.safe_load(open("clab/versions.yaml"))["nodes"]}
eve = set(yaml.safe_load(open("topology/enterprise.yaml"))["nodes"])
rows = [line.split(" ", 1) for line in open(inv_file).read().splitlines()]
lab_nodes = {n for i, n in rows if i == lab}
assert lab_nodes == clab, f"inventory {lab}: {sorted(lab_nodes)} != clab oracle {sorted(clab)}"
assert cm == clab, f"Configuration Manager devices {sorted(cm)} != clab oracle {sorted(clab)}"
everywhere = {n for _, n in rows} | cm | dg
leaked = sorted(everywhere & eve)
assert not leaked, f"EVE-NG device names on dev: {leaked}"
print(f"inventory {lab} = Configuration Manager = {sorted(clab)}; {len(rows)} nodes across inventories, no EVE-NG name")
PY
}
check "S12.3 dev inventory and Configuration Manager hold exactly the clab/versions.yaml nodes; no topology/enterprise.yaml name on dev" c3

# --- S12.4 the dev NetBox credential is read-only ---------------------------------------------------------
c4() {
  local st wr
  # NetBox answers 403 for an invalid token too, so the token must first prove it authenticates
  st=$(nb -o /dev/null -w '%{http_code}' -H "Authorization: Token ${NETBOX_DEV_RO_TOKEN}" "${NETBOX_URL}/api/status/")
  [ "$st" = 200 ] || { echo "NETBOX_DEV_RO_TOKEN does not authenticate (GET /api/status/ ${st})"; return 1; }
  # an empty body: a token that could write would get 400 (validation) and still create nothing
  wr=$(nb -o /dev/null -w '%{http_code}' -X POST -H "Content-Type: application/json" -H "Authorization: Token ${NETBOX_DEV_RO_TOKEN}" -d '{}' "${NETBOX_URL}/api/ipam/vlan-groups/")
  [ "$wr" = 403 ] || { echo "POST /api/ipam/vlan-groups/ with the dev token answered ${wr}, not 403"; return 1; }
  # second source: NetBox's own record of the token (read with the full token)
  nb -H "Authorization: Token ${NETBOX_TOKEN}" "${NETBOX_URL}/api/users/tokens/?user=itential-dev&limit=50" | ${PY} -c '
import sys, json
r = json.load(sys.stdin)["results"]
assert r, "no token for itential-dev"
assert not any(t["write_enabled"] for t in r), [t["id"] for t in r if t["write_enabled"]]
assert all(t.get("description") for t in r), "an undescribed token would be deleted by netbox-token.yml"
print(f"{len(r)} itential-dev token(s), write_enabled false; GET 200, POST 403")' || return 1
  nb -H "Authorization: Token ${NETBOX_TOKEN}" "${NETBOX_URL}/api/users/users/?username=itential-dev" | ${PY} -c '
import sys, json
u = json.load(sys.stdin)["results"]
assert len(u) == 1 and not u[0].get("is_superuser") and not u[0].get("is_staff"), "itential-dev must be a plain user"'
}
check "S12.4 NETBOX_DEV_RO_TOKEN reads (200) and cannot write (403); NetBox records it write_enabled=false for plain user itential-dev" c4

# --- S12.5 local inference only -----------------------------------------------------------------------------
c5() {
  api "$DJAR" "${DEV}/model-registry-service/profiles" | ${PY} -c '
import sys, json
d = json.load(sys.stdin); p = d.get("profiles") or d.get("data") or d
names = sorted(x["name"] for x in p)
assert names == ["ollama-mac"], f"profiles {names}"
print("profiles:", names)' || return 1
  api "$DJAR" "${DEV}/agent-project-service/operable-agents" | ${PY} -c '
import sys, json
names = sorted(a["name"] for a in json.load(sys.stdin)["data"]["items"])
assert names, "no agents on dev"
other = [n for n in names if not n.endswith("-local")]
assert not other, f"agents that are not -local twins: {other}"
print("agents:", names)'
}
check "S12.5 dev Model Registry profiles are exactly [ollama-mac]; every agent is a -local twin" c5

# --- S12.6 svc-copilot builds on dev ---------------------------------------------------------------------
c6() {
  local groups probe resp node want st
  login "$DEV" "$SVC_USER" "$SVC_COPILOT_DEV_PASSWORD" "$SJAR" || { echo "login as ${SVC_USER} on dev failed"; return 1; }
  groups=$(groups_of "$SJAR" "$DEV") || return 1
  # copilot-builders-local only when tasks/ldap-service-account.yml had to take its measured fallback
  echo " ${groups} " | grep -qE " ${DEV_GROUP}(-local)? " || { echo "${SVC_USER} groups on dev: ${groups}"; return 1; }
  echo " ${groups} " | grep -q " admin_group " && { echo "${SVC_USER} is in admin_group on dev"; return 1; }
  probe="copilot-probe-$(echo "$ts" | tr 'A-Z' 'a-z')"
  resp=$(${PY} -c '
import json, sys
name = sys.argv[1]
wf = {"name": name, "type": "automation", "description": "S12.6 probe, deleted by the same run", "groups": [], "canvasVersion": 3,
      "tasks": {"workflow_start": {"name": "workflow_start", "summary": "workflow_start", "groups": [], "nodeLocation": {"x": -600, "y": 0}},
                "workflow_end": {"name": "workflow_end", "summary": "workflow_end", "groups": [], "nodeLocation": {"x": 600, "y": 0}}},
      "transitions": {"workflow_start": {"workflow_end": {"state": "success", "type": "standard"}}, "workflow_end": {}},
      "inputSchema": {"type": "object", "properties": {}, "required": []}, "outputSchema": {"type": "object", "properties": {}},
      "tags": [], "decorators": []}
print(json.dumps({"automations": [wf]}))' "$probe" | api "$SJAR" -X POST "${DEV}/automation-studio/automations/import" -d @-)
  echo "$resp" | ${PY} -c 'import sys,json;r=json.load(sys.stdin)["imported"];assert r and all(x["success"] for x in r),r' || { echo "import as ${SVC_USER}: $(echo "$resp" | head -c 300)"; return 1; }
  st=$(code "$SJAR" -X DELETE "${DEV}/workflow_builder/workflows/delete/${probe}")
  [ "$st" = 200 ] || { echo "delete ${probe} as ${SVC_USER} answered ${st}"; return 1; }
  api "$SJAR" "${DEV}/automation-studio/workflows?limit=200" | grep -q "\"${probe}\"" && { echo "${probe} still listed after delete"; return 1; }
  node=$(val "$CLAB" "next(n['name'] for n in d['nodes'] if n['kind']=='arista_veos')")
  want=$(val "$CLAB" "d['images']['veos']['version']")
  api "$SJAR" -X POST "${DEV}/gateway_manager/v1/services/run" \
    -d "{\"serviceName\":\"send-command\",\"clusterId\":\"$(val "$V" "d['stack']['gateway5_cluster_id']")\",\"params\":{\"commands\":[\"show version\"]},\"inventory\":[{\"inventory\":\"$(val "$V" "d['stack']['inventory']")\",\"nodeNames\":[\"${node}\"]}]}" \
    | ${PY} -c '
import sys, json
want = sys.argv[1]
d = json.load(sys.stdin); r = (d.get("result") or {}).get("results") or []
assert r and r[0].get("success"), json.dumps(d)[:300]
assert want in json.dumps(r[0]), f"show version lacks {want}"' "$want" || { echo "show version on ${node} as ${SVC_USER} failed"; return 1; }
  echo "${SVC_USER} in ${groups}; ${probe} created and deleted; ${node} show version = ${want} through Gateway 5"
}
check "S12.6 svc-copilot on dev: in copilot-builders, creates and deletes copilot-probe-<ts>, runs show version on a clab switch through Gateway 5" c6

# --- S12.7 / S12.8: production, after make copilot-prod -----------------------------------------------------
if [ "${PROD:-0}" = 1 ]; then
  : "${SVC_COPILOT_PROD_PASSWORD:?}"
  PJAR=$TMP/prod-svc; PAJAR=$TMP/prod-admin
  c7() {
    local groups st ap=${AUTOMATION_PASSWORD:-}
    # without it the credential scan below would pass vacuously
    [ "${#ap}" -ge 8 ] || { echo "AUTOMATION_PASSWORD missing from .env: the credential scan needs it"; return 1; }
    login "$PROD_URL" "$SVC_USER" "$SVC_COPILOT_PROD_PASSWORD" "$PJAR" || { echo "login as ${SVC_USER} on production failed"; return 1; }
    groups=$(groups_of "$PJAR" "$PROD_URL") || return 1
    # copilot-access.yml passes ldap_service_account_fallback: false, so production never holds a -local group
    [ "$groups" = "$PROD_GROUP" ] || { echo "${SVC_USER} groups on production: '${groups}', expected only ${PROD_GROUP}"; return 1; }
    # the group's roles, read as the administrator (svc-copilot holds no Authorization role), against the oracle
    login "$PROD_URL" "$ADMIN_USER" "$ITENTIAL_ADMIN_PASSWORD" "$PAJAR" || { echo "admin login on production failed"; return 1; }
    api "$PAJAR" "${PROD_URL}/authorization/groups?limit=100" > "$TMP/groups.json"
    : > "$TMP/roles.json"; local skip; for skip in 0 100 200 300 400; do api "$PAJAR" "${PROD_URL}/authorization/roles?limit=100&skip=${skip}" >> "$TMP/roles.json"; echo >> "$TMP/roles.json"; done
    ${PY} - "$TMP/groups.json" "$TMP/roles.json" "$groups" <<'PY' || return 1
import fnmatch, json, sys, yaml
groups = {g["name"]: g for g in json.load(open(sys.argv[1]))["results"]}
roles = {r["_id"]: r for line in open(sys.argv[2]) if line.strip() for r in json.loads(line)["results"]}
o = yaml.safe_load(open("itential/copilot/roles.yaml"))["prod"]
allowed = {f"{r['provenance']}/{r['name']}" for r in o["builtin"]}
customs = {c["name"] for c in o["custom"]}
held = [roles[a["roleId"]] for a in groups[sys.argv[3]].get("assignedRoles", [])]
keys = {f"{r['provenance']}/{r['name']}" for r in held}
extra = sorted(k for k in keys if k not in allowed and k.split("/", 1)[1] not in customs)
assert not extra, f"roles outside the oracle: {extra}"
deny = [f"{d['provenance']}/{d['name']}" for d in o["deny"]]
denied = sorted(k for k in keys if any(fnmatch.fnmatchcase(k, p) for p in deny))
assert not denied, f"denied roles held: {denied}"
print(f"{sys.argv[3]}: {len(keys)} roles, all in the oracle, none denied")
PY
    st=$(code "$PJAR" "${PROD_URL}/automation-studio/workflows?limit=1")
    [ "$st" = 200 ] || { echo "GET workflows as ${SVC_USER}: ${st}"; return 1; }
    # a name that does not exist: a wrongly granted delete answers 404 and still changes nothing
    st=$(code "$PJAR" -X DELETE "${PROD_URL}/workflow_builder/workflows/delete/copilot-probe-does-not-exist")
    case "$st" in 401|403) ;; *) echo "DELETE as ${SVC_USER} answered ${st}; only 401/403 proves the refusal"; return 1;; esac
    # the body would hold device passwords if this were allowed: it goes to a file that is never printed
    st=$(code "$PJAR" "${PROD_URL}/inventory_manager/v1/inventories/lab/nodes")
    case "$st" in 401|403) ;; *) echo "inventory nodes readable by ${SVC_USER} (HTTP ${st})"; return 1;; esac
    api "$PJAR" -X POST "${PROD_URL}/configuration_manager/devices" -d '{"options":{"start":0,"limit":200}}' > "$TMP/cm.json"
    api "$PJAR" "${PROD_URL}/gateway_manager/v1/gateways" > "$TMP/gw.json"
    api "$PJAR" "${PROD_URL}/adapters?limit=100" > "$TMP/adapters.json"
    api "$PJAR" "${PROD_URL}/integrations?limit=100" > "$TMP/integrations.json"
    local secret f leaked=0
    for secret in "${AUTOMATION_PASSWORD:-}" "${NETBOX_TOKEN:-}" "${SNOW_PASSWORD:-}" "${ANTHROPIC_API_KEY:-}"; do
      [ "${#secret}" -ge 8 ] || continue
      # the pattern comes from a file fed by printf (a builtin, so the value never appears in grep's argv)
      for f in cm gw adapters integrations; do grep -qF -f <(printf '%s\n' "$secret") -- "$TMP/${f}.json" && { echo "a .env credential appears in ${f} as ${SVC_USER} (value not shown)"; leaked=1; }; done
    done
    [ "$leaked" = 0 ] || return 1
    echo "${SVC_USER} in ${groups}: lists workflows, delete and inventory nodes refused, no .env credential in CM devices, gateways, adapters or integrations"
  }
  check "S12.7 svc-copilot on production: roles within the oracle, lists workflows, delete refused, no inventory nodes, no AUTOMATION_PASSWORD" c7

  c8() {
    # the documented additions, derived from the oracle rather than typed here: one role per custom role, and
    # the copilot group - never a -local twin, because production runs the task with its fallback disabled
    ${PY} -c '
import json, yaml
o = yaml.safe_load(open("itential/copilot/roles.yaml"))["prod"]
g = o["group"]
print(json.dumps({"delta": {"authorization::roles_total": len(o["custom"]), "authorization::groups_total": 1},
                  "added": [f"authorization::groups::{g}"]}))'> "$TMP/allow.json" || return 1
    ${PY} verify/prod-snapshot.py --compare --allow "$TMP/allow.json"
  }
  check "S12.8 production fingerprint equals the pre-build snapshot except the Copilot roles and group (prod-snapshot.py --compare --allow)" c8
else
  skip "S12.7 svc-copilot on production (PROD=1, after make copilot-prod)"
  skip "S12.8 production fingerprint against the pre-build snapshot (PROD=1, after make copilot-prod)"
fi

# --- S12.9 mcp-dev from this Mac ---------------------------------------------------------------------------
c9() {
  local url="http://${MCP_DEV_HOST}:8000/mcp" tools res
  ${PY} -c "import socket;assert socket.gethostbyname('${MCP_DEV_HOST}')=='${DEV_IP}','${MCP_DEV_HOST} resolves elsewhere'" || return 1
  tools=$(${PY} verify/mcpcall.py "$url" tools) || { echo "$tools"; return 1; }
  echo "$tools" | grep -qx get_health || { echo "get_health not in tools: $(echo "$tools" | tr '\n' ' ')"; return 1; }
  # ADR 0039 amendment: the tools that return node attributes (device passwords) stay hidden from MCP clients
  local hidden; for hidden in describe_inventory get_devices; do echo "$tools" | grep -qx "$hidden" && { echo "${hidden} is exposed on mcp-dev"; return 1; }; done
  res=$(${PY} verify/mcpcall.py "$url" call get_health) || { echo "$res"; return 1; }
  echo "$res" | grep -q "$PLATFORM_VER" || { echo "get_health did not return ${PLATFORM_VER}: $(echo "$res" | head -c 300)"; return 1; }
  echo "$(echo "$tools" | wc -l | tr -d ' ') tools on ${MCP_DEV_HOST}; get_health reports ${PLATFORM_VER}"
}
check "S12.9 mcp-dev from this Mac: get_health listed and returns ${PLATFORM_VER}; describe_inventory and get_devices hidden" c9

echo
echo "passed=${pass} failed=${fail}"
[ "$fail" = 0 ]
