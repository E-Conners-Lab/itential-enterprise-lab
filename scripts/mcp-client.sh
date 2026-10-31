#!/bin/bash
# ADR 0083 decision 5: one client machine's side of the production MCP server's login. Whoever logs in with this
# machine's AppRole (mcp-client-<hostname>, bound to its address in topology/ipam.yaml home_lan.mcp_clients) can read a
# one-hour token signed by Vault's identity provider; the MCP server accepts nothing else.
#
#   token        Claude Code's headersHelper: prints {"Authorization": "Bearer <token>"} and nothing else
#   check        logs in, takes a token and prints its issuer, audience and expiry - never the token
#   unwrap       on the new machine: asks for a wrapping token without echoing it, unwraps the machine's role and
#                secret IDs straight into this Keychain, then runs check
#   enrol NAME   ADMIN, on machine NAME itself: a new secret ID straight into this Keychain, then every older one of
#                NAME's destroyed
#   wrap NAME    ADMIN, in YOUR terminal: a new secret ID for machine NAME, response-wrapped (single use, wrap_ttl in
#                itential/versions.yaml); prints the wrapping token, the one value this script ever shows
#   revoke NAME  ADMIN: destroys every secret ID of NAME's AppRole; that machine can no longer log in
#
# Stock macOS only (bash 3.2, curl, plutil, security), so it runs on a machine without the repo: copy it with
# docs/lab-root-ca.crt beside it (MCP_CA overrides). Vault is reached at its VIP under its own name, so the machine
# needs a route to the lab but no lab.internal resolver. ADMIN commands read the owner's lab-admin token from
# VAULT_ADMIN_FILE (make vault-login). No secret goes into argv, a file or a here-string (which bash 3.2 writes to a
# temporary file): values move through pipes and file descriptors only. tests/test_mcp_access.py ties the constants
# below to itential/versions.yaml.
set -eu

VAULT_HOST=vault.lab.internal
VAULT_PORT=8200
VAULT_VIP=10.100.0.41
APPROLE_MOUNT=approle
APPROLE_PREFIX=mcp-client-
TOKEN_ROLE=itential-mcp
WRAP_TTL=10m
KEYCHAIN_SERVICE=itential-lab-mcp

here=$(cd "$(dirname "$0")" && pwd)
if [ -n "${MCP_CA:-}" ]; then CA=$MCP_CA
elif [ -f "$here/../docs/lab-root-ca.crt" ]; then CA=$here/../docs/lab-root-ca.crt
else CA=$here/lab-root-ca.crt; fi
ADMIN_FILE=${VAULT_ADMIN_FILE:-$HOME/.config/itential-enterprise-lab/vault-admin-token}

die() { echo "mcp-client: $*" >&2; exit 1; }
# exit 3: the login was accepted but Vault issued no MCP token (its identity provider is not set up yet: the ceremony)
no_token() { echo "mcp-client: $*" >&2; exit 3; }

# vcall METHOD PATH [TOKEN-VAR] [WRAP-TTL]: the body on stdin, the token from a file descriptor; sets $code and $body
vcall() {
  local method=$1 path=$2 tok=${3:-} wrap=${4:-} out
  [ -f "$CA" ] || die "no lab CA at $CA (set MCP_CA)"
  if [ -n "$tok" ]; then
    out=$(curl -s -m 20 --cacert "$CA" --resolve "$VAULT_HOST:$VAULT_PORT:$VAULT_VIP" -X "$method" \
            -H 'Content-Type: application/json' -H @<(printf 'X-Vault-Token: %s\n' "${!tok}") \
            ${wrap:+-H "X-Vault-Wrap-TTL: $wrap"} --data-binary @- -w '\n%{http_code}' \
            "https://$VAULT_HOST:$VAULT_PORT/v1/$path") || out=$'\n000'
  else
    out=$(curl -s -m 20 --cacert "$CA" --resolve "$VAULT_HOST:$VAULT_PORT:$VAULT_VIP" -X "$method" \
            -H 'Content-Type: application/json' --data-binary @- -w '\n%{http_code}' \
            "https://$VAULT_HOST:$VAULT_PORT/v1/$path") || out=$'\n000'
  fi
  code=${out##*$'\n'}
  body=${out%$'\n'*}
}

# jget KEYPATH: one value out of the JSON on stdin (plutil reads JSON; array elements are numbered keys)
jget() { plutil -extract "$1" raw -o - - 2>/dev/null; }

kc_get() { security find-generic-password -s "$KEYCHAIN_SERVICE" -a "$1" -w 2>/dev/null; }
# kc_put ACCOUNT VALUE-VAR: security reads the command on stdin, so the value never reaches its argv
kc_put() {
  printf 'add-generic-password -U -s %s -a %s -w %s\n' "$KEYCHAIN_SERVICE" "$1" "${!2}" | security -i >/dev/null 2>&1
  [ "$(kc_get "$1")" = "${!2}" ] || die "could not store $1 in the Keychain"
}

admin_token() {
  [ -s "$ADMIN_FILE" ] || die "no administrator token: make vault-login first (in your own terminal)"
  at=$(tr -d '\n' < "$ADMIN_FILE")
}

# a client machine's AppRole; NAME is a hostname from topology/ipam.yaml home_lan.mcp_clients
approle() { case "${1:-}" in ''|-*|*[!a-z0-9-]*) die "name a client machine (a hostname, a-z 0-9 -)";; esac; printf '%s%s' "$APPROLE_PREFIX" "$1"; }

