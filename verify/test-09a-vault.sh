#!/usr/bin/env bash
# PID S8 criterion 2 (amendment 1.34, ADR 0065) on PRODUCTION: Vault on k3s is up, unsealed, reachable only over the
# lab CA, refuses anyone without a token, and its two AppRoles read only their own paths from only their own hosts.
# Criterion 3 (the Platform and the Gateway reading through Vault) joins this test at the cut-over (step 5); until
# then production still holds its credentials and S8.3 is reported as not yet due. The dev tier's full run, E14 and
# E15 included, is verify/test-09a-vault-dev.sh. Intent: itential/versions.yaml (vault), topology/ipam.yaml.
#
# Administrator checks need a Vault token: VAULT_ADMIN_TOKEN, else the root token in the owner's init file
# (VAULT_INIT_FILE, until the root token is revoked). Without one they are SKIPPED, never passed; the checks any
# client can make still run. Writes: short-lived tokens and secret IDs, destroyed again. Never prints a secret.
set -uo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "missing .env"; exit 1; }
set -a; . ./.env; set +a
: "${AUTOMATION_PASSWORD:?}"
PY=.venv/bin/python
V=itential/versions.yaml
CA=docs/lab-root-ca.crt
INIT_FILE=${VAULT_INIT_FILE:-$HOME/.config/itential-enterprise-lab/vault-init.json}
SNAP_DIR=${VAULT_SNAPSHOT_DIR:-$HOME/Backups/itential-enterprise-lab/vault}
ts=$(date -u +%Y%m%dT%H%M%SZ)
fail=0; pass=0; skipped=0
ok()   { echo "PASS  $1"; pass=$((pass+1)); }
bad()  { echo "FAIL  $1"; fail=$((fail+1)); }
skip() { echo "SKIP  $1"; skipped=$((skipped+1)); }
check() { local name=$1; shift; if [ -n "${ONLY:-}" ] && ! echo " ${ONLY} " | grep -q " ${name%% *} "; then skip "$name"; return; fi; if "$@" >/tmp/verify09p.$$ 2>&1; then ok "$name"; sed 's/^/      /' /tmp/verify09p.$$; else bad "$name"; sed 's/^/      /' /tmp/verify09p.$$ | head -14; fi; }
trap 'rm -f /tmp/verify09p.$$' EXIT
mkdir -p verify/results; exec > >(tee "verify/results/${ts}-09a-vault.log") 2>&1
echo "# test-09a-vault ${ts}"
[ -s "$CA" ] || { bad "$CA missing"; echo; echo "passed=0 failed=1"; exit 1; }

export VAULT_ADDR; VAULT_ADDR=$(${PY} -c "import yaml;print(yaml.safe_load(open('$V'))['vault']['prod']['url'])")
export DEVICE_PASSWORD="$AUTOMATION_PASSWORD"
# each role's bound addresses from the oracle: versions.yaml names the hosts, topology/ipam.yaml their addresses
export VAULT_ROLE_CIDRS; VAULT_ROLE_CIDRS=$(${PY} -c "
import json, yaml
v = yaml.safe_load(open('$V'))['vault']; ip = {a['hostname']: a['address'] for a in yaml.safe_load(open('topology/ipam.yaml'))['addresses']}
print(json.dumps({r: [ip[h] + '/32' for h in c['bound_hosts']] for r, c in v['approles'].items()}))")
if [ -z "${VAULT_ADMIN_TOKEN:-}" ] && [ -s "$INIT_FILE" ]; then
  VAULT_ADMIN_TOKEN=$(${PY} -c 'import json,sys;print(json.load(open(sys.argv[1])).get("root_token") or "")' "$INIT_FILE" 2>/dev/null)
fi
export VAULT_ADMIN_TOKEN=${VAULT_ADMIN_TOKEN:-}
vc() { ${PY} verify/vaultcheck.py "$1"; }
admin_check() { if [ -n "$VAULT_ADMIN_TOKEN" ]; then check "$@"; else skip "$1 (no administrator token: VAULT_ADMIN_TOKEN)"; fi; }

# --- S8.2: Vault on k3s, unsealed, TLS from the lab CA, no anonymous reads ----------------------------------------
check "S8.2a Vault answers at vault.lab.internal over the lab CA and is unsealed" vc health
c_vip() {
  # the system resolver, as every client uses it (dig ignores macOS's /etc/resolver/lab.internal)
  local ip; ip=$(${PY} -c "import socket,yaml;from urllib.parse import urlsplit;print(socket.gethostbyname(urlsplit(yaml.safe_load(open('$V'))['vault']['prod']['url']).hostname))")
  local want; want=$(${PY} -c "import yaml;print(yaml.safe_load(open('$V'))['vault']['prod']['vip'])")
  echo "vault.lab.internal -> ${ip} (want ${want})"; [ "$ip" = "$want" ]
}
check "S8.2g the service name resolves to the VIP versions.yaml gives it" c_vip
check "S8.2b a read of lab/devices/automation with no token is refused" vc no-token
admin_check "S8.2c each reader's policy reads only its own paths (Gateway: devices yes; Platform: devices no, NetBox yes)" vc policies
admin_check "S8.2e each AppRole is bound to its own hosts (iap-01/02, iag-01): a valid secret ID from this machine is refused" vc bound
admin_check "S8.2d the device password in Vault equals the .env seed (one-way until Phase 9b)" vc seeded
c_snapshot() {
  local newest; newest=$(ls -t "$SNAP_DIR"/vault-raft-*.snap 2>/dev/null | head -1)
  [ -n "$newest" ] || { echo "no snapshot in ${SNAP_DIR} (make vault-snapshot)"; return 1; }
  echo "newest off-host snapshot: $(basename "$newest"), $(( ($(date +%s) - $(stat -f %m "$newest" 2>/dev/null || stat -c %Y "$newest")) / 3600 )) h old, $(du -h "$newest" | cut -f1)"
}
check "S8.2f an off-host Raft snapshot exists on this workstation (ADR 0065 decision 8)" c_snapshot
echo "NOTE  S8.3 (the Platform and the Gateway reading through Vault) is due at the cut-over, not before"

echo
echo "passed=${pass} failed=${fail} skipped=${skipped}"
[ "$fail" = 0 ]
