#!/usr/bin/env bash
# Runs every verify/test-*.sh, records stdout+exit code to verify/results/<UTC-timestamp>-<name>.log,
# and fails if any test fails. No test found is itself a failure (fail loud).
#
# Dev-tier scripts are not selected (ADR 0063): a script with the word `dev` in its name after the test number
# (`test-<n>-dev-*.sh` or `test-<n>-*-dev.sh`, e.g. test-05b-dev-copilot.sh, test-12a-clab-dev.sh) belongs to
# `make verify-dev`, so a torn-down sandbox can never turn this suite red. `dev` must be a whole word:
# test-NN-device.sh is still a production test.
#
#   verify/run.sh          run the selection
#   verify/run.sh --list   print the selection, one name per line, and run nothing
set -euo pipefail
cd "$(dirname "$0")"
shopt -s nullglob
tests=()
for t in test-*.sh; do
  case "${t#test-}" in
    *-dev-*|*-dev.sh) continue ;;
  esac
  tests+=("$t")
done
if [ ${#tests[@]} -eq 0 ]; then echo "no verify/test-*.sh found"; exit 1; fi
if [ "${1:-}" = --list ]; then printf '%s\n' "${tests[@]}"; exit 0; fi
mkdir -p results
ts=$(date -u +%Y%m%dT%H%M%SZ)
fail=0
for t in "${tests[@]}"; do
  name=${t#test-}; name=${name%.sh}
  log="results/${ts}-${name}.log"
  echo "== $t -> $log"
  if bash "$t" >"$log" 2>&1; then echo "PASS $name"; echo "RESULT: PASS" >>"$log"
  else echo "FAIL $name"; echo "RESULT: FAIL" >>"$log"; fail=1; fi
done
exit $fail