# the identity token, on stdout; the Vault login behind it is revoked once the token is read
mint() {
  local rid sid vt jwt
  rid=$(kc_get role-id) && sid=$(kc_get secret-id) || die "no role or secret ID in this Keychain: run unwrap (or enrol) first"
  vcall POST "auth/$APPROLE_MOUNT/login" < <(printf '{"role_id":"%s","secret_id":"%s"}' "$rid" "$sid")
  unset sid
  [ "$code" = 200 ] || die "Vault refused the login (HTTP $code): wrong machine address, or this machine's secret ID was revoked"
  vt=$(printf '%s' "$body" | jget auth.client_token)
  vcall GET "identity/oidc/token/$TOKEN_ROLE" vt < <(printf '')
  [ "$code" = 200 ] || { printf '{}' | { vcall POST auth/token/revoke-self vt; }; no_token "login accepted, but Vault issued no MCP token (HTTP $code)"; }
  jwt=$(printf '%s' "$body" | jget data.token)
  printf '{}' | { vcall POST auth/token/revoke-self vt; } || true
  unset vt
  [ -n "$jwt" ] || die "Vault's answer held no token"
  printf '%s' "$jwt"
}

# the claims of a JWT on stdin (base64url, unpadded), as JSON
claims() {
  local p
  p=$(cut -d. -f2 | tr '_-' '/+')
  case $(( ${#p} % 4 )) in 2) p="$p==";; 3) p="$p=";; esac
  printf '%s' "$p" | base64 -D
}

check() {
  local jwt c
  jwt=$(mint) || return $?
  c=$(printf '%s' "$jwt" | claims)
  unset jwt
  printf 'token issued: iss=%s aud=%s, valid for %s min\n' \
    "$(printf '%s' "$c" | jget iss)" "$(printf '%s' "$c" | jget aud)" \
    $(( ($(printf '%s' "$c" | jget exp) - $(date +%s)) / 60 ))
}

# new_secret ROLE: a fresh secret ID for ROLE; sets $rid, $sid and $acc (needs $at)
new_secret() {
  vcall GET "auth/$APPROLE_MOUNT/role/$1/role-id" at < <(printf '')
  [ "$code" = 200 ] || die "no AppRole $1 (HTTP $code): make vault-config first"
  rid=$(printf '%s' "$body" | jget data.role_id)
  vcall POST "auth/$APPROLE_MOUNT/role/$1/secret-id" at < <(printf '{}')
  [ "$code" = 200 ] || die "could not create a secret ID for $1 (HTTP $code)"
  sid=$(printf '%s' "$body" | jget data.secret_id)
  acc=$(printf '%s' "$body" | jget data.secret_id_accessor)
}

# destroy_secrets ROLE [KEEP-ACCESSOR]: every secret ID of ROLE except KEEP; prints how many went
destroy_secrets() {
  local i=0 a n=0
  vcall LIST "auth/$APPROLE_MOUNT/role/$1/secret-id" at < <(printf '')
  [ "$code" = 200 ] || { echo 0; return; } # 404: none
  while a=$(printf '%s' "$body" | jget "data.keys.$i"); do
    i=$((i + 1))
    [ "$a" = "${2:-}" ] && continue
    printf '{"secret_id_accessor":"%s"}' "$a" | { vcall POST "auth/$APPROLE_MOUNT/role/$1/secret-id-accessor/destroy" at; [ "$code" = 204 ]; } \
      || die "could not destroy secret ID accessor $a of $1"
    n=$((n + 1))
  done
  echo "$n"
}

case "${1:-}" in
  token)
    jwt=$(mint)
    printf '{"Authorization": "Bearer %s"}\n' "$jwt"
    unset jwt
    ;;
  check)
    check
    ;;
  unwrap)
    read -rs -p "wrapping token: " wt; echo
    vcall POST sys/wrapping/unwrap wt < <(printf '{}')
    unset wt
    [ "$code" = 200 ] || die "unwrap refused (HTTP $code): used already, expired, or mistyped - ask for a new one"
    name=$(printf '%s' "$body" | jget data.name)
    rid=$(printf '%s' "$body" | jget data.role_id)
    sid=$(printf '%s' "$body" | jget data.secret_id)
    body=''
    kc_put role-id rid
    kc_put secret-id sid
    unset rid sid
    echo "stored the role and secret IDs of $(approle "$name") in this Keychain (service $KEYCHAIN_SERVICE)"
    check
    ;;
  enrol)
    role=$(approle "${2:-}")
    admin_token
    new_secret "$role"
    kc_put role-id rid
    kc_put secret-id sid
    unset sid
    echo "stored a new secret ID of $role in this Keychain (accessor $acc)"
    rc=0; check || rc=$?
    # the older secret IDs go only once the new one has logged in from here (rc 3: login proven, no token until the ceremony)
    [ "$rc" = 0 ] || [ "$rc" = 3 ] || die "the new secret ID did not log in from this machine: older ones kept"
    gone=$(destroy_secrets "$role" "$acc")
    echo "older secret IDs of $role destroyed: $gone"
    unset at
    ;;
  wrap)
    role=$(approle "${2:-}")
    admin_token
    new_secret "$role"
    vcall POST sys/wrapping/wrap at "$WRAP_TTL" \
      < <(printf '{"name":"%s","role_id":"%s","secret_id":"%s"}' "$2" "$rid" "$sid")
    unset sid at
    [ "$code" = 200 ] || die "wrapping refused (HTTP $code)"
    echo "secret ID for $role made (accessor $acc); its single-use wrapping token, valid $WRAP_TTL:"
    printf '%s' "$body" | jget wrap_info.token
    echo
    echo "on that machine: scripts/mcp-client.sh unwrap   (then paste it; it is not echoed)"
    ;;
  revoke)
    role=$(approle "${2:-}")
    admin_token
    gone=$(destroy_secrets "$role")
    echo "secret IDs of $role destroyed: $gone"
    unset at
    ;;
  *)
    sed -n '2,24p' "$0"
    exit 2
    ;;
esac
