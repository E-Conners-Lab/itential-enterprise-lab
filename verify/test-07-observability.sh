#!/usr/bin/env bash
# Phase 7 verification: observability (PID S7 criteria 1-4, 6, 7; S7.5 is asserted in phase 9, ADR 0050).
# Intent: k8s/observability/versions.yaml, observability/observability.yaml, observability/expiries.yaml, NetBox.
# State: the Zabbix API, the Prometheus/Alertmanager/Loki APIs and gNMIc's metrics through the VIPs (lab CA),
# kubectl, and independent second sources: snmpget from the workstation, systemctl over SSH, verify/devcmd.py,
# the NetBox token and the lab CA file, the Platform's own metrics API. Writes: two no-op wf-config-push-v1 jobs
# (S7.4, approved here as test-06b does) and, only with VERIFY_DRILLS=1, the br1-wan01 stop/start drill (S7.6).
# No agent sessions: nothing here spends Anthropic tokens (ADR 0049).
set -uo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "missing .env"; exit 1; }
set -a; . ./.env; set +a
: "${NETBOX_URL:?}" "${NETBOX_TOKEN:?}" "${AUTOMATION_PASSWORD:?}" "${ITENTIAL_ADMIN_PASSWORD:?}" "${ZABBIX_ADMIN_PASSWORD:?}" "${SNMPV3_AUTH_PASSWORD:?}" "${SNMPV3_PRIV_PASSWORD:?}"
export KUBECONFIG="${KUBECONFIG_PATH:-$HOME/.kube/lab-k3s.yaml}"
PY=.venv/bin/python
CA=docs/lab-root-ca.crt
V=k8s/observability/versions.yaml
OBS=observability/observability.yaml
NS=$(${PY} -c "import yaml;print(yaml.safe_load(open('$V'))['obs_namespace'])")
ZBX=https://zabbix.lab.internal/api_jsonrpc.php
PROM=https://prometheus.lab.internal
AM=https://alertmanager.lab.internal
LOKI=https://loki.lab.internal
GNMIC=https://gnmic.lab.internal/metrics
GRAFANA=https://grafana.lab.internal
IT_HOST=itential.lab.internal; PLATFORM="https://${IT_HOST}"
# the S11 cut-over moved the name onto the load balancer: no --resolve, the record decides
ADMIN_USER=${ITENTIAL_ADMIN_USER:-admin@itential}
SSH="ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"
ts=$(date -u +%Y%m%dT%H%M%SZ)
fail=0; pass=0; deferred=0
ok()    { echo "PASS  $1"; pass=$((pass+1)); }
bad()   { echo "FAIL  $1"; fail=$((fail+1)); }
defer() { echo "DEFER $1"; deferred=$((deferred+1)); }
skip()  { echo "SKIP  $1"; }
# ONLY="S7.1 S7.3" runs a subset while iterating
check() { local name=$1; shift; if [ -n "${ONLY:-}" ] && ! echo " ${ONLY} " | grep -q " ${name%% *} "; then skip "$name"; return; fi; if "$@" >/tmp/verify07.$$ 2>&1; then ok "$name"; sed 's/^/      /' /tmp/verify07.$$; else bad "$name"; sed 's/^/      /' /tmp/verify07.$$ | head -25; fi; }
nb()    { curl -s -m 20 -H "Authorization: Token ${NETBOX_TOKEN}" "$@"; }
web()   { curl -s -m 30 --cacert "$CA" "$@"; }
JAR=$(mktemp); trap 'rm -f "$JAR" /tmp/verify07.$$ /tmp/verify07.*.$$' EXIT
iap()   { curl -s -m 180 --cacert "$CA" -b "$JAR" -H "Content-Type: application/json" "$@"; }
iap_login() { iap -c "$JAR" -X POST "${PLATFORM}/login" -d "{\"username\":\"${ADMIN_USER}\",\"password\":\"${ITENTIAL_ADMIN_PASSWORD}\"}" -o /dev/null -w '%{http_code}' | grep -qx 200; }
mkdir -p verify/results; exec > >(tee "verify/results/${ts}-07-observability.log") 2>&1
echo "# test-07-observability ${ts}"
[ -s "$CA" ] || { bad "$CA missing"; echo; echo "passed=0 failed=1"; exit 1; }

