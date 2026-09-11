# 0059 — A workflow a twin can call must always reach its end, and a prompt must not forbid what its tools cannot supply

- **Status:** accepted (2026-09-11)
- **Date:** 2026-09-11
- **Related:** ADR 0037 (FlowAI agents), ADR 0038 (Gateway 5 parsers and the UUID rule), ADR 0040 (the governed push), ADR 0046 (the agent fleet, tiered autonomy), ADR 0054 (Integration Models), PID S4c/S4d.5

## Context

`device-ops-local` was asked a question, invented the device name `R1`, and called `wf-show-command-v1`
with it. Gateway 5 answered `404 "Missing nodes - Inventory 'lab': [R1]"`. The job's `sendCommand` task
had no failure transition, so the job ended:

```
"Job has no available transitions. 4d could have led to the workflow end task, but did not."
```

The job errored **69 seconds** in. The agent session was still `RUNNING` **eighteen minutes** later, when
the operator cancelled it. Ollama runs `OLLAMA_NUM_PARALLEL=1` and `OLLAMA_MAX_LOADED_MODELS=1` on
tools-01, so that session held the only local inference slot for the whole time: one bad device name
denies the local fleet to every other agent.

Two independent defects met here, and both were invisible to the tests we had.

**A workflow that cannot reach `workflow_end` never returns to its caller.** ADR 0038's known failure -
a stale tool UUID - at least *ends*: the session fails in about two seconds having spent zero tokens,
and the runbook teaches that signature. A dead-ended job is worse, because it never resolves at all.
`tests/test_lcm.py` already asserted every task is *reachable from* `workflow_start`; nothing asserted a
failing task can still *reach* `workflow_end`.

**A prompt that forbids what the tools cannot supply is an instruction to disobey.** `device-ops-local`
was told *"you never invent a device name"* while holding only `wf-show-command-v1` and
`wf-show-all-v1` - no tool that returns a real name. `wf-show-command-v1` takes `device` as a free-text
string, so the workflow does not constrain the name either; the protection ADR 0046 describes ("the
twins do not get the raw gateway tool") is thinner than it reads. A 7B model handed an impossible
instruction does the thing anyway.

It survived because **four of the five local twins were never exercised by any verify**:
`device-ops-local`, `compliance-local`, `diagnostics-local` and `remediation-local`. The twins cost no
provider tokens, so there was never a budget reason for them to be the untested ones. A static audit of
all thirteen documents - tools resolve, no prompt names a tool its agent lacks, every device and site
cited exists in NetBox - found **zero issues**, which is the point: this class of defect is behavioural
and only running the agent finds it.

## Decision

1. **Every task that sends to a device carries an `error` transition that reaches `workflow_end` and
   publishes a message.** The state matters and the first attempt got it wrong: a Gateway task that
   answers 404 lands in state **`error`**, and a `failure` edge does **not** fire for it. Measured
   2026-09-11 — with `2a -> {3a: success, 5a: failure}` stored correctly on the job and `2a` in state
   `error`, the job still ended *"5a could have led to the workflow end task, but did not"*. `failure`
   is for a task that **completes** unsuccessfully: an `evaluate` returning false, or a rejected
   approval form (which is why `wf-config-push-v1`'s reject edge has always worked). `branch_vlan`
   already guarded its `sendConfig` with `state: error`; the pattern was in the file. `wf-show-command-v1` gains `device_error`; `wf-config-push-v1` gains
   `device_error` on the push and `save_error` on the `write memory` that follows it. An unknown device
   now ends the job with something the agent can report and stops retrying.
   `tests/test_agent_fleet.py` holds it for every device-sending task in both workflows.
2. **The two deliberate error-ends in `wf-config-push-v1` stay.** A *rejected* approval (`9a`) and a
   push that did not apply (`3b`) end the job in error on purpose - that is how "nothing was changed"
   is signalled, and changing it would change what an approval means. Whether a rejected approval
   should also return cleanly to a calling agent is a separate decision, deliberately not taken here.
3. **An agent forbidden from inventing a device name holds a tool that returns real ones.**
   `device-ops-local` gains `wf-netbox-devices-v1` (three tools, inside ADR 0046's one-to-three rule).
   A test fails any document whose prompt forbids inventing a name without such a tool. The rule is
   about names specifically: `lab-netops-mac`'s "never invent device *output*" is a different
   instruction and is not caught by it.
4. **Every `ollama-lab` document runs in `verify/test-06-flowai.sh`** (S4d.5h-k), and a test fails if
   one does not. `remediation-local` is exercised through its *refusal* guardrail rather than a second
   live write: S4d.5e already proves the governed push, and a second change per run buys nothing.

## Consequences

- An unknown device is now a fast, legible failure instead of an indefinite hang, and the local
  inference slot is no longer deniable by one bad name.
- The verify grows four criteria that cost no provider tokens.
- The static audit stays available but is not trusted as coverage: it passed while the fleet was broken.
- Not addressed: `wf-show-command-v1` still accepts any string as `device`. Constraining it to the
  inventory at the schema level would be stronger than asking the model to check first, and is left as
  a later element.
