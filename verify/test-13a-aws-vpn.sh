#!/usr/bin/env bash
# PID S13 criteria 2 and 4 (ADR 0068, amendment 2026-10-03; build step 12): the test of the AWS VPN while a deployment is
# up, on PRODUCTION, which runs it since ADR 0070 (versions.yaml aws_vpn.tier). Opt-in: it does nothing unless
# AWS_VPN=1, because it needs a live deployment and runs Verify AWS VPN against it; so `make verify` (verify/run.sh)
# selects it and it skips there. Intent: itential/versions.yaml (aws_vpn, workflows, vault) and itential/ha2/versions.yaml
# (the hosts). State: production's Vault, its Platform's API, its containers' logs over SSH (the Platform nodes and the
# Gateway VM), both repositories, and the router's own records counted on its Gateway (lab-edge-push sweep).
#
# Reads only, except one Verify AWS VPN job (reads too: router, AWS monitor, a ping). The secrets are held in memory by
# verify/awsvpncheck.py and never printed: every sweep prints labels and counts. A router log never reaches a job or
# this machine. Vault: the owner's administrator token (make vault-login), through the environment, never printed.
set -uo pipefail
cd "$(dirname "$0")/.."
if [ "${AWS_VPN:-}" != 1 ]; then
  echo "SKIP  test-13a-aws-vpn: needs a live AWS deployment; run with AWS_VPN=1"
  exit 0
fi
[ -f .env ] || { echo "missing .env"; exit 1; }
set -a; . ./.env; set +a
: "${ITENTIAL_ADMIN_PASSWORD:?}"
PY=.venv/bin/python
V=itential/versions.yaml
CA=docs/lab-root-ca.crt
val() { ${PY} -c "import yaml,sys;d=yaml.safe_load(open(sys.argv[1]));print(eval(sys.argv[2],{'d':d}))" "$1" "$2"; }
ts=$(date -u +%Y%m%dT%H%M%SZ)
fail=0; pass=0
ok()   { echo "PASS  $1"; pass=$((pass+1)); }
bad()  { echo "FAIL  $1"; fail=$((fail+1)); }
skip() { echo "SKIP  $1"; }
out=$(mktemp)
# ONLY="S13.2e S13.2h" runs a subset while iterating
check() { local name=$1; shift; if [ -n "${ONLY:-}" ] && ! echo " ${ONLY} " | grep -q " ${name%% *} "; then skip "$name"; return; fi; if "$@" >"$out" 2>&1; then ok "$name"; sed 's/^/      /' "$out"; else bad "$name"; sed 's/^/      /' "$out" | head -40; fi; }
trap 'rm -f "$out"' EXIT
mkdir -p verify/results; exec > >(tee "verify/results/${ts}-13a-aws-vpn.log") 2>&1
echo "# test-13a-aws-vpn ${ts}"

[ -s "$CA" ] || { bad "$CA missing"; echo; echo "passed=0 failed=1"; exit 1; }
[ "$(val "$V" "d['aws_vpn']['tier']")" = prod ] || { bad "versions.yaml aws_vpn.tier is not prod: this test runs where the AWS VPN runs"; echo; echo "passed=${pass} failed=${fail}"; exit 1; }

ADMIN_FILE=${VAULT_ADMIN_FILE:-$HOME/.config/itential-enterprise-lab/vault-admin-token}
if [ -z "${VAULT_ADMIN_TOKEN:-}" ] && [ -s "$ADMIN_FILE" ]; then
  VAULT_ADMIN_TOKEN=$(tr -d '\n' < "$ADMIN_FILE")
fi
[ -n "${VAULT_ADMIN_TOKEN:-}" ] || { bad "no Vault administrator token (make vault-login first)"; echo; echo "passed=${pass} failed=${fail}"; exit 1; }
export VAULT_ADMIN_TOKEN
export VAULT_ADDR; VAULT_ADDR=$(val "$V" "d['vault']['prod']['url']")
export PLATFORM_URL="https://$(val itential/ha2/versions.yaml "d['service_name'] + '.' + d['domain']")"
export AWS_VPN_TIER=prod VAULT_TIER=prod
vpn() { ${PY} verify/awsvpncheck.py "$1"; }

# --- the counter first: every AWS VPN secret read from Vault, and a planted copy of each is found -----------------
check "S13.2e the sweep holds every AWS VPN secret in Vault (each live version) and counts a planted copy, whole and cut" vpn control

# --- criterion 4, live (before the sweeps, so its job is swept too) -----------------------------------------------
check "S13.4a Verify AWS VPN through its endpoint trigger: router, AWS monitor and data plane all say up" vpn verify

# --- criterion 2: no copy anywhere ----------------------------------------------------------------------------------
check "S13.2f no secret in any job document or task record of the AWS VPN and lab-edge workflows" vpn jobs
check "S13.2g no secret in the platform logs of both Platform nodes or the gateway5 and gateway5-runner logs of the Gateway VM" vpn logs
check "S13.2h no secret in a tracked file of this repository or of cloud-devops-pipeline main" vpn repos
check "S13.2i no secret in the router's log, change log or running config, counted on the Gateway; the key is type 6" vpn router

echo
echo "passed=${pass} failed=${fail}"
[ "$fail" -eq 0 ]
