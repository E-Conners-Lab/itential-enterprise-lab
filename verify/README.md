# verify/ — integration tests, one per service

`verify/run.sh` executes every `test-*.sh` and writes a timestamped log to
`verify/results/`. Results are committed: they are the evidence a phase is done.

Rules (from the kickoff brief):
- A test proves **integration**, not liveness. "NetBox returns the reserved IP",
  "Zabbix discovered a device over OOB", "Oxidized pulled a config" — not "port open".
- Fail loud. A test that cannot reach its target exits non-zero; it never skips.
- Discovery (`discover.sh`) is read-only and also records to `results/`.
