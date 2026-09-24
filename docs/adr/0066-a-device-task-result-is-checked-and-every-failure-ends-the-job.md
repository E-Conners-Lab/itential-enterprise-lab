# 0066 — A device task's result is checked, and every failure ends the job with a reason

- **Status:** accepted (owner decisions, 2026-09-23 and 2026-09-24)
- **Date:** 2026-09-24
- **Amends:** ADR 0059 (its error edges catch a *thrown* task; this adds the failures that do not throw)
- **Related:** ADR 0040 (the governed push), ADR 0043 (Lifecycle Manager, branch-vlan), ADR 0046 (the agent fleet),
  ADR 0054 (Integration Models), ADR 0063 (the dev tier where every measurement below was made)

## Context

ADR 0059 gave the device-sending tasks an `error` edge after a Gateway 404 dead-ended a job and hung an agent. Testing
failure paths on the dev tier (Platform 6.5.2, Gateway 5.5.2) found three kinds of failure that rule does not reach:

1. **A Gateway error answer is a success.** When the Gateway cannot run a device service (seen with a sealed secrets
   store, and with the runner missing the netsdk file after a Gateway restart), it answers a JSON-RPC error envelope
   (`"status": "error"`), and `sendCommand`/`sendConfig` still finish `success`. `wf-show-version-v1` then ends
   `complete` with the error object as its "show version": a silent failure.
2. **A device's refusal is a success.** `send-config` reports `success: true` for lines the device rejects. Measured:
   IOS-XE answered `% Invalid input detected` inside `output`, with `success: true`. So the "config applied?" checks
   in `wf-config-push-v1` and `wf-branch-vlan-v1`, which read that flag, report refused lines as applied.
3. **Failures that dead-end.** A task without an edge for its failure leaves the job in `error`, which the Platform
   treats as retryable, not finished: a calling agent session stays `RUNNING` and a Lifecycle Manager action stays
   `running`. Found in `wf-show-command-v1` (the NetBox platform lookup and the parse after the device had already
   answered, losing that answer), `wf-netbox-devices-v1` (its NetBox read), `wf-compliance-report-v1` (ten Configuration
   Manager and runner calls, and a run still unfinished after the last attempt, by design), `wf-config-push-v1`
   ("config applied?" false, by design), and `wf-branch-vlan-v1` (thirteen NetBox and ServiceNow calls).

Also found: `wf-compliance-report-v1` computed `compliant = not bad` over the reports that existed, so a run with no
device report returned `"compliant": true, "devices_checked": 0`.

**Production already hit the first and third.** Its job history (570 jobs, read GET-only on 2026-09-24) holds nine
`show` jobs on 2026-09-10 that ended `complete` while the Gateway could not start its device tool, and about twenty
jobs on 2026-09-10/11 that dead-ended (a NetBox parameter error, a ServiceNow `sys_id` error, Gateway 503s). None
since; nothing has broken since. No compliance run ever returned zero devices.

## Decision

1. **Every `sendCommand`/`sendConfig` is followed by an evaluation of its result**: `result.results[0].success == true`
   for one device, the envelope's `status == "completed"` for the multi-device `wf-show-all-v1`. The evaluation's
   failure reaches `workflow_end` through a note that publishes `device_error`. `tests/test_workflow_failure_paths.py`
   holds every `wf-*.json` to it.
2. **A write reads the device's own reply.** `wf-config-push-v1` and `wf-branch-vlan-v1` run `REPLY_CODE` on the runner
   after the push: a line starting `% Invalid`, `% Incomplete`, `% Ambiguous` (and the like) is a refusal, and
   `% Warning` is not. A refused config push is **not saved**, and ends `changed = false`, naming the refused lines
   and saying that lines accepted before them are in the running configuration, unsaved. A branch-vlan refusal rolls
   the NetBox reservation back (it may leave the VLAN on the switch if only its `name` line was refused, and says so).
   A reply that cannot be read skips the save (config push), or ends in branch-vlan's designed error **without** a
   rollback, because the push most likely worked.
3. **Read-only agent tools end cleanly with a reason** (owner decision 2026-09-23): `wf-compliance-report-v1`
   (`report_error`, and `compliant` needs at least one evaluated device, with the others listed in
   `devices_not_checked`), `wf-netbox-devices-v1` (`netbox_error`), and `wf-show-command-v1`, which keeps the raw
   answer and says why no parser ran (`parse_error`).
4. **The config push ends cleanly when nothing was applied** (owner decision 2026-09-23), where it used to end in error
   by design. Its approval-reject path keeps its deliberate error-end: a separate decision.
5. **`wf-branch-vlan-v1` keeps its designed error-end** (owner decision 2026-09-23). Only its one external call between
   the NetBox reservation and the push (`e6`, the ServiceNow work note) now goes through the existing rollback on an
   error. Calls before the reservation have changed nothing; calls after the push must not roll back, because the
   VLAN is live on the switch.

## Proven on the dev tier (2026-09-23/24)

- A device job and a NetBox job with the Gateway, and then the Platform, unable to reach their credentials: both ended
  through their error paths, succeeded again afterwards, and the router's configuration was unchanged.
- A refused push (`this-is-not-a-command` to a C8000v, through the Work Center approval): `complete`, `changed = false`,
  the refusal in `device_error`, no `write memory`. An accepted push: `changed = true`, saved.
- `wf-show-command-v1` on a C8000v and a vEOS node that NetBox does not know: `complete` with the raw output and
  `parse_error`, where the job used to dead-end.
- The Platform imported every changed workflow without errors (a workflow it disagrees with is left as a draft);
  `wf-branch-vlan-v1` shows only dev's expected "no ServiceNow" errors.
- `wf-compliance-report-v1` and `wf-branch-vlan-v1` cannot run on dev (no compliance plans, no ServiceNow): their
  failure paths are proven by the tests and the import only.

## Consequences

- An agent or a script gets an answer - a result or a reason - instead of a false success or a hung session.
- The write path now depends on the Gateway runner to read the reply (as the parsers already do). A refused line is
  quoted in `device_error`; the job's own `config` input already holds the same text.
- `changed = false` after a refusal does not mean "nothing on the device changed": lines before the refused one were
  applied and not saved, and the message says so.
- Still open: a branch-vlan job that ends in error (by design) still leaves its caller waiting. The fix for that goes
  with a decision on branch-vlan's Lifecycle Manager semantics.
