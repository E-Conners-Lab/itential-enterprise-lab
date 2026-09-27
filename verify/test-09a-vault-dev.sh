#!/usr/bin/env bash
# PID S8 criteria 2-3 and eval E15 (amendment 1.34, ADR 0065): Vault holds the device and API credentials, and the
# Platform and the Gateway read them through their built-in clients. The DEV TIER's test: the dev Vault on
# itential-dev (make vault-dev) and the dev stack with vault_enabled. Production gets its own test-09a-vault.sh once
# its Vault exists (Phase 9a step 3); the `-dev` in this name keeps verify/run.sh (make verify) from selecting it. Intent: itential/versions.yaml (vault, stack, vm) and
# clab/versions.yaml. State: Vault's API over the lab CA, the dev Platform's API, the Gateway log over SSH.
#
# Writes (dev only): throwaway AppRole secret IDs, destroyed again; one show clock per clab device; and, unless
# SKIP_ROTATION=1, the E15 rotation - one clab router's password changed in its running config (never saved) and
# in Vault, then both restored to the .env value and checked.
#
# Also E14: the dev Vault is sealed for about a minute and unsealed again (SKIP_SEAL=1 leaves it out). Run by
# `make verify-dev`, never by `make verify`.
set -uo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "missing .env"; exit 1; }
set -a; . ./.env; set +a
: "${ITENTIAL_ADMIN_PASSWORD:?}" "${CLAB_AUTOMATION_PASSWORD:?}"
PY=.venv/bin/python
V=itential/versions.yaml
CA=docs/lab-root-ca.crt
val() { ${PY} -c "import yaml,sys;d=yaml.safe_load(open(sys.argv[1]));print(eval(sys.argv[2],{'d':d}))" "$1" "$2"; }
DEV_IP=$(val "$V" "d['vm']['ip']")
DEV_HOST="$(val "$V" "d['dev']['hostname']").lab.internal"
VAULT_DIR=$(val "$V" "d['vault']['dev']['dir']")
NODES=$(val clab/versions.yaml "len(d['nodes'])")
SSH="ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new"
ts=$(date -u +%Y%m%dT%H%M%SZ)
fail=0; pass=0
ok()   { echo "PASS  $1"; pass=$((pass+1)); }
bad()  { echo "FAIL  $1"; fail=$((fail+1)); }
skip() { echo "SKIP  $1"; }
# ONLY="S8.2a E15" runs a subset while iterating
check() { local name=$1; shift; if [ -n "${ONLY:-}" ] && ! echo " ${ONLY} " | grep -q " ${name%% *} "; then skip "$name"; return; fi; if "$@" >/tmp/verify09a.$$ 2>&1; then ok "$name"; sed 's/^/      /' /tmp/verify09a.$$; else bad "$name"; sed 's/^/      /' /tmp/verify09a.$$ | head -25; fi; }
trap 'rm -f /tmp/verify09a.$$' EXIT
mkdir -p verify/results; exec > >(tee "verify/results/${ts}-09a-vault-dev.log") 2>&1
echo "# test-09a-vault-dev ${ts} (SKIP_ROTATION=${SKIP_ROTATION:-0})"

[ -s "$CA" ] || { bad "$CA missing"; echo; echo "passed=0 failed=1"; exit 1; }

# The dev Vault's administrator token is its root token, kept on the dev VM (vault-dev.yml, dev only). It reaches the
# checks through the environment, never argv, and is never printed.
VAULT_ADMIN_TOKEN=$(${SSH} "ubuntu@${DEV_IP}" "sudo cat ${VAULT_DIR}/init.json" 2>/dev/null \
                    | ${PY} -c 'import json,sys;print(json.load(sys.stdin)["root_token"])' 2>/dev/null) \
  || { bad "could not read the dev Vault's init output on ${DEV_IP} (make vault-dev first)"; echo; echo "passed=${pass} failed=${fail}"; exit 1; }
