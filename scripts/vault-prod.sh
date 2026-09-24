#!/usr/bin/env bash
# Production Vault's manual operations (ADR 0065 decision 7): `make vault-status | vault-init | vault-unseal`.
#   status  initialised / sealed / version, nothing secret
#   init    once: one unseal key share, written with the root token to a mode-600 file OUTSIDE the repo
#           (VAULT_INIT_FILE, default ~/.config/itential-enterprise-lab/vault-init.json). Nothing is printed. Run it in
#           your own terminal, not through Claude, so neither value enters a transcript. Move the unseal key to your
#           password manager; `make vault-config` uses the root token and then revokes it.
#   unseal  asks for the unseal key without echoing it (or reads it from VAULT_INIT_FILE with FROM_FILE=1)
#   snapshot  a Raft snapshot to VAULT_SNAPSHOT_DIR (default ~/Backups/itential-enterprise-lab/vault), mode 600,
#           outside the repo (ADR 0065 decision 8). Encrypted by Vault's barrier: useless without the unseal key,
#           which is kept apart from it. Token: VAULT_TOKEN, else the root token in VAULT_INIT_FILE.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
CA=docs/lab-root-ca.crt
ADDR=$(${PY} -c "import yaml;print(yaml.safe_load(open('itential/versions.yaml'))['vault']['prod']['url'])")
INIT_FILE=${VAULT_INIT_FILE:-$HOME/.config/itential-enterprise-lab/vault-init.json}
SNAP_DIR=${VAULT_SNAPSHOT_DIR:-$HOME/Backups/itential-enterprise-lab/vault}

state() { curl -s -m 15 --cacert "$CA" "${ADDR}/v1/sys/seal-status"; }

case "${1:-}" in
  status)
    state | ${PY} -c 'import sys,json;d=json.load(sys.stdin);print("%s: initialized=%s sealed=%s version=%s" % (sys.argv[1], d["initialized"], d["sealed"], d.get("version")))' "$ADDR"
    ;;
  init)
    [ "$(state | ${PY} -c 'import sys,json;print(json.load(sys.stdin)["initialized"])')" = False ] || { echo "already initialised - nothing done"; exit 1; }
    [ ! -e "$INIT_FILE" ] || { echo "$INIT_FILE exists - refusing to overwrite it"; exit 1; }
    mkdir -p "$(dirname "$INIT_FILE")"; chmod 700 "$(dirname "$INIT_FILE")"
    ( umask 077; curl -s -m 60 --cacert "$CA" -X PUT -d '{"secret_shares":1,"secret_threshold":1}' "${ADDR}/v1/sys/init" > "$INIT_FILE" )
    ${PY} -c 'import sys,json;d=json.load(open(sys.argv[1]));assert len(d["keys"])==1 and d["root_token"], "init failed: see the file"' "$INIT_FILE"
    echo "initialised: one unseal key and the root token are in $INIT_FILE (mode 600); nothing was printed"
    echo "next: make vault-unseal FROM_FILE=1, then make vault-config; keep the unseal key in your password manager"
    ;;
  unseal)
    if [ "${FROM_FILE:-0}" = 1 ]; then
      key=$(${PY} -c 'import sys,json;print(json.load(open(sys.argv[1]))["keys"][0])' "$INIT_FILE")
    else
      read -rs -p "unseal key: " key; echo
    fi
    ${PY} -c 'import json,sys;print(json.dumps({"key": sys.stdin.read().strip()}))' <<<"$key" \
      | curl -s -m 30 --cacert "$CA" -X PUT --data-binary @- "${ADDR}/v1/sys/unseal" \
      | ${PY} -c 'import sys,json;d=json.load(sys.stdin);print("sealed" if d.get("sealed", True) else "unsealed", d.get("errors") or "")'
    unset key
    ;;
  snapshot)
    token=${VAULT_TOKEN:-$(${PY} -c 'import sys,json;print(json.load(open(sys.argv[1])).get("root_token") or "")' "$INIT_FILE" 2>/dev/null)}
    [ -n "$token" ] || { echo "no token: set VAULT_TOKEN (the root token in $INIT_FILE is gone once revoked)"; exit 1; }
    mkdir -p "$SNAP_DIR"; chmod 700 "$SNAP_DIR"
    out="$SNAP_DIR/vault-raft-$(date -u +%Y%m%dT%H%M%SZ).snap"
    # the header comes from a file descriptor, so the token never appears in argv (ps)
    code=$( umask 077; curl -s -m 120 --cacert "$CA" -H @<(printf 'X-Vault-Token: %s\n' "$token") \
            -o "$out" -w '%{http_code}' "${ADDR}/v1/sys/storage/raft/snapshot" )
    unset token
    [ "$code" = 200 ] && [ -s "$out" ] || { rm -f "$out"; echo "snapshot failed (HTTP ${code})"; exit 1; }
    echo "snapshot: $out ($(du -h "$out" | cut -f1), mode $(stat -f %Lp "$out" 2>/dev/null || stat -c %a "$out"))"
    ;;
  *) echo "usage: $0 status|init|unseal|snapshot"; exit 2 ;;
esac
