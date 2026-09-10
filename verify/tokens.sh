#!/usr/bin/env bash
# Anthropic token meter for the lab (ADR 0049, PID Domain 7). The Agent Session Manager is the ledger: every
# session document carries provider, modelVersion, startedAt and its token totals. Prints the last 7 days per
# model with the cost at the prices in itential/versions.yaml (llm.budget) and the budget left.
#   verify/tokens.sh            report
#   verify/tokens.sh --check    exit 1 when the 7-day spend is at or over llm.budget.weekly_usd (the verifies' guard)
#   DAYS=30 verify/tokens.sh    another window
set -uo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "missing .env"; exit 1; }
set -a; . ./.env; set +a
: "${ITENTIAL_ADMIN_PASSWORD:?}"
PY=.venv/bin/python
V=itential/versions.yaml
# the S11 cut-over moved the name onto the load balancer: no --resolve, the record decides
IT_HOST=itential.lab.internal
PLATFORM="https://${IT_HOST}"
CA=docs/lab-root-ca.crt
JAR=$(mktemp); trap 'rm -f "$JAR"' EXIT
iap() { curl -s -m 60 --cacert "$CA" -b "$JAR" -H "Content-Type: application/json" "$@"; }
iap -c "$JAR" -X POST "${PLATFORM}/login" -d "{\"username\":\"${ITENTIAL_ADMIN_USER:-admin@itential}\",\"password\":\"${ITENTIAL_ADMIN_PASSWORD}\"}" -o /dev/null -w '%{http_code}' | grep -qx 200 || { echo "login to ${PLATFORM} failed"; exit 2; }
DAYS=${DAYS:-7}
# every session of the window (paged), then the sums per provider/model in python
offset=0; : > "$JAR.sessions"
while :; do
  page=$(iap "${PLATFORM}/agent-session-manager/sessions?limit=200&offset=${offset}&sortBy=createdAt&sortOrder=desc")
  n=$(echo "$page" | ${PY} -c 'import sys,json;d=json.load(sys.stdin);print(len(d.get("data") or []))')
  [ "$n" -gt 0 ] || break
  echo "$page" >> "$JAR.sessions"
  oldest=$(echo "$page" | ${PY} -c 'import sys,json;d=json.load(sys.stdin)["data"];print(min(s.get("startedAt") or s.get("createdAt") or "9" for s in d))')
  offset=$((offset+n))
  [[ "$oldest" < "$(date -u -v-${DAYS}d +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "-${DAYS} days" +%Y-%m-%dT%H:%M:%SZ)" ]] && break
done
MODE=${1:-report} DAYS="$DAYS" ${PY} - "$JAR.sessions" <<'PY'
import datetime, json, os, sys, yaml
budget = yaml.safe_load(open("itential/versions.yaml"))["llm"]["budget"]
since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=int(os.environ["DAYS"]))
sessions = []
for line in open(sys.argv[1]):
    if line.strip():
        sessions += json.loads(line)["data"]
seen, rows = set(), []
for s in sessions:
    if s["sessionId"] in seen:
        continue
    seen.add(s["sessionId"])
    started = datetime.datetime.fromisoformat((s.get("startedAt") or s.get("createdAt")).replace("Z", "+00:00"))
    if started >= since:
        rows.append(s)
per = {}
for s in rows:
    key = (s.get("provider") or "?", s.get("modelVersion") or "?")
    p = per.setdefault(key, {"sessions": 0, "in": 0, "out": 0})
    p["sessions"] += 1; p["in"] += s.get("totalInputTokens") or 0; p["out"] += s.get("totalOutputTokens") or 0
usd = 0.0
print(f"Anthropic token meter, last {os.environ['DAYS']} days ({len(rows)} sessions; prices per MTok in={budget['input_usd_per_mtok']} out={budget['output_usd_per_mtok']}, {budget['provider']} only is billed)")
for (prov, model), p in sorted(per.items(), key=lambda kv: -kv[1]["in"]):
    cost = 0.0
    if prov == budget["provider"]:
        cost = p["in"] / 1e6 * budget["input_usd_per_mtok"] + p["out"] / 1e6 * budget["output_usd_per_mtok"]
        usd += cost
    print(f"  {prov:10} {model:18} sessions={p['sessions']:4d} in={p['in']:9d} out={p['out']:7d}  ${cost:6.2f}")
left = budget["weekly_usd"] - usd
print(f"spent ${usd:.2f} of the ${budget['weekly_usd']:.2f} weekly budget; ${left:.2f} left")
big = sorted((s for s in rows if (s.get("provider") == budget["provider"])), key=lambda s: -(s.get("totalInputTokens") or 0))[:3]
for s in big:
    print(f"  largest: {s['agentSnapshot'].get('name') if s.get('agentSnapshot') else '?'} {s.get('startedAt','')[:16]} in={s.get('totalInputTokens')} ({s.get('status')})")
if os.environ["MODE"] == "--check" and usd >= budget["weekly_usd"]:
    print("BUDGET EXCEEDED: no new Anthropic sessions from the verifies (ANTHROPIC_VERIFY=force overrides)")
    sys.exit(1)
PY
