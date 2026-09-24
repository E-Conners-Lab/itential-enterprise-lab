#!/usr/bin/env bash
# Production Vault's manual operations (ADR 0065 decision 7). Run the ones marked OWNER in your own terminal, not
# through Claude, so no key, password or token enters a transcript. Nothing secret is ever printed.
#   status        initialised / sealed / version
#   init          OWNER, once: one unseal key share, written with the root token to a mode-600 file outside the repo
#                 (VAULT_INIT_FILE, default ~/.config/itential-enterprise-lab/vault-init.json)
#   unseal        OWNER: asks for the unseal key without echoing it (FROM_FILE=1 reads it from VAULT_INIT_FILE)
#   admin-user    OWNER, once: sets the password of the administrator login (versions.yaml vault.prod.admin), asked
#                 twice without echo, using the root token in VAULT_INIT_FILE. Keep it in your password manager.
#   login         OWNER: logs in as the administrator (password asked without echo) and writes a short-lived token
#                 to VAULT_ADMIN_FILE (default ~/.config/itential-enterprise-lab/vault-admin-token, mode 600), which
#                 make vault-config / vault-snapshot / vault-cutover and the verify's admin checks use
#   logout        revokes that token and deletes the file
#   snapshot      a Raft snapshot to VAULT_SNAPSHOT_DIR (default ~/Backups/itential-enterprise-lab/vault), mode 600,
#                 outside the repo (ADR 0065 decision 8); useless without the unseal key, which is kept apart
#   revoke-root   revokes the root token in VAULT_INIT_FILE - but only once an administrator login has been proven
#                 (Vault 2.0 needs a token for generate-root, so without that login there would be no way back)
# The administrator token for a run: VAULT_TOKEN, else VAULT_ADMIN_FILE (make vault-login), else the init file's root.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
CA=docs/lab-root-ca.crt
ADDR=$(${PY} -c "import yaml;print(yaml.safe_load(open('itential/versions.yaml'))['vault']['prod']['url'])")
INIT_FILE=${VAULT_INIT_FILE:-$HOME/.config/itential-enterprise-lab/vault-init.json}
SNAP_DIR=${VAULT_SNAPSHOT_DIR:-$HOME/Backups/itential-enterprise-lab/vault}
ADMIN_FILE=${VAULT_ADMIN_FILE:-$HOME/.config/itential-enterprise-lab/vault-admin-token}

