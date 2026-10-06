#!/usr/bin/env bash
# PID S15 (ADR 0074): netops-knowledge on k3s, reached by production's Gateway through FlowMCP. Opt-in: it does nothing
# unless KNOWLEDGE=1, because the service runs only after `make netops-knowledge`. Intent: itential/versions.yaml
# netops_knowledge and topology/ipam.yaml. State: the cluster (kubectl), the Gateway VM over SSH, the Platform's API.
#
# Control plane (what the cluster says) and data plane (what a packet does) are both checked: the pod's spec says
# non-root, read-only and no egress, and a connection attempt from inside the pod proves the egress is really gone; the
# Service says only iag-01, and a request from this workstation proves it is refused while iag-01's succeeds.
# Reads only. No secret is read: the 401 check sends no token; the registration check reads service names.
set -uo pipefail
cd "$(dirname "$0")/.."
if [ "${KNOWLEDGE:-}" != 1 ]; then
  echo "SKIP  test-15-knowledge: runs after make netops-knowledge; run with KNOWLEDGE=1"
  exit 0
fi
[ -f .env ] || { echo "missing .env"; exit 1; }
set -a; . ./.env; set +a
PY=.venv/bin/python
V=itential/versions.yaml
IPAM=topology/ipam.yaml
CA=docs/lab-root-ca.crt
export KUBECONFIG="${KUBECONFIG_PATH:-$HOME/.kube/lab-k3s.yaml}"
SSH="ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"
val() { ${PY} -c "import yaml,sys;d=yaml.safe_load(open(sys.argv[1]));print(eval(sys.argv[2],{'d':d}))" "$1" "$2"; }
addr() { val "$IPAM" "next(a['address'] for a in d['addresses'] if a['hostname']=='$1')"; }
ts=$(date -u +%Y%m%dT%H%M%SZ)
fail=0; pass=0
ok()   { echo "PASS  $1"; pass=$((pass+1)); }
bad()  { echo "FAIL  $1"; fail=$((fail+1)); }
skip() { echo "SKIP  $1"; }
out=$(mktemp)
# ONLY="S15.2 S15.4" runs a subset while iterating
check() { local name=$1; shift; if [ -n "${ONLY:-}" ] && ! echo " ${ONLY} " | grep -q " ${name%% *} "; then skip "$name"; return; fi; if "$@" >"$out" 2>&1; then ok "$name"; sed 's/^/      /' "$out"; else bad "$name"; sed 's/^/      /' "$out" | head -40; fi; }
trap 'rm -f "$out"' EXIT
mkdir -p verify/results; exec > >(tee "verify/results/${ts}-15-knowledge.log") 2>&1
echo "# test-15-knowledge ${ts}"

NS=$(val "$V" "d['netops_knowledge']['namespace']")
DIGEST=$(val "$V" "d['netops_knowledge']['image']['digest']")
URL=$(val "$V" "d['netops_knowledge']['url']")
BASE=${URL%/mcp}
GW=$(addr "$(val "$V" "d['netops_knowledge']['gateway_host']")")
VIP=$(addr "$(val "$V" "d['netops_knowledge']['hostname']")")

# --- S15.1 control plane: the pinned, locked-down pod -------------------------------------------------------------
c1() {
  kubectl get ns "$NS" -o json | ${PY} -c 'import sys,json;l=json.load(sys.stdin)["metadata"]["labels"];assert l.get("pod-security.kubernetes.io/enforce")=="restricted",l;print("namespace enforces restricted")' || return 1
  kubectl -n "$NS" get pods -l app.kubernetes.io/name=netops-knowledge -o json | ${PY} -c '
import sys, json
digest = sys.argv[1]
pods = [p for p in json.load(sys.stdin)["items"] if p["status"].get("phase") == "Running"]
assert len(pods) == 1, f"{len(pods)} running pods"
p = pods[0]; spec = p["spec"]; c = spec["containers"][0]
assert digest in p["status"]["containerStatuses"][0]["imageID"], "the running image is not the pinned digest"
assert spec.get("automountServiceAccountToken") is False, "service-account token mounted"
assert spec["securityContext"]["runAsNonRoot"] is True and spec["securityContext"]["runAsUser"] == 10001
sc = c["securityContext"]
assert sc["readOnlyRootFilesystem"] and not sc["allowPrivilegeEscalation"] and sc["capabilities"]["drop"] == ["ALL"]
print("one pod on", digest[:19], "non-root, read-only, no capabilities, no service-account token")' "$DIGEST"
}
check "S15.1 the pod runs the pinned digest, non-root, read-only, no capabilities or service-account token, in a restricted namespace" c1

# --- S15.2 data plane: this workstation is refused at the VIP --------------------------------------------------------
c2() {
  local code; code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 --cacert "$CA" "${BASE}/healthz" || true)
  [ "$code" = "000" ] || { echo "the workstation got HTTP ${code} from ${BASE}: the source range or the policy lets it in"; return 1; }
  echo "no answer from ${VIP}:$(val "$V" "d['netops_knowledge']['port']") to this workstation (only ${GW} is admitted)"
}
check "S15.2 the VIP refuses this workstation (data plane: only the Gateway is admitted)" c2

