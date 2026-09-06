#!/usr/bin/env bash
# Runs every verify/test-*.sh, records stdout+exit code to verify/results/<UTC-timestamp>-<name>.log,
# and fails if any test fails. No test found is itself a failure (fail loud).
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p results
ts=$(date -u +%Y%m%dT%H%M%SZ)
shopt -s nullglob
tests=(test-*.sh)
if [ ${#tests[@]} -eq 0 ]; then echo "no verify/test-*.sh found"; exit 1; fi
fail=0
for t in "${tests[@]}"; do
  name=${t#test-}; name=${name%.sh}
  log="results/${ts}-${name}.log"
  echo "== $t -> $log"
  if bash "$t" >"$log" 2>&1; then echo "PASS $name"; echo "RESULT: PASS" >>"$log"
  else echo "FAIL $name"; echo "RESULT: FAIL" >>"$log"; fail=1; fi
done
exit $fail
