#!/usr/bin/env bash
# ADR 0074 amendment (2026-10-06): copy netops-knowledge's FlowMCP bearer token from production's Vault into the
# Gateway's LOCAL secret netops_knowledge.mcp_secret, which `iagctl create secret` encrypts with the Gateway's key.
# Why: on Gateway 5.5.2 an MCP header cannot resolve the Vault secret provider, and a local secret imported through
# Gateway Manager is stored unencrypted, which the server cannot decrypt. Vault stays the source of the token.
#
# Run it in YOUR terminal (make knowledge-mcp-secret), after `make vault-login`: iagctl's login is interactive only.
# It puts the Gateway admin password (Vault vault.gateway_admin_path) on your clipboard for the one prompt and clears
# the clipboard on exit. The token is never in argv or on screen: it moves only through mode-600 temporary files on
# this machine, on iag-01 and in the gateway5 container, each removed on exit.
# Afterwards: the Gateway play with -e nk_reregister=true, so FlowMCP reconnects with the token (a rotation needs it).
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
VAULT_TOKEN_FILE=${VAULT_TOKEN_FILE:-$HOME/.config/itential-enterprise-lab/vault-admin-token}
SSH_USER=${SSH_USER:-ubuntu}
[ -r "$VAULT_TOKEN_FILE" ] || { echo "no Vault administrator token at $VAULT_TOKEN_FILE: make vault-login first" >&2; exit 1; }

eval "$("$PY" - <<'EOF'
import shlex, yaml
v = yaml.safe_load(open("itential/versions.yaml"))
ipam = yaml.safe_load(open("topology/ipam.yaml"))
nk, vault = v["netops_knowledge"], v["vault"]
host = next(a["address"] for a in ipam["addresses"] if a["hostname"] == nk["gateway_host"])
for k, val in {"VAULT_URL": vault["prod"]["url"], "KV": vault["kv_mount"], "TOKEN_PATH": vault["knowledge"]["token_path"],
               "ADMIN_PATH": vault["gateway_admin_path"], "SECRET": nk["mcp_secret"], "GW_HOST": host}.items():
    print(f"{k}={shlex.quote(str(val))}")
EOF
)"

tmp=$(mktemp -d)
chmod 700 "$tmp"
remote="/tmp/nk-tok.$$"
cleanup() {
  rm -rf "$tmp"
  pbcopy </dev/null 2>/dev/null || true
}
trap cleanup EXIT

# the token into a mode-600 file, the admin password onto the clipboard; neither is printed
VAULT_URL=$VAULT_URL KV=$KV TOKEN_PATH=$TOKEN_PATH ADMIN_PATH=$ADMIN_PATH VAULT_TOKEN_FILE=$VAULT_TOKEN_FILE \
  "$PY" - "$tmp/tok" <<'EOF'
import json, os, ssl, subprocess, sys, urllib.request
ctx = ssl.create_default_context(cafile="docs/lab-root-ca.crt")
vt = open(os.environ["VAULT_TOKEN_FILE"]).read().strip()
def read(path):
    req = urllib.request.Request(f"{os.environ['VAULT_URL']}/v1/{os.environ['KV']}/data/{path}", headers={"X-Vault-Token": vt})
    with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
        return json.load(r)["data"]["data"]
token = read(os.environ["TOKEN_PATH"])["token"]
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.write(fd, token.encode())
os.close(fd)
subprocess.run(["pbcopy"], input=read(os.environ["ADMIN_PATH"])["password"].encode(), check=True)
EOF
echo "The Gateway admin password is on your clipboard: paste it at the iagctl login prompt."

scp -q "$tmp/tok" "$SSH_USER@$GW_HOST:$remote"
# one interactive session: the token into the container (mode 600), the login, the secret, then everything removed
ssh -t "$SSH_USER@$GW_HOST" "
  E='-e NO_COLOR=1 -e GATEWAY_APPLICATION_MODE=client -e GATEWAY_CLIENT_USE_TLS=false -e GATEWAY_CLIENT_HOST=gateway5'
  trap 'sudo docker exec gateway5 rm -f /tmp/nk-tok; rm -f $remote' EXIT
  set -e
  sudo docker exec -i gateway5 sh -c 'umask 077; cat > /tmp/nk-tok' < $remote
  rm -f $remote
  sudo docker exec -it \$E gateway5 iagctl login admin
  sudo docker exec \$E gateway5 iagctl delete secret $SECRET >/dev/null 2>&1 || true
  sudo docker exec \$E gateway5 iagctl create secret $SECRET --value @/tmp/nk-tok \
    --description 'FlowMCP header copy of Vault $KV/$TOKEN_PATH (ADR 0074 amendment)'
"
echo "Done: $SECRET is set on the Gateway. Now run the Gateway play with -e nk_reregister=true so FlowMCP reconnects."
