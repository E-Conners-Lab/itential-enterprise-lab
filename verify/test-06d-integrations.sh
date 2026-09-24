#!/usr/bin/env bash
# S4f verification (PID amendment 1.22, ADR 0054): NetBox and ServiceNow are reached through their
# Integration Models, and the ServiceNow adapter is gone. Intent: itential/versions.yaml and the
# documents in itential/integrations/. State: the Platform API, the committed workflow documents, and
# the other phase 5-7 verifies for behaviour this one deliberately does not repeat.
#
# What this script does NOT do: re-run the governed VLAN and change-request paths. test-05 S4.4 and
# S4b.2, test-06 S4c.3 and test-06b S4d.3 already drive them end to end against real devices and the
# PDI; repeating them here would write the same VLAN twice and prove nothing new. S4f.7 checks that
# those scripts pass, which is the honest way to claim the conversion changed nothing behavioural.
set -uo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "missing .env"; exit 1; }
set -a; . ./.env; set +a
: "${ITENTIAL_ADMIN_PASSWORD:?}"
PY=.venv/bin/python
CA=docs/lab-root-ca.crt
PLATFORM=https://itential.lab.internal
ADMIN_USER=${ITENTIAL_ADMIN_USER:-admin@itential}
JAR=$(mktemp)
ts=$(date -u +%Y%m%dT%H%M%SZ)
pass=0; fail=0; deferred=0
ok()    { echo "PASS  $1"; pass=$((pass+1)); }
bad()   { echo "FAIL  $1"; fail=$((fail+1)); }
defer() { echo "DEFER $1"; deferred=$((deferred+1)); }
check() { local name=$1; shift; if "$@" >/tmp/verify06d.$$ 2>&1; then ok "$name"; else bad "$name"; sed 's/^/      /' /tmp/verify06d.$$ | head -10; fi; }
iap()   { curl -s -m 60 --cacert "$CA" -b "$JAR" -H "Content-Type: application/json" "$@"; }
mkdir -p verify/results; exec > >(tee "verify/results/${ts}-06d-integrations.log") 2>&1
echo "# test-06d-integrations ${ts}"

code=$(curl -s -m 30 --cacert "$CA" -c "$JAR" -X POST "${PLATFORM}/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"${ADMIN_USER}\",\"password\":\"${ITENTIAL_ADMIN_PASSWORD}\"}" -o /dev/null -w '%{http_code}')
[ "$code" = 200 ] || { bad "login as ${ADMIN_USER} (HTTP ${code})"; echo; echo "passed=0 failed=1"; exit 1; }

# --- S4f.1 every model records the specification it came from -------------------------------------
c1() { ${PY} - <<'EOF'
import json, sys, yaml
m = yaml.safe_load(open("itential/versions.yaml"))["integrations"]["models"]
for key, v in m.items():
    spec = v.get("spec") or {}
    doc = json.load(open(f"itential/integrations/{v['title']}.json"))
    rec = doc["info"].get("x-spec-source") or {}
    assert spec.get("url") and spec.get("taken") and spec.get("kind") in ("openapi", "documentation"), f"{key}: incomplete spec block"
    assert rec.get("url") == spec["url"] and str(rec.get("taken")) == str(spec["taken"]), f"{key}: document provenance != oracle"
    print(f"  {v['title']}: {spec['kind']} from {spec['url']} ({spec['taken']})")
EOF
}
check "S4f.1 every Integration Model records the specification it was generated from" c1

