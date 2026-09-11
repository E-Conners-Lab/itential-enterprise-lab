#!/usr/bin/env bash
# Run one agent session and print what it did: tool calls with their inputs, the answer, token usage.
# A hand probe for iterating on an agent, not a verify - verify/test-06-flowai.sh is the contract.
#
#   verify/agent-probe.sh netbox-sot-local '{"request":"Which devices are at site br1?"}'
#
# Every response is printed as raw text when it is not JSON: a Platform 500 answers
# {"message":"Internal Server Error", "metadata":{"error":[...]}} and a deleted session answers a
# bare JSON string, and both matter more than the happy path (measured 2026-09-11).
set -uo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "missing .env"; exit 1; }
set -a; . ./.env; set +a
PY=.venv/bin/python
CA=${CA:-docs/lab-root-ca.crt}
PLATFORM=${PLATFORM:-https://itential.lab.internal}
ADMIN_USER=${ITENTIAL_ADMIN_USER:-admin@itential}
agent=${1:?agent name}
inputs=${2:-'{"request":"hello"}'}
timeout=${TIMEOUT:-600}

JAR=$(mktemp); trap 'rm -f "$JAR"' EXIT
iap() { curl -s -m 60 --cacert "$CA" -b "$JAR" -H "Content-Type: application/json" "$@"; }
iap -c "$JAR" -X POST "${PLATFORM}/login" \
  -d "{\"username\":\"${ADMIN_USER}\",\"password\":\"${ITENTIAL_ADMIN_PASSWORD}\"}" -o /dev/null

aid=$(iap "${PLATFORM}/agent-project-service/operable-agents" | ${PY} -c "
import sys,json
try: d=json.load(sys.stdin)
except Exception as e: print('', file=sys.stderr); raise SystemExit(f'agent list not JSON: {e}')
a=[x for x in d['data']['items'] if x['name']=='${agent}']
print(a[0]['_id'] if a else '')")
[ -n "$aid" ] || { echo "agent ${agent} not on the Platform"; exit 1; }

start=$(iap -X POST "${PLATFORM}/agent-session-manager/sessions" \
  -d "{\"agentDefinitionId\":\"${aid}\",\"inputs\":${inputs}}")
sid=$(echo "$start" | ${PY} -c "
import sys,json
raw=sys.stdin.read()
try: print(json.loads(raw).get('sessionId',''))
except Exception: print('')")
[ -n "$sid" ] || { echo "session did not start; the Platform said:"; echo "$start" | head -c 600; exit 1; }
echo "agent   ${agent}"
echo "session ${sid}"

t0=$(date +%s); status=UNKNOWN
while [ $(( $(date +%s) - t0 )) -lt "$timeout" ]; do
  # the list, not GET /sessions/<id>: a FAILED session's record is deleted and the detail 404s
  status=$(iap "${PLATFORM}/agent-session-manager/sessions?limit=10&sortBy=createdAt&sortOrder=desc" | ${PY} -c "
import sys,json
try: d=json.load(sys.stdin)['data']
except Exception: print('UNREADABLE'); raise SystemExit
for s in d:
    if s['sessionId']=='${sid}':
        print(s['status']); break
else: print('GONE')")
  case "$status" in COMPLETE|COMPLETED|FAILED|ERROR|CANCELED|GONE|UNREADABLE) break;; esac
  sleep 5
done
echo "status  ${status}  ($(( $(date +%s) - t0 ))s)"

iap "${PLATFORM}/agent-session-manager/sessions/${sid}/messages?limit=500&sortBy=eventId&sortOrder=asc" | ${PY} -c "
import sys,json
raw=sys.stdin.read()
try: m=json.loads(raw)
except Exception: print('messages not JSON:', raw[:400]); raise SystemExit
if not isinstance(m,list): print('messages:', json.dumps(m)[:400]); raise SystemExit
tin=tout=0
for x in m:
    d=x.get('data') or {}
    if x.get('category')=='TOOL_CALLED':
        print('TOOL ', d.get('toolName'), '<-', json.dumps(d.get('input'))[:220])
        e=d.get('error') or d.get('errorMessage')
        if e: print('      ERROR:', str(e)[:400])
    u=d.get('tokenUsage') or {}
    tin+=u.get('inputTokens',0); tout+=u.get('outputTokens',0)
t=[x for x in m if x.get('type')=='inference-succeeded' and x.get('text')]
print('ANSWER:', t[-1]['text'][:800] if t else '(none)')
print(f'tokens  in={tin} out={tout}')
"
