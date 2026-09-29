#!/usr/bin/env bash
# OWNER, in your own terminal (ADR 0068 decision 3): create an access key for the IAM user itential-terraform and
# write it straight into one tier's Vault at lab/aws/terraform (versions.yaml vault.aws). The secret key is never
# printed, never written to a file or argv, and never in a transcript: it lives in this shell's memory only until
# Vault has it. Each tier has its own key (owner decision 2026-09-29), so either can be revoked alone.
#   scripts/aws-key-to-vault.sh dev    the dev Vault on itential-dev (its root token, read over SSH)
#   scripts/aws-key-to-vault.sh prod   production's Vault (your lab-admin token: make vault-login first)
# Uses your own AWS admin credentials (AWS_PROFILE, default "default"). It refuses when the tier already holds a key
# (rotation is a later workflow) and when the user already has two keys. If Vault does not take the key, the new
# key is deleted again, so no key exists that Vault does not hold. The key's IAM description tag names its tier.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
CA=docs/lab-root-ca.crt
V=itential/versions.yaml
val() { ${PY} -c "import yaml,sys;d=yaml.safe_load(open(sys.argv[1]));print(eval(sys.argv[2],{'d':d}))" "$1" "$2"; }
SSH="ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new"

TIER=${1:-}
case "$TIER" in
  dev)
    ADDR=$(val "$V" "d['vault']['dev']['url']")
    TOKEN=$(${SSH} "ubuntu@$(val "$V" "d['vm']['ip']")" "sudo cat $(val "$V" "d['vault']['dev']['dir']")/init.json" \
            | ${PY} -c 'import json,sys;print(json.load(sys.stdin)["root_token"],end="")') \
      || { echo "could not read the dev Vault's root token over SSH (make vault-dev first)"; exit 1; }
    ;;
  prod)
    ADDR=$(val "$V" "d['vault']['prod']['url']")
    ADMIN_FILE=${VAULT_ADMIN_FILE:-$HOME/.config/itential-enterprise-lab/vault-admin-token}
    [ -s "$ADMIN_FILE" ] || { echo "no administrator token: run make vault-login first"; exit 1; }
    TOKEN=$(tr -d '\n' < "$ADMIN_FILE")
    ;;
  *) echo "usage: $0 dev|prod"; exit 2 ;;
esac
USER_NAME=$(val "$V" "d['vault']['aws']['iam_user']")
KV_PATH="$(val "$V" "d['vault']['kv_mount']")/data/$(val "$V" "d['vault']['aws']['key_path']")"

# vault METHOD: the body on stdin, the token from a file descriptor (never argv); prints the HTTP code only
vault() {
  curl -s -m 30 --cacert "$CA" -X "$1" -H 'Content-Type: application/json' \
       -H @<(printf 'X-Vault-Token: %s\n' "$TOKEN") --data-binary @- -o /dev/null -w '%{http_code}' "${ADDR}/v1/${KV_PATH}"
}

# 1. Vault answers this token, and the tier holds no key yet (a 404; the body is never read)
have=$(printf '' | vault GET)
case "$have" in
  404) ;;
  200) echo "$TIER Vault already holds a key at $KV_PATH - nothing done (rotation is a later workflow)"; exit 1 ;;
  *) echo "$TIER Vault answered $have for $KV_PATH (token expired? make vault-login / make vault-dev)"; exit 1 ;;
esac

# 2. The user exists and has room for one more key (IAM allows two)
count=$(aws iam list-access-keys --user-name "$USER_NAME" --query 'length(AccessKeyMetadata)' --output text) \
  || { echo "cannot list $USER_NAME's keys: apply terraform/bootstrap/itential-iam in cloud-devops-pipeline first"; exit 1; }
[ "$count" -lt 2 ] || { echo "$USER_NAME already has two access keys - nothing done"; exit 1; }

# 3. Create the key and hand it to Vault through a pipe; on any failure the new key is deleted again
KEY_JSON=$(aws iam create-access-key --user-name "$USER_NAME" --output json)
KEY_ID=$(printf '%s' "$KEY_JSON" | jq -r '.AccessKey.AccessKeyId')
drop_key() { aws iam delete-access-key --user-name "$USER_NAME" --access-key-id "$KEY_ID" && echo "the new key was deleted again"; }
stored=$(printf '%s' "$KEY_JSON" \
         | jq '{data: {access_key_id: .AccessKey.AccessKeyId, secret_access_key: .AccessKey.SecretAccessKey}}' \
         | vault POST) || stored=000
unset KEY_JSON
if [ "$stored" != 200 ]; then
  echo "$TIER Vault answered $stored to the write - nothing stored"
  drop_key
  exit 1
fi

# 4. Name the key's tier (the IAM console's description tag), and show only what is not secret
aws iam tag-user --user-name "$USER_NAME" --tags "Key=${KEY_ID},Value=vault ${TIER} ${KV_PATH}"
echo "key ...${KEY_ID: -4} of $USER_NAME is in the $TIER Vault at $KV_PATH (HTTP $stored); the secret key was never shown"
aws iam list-access-keys --user-name "$USER_NAME" \
  --query 'AccessKeyMetadata[].[join(``, [`...`, AccessKeyId]), Status, CreateDate]' --output text \
  | sed -E 's/\.\.\.[A-Z0-9]*([A-Z0-9]{4})/...\1/'