# --- Zabbix JSON-RPC ---------------------------------------------------------------------------------
ZTOKEN=""
zbx() { # zbx <method> <params json> -> result json (stdout); exit 1 on an API error
  local auth=""; [ -n "$ZTOKEN" ] && auth="-H \"Authorization: Bearer ${ZTOKEN}\""
  local body; body=$(eval curl -s -m 60 --cacert "$CA" "$auth" -H '"Content-Type: application/json-rpc"' "$ZBX" -d "'{\"jsonrpc\":\"2.0\",\"method\":\"$1\",\"params\":$2,\"id\":1}'")
  echo "$body" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);
if "error" in d: print(json.dumps(d["error"]), file=sys.stderr); sys.exit(1)
print(json.dumps(d["result"]))'
}
zbx_login() { ZTOKEN=""; ZTOKEN=$(zbx user.login "{\"username\":\"Admin\",\"password\":\"${ZABBIX_ADMIN_PASSWORD}\"}" | tr -d '"'); [ -n "$ZTOKEN" ]; }
zbx_login || { bad "Zabbix API login at ${ZBX} (ZABBIX_ADMIN_PASSWORD)"; }

# expected monitored hosts from NetBox (ADR 0051 decision 12) + the two pre-existing machines
expected_hosts() {
  ${PY} - <<'PY'
import os, json, urllib.request, yaml
obs = yaml.safe_load(open("observability/observability.yaml"))["zabbix"]
h = {"Authorization": f"Token {os.environ['NETBOX_TOKEN']}"}
def get(p):
    return json.load(urllib.request.urlopen(urllib.request.Request(os.environ["NETBOX_URL"] + "/api/" + p, headers=h)))["results"]
rows = []
for d in get("dcim/devices/?status=active&limit=200"):
    plat = (d.get("platform") or {}).get("slug")
    if plat and plat not in obs["excluded_platforms"]:
        rows.append((d["name"], plat, d["primary_ip4"]["address"].split("/")[0], d["site"]["slug"]))
for v in get("virtualization/virtual-machines/?status=active&limit=200"):
    # ADR 0063: the Copilot sandbox's VMs are active (the inventory needs that) but deliberately unmonitored
    if (v.get("role") or {}).get("slug") in obs.get("excluded_vm_roles", []):
        continue
    rows.append((v["name"], (v.get("platform") or {}).get("slug"), v["primary_ip4"]["address"].split("/")[0], "vms"))
for e in obs["extra_hosts"]:
    rows.append((e["name"], e["platform"], e["address"], "vms"))
for r in sorted(rows): print(*r)
PY
}

