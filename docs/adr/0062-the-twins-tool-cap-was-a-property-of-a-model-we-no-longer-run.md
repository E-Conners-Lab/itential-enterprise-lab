# 0062 — A local twin gets the tools its job needs; the three-tool cap was a property of qwen2.5:7b

- **Status:** accepted (owner instruction, 2026-09-12)
- **Date:** 2026-09-12
- **Related:** ADR 0046 (the agent fleet, tiered autonomy), ADR 0059 (a tool call must end the job), ADR 0060 (inference leaves the lab), ADR 0061 (the prompt picks the model)

## Context

ADR 0046 gave every local twin one to three tools, and `tests/test_agent_fleet.py` enforced it with an
error message that named its own expiry date:

```python
assert 1 <= len(docs[f"{name}-local"]["tools"]) <= 3, "a 7B model gets one to three tools"
```

That was measured on `qwen2.5:7b` on four CPU cores - the same model that filled optional parameters
with `null` and handed an object to a string input. ADR 0060 and ADR 0061 both flagged that the twins'
constraints were shaped by it and needed re-measuring; the cap was not re-measured, and it started to
cost something real.

`netbox-sot-local` had two tools and could not answer a question about interfaces. Asked one, it did
not say "I cannot see that" - it said **"Interfaces on br1-sw01: not in NetBox"**, which is false: the
interfaces are in NetBox, the agent simply had no tool to read them. For a source-of-truth agent that is
the dangerous failure, because an operator can reasonably read it as "this switch has no interfaces
configured". The prompt had given it one way to say no (`If nothing matches, say "not in NetBox"`), so
it used that one for a different situation. It also burned a tool call first, trying to answer the
interface question with the device tool.

## Decision

1. **A twin gets the tools its job needs.** `netbox-sot-local` gains `dcim_interfaces_list` and
   `ipam_ip_addresses_list` - the two that answer the questions it was fielding.
2. **Not every tool the Claude agent has.** `dcim_devices_retrieve`, `dcim_racks_list`,
   `circuits_circuits_list`, `ipam_asns_list`, `ipam_vrfs_list`, `ipam_prefixes_list` and
   `dcim_sites_list` stay off it. Those return the wide NetBox objects - `dcim_devices_retrieve` pulls
   the full 52-field record plus rendered config context - whose size caused the inference timeout of
   ADR 0059. The job needs interfaces and addresses; it does not need the estate.
3. **The cap is six, set from measurement.** Not removed: tool schemas cost input tokens on every
   request, and an unbounded list is how a twin ends up holding the wide reads again.
4. **A twin must distinguish "not in NetBox" from "I cannot see that".** Every prompt now says to use
   the first only when a tool it called returned nothing, and otherwise to name the agent that has the
   tools - never to report as absent something it cannot see.

## The measurement

The same six-clause question, before and after, against a verified ground truth:

| clause | 2 tools | 4 tools |
|---|---|---|
| active **and** non-client devices at br1 | correct (excluded the `planned` firewall) | correct |
| their platforms | correct | correct |
| VLAN count and name | correct | correct |
| more devices than VLANs | correct | correct, and shows the counts |
| **interfaces on br1-sw01** | **false "not in NetBox"** | **all five, matching the NetBox API exactly** |
| does site br3 exist | correct refusal | correct refusal |
| wasted tool calls | 1 | **0** |

**More tools made tool selection better, not worse.** At two tools it reached for the device list to
answer an interface question; at four it went straight to `dcim_interfaces_list` with the right
argument. Whatever "loses its way with more tools" described, it is not this model.

The cost is real and is why the cap stays: input tokens for that question went **10,547 → 18,572**,
four tool schemas plus a genuine interface payload. Output fell (292 → 238). On the Mac that is
latency only; on the metered profile it would be money.

## Consequences

- The twins answer questions they previously refused, and stop refusing them *incorrectly*.
- Every per-agent tool list is now a deliberate choice rather than a number inherited from a retired
  model - and the wide reads are excluded for a stated reason, not by accident of the cap.
- Six is not evidence about six. It is headroom above the four that was measured. Raising it again
  means measuring again, which is what the test's comment says.
- Not addressed: the other twins were not re-examined. `compliance-local` and `remediation-local` hold
  one tool each and answer their jobs; `device-ops-local`, `diagnostics-local` and `lab-netops-local`
  hold three. None of them is currently refusing work it should be able to do, so none was changed -
  but the same question is worth asking of each.
