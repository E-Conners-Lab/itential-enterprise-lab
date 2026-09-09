# 0044 — The JSON form is the approval task of `wf-branch-vlan-v1` (ShowJsonForm with a decision field replaces ViewData)

- **Status:** accepted
- **Date:** 2026-09-08
- **Amends:** ADR 0036 (the S4.4 approval task), PID S4.4 note in amendment 1.9
- **Related:** ADR 0043, PID S4d.3

## Context

PID S4d.3 wants branch, VLAN, switch and the NetBox reservation shown on the approval task. The
`ViewData` task of ADR 0036 shows a header, a message and a body text; JSON Forms render typed fields.
Measured on 2026-09-08 (memory `itential-platform-lessons`): the manual task `ShowJsonForm` (app
`JsonForms`, view `/json-forms/task/ShowJsonForm`) takes `form_id` (the form's name works) and
`instance_data` (defaults; it must be a top-level `$var` object because a nested `$var` never resolves),
pauses the job, appears in Work Center as a pending item whose resolved incoming variables the Work Center
API returns, and returns the submitted form as `export`. The task has no reject button, so a rejection has
to be a value on the form. Finishing the task through the API takes the form data in `variables.export`.

## Decision

- **Form** `lab-branch-vlan-approval`, generated into `itential/forms/lab-branch-vlan-approval.json` by
  `itential/forms/build.py` from one field list so `struct`, `schema` and `uiSchema` cannot drift: read-only
  `branch`, `vid`, `vlan_name`, `switch`, `netbox_vlan_id` (the reservation) and `status`, plus `decision`
  (approve | reject, required, **default reject**: an untouched form pushes nothing). `tasks/lcm.yml` creates
  it and replaces it in place when the generated document differs.
- **`wf-branch-vlan-v1`**: the approval task `4a` becomes `ShowJsonForm` on that form with the instance
  data built from the job variables (the same object the workflow later stores as the LCM instance, ADR
  0043); an evaluation of `export.decision == approve` gates the device push; any other decision, and a
  `failure` finish from the API, take the existing rollback (NetBox reservation deleted, job in error).
- **Verify scripts** 05, 06 and 06b finish the task with `finish_state success` and
  `variables.export.decision = approve`; a rejection stays a `failure` finish. Verify S4d.3 reads the pending
  Work Center item's incoming variables as the proof of what the approver sees, with NetBox as the second
  source for the reservation.

## Consequences

- S4.4, S4b.2 and S4c.3 keep their behaviour and inputs; only the finish payload of an API approval changed
  (PID 1.9 records it under S4.4). The card Elliot approves shows typed fields instead of one summary line.
  A reject still ends the job in error after the rollback; under Lifecycle Manager that execution is then
  cancelled (ADR 0043).
- Forms live as generated JSON with a `--check` mode in the generator, held by `tests/test_lcm.py` like the
  workflows are held by `tests/test_itential.py`; the same form can back a manual trigger later.
- Rejected: keeping `ViewData` with the fields in the body text (not typed, not a form); a form task before
  the `ViewData` (two cards per change); a default decision of approve.