# --- S7.1 Zabbix: every managed host monitored and green; the Expiries host ---------------------------
c1() {
  [ -n "$ZTOKEN" ] || return 1
  expected_hosts > /tmp/verify07.hosts.$$ || return 1
  local n_exp; n_exp=$(wc -l < /tmp/verify07.hosts.$$ | tr -d ' ')
  zbx host.get '{"output":["host","status"],"selectInterfaces":["ip","type","available","error"],"selectParentTemplates":["name"]}' > /tmp/verify07.zh.$$ || return 1
  ${PY} - /tmp/verify07.hosts.$$ /tmp/verify07.zh.$$ <<'PY' || return 1
import json, sys, yaml
obs = yaml.safe_load(open("observability/observability.yaml"))["zabbix"]
exp = {l.split()[0]: l.split() for l in open(sys.argv[1]) if l.strip()}
hosts = {h["host"]: h for h in json.load(open(sys.argv[2]))}
errs = []
for name, (_, plat, ip, site) in exp.items():
    h = hosts.get(name)
    if not h: errs.append(f"{name}: not in Zabbix"); continue
    if h["status"] != "0": errs.append(f"{name}: disabled")
    tpl = obs["templates"][plat]["lab"]
    if tpl not in {t["name"] for t in h["parentTemplates"]}: errs.append(f"{name}: template {tpl} not linked")
    for i in h["interfaces"]:
        if i["ip"] != ip: errs.append(f"{name}: interface {i['ip']} != NetBox {ip}")
        if i["available"] != "1": errs.append(f"{name}: interface type {i['type']} available={i['available']} {i.get('error','')}")
for s in obs["synthetic_hosts"]:
    if s["name"] not in hosts: errs.append(f"{s['name']}: synthetic host missing")
extra = set(hosts) - set(exp) - {s["name"] for s in obs["synthetic_hosts"]} - {"Zabbix server"}
if extra: errs.append(f"unexpected Zabbix hosts: {sorted(extra)}")
if errs: print("\n".join(errs)); sys.exit(1)
print(f"{len(exp)} NetBox-derived hosts monitored and available; synthetic: {[s['name'] for s in obs['synthetic_hosts']]}")
PY
  # no open problem from a lab trigger (LAB: prefix) on any host
  local probs; probs=$(zbx problem.get '{"output":["name"],"search":{"name":"LAB:"},"recent":false}') || return 1
  [ "$(echo "$probs" | ${PY} -c 'import sys,json;print(len(json.load(sys.stdin)))')" = 0 ] || { echo "open LAB problems: $probs"; return 1; }
  # second source 1: SNMPv3 from the workstation with the same credentials, one device per vendor
  local ip name
  for name in br1-wan01 dc1-spine01; do
    ip=$(awk -v n="$name" '$1==n{print $3}' /tmp/verify07.hosts.$$)
    snmpget -v3 -u zabbix -l authPriv -a SHA -A "$SNMPV3_AUTH_PASSWORD" -x AES -X "$SNMPV3_PRIV_PASSWORD" -t 5 -r 1 "$ip" 1.3.6.1.2.1.1.5.0 2>&1 | grep -q "$name" || { echo "snmpget sysName from $name ($ip) failed"; return 1; }
  done
  echo "snmpget sysName over SNMPv3 from the workstation: br1-wan01, dc1-spine01"
  # second source 2: the agent runs on every Ubuntu machine
  local plat user
  while read -r name plat ip site; do
    case "$plat" in ubuntu-*) ;; *) continue;; esac
    case "$name" in eve|netbox) user=root;; k3s-*|oob-gw|itential) user=ubuntu;; *) user=automation;; esac
    $SSH "${user}@${ip}" "systemctl is-active zabbix-agent2 && systemctl is-active rsyslog" 2>/dev/null | grep -cx active | grep -qx 2 || { echo "$name: zabbix-agent2 / rsyslog not active"; return 1; }
  done < /tmp/verify07.hosts.$$
  echo "zabbix-agent2 + rsyslog active on every Ubuntu machine (systemctl over SSH)"
}
check "S7.1 Zabbix monitors every NetBox-active host (+eve, netbox) green with the lab templates; SNMPv3 and the agents confirmed from the workstation" c1

c1b() {
  [ -n "$ZTOKEN" ] || return 1
  local hid; hid=$(zbx host.get '{"output":["hostid"],"filter":{"host":"expiries"}}' | ${PY} -c 'import sys,json;d=json.load(sys.stdin);print(d[0]["hostid"] if d else "")'); [ -n "$hid" ] || { echo "host expiries missing"; return 1; }
  zbx item.get "{\"output\":[\"key_\",\"lastvalue\",\"lastclock\",\"state\",\"error\"],\"hostids\":\"$hid\"}" > /tmp/verify07.items.$$ || return 1
  zbx trigger.get "{\"output\":[\"description\",\"expression\"],\"hostids\":\"$hid\",\"expandExpression\":true}" > /tmp/verify07.trig.$$ || return 1
  local tok; tok=$(nb "${NETBOX_URL}/api/users/tokens/?limit=50" | ${PY} -c 'import sys,json;print(max((t.get("expires") or "") for t in json.load(sys.stdin)["results"] if "phase 2" in (t.get("description") or ""))[:10])')
  local ca; ca=$(openssl x509 -in "$CA" -noout -enddate | sed 's/notAfter=//'); ca=$(${PY} -c "import datetime,sys;print(datetime.datetime.strptime(sys.argv[1].strip(),'%b %d %H:%M:%S %Y %Z').date())" "$ca")
  ${PY} - /tmp/verify07.items.$$ /tmp/verify07.trig.$$ "$tok" "$ca" <<'PY' || return 1
import datetime, json, sys, yaml
doc = yaml.safe_load(open("observability/expiries.yaml"))
items = {i["key_"]: i for i in json.load(open(sys.argv[1]))}
trig = json.load(open(sys.argv[2]))
now = datetime.datetime.now(datetime.UTC); errs = []
by_key = {e["key"]: e for e in doc["expiries"]}
if by_key["netbox-api-token"]["expires"] != sys.argv[3]: errs.append(f"NetBox token expires {sys.argv[3]}, YAML says {by_key['netbox-api-token']['expires']}")
if by_key["lab-root-ca"]["expires"] != sys.argv[4]: errs.append(f"lab CA notAfter {sys.argv[4]}, YAML says {by_key['lab-root-ca']['expires']}")
for e in doc["expiries"]:
    it = items.pop(f"expiry.days[{e['key']}]", None)
    if not it: errs.append(f"{e['key']}: no item"); continue
    if it["state"] != "0": errs.append(f"{e['key']}: item unsupported: {it['error']}"); continue
    # the item computes (expiry midnight UTC - now) once an hour; the verify recomputes it the same way
    want = (datetime.datetime.fromisoformat(e["expires"] + "T00:00:00+00:00") - now).total_seconds() / 86400
    got = float(it["lastvalue"]) if it["lastclock"] != "0" else None
    if got is None or abs(got - want) > 0.1: errs.append(f"{e['key']}: days left {got} vs {want:.2f}")
if items: errs.append(f"items without a YAML entry: {sorted(items)}")
want_t = {f"expiry.days[{e['key']}]" for e in doc["expiries"]}
import re
have_t = {m.group(1) for t in trig for m in [re.search(r"\(/[^/]+/([^)]+\])\)", t["expression"])] if m}
for t in trig:
    if f"<{doc['warn_days']}" not in t["expression"].replace(" ", ""): errs.append(f"trigger without <{doc['warn_days']}: {t['expression']}")
if want_t - have_t: errs.append(f"no trigger for {sorted(want_t - have_t)}")
if errs: print("\n".join(errs)); sys.exit(1)
print(f"{len(doc['expiries'])} expiry items agree with the YAML dates (within 0.1 d); {len(trig)} triggers at {doc['warn_days']} days; NetBox token and lab CA dates match the live sources")
PY
}
check "S7.1 Expiries host: one item per manifest expiry (days left agrees with the date, NetBox token and lab CA read back), trigger at 14 days" c1b