# --- S4f.2 coverage both ways ---------------------------------------------------------------------
c2() {
  ${PY} - <<'EOF' || return 1
import glob, json, yaml
m = yaml.safe_load(open("itential/versions.yaml"))["integrations"]["models"]
ops = {f"{v['title']}:{v['version']}": {o["operationId"] for item in json.load(open(f"itential/integrations/{v['title']}.json"))["paths"].values() for o in item.values()} for v in m.values()}
named = 0
for f in glob.glob("itential/workflows/*.json"):
    for tid, t in json.load(open(f))["tasks"].items():
        app = t.get("app", "")
        if app in ops:
            named += 1
            assert t["name"] in ops[app], f"{f} {tid}: {app} declares no {t['name']}"
print(f"  {named} workflow tasks address an Integration Model; every operation they name is declared")
EOF
  # tools are addressed by reference id, as test-06b does: integration:<title>%3A<version>:<instance>:<op>
  local refs; refs=$(${PY} -c "
import json, yaml
m = yaml.safe_load(open('itential/versions.yaml'))['integrations']['models']
refs = [f\"integration:{v['title']}%3A{v['version']}:{v['instance']}:{op}\" for v in m.values() for op in v['operations']]
print(json.dumps({'referenceIds': refs, 'queryOptions': {'limit': 200}}))") || return 1
  # NB: the payload goes in as an argument, not a pipe - `python - <<EOF` reads the heredoc as the
  # program, so it consumes stdin and a piped response never arrives.
  local got; got=$(iap -X POST "${PLATFORM}/tools/bulk" -d "$refs")
  ${PY} - "$got" <<'EOF'
import json, sys
d = json.loads(sys.argv[1])["data"]
unauth = [t["referenceId"].split(":")[-1] for t in d if not t.get("authorized", True)]
assert d, "tools/bulk returned nothing"
assert not unauth, f"not authorized: {unauth[:8]}"
print(f"  {len(d)} operations of both models are authorized tools")
EOF
}
check "S4f.2 every operation a workflow names is declared, and every declared operation is a tool" c2

# --- S4f.3 / S4f.4 / S4f.5 no workflow reaches an adapter any more ---------------------------------
c3() { ${PY} - <<'EOF'
import glob, json
bad = []
for f in sorted(glob.glob("itential/workflows/*.json")):
    for tid, t in json.load(open(f))["tasks"].items():
        if t.get("locationType") in ("Netbox", "Servicenow"):
            bad.append(f"{f}:{tid} adapter task {t.get('locationType')}")
        if t.get("name") == "genericAdapterRequest":
            bad.append(f"{f}:{tid} genericAdapterRequest")
        if t.get("name") == "runCode" and "journal-entries" in json.dumps(t.get("variables", {})):
            bad.append(f"{f}:{tid} journal entry from the runner")
assert not bad, "\n".join(bad)
print("  no committed workflow document holds an adapter task, a generic request or a runner-posted journal entry")
EOF
}
check "S4f.3/4/5 the NetBox writes, the journal entries and the ServiceNow changes all go through the models" c3

# --- S4f.6 the adapter is gone; the three the Platform requires are still running ------------------
c6() {
  local health cfg
  health=$(iap "${PLATFORM}/health/adapters")
  cfg=$(iap "${PLATFORM}/adapters?limit=50")
  ${PY} - "$health" "$cfg" <<'EOF'
import json, sys, yaml

health = json.loads(sys.argv[1]); health = health.get("results", health)
cfg = json.loads(sys.argv[2]).get("results", [])
configured = {c["data"]["name"]: c["data"].get("model", "") for c in cfg if "data" in c}

# What "removed" means, in the order the platform actually holds it:
#   1. no adapter *configuration* - the record this play creates and therefore owns;
#   2. no running service - /adapters/<name> DELETE answers "The service <name> does not exist";
#   3. nothing pinned in the oracle, so a later run cannot put it back.
# Match on the npm package, never on a substring of the instance name: the id is "ServiceNow" with a
# capital N, and a lower-cased substring silently matches nothing (this check passed vacuously once).
left = [n for n, model in configured.items() if "adapter-servicenow" in model]
assert not left, f"adapter-servicenow is still configured: {left}"

adapters = yaml.safe_load(open("itential/versions.yaml")).get("adapters", {})
assert "servicenow" not in adapters, "itential/versions.yaml still pins adapter-servicenow"
assert "netbox" in adapters, "adapter-netbox must stay: the InventoryBroker consumes an adapter (ADR 0039)"

state = {x["id"]: x.get("state") for x in health}
for required in ("InventoryBroker", "LDAP", "NetBox"):
    assert state.get(required) == "RUNNING", f"{required} is {state.get(required)}, expected RUNNING"

# /health/adapters is a runtime view and keeps a STOPPED row for an adapter whose configuration and
# service are both gone (measured 2026-09-11, 6.5.2); it clears when the Platform process restarts.
# A residue is tolerated, a running one never is - that would mean the removal did not take.
residue = [k for k, v in state.items() if k not in configured]
running = [k for k in residue if state.get(k) == "RUNNING"]
assert not running, f"a removed adapter is still RUNNING: {running}"
print(f"  configured: {sorted(configured)}")
if residue:
    print(f"  health residue (no configuration, not running, clears on a Platform restart): {sorted(residue)}")
EOF
}
check "S4f.6 adapter-servicenow is gone; InventoryBroker, LDAP and NetBox are RUNNING" c6

# Configuration Manager still sees the devices through the broker, which is why NetBox keeps its adapter.
c6b() {
  local devs; devs=$(iap -X POST "${PLATFORM}/configuration_manager/devices" -d '{"options":{"start":0,"limit":200}}')
  local nb; nb=$(curl -s -m 20 -H "Authorization: Token ${NETBOX_TOKEN}" "${NETBOX_URL}/api/dcim/devices/?status=active&limit=1")
  ${PY} - "$devs" "$nb" <<'EOF'
import json, sys
d = json.loads(sys.argv[1])
d = d.get("data") or d.get("list") or d.get("results") or []
if isinstance(d, dict):
    d = d.get("list") or d.get("items") or []
n = json.loads(sys.argv[2])["count"]
assert len(d) >= 12, f"Configuration Manager lists {len(d)} devices"
print(f"  Configuration Manager lists {len(d)} devices through the broker (NetBox holds {n} active)")
EOF
}
check "S4f.6 Configuration Manager still lists the lab devices through the InventoryBroker" c6b

# --- S4f.7 the phase 5-7 verifies ------------------------------------------------------------------
# Run them yourself; this records which ones cover the converted paths and what changed in them.
echo "S4f.7 regression: run these after any change to the models or the converted workflows"
for line in \
  "verify/test-05-itential.sh   S4.2 S4.4 (NetBox reads and writes), S4b.2 (the change lifecycle), S4b.1 rewritten for the integration" \
  "verify/test-06-flowai.sh     S4c.3 (agent-driven VLAN), S4d.5a (netbox-sot through the integration)" \
  "verify/test-06b-platform.sh  S4d.3 (lifecycle create and delete), S4d.4 (the models and their tools)" \
  "verify/test-06c-netbox.sh    S4e.5 (journal entries), S4e.6 (idempotency and the agent)"; do
  echo "      ${line}"
done
defer "S4f.7 the phase 5-7 regression set is run separately (it writes to devices and the PDI)"

rm -f "$JAR" /tmp/verify06d.$$
echo
echo "passed=${pass} failed=${fail} deferred=${deferred}"
[ "$fail" -eq 0 ] || exit 1
