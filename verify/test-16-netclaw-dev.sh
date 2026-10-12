#!/usr/bin/env bash
# ADR 0082 (S17): NetClaw's Platform contract on the dev tier (the subset dev can run: no dc1-spine01, no BGP drill, no
# ServiceNow for Add Branch VLAN), re-run without Slack and without NetClaw: svc-netclaw's account,
# the gbac boundary, the refusals, and (a)-(d) through the routes NetClaw's tools post to (verify/netclawcheck.py). The
# platform-guide case runs the local twin (no Anthropic tokens). (d) opens one Add Branch VLAN approval and rejects it;
# the reject path deletes the NetBox reservation, so nothing changes on the switch or in NetBox.
set -uo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "missing .env"; exit 1; }
set -a; . ./.env; set +a
PY=.venv/bin/python
ts=$(date -u +%Y%m%dT%H%M%SZ)
fail=0; pass=0
ok()   { echo "PASS  $1"; pass=$((pass+1)); }
bad()  { echo "FAIL  $1"; fail=$((fail+1)); }
skip() { echo "SKIP  $1"; }
out=$(mktemp)
# ONLY="S17.2 S17.5" runs a subset while iterating
check() { local name=$1; shift; if [ -n "${ONLY:-}" ] && ! echo " ${ONLY} " | grep -q " ${name%% *} "; then skip "$name"; return; fi; if "$@" >"$out" 2>&1; then ok "$name"; sed 's/^/      /' "$out"; else bad "$name"; sed 's/^/      /' "$out" | head -40; fi; }
trap 'rm -f "$out"' EXIT
mkdir -p verify/results; exec > >(tee "verify/results/${ts}-16-netclaw-dev.log") 2>&1
echo "# test-16-netclaw-dev ${ts}"
T=dev

check "S17.1 svc-netclaw logs in and holds exactly the netclaw group" ${PY} verify/netclawcheck.py account --tier $T
check "S17.2 every automation carries the gbac versions.yaml names; netclaw reads exactly its tools' automations" ${PY} verify/netclawcheck.py gbac --tier $T
check "S17.3 svc-netclaw is refused everywhere else, and the show gate refuses a non-show command before a job" ${PY} verify/netclawcheck.py refusals --tier $T
check "S17.4 (a) platform-guide's twin answers through its route with the guide's counts, through its one tool" ${PY} verify/netclawcheck.py guide --tier $T
check "S17.5 (b) one show job as svc-netclaw" ${PY} verify/netclawcheck.py show --tier $T
check "S17.6 (c) the newest show job described; svc-netclaw finds the card; closed" ${PY} verify/netclawcheck.py describe --tier $T
skip "S17.7 (d) Add Branch VLAN: production only - dev runs no ServiceNow adapter, so the Platform refuses the route (400)"

echo "# ${pass} passed, ${fail} failed"
[ "$fail" -eq 0 ]