# --- S7.2 Prometheus: targets equal the declared jobs, none down ------------------------------------------
c2() {
  web "${PROM}/api/v1/targets?state=active" > /tmp/verify07.targets.$$ || return 1
  local n_nodes n_dev n_eos n_ios
  n_nodes=$(kubectl get nodes --no-headers | wc -l | tr -d ' ')
  n_dev=$(awk '$2=="ios-xe"||$2=="eos"' /tmp/verify07.hosts.$$ 2>/dev/null | wc -l | tr -d ' ')
  [ "$n_dev" -gt 0 ] || { expected_hosts > /tmp/verify07.hosts.$$; n_dev=$(awk '$2=="ios-xe"||$2=="eos"' /tmp/verify07.hosts.$$ | wc -l | tr -d ' '); }
  n_eos=$(awk '$2=="eos"' /tmp/verify07.hosts.$$ | wc -l | tr -d ' '); n_ios=$(awk '$2=="ios-xe"' /tmp/verify07.hosts.$$ | wc -l | tr -d ' ')
  ${PY} - /tmp/verify07.targets.$$ "$n_nodes" "$n_dev" "$n_eos" "$n_ios" <<'PY' || return 1
import collections, json, sys, yaml
obs = yaml.safe_load(open("observability/observability.yaml"))
ha2 = yaml.safe_load(open("itential/ha2/versions.yaml"))
nodes, dev, eos, ios = map(int, sys.argv[2:6])
rule = {"nodes": nodes, "devices": dev, "eos": eos, "ios-xe": ios, "web_checks": len(obs["web_checks"])}
# the official dashboard's exporters follow the production environment (ADR 0055): one per VM of that role
for role in ("platform", "redis", "mongodb"):
    rule[f"ha2-{role}"] = sum(1 for v in ha2["vms"] if v["role"] == role)
t = json.load(open(sys.argv[1]))["data"]["activeTargets"]
per = collections.defaultdict(list)
for x in t: per[x["labels"]["job"]].append(x)
errs = []
for j in obs["prometheus"]["jobs"]:
    want = j["count"] if isinstance(j["count"], int) else rule[j["count"]]
    want *= j.get("endpoints", 1)
    got = per.pop(j["job"], [])
    if len(got) != want: errs.append(f"{j['job']}: {len(got)} targets, declared {want}")
    down = [x["scrapeUrl"] + " " + x.get("lastError", "") for x in got if x["health"] != "up"]
    if down: errs.append(f"{j['job']}: down: {down}")
if per: errs.append(f"jobs not declared in git: {sorted(per)}")
if errs: print("\n".join(errs)); sys.exit(1)
print(f"{len(t)} active targets in {len(obs['prometheus']['jobs'])} declared jobs, all up")
PY
  # second source: the scrape objects in the cluster carry the same job names
  local objs; objs=$(kubectl -n "$NS" get servicemonitors,probes,scrapeconfigs -o name | wc -l | tr -d ' ')
  [ "$objs" -ge 10 ] || { echo "only $objs scrape objects in $NS"; return 1; }
  echo "$objs ServiceMonitor/Probe/ScrapeConfig objects in $NS"
  # lab alert rules loaded
  local rules; rules=$(web "${PROM}/api/v1/rules" | ${PY} -c 'import sys,json;print(sorted(r["name"] for g in json.load(sys.stdin)["data"]["groups"] for r in g["rules"] if g["name"].startswith("lab")))')
  for a in $(${PY} -c "import yaml;print(' '.join(a['name'] for a in yaml.safe_load(open('$OBS'))['prometheus']['alerts']))"); do echo "$rules" | grep -q "'$a'" || { echo "rule $a not loaded: $rules"; return 1; }; done
  echo "lab alert rules loaded: $rules"
}
check "S7.2 Prometheus target count per job equals observability.yaml (NetBox-sized); no target down; lab rules loaded" c2

