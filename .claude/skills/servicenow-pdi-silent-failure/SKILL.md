---
name: servicenow-pdi-silent-failure
description: Use when a ServiceNow workflow or agent reports success but no ticket data came back, when a ServiceNow response is HTML or "Instance Hibernating", when building or reviewing ServiceNow tasks in workflows (change requests, incident reads), or when the PDI may be asleep.
---

# ServiceNow PDI silent failure

A hibernating Personal Developer Instance answers HTTP 200 with an HTML page. Workflow tasks treat 200 as
success, so jobs "complete" with garbage. ADR 0066 and ADR 0059 are the rule: check every result and end the job on
failure with a reason.

## Checks first (read-only)

1. Look at the raw response, not the status: content-type and the first characters of the body.
2. Healthy: `application/json` and a body starting with `{` containing `result`. Asleep: `text/html`, a body with
   `<html`, "Hibernating" or a login page.
3. Read the workflow's current check for ServiceNow tasks (`itential/versions.yaml` names the workflows).

## Procedure: make a workflow detect it

After each ServiceNow call add an evaluation task:
- content-type contains `application/json` AND the parsed body has a `result` key, else failure.
- The failure edge goes to `workflow_end` with a note that publishes a reason, e.g. `servicenow_error: non-JSON
  response (instance asleep?)`. Never leave a task without its failure edge (it dead-ends as a retryable `error`).
- Add a failing-path test beside the existing ones (`tests/test_workflow_failure_paths.py`) rather than testing
  by hand on production.

## Waking the PDI

Waking needs the owner's own login on the ServiceNow developer site. You cannot and should not do it: tell the user
the instance is asleep and ask them to wake it. Never ask for or type the credentials (see
`browser-automation-on-itential`). After they say it is up, re-run the read-only check.

## Don't

- Don't trust HTTP 200 from ServiceNow.
- Don't "fix" an empty result by retrying in a loop; one retry after the user wakes the instance.
- Don't write change records to test detection; use a read.

## Verify

With the PDI asleep, the job ends failed with the reason (or a unit test of the evaluator shows it); with it awake,
the same job completes with a `result` payload.