admin_token() {
  if [ -n "${VAULT_TOKEN:-}" ]; then printf '%s' "$VAULT_TOKEN"
  elif [ -s "$ADMIN_FILE" ]; then tr -d '\n' < "$ADMIN_FILE"
  else root_token
  fi
}
root_token() { ${PY} -c 'import sys,json;print(json.load(open(sys.argv[1])).get("root_token") or "",end="")' "$INIT_FILE" 2>/dev/null || true; }
state() { curl -s -m 15 --cacert "$CA" "${ADDR}/v1/sys/seal-status"; }
# vcall METHOD PATH [TOKEN-VAR-NAME]: the body on stdin, the token from a file descriptor (never argv); prints
# "<http code> <json>" on one line
vcall() {
  local method=$1 path=$2 tok=${3:-}
  curl -s -m 30 --cacert "$CA" -X "$method" -H 'Content-Type: application/json' \
       ${tok:+-H @<(printf 'X-Vault-Token: %s\n' "${!tok}")} --data-binary @- -w '\n%{http_code}' "${ADDR}/v1/${path}" \
    | ${PY} -c 'import sys;lines=sys.stdin.read().rsplit("\n",1);print(lines[-1], lines[0] if len(lines)>1 else "")'
}
admin_cfg() { ${PY} -c "import yaml;a=yaml.safe_load(open('itential/versions.yaml'))['vault']['prod']['admin'];print(a['$1'])"; }

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
  admin-user)
    rt=$(root_token); [ -n "$rt" ] || { echo "no root token in $INIT_FILE: the administrator's password is set right after init"; exit 1; }
    user=$(admin_cfg user)
    read -rs -p "new password for ${user}: " pw1; echo
    read -rs -p "again: " pw2; echo
    [ "$pw1" = "$pw2" ] || { echo "the two entries differ - nothing changed"; exit 1; }
    [ ${#pw1} -ge 16 ] || { echo "use at least 16 characters - nothing changed"; exit 1; }
    out=$(${PY} -c 'import json,sys;print(json.dumps({"password": sys.stdin.read().rstrip("\n"), "token_policies": [sys.argv[1]], "token_ttl": sys.argv[2], "token_max_ttl": sys.argv[3]}))' \
            "$(admin_cfg policy)" "$(admin_cfg token_ttl)" "$(admin_cfg token_max_ttl)" <<<"$pw1" \
          | vcall POST "auth/$(admin_cfg auth_mount)/users/${user}" rt)
    unset pw1 pw2 rt
    [ "${out%% *}" = 204 ] || { echo "setting the password failed: ${out}"; exit 1; }
    echo "administrator ${user} has its password; next: make vault-login (and keep the password in your password manager)"
    ;;
  login)
    [ ! -s "$ADMIN_FILE" ] || { echo "$ADMIN_FILE exists - make vault-logout first"; exit 1; }
    user=$(admin_cfg user)
    read -rs -p "password for ${user}: " pw; echo
    mkdir -p "$(dirname "$ADMIN_FILE")"; chmod 700 "$(dirname "$ADMIN_FILE")"
    ${PY} -c 'import json,sys;print(json.dumps({"password": sys.stdin.read().rstrip("\n")}))' <<<"$pw" \
      | curl -s -m 30 --cacert "$CA" -X POST --data-binary @- "${ADDR}/v1/auth/$(admin_cfg auth_mount)/login/${user}" \
      | ${PY} -c '
import json, os, sys
d = json.load(sys.stdin); out, policy = sys.argv[1], sys.argv[2]
auth = d.get("auth") or {}
if not auth.get("client_token"):
    sys.exit(f"login refused: {d.get("errors")}")
if policy not in auth.get("policies", []):
    sys.exit(f"logged in, but without the {policy} policy - nothing written")
fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.write(fd, auth["client_token"].encode()); os.close(fd)
print(f"logged in: a {auth["lease_duration"] // 60}-minute {policy} token is in {out} (mode 600); make vault-logout when done")
' "$ADMIN_FILE" "$(admin_cfg policy)"
    unset pw
    ;;
  logout)
    [ -s "$ADMIN_FILE" ] || { echo "not logged in (no $ADMIN_FILE)"; exit 0; }
    at=$(tr -d '\n' < "$ADMIN_FILE")
    out=$(printf '{}' | vcall POST auth/token/revoke-self at); unset at
    rm -f "$ADMIN_FILE"
    echo "logged out: token revoked (HTTP ${out%% *}) and $ADMIN_FILE deleted"
    ;;
  snapshot)
    token=$(admin_token)
    [ -n "$token" ] || { echo "no administrator token: make vault-login first (in your own terminal)"; exit 1; }
    mkdir -p "$SNAP_DIR"; chmod 700 "$SNAP_DIR"
    out="$SNAP_DIR/vault-raft-$(date -u +%Y%m%dT%H%M%SZ).snap"
    # the header comes from a file descriptor, so the token never appears in argv (ps)
    code=$( umask 077; curl -s -m 120 --cacert "$CA" -H @<(printf 'X-Vault-Token: %s\n' "$token") \
            -o "$out" -w '%{http_code}' "${ADDR}/v1/sys/storage/raft/snapshot" )
    unset token
    [ "$code" = 200 ] && [ -s "$out" ] || { rm -f "$out"; echo "snapshot failed (HTTP ${code})"; exit 1; }
    echo "snapshot: $out ($(du -h "$out" | cut -f1), mode $(stat -f %Lp "$out" 2>/dev/null || stat -c %a "$out"))"
    ;;
  revoke-root)
    rt=$(root_token); [ -n "$rt" ] || { echo "no root token in $INIT_FILE - nothing to revoke"; exit 1; }
    # the lesson of 2026-09-24: never revoke the last way in. The administrator login must work first.
    [ -s "$ADMIN_FILE" ] || { echo "refusing: log in as the administrator first (make vault-login) so a working admin path exists"; exit 1; }
    at=$(tr -d '\n' < "$ADMIN_FILE")
    me=$(printf '' | vcall GET auth/token/lookup-self at); unset at
    ${PY} -c 'import json,sys;c,_,b=sys.argv[1].partition(" ");d=json.loads(b or "{}");sys.exit(0 if c=="200" and sys.argv[2] in d.get("data",{}).get("policies",[]) else 1)' "$me" "$(admin_cfg policy)" \
      || { echo "refusing: the token in $ADMIN_FILE does not work as the administrator"; exit 1; }
    out=$(printf '' | vcall POST auth/token/revoke-self rt)
    [ "${out%% *}" = 204 ] || { echo "revoke failed (${out}) - the root token is unchanged"; exit 1; }
    after=$(printf '' | vcall GET auth/token/lookup-self rt); unset rt
    ${PY} -c 'import sys,json,os;p=sys.argv[1];d=json.load(open(p));d.pop("root_token",None);f=os.open(p+".tmp",os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600);os.write(f,json.dumps(d,indent=2).encode());os.close(f);os.replace(p+".tmp",p)' "$INIT_FILE"
    echo "root token revoked (a lookup with it now answers HTTP ${after%% *}) and removed from $INIT_FILE; the administrator login remains"
    [ "${after%% *}" = 403 ]
    ;;
  *) echo "usage: $0 status|init|unseal|admin-user|login|logout|snapshot|revoke-root"; exit 2 ;;
esac