c2b() {
  local dash; dash=$(web -u "admin:${GRAFANA_ADMIN_PASSWORD:?}" "${GRAFANA}/api/search?type=dash-db&tag=lab" | ${PY} -c 'import sys,json;print(sorted(d["title"] for d in json.load(sys.stdin)))')
  for d in $(${PY} -c "import yaml;print(' '.join(yaml.safe_load(open('$OBS'))['grafana']['dashboards']))"); do echo "$dash" | grep -qi "$(echo "$d" | tr '-' ' ')" || { echo "dashboard $d missing: $dash"; return 1; }; done
  local ds; ds=$(web -u "admin:${GRAFANA_ADMIN_PASSWORD}" "${GRAFANA}/api/datasources" | ${PY} -c 'import sys,json;print(sorted(d["type"] for d in json.load(sys.stdin)))')
  for t in prometheus alertmanager loki alexanderzobnin-zabbix-datasource; do echo "$ds" | grep -q "$t" || { echo "datasource $t missing: $ds"; return 1; }; done
  echo "dashboards $dash; datasources $ds"
}
check "S7.5-prep Grafana provisions the lab dashboards and the Prometheus/Alertmanager/Loki/Zabbix datasources (login via Keycloak: phase 9)" c2b

# --- S7.3 gNMIc BGP session count for dc1-spine01 equals show bgp summary ---------------------------------
c3() {
  local ip; ip=$(nb "${NETBOX_URL}/api/dcim/devices/?name=dc1-spine01" | ${PY} -c 'import sys,json;print(json.load(sys.stdin)["results"][0]["primary_ip4"]["address"].split("/")[0])')
  local live; live=$(${PY} verify/devcmd.py "$ip" "show bgp summary" | grep -c " Established ")
  [ "$live" -gt 0 ] || { echo "show bgp summary shows no Established session"; return 1; }
  local metrics; metrics=$(web "$GNMIC") || return 1
  local g; g=$(echo "$metrics" | grep "session_state" | grep 'source="dc1-spine01' | grep -c 'ESTABLISHED')
  [ "$g" = "$live" ] || { echo "gNMIc ESTABLISHED series for dc1-spine01: $g, show bgp summary: $live"; echo "$metrics" | grep session_state | grep dc1-spine01 | head -5; return 1; }
  # third source: through Prometheus
  local p; p=$(web "${PROM}/api/v1/query" --data-urlencode "query=count({__name__=~\"gnmic_bgp.*session_state\",source=~\"dc1-spine01.*\",session_state=\"ESTABLISHED\"})" | ${PY} -c 'import sys,json;r=json.load(sys.stdin)["data"]["result"];print(r[0]["value"][1] if r else 0)')
  [ "$p" = "$live" ] || { echo "Prometheus counts $p ESTABLISHED for dc1-spine01, device says $live"; return 1; }
  echo "dc1-spine01: $live Established sessions on the device, $g in gNMIc, $p in Prometheus"
}
check "S7.3 gNMIc BGP session count for dc1-spine01 equals show bgp summary (and Prometheus agrees)" c3