export VAULT_ADMIN_TOKEN
export VAULT_ADDR; VAULT_ADDR=$(val "$V" "d['vault']['dev']['url']")
export PLATFORM_URL="https://${DEV_HOST}"
export DEVICE_PASSWORD="$CLAB_AUTOMATION_PASSWORD"
# the dev AppRoles are bound to the pinned vault-dev Docker gateway (vars/itential-dev.yml vault_role_cidrs)
export VAULT_BOUND_CIDRS="$(val "$V" "d['vault']['dev']['docker_gateway']")/32"
vc() { ${PY} verify/vaultcheck.py "$1"; }

# --- S8.2: Vault is unsealed, answers the AppRoles and refuses anyone without a token ----------------------------
check "S8.2a Vault answers over the lab CA and is unsealed" vc health
check "S8.2b a read of lab/devices/automation with no token is refused" vc no-token
check "S8.2c each reader's policy reads only its own paths (Gateway: devices yes; Platform: devices no, NetBox yes)" vc policies
check "S8.2e the AppRoles are bound to the dev VM: a login with a valid secret ID from this machine is refused" vc bound
check "S8.2d the device password in Vault equals the .env seed (seeded one way)" vc seeded

# --- S8.3: the Platform holds references, the Gateway resolves them, and both still work --------------------------
check "S8.3a no credential on the Platform: nodes, NetBox adapter and integration carry Vault references" vc references
check "S8.3b the Gateway's Vault provider and aliases match the oracle (export compared, role ID from Vault)" vc gateway
check "S8.3c the NetBox adapter reads NetBox with the token the Platform resolves from Vault" vc platform-read

# The Gateway logs every resolution (alias, provider, outcome - never the value): the device check must add exactly one
# success per device, which is the second source for "the password came from Vault", not the job status alone.
c3d() {
  local since before after
  since=$(date -u +%Y-%m-%dT%H:%M:%SZ); sleep 1
  vc devices || return 1
  after=$(${SSH} "ubuntu@${DEV_IP}" "sudo docker logs --since ${since} gateway5 2>&1" \
          | sed -E 's/\x1b\[[0-9;]*m//g' | grep -c 'secret_resolution.*outcome=success')
  echo "Gateway log since ${since}: ${after} successful resolutions for ${NODES} devices"
  [ "$after" -ge "$NODES" ]
}
check "S8.3d every clab device logs in with its password resolved from Vault (job result and Gateway log)" c3d

# --- E15 (dev): rotation is a device + Vault change, and the next job picks it up --------------------------------
if [ "${SKIP_ROTATION:-0}" = 1 ]; then
  skip "E15 password rotation (SKIP_ROTATION=1)"
else
  check "E15 rotation: device only -> next job fails; Vault only -> next job succeeds; both restored" vc rotation
  check "E15 restored: Vault equals the .env seed again" vc seeded
fi

# --- E14 (dev): a sealed Vault fails the job cleanly -------------------------------------------------------------
# Seals the dev Vault for about a minute: any other dev job in that window fails. The unseal key comes from the same
# dev init output as the root token (dev only; production's unseal is manual, ADR 0065 decision 7).
if [ "${SKIP_SEAL:-0}" = 1 ]; then
  skip "E14 sealed Vault (SKIP_SEAL=1)"
else
  VAULT_UNSEAL_KEY=$(${SSH} "ubuntu@${DEV_IP}" "sudo cat ${VAULT_DIR}/init.json" 2>/dev/null \
                     | ${PY} -c 'import json,sys;print(json.load(sys.stdin)["keys"][0])' 2>/dev/null)
  export VAULT_UNSEAL_KEY
  check "E14 sealed Vault: device and NetBox jobs reach their end via the error path; unsealed, both succeed" vc sealed
  check "E14 unsealed afterwards" vc health
fi

echo
echo "passed=${pass} failed=${fail}"
[ "$fail" = 0 ]