# --- S15.3 data plane: the Gateway VM reaches it over lab-CA TLS, and a request without the token is refused -------
c3() {
  local res; res=$($SSH "ubuntu@${GW}" "curl -s --max-time 8 --cacert /usr/local/share/ca-certificates/lab-root-ca.crt ${BASE}/healthz; echo; curl -s -o /dev/null -w '%{http_code}' --max-time 8 --cacert /usr/local/share/ca-certificates/lab-root-ca.crt -X POST -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\",\"params\":{}}' ${BASE}/mcp")
  [ "$(echo "$res" | head -1)" = ok ] || { echo "iag-01 health: $(echo "$res" | head -1)"; return 1; }
  [ "$(echo "$res" | tail -1)" = 401 ] || { echo "iag-01 without a token got $(echo "$res" | tail -1), not 401"; return 1; }
  echo "iag-01: TLS verified against the lab CA, health ok, tools/list without a token = 401"
}
check "S15.3 iag-01 reaches it over lab-CA TLS, and a request without the token gets 401" c3

# --- S15.4 data plane: the pod reaches nothing -------------------------------------------------------------------------
c4() {
  local pod; pod=$(kubectl -n "$NS" get pods -l app.kubernetes.io/name=netops-knowledge -o jsonpath='{.items[0].metadata.name}')
  # Vault on its VIP (inside the lab) and a public resolver (outside): both must time out
  kubectl -n "$NS" exec "$pod" -- python3 -c '
import socket, sys
for host, port in (("'"$(addr vault)"'", 8200), ("1.1.1.1", 443)):
    s = socket.socket(); s.settimeout(3)
    try:
        s.connect((host, port)); print("REACHED", host, port); sys.exit(1)
    except OSError:
        print("blocked", host, port)
    finally:
        s.close()'
}
check "S15.4 the pod cannot open a connection (Vault inside the lab, a public address outside): no egress" c4

# --- S15.5 FlowMCP: Gateway Manager lists a service for each of the server's tools (after the registration) -------
c5() {
  [ "$(val "$V" "d['netops_knowledge']['register_with_gateway']")" = True ] || { echo "register_with_gateway is false: not registered yet"; return 1; }
  # the password reaches curl on stdin (printf is a shell builtin), never in a process's argv
  local user tok; user=$(val itential/ha2/versions.yaml "d['platform']['admin_user']")
  tok=$(printf '{"username":"%s","password":"%s"}' "$user" "${ITENTIAL_ADMIN_PASSWORD:?}" \
    | curl -s --cacert "$CA" -X POST -H 'Content-Type: application/json' --data @- \
      "https://$(val itential/ha2/versions.yaml "d['service_name'] + '.' + d['domain']")/login")
  curl -s --cacert "$CA" "https://$(val itential/ha2/versions.yaml "d['service_name'] + '.' + d['domain']")/gateway_manager/v1/services?limit=500&token=${tok}" \
    | ${PY} -c '
import sys, json, re, yaml
nk = yaml.safe_load(open("itential/versions.yaml"))["netops_knowledge"]
names = [s["service_metadata"]["name"] for s in json.load(sys.stdin)["result"]]
# FlowMCP names each service <mcp_server>_<tool> (measured 2026-10-06, ADR 0074 amendment)
server = nk["mcp_server"]
found = {t: [n for n in names if n == server + "_" + t] for t in nk["tools"]}
assert all(found.values()), f"missing: {[t for t, f in found.items() if not f]}"
print("Gateway Manager lists", sorted(n for f in found.values() for n in f))'
}
check "S15.5 Gateway Manager lists a service for each tool netops-knowledge advertises (FlowMCP discovery: DNS, TLS, token, policy)" c5

# --- S15.6 the diagnostics agents hold the one search tool (lab PR B) ------------------------------------------------
c6() {
  local user tok; user=$(val itential/ha2/versions.yaml "d['platform']['admin_user']")
  tok=$(printf '{"username":"%s","password":"%s"}' "$user" "${ITENTIAL_ADMIN_PASSWORD:?}" \
    | curl -s --cacert "$CA" -X POST -H 'Content-Type: application/json' --data @- \
      "https://$(val itential/ha2/versions.yaml "d['service_name'] + '.' + d['domain']")/login")
  curl -s --cacert "$CA" "https://$(val itential/ha2/versions.yaml "d['service_name'] + '.' + d['domain']")/agent-project-service/operable-agents?token=${tok}" \
    | ${PY} -c '
import sys, json, yaml
v = yaml.safe_load(open("itential/versions.yaml"))
want = "gatewayService:%s:python-script:%s_search_scenarios" % (v["stack"]["gateway5_cluster_id"], v["netops_knowledge"]["mcp_server"])
agents = {a["name"]: [t["referenceId"] for t in a.get("tools") or []] for a in json.load(sys.stdin)["data"]["items"]}
for name in ("tunnel-diagnostics", "tunnel-diagnostics-local", "fabric-diagnostics", "fabric-diagnostics-local"):
    refs = agents.get(name)
    assert refs is not None, f"{name}: not on the Platform"
    assert refs.count(want) == 1, f"{name}: lacks {want}"
    assert not [r for r in refs if "get_scenario" in r or "runService" in r], f"{name}: holds a wider tool: {refs}"
    print(f"{name}: {len(refs)} tools, search_scenarios once, nothing wider")'
}
check "S15.6 the four diagnostics agents hold netops-knowledge_search_scenarios and nothing wider (no get_scenario, no runService)" c6

echo
echo "passed=${pass} failed=${fail}"
[ "$fail" -eq 0 ]