# --- S7.4 Loki: a syslog line from each vendor within 60 s of a config change ----------------------------
start_job() { iap -X POST "${PLATFORM}/operations-manager/jobs/start" -d "{\"workflow\":\"$1\",\"options\":{\"type\":\"automation\",\"description\":\"verify ${ts}\",\"variables\":$2}}" | ${PY} -c 'import sys,json;d=json.load(sys.stdin).get("data");print(d.get("_id","") if isinstance(d,dict) else "")'; }
job_status() { iap "${PLATFORM}/operations-manager/jobs/$1" | ${PY} -c 'import sys,json;print(json.load(sys.stdin)["data"]["status"])'; }
wait_job() { local st; for _ in $(seq 1 48); do st=$(job_status "$1"); case "$st" in complete) return 0;; error|canceled|cancelled) echo "job $1 ${st}"; return 1;; esac; sleep 5; done; echo "job $1 timeout (${st})"; return 1; }
approve_task() { local i; for i in $(seq 1 24); do if iap -X POST "${PLATFORM}/operations-manager/jobs/$1/tasks/$2/finish" -d '{"taskData":{"finish_state":"success","variables":{}}}' -o /dev/null -w '%{http_code}' | grep -qx 200; then return 0; fi; sleep 5; done; echo "approval of $1/$2 never accepted"; return 1; }
push() { local id; id=$(start_job wf-config-push-v1 "{\"device\":\"$1\",\"config\":\"$2\",\"reason\":\"$3\"}"); [ -n "$id" ] || { echo "wf-config-push-v1 did not start"; return 1; }; sleep 8; approve_task "$id" 2a || return 1; wait_job "$id" || return 1; echo "pushed to $1 (job ${id})"; }
loki_since() { # loki_since <host name> <start ns> <pattern> -> prints matching lines (label host parsed from the line by Alloy)
  web -G "${LOKI}/loki/api/v1/query_range" --data-urlencode "query={job=\"syslog-device\",host=\"$1\"} |~ \"$3\"" --data-urlencode "start=$2" --data-urlencode "limit=20" | ${PY} -c 'import sys,json;d=json.load(sys.stdin)["data"]["result"];[print(v[1][:160]) for s in d for v in s["values"]]'
}
c4() {
  iap_login || { echo "platform login failed"; return 1; }
  local start; start=$(date +%s)000000000
  local dev ip site line pat rc=0
  for dev in br1-wan01 dc1-spine01; do
    ip=$(nb "${NETBOX_URL}/api/dcim/devices/?name=${dev}" | ${PY} -c 'import sys,json;print(json.load(sys.stdin)["results"][0]["primary_ip4"]["address"].split("/")[0])')
    site=${dev%%-*}
    push "$dev" "snmp-server location ${site}" "verify ${ts} S7.4 no-op config change for syslog" || return 1
    pat="CONFIG"; local i found=""
    for i in $(seq 1 12); do found=$(loki_since "$dev" "$start" "$pat"); [ -n "$found" ] && break; sleep 5; done
    [ -n "$found" ] || { echo "$dev: no syslog line in Loki within 60 s of the push"; rc=1; continue; }
    echo "$dev -> Loki: $(echo "$found" | tail -1)"
    # second source: the device's own log buffer holds the same event after the push started
    case "$dev" in br1-wan01) line=$(${PY} verify/devcmd.py "$ip" "show logging | include CONFIG_I" | tail -1);; *) line=$(${PY} verify/devcmd.py "$ip" "show logging last 3 minutes | include CONFIG" | tail -1);; esac
    [ -n "$line" ] || { echo "$dev: show logging has no CONFIG line"; rc=1; }
    echo "$dev device log: ${line:0:120}"
  done
  return $rc
}
check "S7.4 Loki returns a syslog line from each vendor within 60 s of a governed no-op config push (device log agrees)" c4

