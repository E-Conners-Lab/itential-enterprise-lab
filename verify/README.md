# verify/ — integration tests, one per service

`verify/run.sh` executes every `test-*.sh` and writes a timestamped log to
`verify/results/`. Results are committed: they are the evidence a phase is done.

Dev-tier scripts are the exception (ADR 0063): a script with the word `dev` in its name after
the test number (`test-05b-dev-copilot.sh`, `test-12a-clab-dev.sh`) is never selected by
`verify/run.sh` or `make verify`; `make verify-dev` runs them. `verify/run.sh --list` prints the
selection without running anything.

Rules (from the kickoff brief):
- A test proves **integration**, not liveness. "NetBox returns the reserved IP",
  "Zabbix discovered a device over OOB", "Oxidized pulled a config" — not "port open".
- Fail loud. A test that cannot reach its target exits non-zero; it never skips.
- Discovery (`discover.sh`) is read-only and also records to `results/`.