# --- S7.7 Platform job/task metrics in Prometheus equal the Platform API ---------------------------------
c7() {
  iap_login || { echo "platform login failed"; return 1; }
  # the exporter refreshes every 60 s and S7.4 has just completed two jobs: give it one refresh
  local try; for try in 1 2 3; do c7_once && return 0; [ "$try" = 3 ] || { echo "(retry after the exporter's next refresh)"; sleep 45; }; done; return 1
}
c7_once() {
  iap "${PLATFORM}/workflow_engine/jobs/metrics?limit=200" > /tmp/verify07.jm.$$ || return 1
  iap "${PLATFORM}/health/applications" > /tmp/verify07.apps.$$; iap "${PLATFORM}/health/adapters" > /tmp/verify07.ad.$$
  web "${PROM}/api/v1/query" --data-urlencode 'query=itential_workflow_jobs_complete_total' > /tmp/verify07.pj.$$ || return 1
  web "${PROM}/api/v1/query" --data-urlencode 'query=min(itential_application_running)' > /tmp/verify07.pa.$$
  web "${PROM}/api/v1/query" --data-urlencode 'query=min(itential_adapter_online)' > /tmp/verify07.pd.$$
  # the Platform's own /prometheus_metrics route (docs.itential.com) scraped as job iap_exporter (ADR 0052)
  local native; native=$(web "${PROM}/api/v1/query" --data-urlencode 'query=iap_active_jobs{job="iap_exporter"}' | ${PY} -c 'import sys,json;r=json.load(sys.stdin)["data"]["result"];print(len(r))')
  [ "$native" = 1 ] || { echo "iap_active_jobs from the Platform's /prometheus_metrics route is not in Prometheus"; return 1; }
  ${PY} - /tmp/verify07.jm.$$ /tmp/verify07.pj.$$ /tmp/verify07.apps.$$ /tmp/verify07.ad.$$ /tmp/verify07.pa.$$ /tmp/verify07.pd.$$ <<'PY' || return 1
import json, sys
api = {r["workflow"]["name"]: r.get("jobsComplete", 0) for r in json.load(open(sys.argv[1]))["results"]}
prom = {r["metric"]["workflow"]: float(r["value"][1]) for r in json.load(open(sys.argv[2]))["data"]["result"]}
errs = [f"{w}: API {api[w]} vs Prometheus {prom.get(w)}" for w in api if prom.get(w) != api[w]]
apps = json.load(open(sys.argv[3]))["results"]; ads = json.load(open(sys.argv[4]))["results"]
notrun = [a["id"] for a in apps if a["state"] != "RUNNING"] + [a["id"] for a in ads if (a.get("connection") or {}).get("state") not in ("ONLINE", None) or a["state"] != "RUNNING"]
pa = json.load(open(sys.argv[5]))["data"]["result"]; pd = json.load(open(sys.argv[6]))["data"]["result"]
if not pa or pa[0]["value"][1] != "1": errs.append(f"min(itential_application_running) = {pa} (API not running: {notrun})")
if not pd or pd[0]["value"][1] != "1": errs.append(f"min(itential_adapter_online) = {pd} (API not online: {notrun})")
if errs: print("\n".join(errs)); sys.exit(1)
print(f"{len(api)} workflows: jobsComplete equal in the API and Prometheus; {len(apps)} applications running, {len(ads)} adapters online in both")
PY
}
check "S7.7 Prometheus holds the Platform job metrics (jobsComplete per workflow equals the API) and every application/adapter is up" c7

# --- S7.8 the official Itential Platform Monitoring dashboard (grafana.com 25527) has data for every family ----------
c8() {
  local uid; uid=$(${PY} -c "import yaml;print(yaml.safe_load(open('$V'))['vendored_dashboards']['itential-platform-monitoring']['uid'])")
  web -u "admin:${GRAFANA_ADMIN_PASSWORD:?}" "${GRAFANA}/api/dashboards/uid/${uid}" | ${PY} -c 'import sys,json;d=json.load(sys.stdin)["dashboard"];print("provisioned:", d["title"], "rows", [p["title"] for p in d["panels"] if p.get("type")=="row"])' || return 1
  # every metric family the dashboard queries (measured from the JSON, ADR 0052) except the replica-set family
  local fam rc=0
  for fam in 'up{job="iap_exporter"}' 'iap_active_sessions' 'itential_job_status_total' 'itential_job_start' 'itential_task_complete' 'itential_up' \
             'node_cpu_seconds_total{job="node_exporter"}' 'node_filesystem_avail_bytes{job="node_exporter"}' \
             'namedprocess_namegroup_num_procs{groupname=~"Pronghorn.* Application"}' 'namedprocess_namegroup_num_procs{groupname=~"Pronghorn.* Adapter"}' \
             'redis_up{job="redis_exporter"}' 'redis_instance_info{job="redis_exporter",role="master"}' 'redis_memory_used_bytes' \
             'mongodb_ss_connections' 'mongodb_ss_opcounters' 'mongodb_ss_wt_cache_bytes_currently_in_the_cache'; do
    local n; n=$(web "${PROM}/api/v1/query" --data-urlencode "query=count(${fam})" | ${PY} -c 'import sys,json;r=json.load(sys.stdin)["data"]["result"];print(r[0]["value"][1] if r else 0)')
    [ "${n:-0}" != 0 ] || { echo "no data: ${fam}"; rc=1; }
  done
  [ $rc = 0 ] || return 1
  # second source: the exporter's job counts equal the Platform's jobs collection (a few jobs may land between the two reads)
  local api prom
  api=$(iap "${PLATFORM}/operations-manager/jobs?include=status&limit=1" | ${PY} -c 'import sys,json;print(json.load(sys.stdin)["metadata"]["total"])')
  prom=$(web "${PROM}/api/v1/query" --data-urlencode 'query=itential_job_start' | ${PY} -c 'import sys,json;r=json.load(sys.stdin)["data"]["result"];print(int(float(r[0]["value"][1])) if r else 0)')
  [ $((api - prom)) -le 3 ] && [ $((api - prom)) -ge 0 ] || { echo "itential_job_start ${prom} vs jobs collection total ${api}"; return 1; }
  echo "every dashboard family has data (MongoDB replica-set family excluded: standalone dev-stack database); jobs total ${api} in the API, ${prom} in Prometheus"
}
check "S7.8 the official Itential Platform Monitoring dashboard (grafana.com 25527) is provisioned and every metric family it queries has data" c8

defer "S7.5 Grafana login via Keycloak and every dashboard renders with data: identity phase (ADR 0050)"

# --- S7.6 drill: stop br1-wan01 -> Zabbix problem + Alertmanager alert within 3 min; start -> both clear ----
if [ "${VERIFY_DRILLS:-0}" = 1 ]; then
  c6() {
    : "${EVE_HOST:?}" "${EVE_USERNAME:?}" "${EVE_PASSWORD:?}"
    local node; node=br1-wan01
    zbx_login || return 1
    local t0 zp am i
    ${PY} - <<'PY' || return 1
import sys; sys.path.insert(0, ".")
from eve.build import eve_from_env
eve = eve_from_env(); nid = eve.nodes()["br1-wan01"]["id"]; eve.stop(nid); print(f"stopped br1-wan01 (node {nid})")
PY
    t0=$(date +%s); zp=""; am=""
    for i in $(seq 1 36); do
      sleep 5
      [ -z "$zp" ] && zp=$(zbx problem.get '{"output":["name","clock"],"search":{"name":"LAB: br1-wan01"},"recent":false}' | ${PY} -c 'import sys,json;d=json.load(sys.stdin);print(d[0]["name"] if d else "")')
      [ -z "$am" ] && am=$(web "${AM}/api/v2/alerts?filter=alertname%3DLabDeviceDown&filter=device%3Dbr1-wan01&active=true" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);print(d[0]["labels"]["alertname"] if d else "")')
      [ -n "$zp" ] && [ -n "$am" ] && break
    done
    local dt=$(( $(date +%s) - t0 ))
    ${PY} - <<'PY'
import sys; sys.path.insert(0, ".")
from eve.build import eve_from_env
eve = eve_from_env(); eve.start(eve.nodes()["br1-wan01"]["id"]); print("started br1-wan01")
PY
    [ -n "$zp" ] && [ -n "$am" ] && [ "$dt" -le 180 ] || { echo "after ${dt}s: zabbix='${zp}' alertmanager='${am}' (need both within 180 s)"; return 1; }
    echo "both raised after ${dt}s: Zabbix '${zp}', Alertmanager '${am}'"
    for i in $(seq 1 72); do
      sleep 10
      zp=$(zbx problem.get '{"output":["name"],"search":{"name":"LAB: br1-wan01"},"recent":false}' | ${PY} -c 'import sys,json;print(len(json.load(sys.stdin)))')
      am=$(web "${AM}/api/v2/alerts?filter=alertname%3DLabDeviceDown&filter=device%3Dbr1-wan01&active=true" | ${PY} -c 'import sys,json;print(len(json.load(sys.stdin)))')
      [ "$zp" = 0 ] && [ "$am" = 0 ] && { echo "both cleared $(( $(date +%s) - t0 ))s after the stop (router rebooted)"; return 0; }
    done
    echo "not cleared within 12 min of the stop: zabbix problems=$zp alerts=$am"; return 1
  }
  check "S7.6 drill: stopping br1-wan01 raises a Zabbix trigger and an Alertmanager alert within 3 min; starting it clears both" c6
else
  skip "S7.6 drill (set VERIFY_DRILLS=1 to run; stops br1-wan01 for a few minutes)"
fi

echo; echo "passed=${pass} failed=${fail} deferred=${deferred}"
[ "$fail" -eq 0 ]
